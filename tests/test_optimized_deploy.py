"""Artifact and remote filesystem tests run entirely on disposable fixtures."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

if os.name == 'posix':
    import pwd

ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / 'bin'
sys.path.insert(0, str(HELPERS))
import build_artifact as artifact
import deploy
import measure


CONFIG = dict(profile='laravel-vite', output_dir='.', immutable_dirs=['public/build/assets'],
              protected_paths=['.env*', 'storage', 'public/storage'], keep_releases=3, retention_days=7,
              php_version='', php_web_user='auto', migrate=True, maintenance=True, require_build_artifact=True)


class TimingTests(unittest.TestCase):
    def test_cache_hit_command_still_executes_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / 'summary.txt'
            result = subprocess.run([sys.executable, str(HELPERS / 'measure.py'), '--cache-hit', 'true', 'dependency-install', '--',
                                     sys.executable, '-c', 'from pathlib import Path; Path("installed").touch()'],
                                    cwd=root, capture_output=True, text=True,
                                    env=os.environ | dict(GITHUB_STEP_SUMMARY=str(summary), HOSTINGER_TIMING_JOB='test'))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'installed').exists())
            metric = json.loads((root / 'deploy-diagnostics/timings-test.jsonl').read_text())
            self.assertEqual(metric['cache_hit'], 'true')
            self.assertEqual(metric['outcome'], 'success')
            self.assertIn('download cache hit: true', summary.read_text())

    def test_command_failure_stops_the_job_and_records_duration_cache_miss_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(HELPERS / 'measure.py'), '--cache-hit', '', 'frontend-build', '--',
                                     sys.executable, '-c', 'raise SystemExit(13)'], cwd=directory, capture_output=True, text=True,
                                    env=os.environ | dict(GITHUB_STEP_SUMMARY='', HOSTINGER_TIMING_JOB='test'))
            self.assertEqual(result.returncode, 13)
            metrics = list((Path(directory) / 'deploy-diagnostics').glob('timings-*.jsonl'))
            metric = json.loads(metrics[0].read_text())
            self.assertEqual(metric['stage'], 'frontend-build')
            self.assertEqual(metric['outcome'], 'failure')
            self.assertEqual(metric['cache_hit'], 'false')
            self.assertGreaterEqual(metric['seconds'], 0)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        recording = patch.object(measure, 'record')
        recording.start()
        self.addCleanup(recording.stop)
        transfer_recording = patch.object(deploy, 'record')
        transfer_recording.start()
        self.addCleanup(transfer_recording.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.build = self.source / 'public/build'
        (self.build / 'assets').mkdir(parents=True)
        manifest = {entry: dict(file=f'assets/entry-{index}.js', isEntry=True,
                               dynamicImports=['chunk']) for index, entry in enumerate(('resources/css/site.css', 'resources/js/site.js'))}
        manifest['chunk'] = dict(file='assets/lazy.js', css=['assets/lazy.css'])
        (self.build / 'manifest.json').write_text(json.dumps(manifest))
        (self.build / 'fonts-manifest.json').write_text(json.dumps(dict(style=dict(file='assets/fonts.css'), preloads=[dict(file='assets/font.woff2')])))
        for name in ('entry-0.js', 'entry-1.js', 'lazy.js', 'lazy.css', 'fonts.css', 'font.woff2'):
            (self.build / 'assets' / name).write_text(name)
        self.folder = self.root / 'artifact'
        self.env = dict(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123', RUNNER_TEMP=str(self.root))
        self.destination = self.root / 'destination'
        self.destination.mkdir()

    def seal(self):
        artifact.seal(self.source, self.folder, self.env, CONFIG)
        return artifact.digest(self.folder / 'build.tar')

    def test_roundtrip_keeps_manifest_fonts_dynamic_chunks_and_exact_bytes(self):
        artifact.restore(self.destination, self.folder, self.seal(), self.env, CONFIG)
        self.assertEqual(artifact.inventory(self.build), artifact.inventory(self.destination / 'public/build'))
        artifact.verify_build(self.destination, self.env, CONFIG)
        (self.destination / 'public/build/assets/lazy.js').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'differ'):
            artifact.verify_build(self.destination, self.env, CONFIG)

    def test_other_commit_run_missing_corrupt_or_wrong_digest_fails_before_restore(self):
        digest = self.seal()
        for env in (self.env | dict(GITHUB_SHA='b' * 40), self.env | dict(GITHUB_RUN_ID='999')):
            with self.subTest(env=env), self.assertRaises(ValueError):
                artifact.restore(self.destination, self.folder, digest, env, CONFIG)
            self.assertFalse((self.destination / 'public/build').exists())
        with self.assertRaises(ValueError):
            artifact.restore(self.destination, self.folder, '0' * 64, self.env, CONFIG)
        with (self.folder / 'build.tar').open('ab') as stream:
            stream.write(b'corrupt')
        with self.assertRaises(ValueError):
            artifact.restore(self.destination, self.folder, digest, self.env, CONFIG)
        (self.folder / 'build.tar').unlink()
        with self.assertRaises(FileNotFoundError):
            artifact.restore(self.destination, self.folder, digest, self.env, CONFIG)

    def test_incomplete_build_cannot_be_sealed(self):
        (self.build / 'assets/lazy.js').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing Vite dependency'):
            self.seal()
        self.assertFalse(self.folder.exists())

    def test_unpreloaded_font_missing_from_families_is_rejected(self):
        fonts = json.loads((self.build / 'fonts-manifest.json').read_text())
        fonts['families'] = dict(example=dict(variants={'500:normal': dict(files=[dict(file='assets/missing.woff2')])}))
        (self.build / 'fonts-manifest.json').write_text(json.dumps(fonts))
        with self.assertRaisesRegex(ValueError, 'Missing font dependency'):
            self.seal()

    def test_artifact_is_validated_before_any_server_operation(self):
        config = CONFIG | dict(output_dir=str(self.source))
        environment = self.env | dict(HOSTINGER_TARGET_DIR='/home/test/app', HOSTINGER_SSH_HOST='example.test', HOSTINGER_SSH_USER='test')
        instance = deploy.Deployment(config, environment)
        with patch.object(instance, 'remote') as remote, patch.object(instance, 'rsync') as rsync:
            with self.assertRaises(FileNotFoundError):
                instance.transfer()
        remote.assert_not_called()
        rsync.assert_not_called()

    def test_archive_links_traversal_and_duplicate_files_are_rejected(self):
        for attack in ('link', 'traversal', 'duplicate'):
            with self.subTest(attack=attack):
                self.seal()
                with tarfile.open(self.folder / 'build.tar', 'a') as stream:
                    member = tarfile.TarInfo('public/build/manifest.json' if attack == 'duplicate' else 'public/build/../../outside')
                    if attack == 'link':
                        member.type, member.linkname = tarfile.SYMTYPE, '/tmp/outside'
                    stream.addfile(member)
                digest = artifact.digest(self.folder / 'build.tar')
                metadata = json.loads((self.folder / 'provenance.json').read_text())
                metadata['archive_sha256'] = digest
                (self.folder / 'provenance.json').write_text(json.dumps(metadata))
                with self.assertRaises(ValueError):
                    artifact.restore(self.destination, self.folder, digest, self.env, CONFIG)
                self.assertFalse((self.destination / 'public/build').exists())

    def test_custom_laravel_immutable_directory_is_included_and_verified(self):
        config = CONFIG | dict(immutable_dirs=['public/build/assets', 'public/extra'])
        extra = self.source / 'public/extra/font-12345678.woff2'
        extra.parent.mkdir()
        extra.write_bytes(b'custom frontend output')
        checkout_extra = self.destination / 'public/extra' / extra.name
        checkout_extra.parent.mkdir(parents=True)
        checkout_extra.write_bytes(b'wrong checkout bytes')
        artifact.seal(self.source, self.folder, self.env, config)
        with self.assertRaisesRegex(ValueError, 'checkout assets differ'):
            artifact.restore(self.destination, self.folder, artifact.digest(self.folder / 'build.tar'), self.env, config)
        self.assertEqual(checkout_extra.read_bytes(), b'wrong checkout bytes')
        self.assertFalse((self.destination / 'public/build').exists())
        checkout_extra.write_bytes(extra.read_bytes())
        artifact.restore(self.destination, self.folder, artifact.digest(self.folder / 'build.tar'), self.env, config)
        self.assertEqual((self.destination / 'public/extra' / extra.name).read_bytes(), extra.read_bytes())
        artifact.verify_build(self.destination, self.env, config)
        (self.destination / 'public/extra' / extra.name).unlink()
        with self.assertRaises(ValueError):
            artifact.verify_build(self.destination, self.env, config)

    def test_missing_artifact_is_rejected_before_credential_materialization(self):
        profile = self.root / 'profile.json'
        profile.write_text(json.dumps(CONFIG | dict(output_dir=str(self.source))))
        environment = self.env | dict(HOSTINGER_TARGET_DIR='/home/test/app', HOSTINGER_SSH_HOST='example.test', HOSTINGER_SSH_USER='test')
        with patch.dict(os.environ, environment), patch.object(sys, 'argv', ['deploy.py', 'transfer', '--config', str(profile)]), patch.object(deploy.Deployment, 'credentials') as credentials:
            with self.assertRaises(FileNotFoundError):
                deploy.main()
        credentials.assert_not_called()


class StaticArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123', RUNNER_TEMP=str(self.root))

    def fixture(self, profile):
        directory, immutable = ('build', '_app/immutable') if profile == 'sveltekit-static' else ('dist', 'assets')
        if profile == 'static':
            directory, immutable = 'site/output', 'chunks'
        config = dict(profile=profile, output_dir=directory, immutable_dirs=[immutable])
        source = self.root / profile
        build = source / directory
        (build / immutable).mkdir(parents=True)
        (build / immutable / 'entry.js').write_text('import("./lazy.js")')
        (build / immutable / 'lazy.js').write_text('export default 42')
        (build / immutable / 'style.css').write_text('body{color:red}')
        (build / 'index.html').write_text(f'<script>import("./{immutable}/entry.js")</script><link rel="stylesheet" href="/{immutable}/style.css">')
        (build / 'manifest.webmanifest').write_text('{"name":"fixture"}')
        (build / 'manifest.json').write_text('{"name":"pwa fixture"}')
        (build / '.hidden').write_text('keep complete output')
        destination = self.root / (profile + '-restored')
        destination.mkdir()
        folder = self.root / (profile + '-artifact')
        return config, source, build, destination, folder

    def test_all_static_profiles_roundtrip_complete_output_and_current_source_changes(self):
        for profile in ('static-vite', 'sveltekit-static', 'static'):
            with self.subTest(profile=profile):
                config, source, build, destination, folder = self.fixture(profile)
                artifact.seal(source, folder, self.env, config)
                first = json.loads((folder / 'provenance.json').read_text())['files']
                entry = build / config['immutable_dirs'][0] / 'entry.js'
                entry.write_text(entry.read_text() + '\nconsole.log("latest source")')
                (build / 'index.html').write_text((build / 'index.html').read_text() + '<div class="min-h-[137px]">updated</div>')
                artifact.seal(source, folder, self.env, config)
                second = json.loads((folder / 'provenance.json').read_text())['files']
                self.assertNotEqual(first['index.html'], second['index.html'])
                self.assertNotEqual(first[entry.relative_to(build).as_posix()], second[entry.relative_to(build).as_posix()])
                artifact.restore(destination, folder, artifact.digest(folder / 'build.tar'), self.env, config)
                restored = destination / config['output_dir']
                self.assertEqual(artifact.inventory(build), artifact.inventory(restored))
                artifact.verify_build(restored, self.env, config)
                (restored / '.hidden').unlink()
                with self.assertRaises(ValueError):
                    artifact.verify_build(restored, self.env, config)

    def test_missing_static_dependency_empty_html_and_wrong_output_contract_fail(self):
        config, source, build, destination, folder = self.fixture('sveltekit-static')
        (build / '_app/immutable/entry.js').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing static dependency'):
            artifact.seal(source, folder, self.env, config)
        (build / '_app/immutable/entry.js').write_text('entry')
        artifact.seal(source, folder, self.env, config)
        with self.assertRaisesRegex(ValueError, 'another output/profile'):
            artifact.restore(destination, folder, artifact.digest(folder / 'build.tar'), self.env, config | dict(output_dir='other'))
        (build / 'index.html').unlink()
        with self.assertRaisesRegex(ValueError, 'contain HTML'):
            artifact.seal(source, folder, self.env, config)

    def test_static_vite_optional_manifest_checks_dynamic_chunks(self):
        config, source, build, destination, folder = self.fixture('static-vite')
        (build / '.vite').mkdir()
        manifest = {'index.html': dict(file='assets/entry.js', isEntry=True, dynamicImports=['lazy']), 'lazy': dict(file='assets/lazy.js')}
        (build / '.vite/manifest.json').write_text(json.dumps(manifest))
        artifact.seal(source, folder, self.env, config)
        (build / 'assets/lazy.js').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing Vite dependency'):
            artifact.seal(source, folder, self.env, config)

    @unittest.skipUnless(os.name == 'posix', 'POSIX executable bits required')
    def test_executable_bits_are_preserved_without_preserving_unsafe_write_bits(self):
        config, source, build, destination, folder = self.fixture('static-vite')
        executable = build / 'tool'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o777)
        artifact.seal(source, folder, self.env, config)
        artifact.restore(destination, folder, artifact.digest(folder / 'build.tar'), self.env, config)
        self.assertEqual((destination / 'dist/tool').stat().st_mode & 0o777, 0o755)


@unittest.skipUnless(os.name == 'posix' and shutil.which('rsync'), 'Linux and rsync required')
class RemoteTests(unittest.TestCase):
    def setUp(self):
        recording = patch.object(measure, 'record')
        recording.start()
        self.addCleanup(recording.stop)
        transfer_recording = patch.object(deploy, 'record')
        transfer_recording.start()
        self.addCleanup(transfer_recording.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'site'
        (self.target / 'public/build/assets').mkdir(parents=True)
        self.state = self.root / '.site.hostinger-ci'
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        php = self.tools / 'php'
        php.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
if sys.argv[1:2] == ['-r']:
    print(os.getuid(), end='')
elif sys.argv[1:2] == ['artisan']:
    command = sys.argv[2]
    with open('commands.log', 'a') as stream: stream.write(command + '\\n')
    if os.environ.get('FAIL_COMMAND') == command: sys.exit(1)
    if command == 'down': Path('maintenance').touch()
    if command == 'up': Path('maintenance').unlink(missing_ok=True)
else:
    sys.stdin.read()
''')
        php.chmod(0o755)
        self.env = os.environ | dict(PATH=str(self.tools) + ':' + os.environ['PATH'])
        (self.target / '.env').write_text('APP_KEY=fixture-only-key\n')
        (self.target / 'artisan').touch()
        self.run = 'a' * 32
        self.script = (HELPERS / 'remote-deploy.sh').read_text()
        self.asset = self.target / 'public/build/assets/app-12345678.js'
        self.asset.write_text('new')
        self.inventory = artifact.digest(self.asset) + '  public/build/assets/' + self.asset.name

    def execute(self, phase, verified=True, max_files=10000, php_version='', composer_lock_hash='', force_vendor_sync=False, **environment):
        script = "incoming='" + self.inventory + "'\n" + self.script
        return subprocess.run(['bash', '-s', '--', str(self.target), phase, self.run, 'public/build/assets',
                               '2', '7', 'laravel-vite', 'true', str(max_files), '536870912', '3', php_version, str(verified).lower(), 'true',
                               pwd.getpwuid(os.getuid()).pw_name, composer_lock_hash, str(force_vendor_sync).lower()], input=script, text=True, capture_output=True,
                              env=self.env | environment)

    def test_new_writable_roots_and_maintenance_success_with_stage_timings(self):
        result = self.execute('prepare')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / 'storage/app/private').is_dir())
        self.assertTrue((self.target / 'maintenance').exists())
        result = self.execute('optimize')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.target / 'maintenance').exists())
        for stage in ('storage-permissions', 'public-permissions', 'migration', 'cache-build'):
            self.assertIn('"stage":"' + stage + '"', result.stdout)
        self.assertEqual((self.target / 'commands.log').read_text().splitlines(),
                         ['down', 'migrate', 'optimize:clear', 'config:cache', 'route:cache', 'view:cache', 'up'])

    def vendor_fixture(self):
        lock = self.target / 'composer.lock'
        lock.write_text('{"fixture":"unchanged production lock"}')
        autoload = self.target / 'vendor/autoload.php'
        autoload.parent.mkdir(exist_ok=True)
        autoload.write_text('<?php // fixture entry point')
        return artifact.digest(lock)

    def test_vendor_marker_is_created_after_optimization_and_enables_warm_reuse(self):
        lock_hash = self.vendor_fixture()
        cold = self.execute('prepare', composer_lock_hash=lock_hash)
        self.assertEqual(cold.returncode, 0, cold.stderr)
        self.assertIn('"reusable":false', cold.stdout)
        marker = self.state / 'vendor-lock'
        self.assertFalse(marker.exists())
        optimized = self.execute('optimize', composer_lock_hash=lock_hash)
        self.assertEqual(optimized.returncode, 0, optimized.stderr)
        self.assertEqual(marker.read_text().strip(), lock_hash)
        warm = self.execute('prepare', composer_lock_hash=lock_hash)
        self.assertEqual(warm.returncode, 0, warm.stderr)
        self.assertIn('"reusable":true', warm.stdout)

    def test_forced_sync_invalidates_marker_and_incomplete_sync_cannot_be_reused(self):
        lock_hash = self.vendor_fixture()
        self.assertEqual(self.execute('prepare', composer_lock_hash=lock_hash).returncode, 0)
        self.assertEqual(self.execute('optimize', composer_lock_hash=lock_hash).returncode, 0)
        forced = self.execute('prepare', composer_lock_hash=lock_hash, force_vendor_sync=True)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn('"reusable":false', forced.stdout)
        self.assertFalse((self.state / 'vendor-lock').exists())
        retry = self.execute('prepare', composer_lock_hash=lock_hash)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn('"reusable":false', retry.stdout)
        for command in ('migrate', 'config:cache'):
            with self.subTest(command=command):
                failed = self.execute('optimize', composer_lock_hash=lock_hash, FAIL_COMMAND=command)
                self.assertNotEqual(failed.returncode, 0)
                self.assertFalse((self.state / 'vendor-lock').exists())

    def test_changed_lock_missing_autoload_and_wrong_marker_require_full_sync(self):
        lock_hash = self.vendor_fixture()
        self.assertEqual(self.execute('prepare', composer_lock_hash=lock_hash).returncode, 0)
        marker = self.state / 'vendor-lock'
        for condition in ('changed-lock', 'missing-autoload', 'wrong-marker'):
            with self.subTest(condition=condition):
                lock_hash = self.vendor_fixture()
                marker.write_text(lock_hash + '\n')
                if condition == 'changed-lock':
                    (self.target / 'composer.lock').write_text('target lock changed')
                elif condition == 'missing-autoload':
                    (self.target / 'vendor/autoload.php').unlink()
                else:
                    marker.write_text('0' * 64 + '\n')
                result = self.execute('prepare', composer_lock_hash=lock_hash)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('"reusable":false', result.stdout)
                self.assertFalse(marker.exists())

    def test_warm_deployment_refreshes_classmap_without_scanning_packages(self):
        from test_deployment import make_local_ssh
        source = self.root / 'source'
        def put(path, content):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        put(source / 'public/build/assets/new-abcdefgh.js', 'new asset')
        put(source / 'public/build/manifest.json', '{}')
        put(source / 'composer.lock', '{"fixture":"same lock"}')
        put(source / 'artisan', '<?php // fixture')
        put(source / 'app/Old.php', '<?php // old application class')
        put(source / 'vendor/autoload.php', '<?php // old autoload')
        put(source / 'vendor/composer/autoload_classmap.php', 'app/Old.php')
        for index in range(80):
            put(source / f'vendor/package/file-{index}.php', f'package byte {index}')
        config = CONFIG | dict(output_dir=str(source), require_build_artifact=False,
                               php_web_user=pwd.getpwuid(os.getuid()).pw_name)
        env = dict(HOSTINGER_TARGET_DIR=str(self.target), HOSTINGER_SSH_HOST='example.test',
                   HOSTINGER_SSH_USER='test', RUNNER_TEMP=str(self.root))
        instance = deploy.Deployment(config, env)
        instance.ssh = [str(make_local_ssh(self.root / 'local-ssh'))]
        with patch.dict(os.environ, self.env), patch.object(deploy, 'record') as metrics:
            instance.transfer()
            cold = next(call.kwargs['files_listed'] for call in metrics.call_args_list
                        if call.args and call.args[0] == 'rsync-application')
            package = self.target / 'vendor/package/file-0.php'
            package_mtime = package.stat().st_mtime_ns
            put(self.target / 'vendor/package/local-only.php', 'preserved package file')
            put(self.target / 'vendor/composer/obsolete.php', 'obsolete metadata')
            (source / 'app/Old.php').unlink()
            put(source / 'app/Services/Moved.php', '<?php // moved application class')
            put(source / 'vendor/autoload.php', '<?php // new autoload')
            put(source / 'vendor/composer/autoload_classmap.php', 'app/Services/Moved.php')
            put(source / 'vendor/composer/sub/nested.php', 'nested metadata')
            metrics.reset_mock()
            instance.transfer()
            warm = next(call.kwargs['files_listed'] for call in metrics.call_args_list
                        if call.args and call.args[0] == 'rsync-application')
        self.assertTrue(instance.vendor_reusable)
        self.assertLess(warm, cold - 70)
        self.assertEqual((self.target / 'vendor/composer/autoload_classmap.php').read_text(), 'app/Services/Moved.php')
        self.assertEqual((self.target / 'vendor/autoload.php').read_text(), '<?php // new autoload')
        self.assertTrue((self.target / 'vendor/composer/sub/nested.php').exists())
        self.assertFalse((self.target / 'vendor/composer/obsolete.php').exists())
        self.assertFalse((self.target / 'app/Old.php').exists())
        self.assertTrue((self.target / 'app/Services/Moved.php').exists())
        self.assertEqual(package.stat().st_mtime_ns, package_mtime)
        self.assertEqual(package.read_text(), 'package byte 0')
        self.assertTrue((self.target / 'vendor/package/local-only.php').exists())
        self.assertTrue((self.target / '.env').exists())

    def test_hostinger_php_selector_is_used_for_identity_probes_and_artisan(self):
        alternate = self.tools / 'selected-php'
        alternate.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
if sys.argv[1:2] == ['-r'] and 'PHP_MAJOR_VERSION' in sys.argv[2]:
    print('8.3', end='')
else:
    with open('selected-php.log', 'a') as stream: stream.write('selected\\n')
    os.execv(str(Path(__file__).with_name('php')), [str(Path(__file__).with_name('php')), *sys.argv[1:]])
''')
        alternate.chmod(0o755)
        self.script = self.script.replace('/opt/alt/php${version_clean}/usr/bin/php', str(alternate))
        for phase in ('prepare', 'optimize'):
            result = self.execute(phase, php_version='8.3')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.target / 'maintenance').exists())
        self.assertGreaterEqual(len((self.target / 'selected-php.log').read_text().splitlines()), 10)

    def test_auto_php_worker_audit_fails_clearly_and_empty_app_key_generation_is_preserved(self):
        ps = self.tools / 'ps'
        ps.write_text('#!/bin/sh\nprintf "999999 lsphp\\n"\n')
        ps.chmod(0o755)
        script = "incoming='" + self.inventory + "'\n" + self.script
        arguments = ['bash', '-s', '--', str(self.target), 'prepare', self.run, 'public/build/assets',
                     '2', '7', 'laravel-vite', 'true', '10000', '536870912', '3', '', 'true', 'true', 'auto']
        result = subprocess.run(arguments, input=script, text=True, capture_output=True, env=self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot verify a PHP web worker', result.stderr)
        self.assertFalse((self.target / 'maintenance').exists())
        (self.target / '.env').write_text('APP_KEY=\n')
        self.assertEqual(self.execute('prepare').returncode, 0)
        result = self.execute('optimize')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('key:generate', (self.target / 'commands.log').read_text())
        self.assertIn('"stage":"application-key"', result.stdout)

    def test_permission_checks_work_in_a_jail_without_dev_fd(self):
        if not shutil.which('chroot') or not shutil.which('ldd'):
            self.skipTest('chroot and ldd are required for the jailed-shell regression')
        privilege = []
        if os.getuid() != 0:
            if not shutil.which('sudo') or subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode:
                self.skipTest('The jailed-shell regression requires root or passwordless sudo')
            privilege = ['sudo', '-n']
        jail = self.root / 'jail'
        jail.mkdir()
        for name in ('bash', 'stat', 'id', 'mkdir'):
            binary = Path(shutil.which(name))
            dependencies = subprocess.check_output(['ldd', str(binary)], text=True)
            for path in [str(binary), *re.findall(r'/[^\s()]+', dependencies)]:
                source = Path(path)
                destination = jail / source.relative_to('/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        (jail / 'dev').mkdir()
        (jail / 'dev/null').touch()
        (jail / 'site/storage').mkdir(parents=True)
        (jail / 'site/public/build/assets').mkdir(parents=True)
        self.assertFalse((jail / 'dev/fd').exists())
        self.assertFalse((jail / 'proc').exists())
        definitions = 'safe_directory() {' + self.script.split('safe_directory() {', 1)[1].split('\n[[ "$target" ==', 1)[0]
        script = ('set -euo pipefail\nPATH=/usr/bin:/bin\ntarget=/site\nprofile=laravel-vite\nasset_dirs=(public/build/assets)\n'
                  'fail() { echo "$*" >&2; exit 1; }\n' + definitions
                  + '\ncd /site\ncheck_writable storage\ncheck_public_permissions\n')
        result = subprocess.run([*privilege, 'chroot', f'--userspec={os.getuid()}:{os.getgid()}', str(jail),
                                 shutil.which('bash'), '-s'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Runtime directory storage:', result.stdout)
        self.assertIn('Public directory public/build/assets:', result.stdout)

    def test_unreadable_permission_metadata_fails_clearly_before_maintenance(self):
        stat = self.tools / 'stat'
        stat.write_text('''#!/bin/bash
if [[ "$2" == '%u %g %a' && "$4" == "$FAIL_STAT_ROOT" ]]; then
  echo 'stat: simulated metadata failure' >&2
  exit 1
fi
exec /usr/bin/stat "$@"
''')
        stat.chmod(0o755)
        for path in ('storage', 'public'):
            with self.subTest(path=path):
                result = self.execute('prepare', FAIL_STAT_ROOT=path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cannot read permission/ownership metadata: ' + path, result.stderr)
                self.assertFalse((self.target / 'maintenance').exists())

    def test_python_remote_transport_collects_metrics_and_propagates_failures(self):
        ssh = self.root / 'local-ssh'
        ssh.write_text('#!/bin/bash\nexec bash -c "$2"\n')
        ssh.chmod(0o755)
        config = CONFIG | dict(php_version='', php_web_user=pwd.getpwuid(os.getuid()).pw_name)
        environment = dict(HOSTINGER_TARGET_DIR=str(self.target), HOSTINGER_SSH_HOST='example.test', HOSTINGER_SSH_USER='test', RUNNER_TEMP=str(self.root))
        instance = deploy.Deployment(config, environment)
        instance.ssh = [str(ssh)]
        with patch.dict(os.environ, self.env), patch.object(deploy, 'record') as metrics:
            instance.remote('prepare', self.run, self.inventory, incoming_bytes=3)
            self.assertTrue((self.target / 'maintenance').exists())
            self.assertTrue(any(call.kwargs.get('stage') == 'storage-permissions' for call in metrics.call_args_list))
            with patch.dict(os.environ, dict(FAIL_COMMAND='migrate')):
                with self.assertRaises(subprocess.CalledProcessError):
                    instance.remote('optimize', self.run)
            self.assertTrue((self.target / 'maintenance').exists())
            self.assertTrue(any(call.kwargs.get('stage') == 'migration' and call.kwargs.get('outcome') == 'failure' for call in metrics.call_args_list))

    def test_upload_tree_is_never_walked_or_chmodded(self):
        uploads = self.target / 'storage/app/public/old/nested'
        uploads.mkdir(parents=True)
        old = uploads / 'photo.jpg'
        old.write_text('legacy upload')
        old.chmod(0o600)
        old_time = old.stat().st_ctime_ns
        find = self.tools / 'find'
        find.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$FIND_LOG"\nexec /usr/bin/find "$@"\n')
        find.chmod(0o755)
        log = self.root / 'find.log'
        for phase in ('prepare', 'optimize', 'cleanup'):
            result = self.execute(phase, FIND_LOG=str(log))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('storage', log.read_text())
        self.assertEqual(old.stat().st_ctime_ns, old_time)
        self.assertEqual(old.stat().st_mode & 0o777, 0o600)

    def test_bad_permission_and_external_symlink_fail_before_maintenance(self):
        storage = self.target / 'storage'
        storage.mkdir()
        storage.chmod(0o500)
        result = self.execute('prepare')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no recursive chmod', result.stderr)
        self.assertFalse((self.target / 'maintenance').exists())
        storage.chmod(0o755)
        outside = self.root / 'outside'
        outside.mkdir()
        (storage / 'app').symlink_to(outside, target_is_directory=True)
        result = self.execute('prepare')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Symlink in runtime directory', result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_wrong_ownership_is_diagnosed(self):
        if os.getuid() != 0:
            self.skipTest('Ownership fixture requires container root')
        storage = self.target / 'storage'
        storage.mkdir()
        os.chown(storage, 1001, 1001)
        result = self.execute('prepare')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Ownership/access mismatch', result.stderr)
        self.assertIn('uid=1001', result.stderr)

    def test_down_migration_cache_and_post_transfer_permission_failures(self):
        result = self.execute('prepare', FAIL_COMMAND='down')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / 'maintenance').exists())
        for command in ('migrate', 'config:cache', 'route:cache', 'view:cache'):
            with self.subTest(command=command):
                self.assertEqual(self.execute('prepare').returncode, 0)
                result = self.execute('optimize', FAIL_COMMAND=command)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((self.target / 'maintenance').exists())
                self.assertIn('"outcome":"failure"', result.stdout)
        (self.target / 'public').chmod(0o700)
        result = self.execute('optimize')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Public directory', result.stderr)
        self.assertTrue((self.target / 'maintenance').exists())

    def test_immutable_collisions_and_retention_budget_fail_closed(self):
        self.inventory = '0' * 64 + '  public/build/assets/' + self.asset.name
        result = self.execute('prepare')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Immutable asset collision', result.stderr)
        self.assertFalse((self.target / 'maintenance').exists())
        self.assertFalse(self.state.exists())
        self.inventory = artifact.digest(self.asset) + '  public/build/assets/' + self.asset.name
        result = self.execute('prepare', max_files=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('budget would be exceeded', result.stderr)
        self.assertFalse((self.target / 'maintenance').exists())

    def test_http_failure_cleanup_retains_last_verified_release_and_grace_period(self):
        ancient = self.target / 'public/build/assets/ancient-12345678.js'
        ancient.write_text('ancient')
        self.assertEqual(self.execute('prepare').returncode, 0)
        self.assertEqual(self.execute('optimize').returncode, 0)
        self.assertEqual(self.execute('cleanup').returncode, 0)
        history = self.state / 'history'
        for path in history.iterdir():
            os.utime(path, (1, 1))
            if 'baseline' not in path.name:
                path.rename(history / '19900101000000-verified.txt')
        latest = self.target / 'public/build/assets/latest-12345678.js'
        latest.write_text('latest')
        self.inventory = artifact.digest(latest) + '  public/build/assets/' + latest.name
        for stamp in ('20000101000000', '20010101000000'):
            path = history / (stamp + '-unverified.txt')
            path.write_text(self.inventory + '\n')
            os.utime(path, (1, 1))
        self.run = 'b' * 32
        self.assertEqual(self.execute('prepare').returncode, 0)
        self.assertEqual(self.execute('optimize').returncode, 0)
        result = self.execute('cleanup', verified=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.asset.exists())
        self.assertTrue(latest.exists())
        self.assertFalse(ancient.exists())

    def test_asset_transfer_failure_does_not_publish_application_or_optimize(self):
        source = self.root / 'source'
        (source / 'public/build/assets').mkdir(parents=True)
        (source / 'public/build/assets/current-12345678.js').write_text('new')
        (source / 'public/build/manifest.json').write_text('{}')
        config = CONFIG | dict(output_dir=str(source), require_build_artifact=False)
        environment = dict(HOSTINGER_TARGET_DIR=str(self.target), HOSTINGER_SSH_HOST='example.test', HOSTINGER_SSH_USER='test', RUNNER_TEMP=str(self.root))
        instance = deploy.Deployment(config, environment)
        with patch.object(instance, 'remote') as remote, patch.object(instance, 'rsync', side_effect=subprocess.CalledProcessError(1, 'rsync')) as transfer:
            with self.assertRaises(subprocess.CalledProcessError):
                instance.transfer()
        self.assertEqual(transfer.call_count, 1)
        self.assertEqual(remote.call_args.args[0], 'prepare')

    def test_rsync_new_file_modes_executable_and_protected_old_assets(self):
        source = self.root / 'source'
        (source / 'public/build/assets').mkdir(parents=True)
        (source / 'public/build/assets/new-12345678.js').write_text('new')
        (source / 'public/build/manifest.json').write_text('new manifest')
        executable = source / 'artisan'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        (source / 'ordinary.txt').write_text('read me')
        ssh = self.root / 'local-ssh'
        ssh.write_text('''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
while args[0].startswith('-'):
    args = args[2:] if args[0] in ('-l', '-p', '-i', '-o') else args[1:]
args = args[1:]
if len(args) == 1: os.execvp('bash', ['bash', '-c', args[0]])
os.execvp(args[0], args)
''')
        ssh.chmod(0o755)
        config = CONFIG | dict(output_dir=str(source), require_build_artifact=False)
        env = dict(HOSTINGER_TARGET_DIR=str(self.target), HOSTINGER_SSH_HOST='example.test', HOSTINGER_SSH_USER='test', RUNNER_TEMP=str(self.root))
        instance = deploy.Deployment(config, env)
        instance.ssh = [str(ssh)]
        instance.rsync(source / 'public/build/assets', str(self.target / 'public/build/assets'))
        instance.rsync(source, str(self.target), config['protected_paths'] + config['immutable_dirs'], delete=True)
        self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.target / 'artisan').stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.target / 'ordinary.txt').stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.target / 'public').stat().st_mode & 0o777, 0o755)
        self.assertTrue(self.asset.exists())
        self.assertEqual((self.target / 'public/build/assets/new-12345678.js').stat().st_mode & 0o777, 0o644)
        self.assertTrue((self.target / '.env').exists())


if __name__ == '__main__':
    unittest.main()

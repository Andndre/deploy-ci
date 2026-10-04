import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / '.test-output'
SCRATCH.mkdir(exist_ok=True)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'bin' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load('deploy', 'deploy.py')


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'dist'
        (self.output / 'assets').mkdir(parents=True)
        (self.output / 'assets/app-12345678.js').write_text('export const value = 1;')
        (self.output / 'index.html').write_text('<html></html>')
        self.config = dict(profile='static-vite', output_dir=str(self.output), immutable_dirs=['assets'],
                           protected_paths=['uploads', '.env*'], keep_releases=3, retention_days=7, migrate=False)
        self.env = dict(HOSTINGER_TARGET_DIR='/home/test/app', HOSTINGER_SSH_HOST='example.test',
                        HOSTINGER_SSH_USER='test', HOSTINGER_SSH_PORT='65002', RUNNER_TEMP=str(self.root))

    def test_immutable_transfer_precedes_application_and_cleanup_is_separate(self):
        instance = deploy.Deployment(self.config, self.env)
        events = []
        instance.remote = lambda phase, *args: events.append(('remote', phase))
        instance.rsync = lambda source, target, excludes=(), delete=False: events.append(('rsync', str(source), excludes, delete))
        instance.transfer()
        self.assertEqual(events[0], ('remote', 'prepare'))
        self.assertEqual(events[1], ('rsync', str(self.output / 'assets'), (), False))
        self.assertEqual(events[2], ('rsync', str(self.output), ['uploads', '.env*', 'assets'], True))
        self.assertEqual(events[3], ('remote', 'optimize'))
        self.assertNotIn(('remote', 'cleanup'), events)
        instance.cleanup()
        self.assertEqual(events[-1], ('remote', 'cleanup'))

    def test_failed_asset_transfer_never_publishes_application(self):
        instance = deploy.Deployment(self.config, self.env)
        instance.remote = lambda *args: None
        with patch.object(instance, 'rsync', side_effect=subprocess.CalledProcessError(1, 'rsync')) as transfer:
            with self.assertRaises(subprocess.CalledProcessError):
                instance.transfer()
        self.assertEqual(transfer.call_count, 1)

    def test_target_paths_and_ssh_option_injection_rejected(self):
        for path in ('/', '/home/test', '/home/test/app/..', '/home//test/app', '/home/test/app\nanything'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                deploy.target_path(path)
        deploy.target_path("/home/test/my app's files")
        for key in ('HOSTINGER_SSH_HOST', 'HOSTINGER_SSH_USER'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                deploy.Deployment(self.config, self.env | {key: '-oProxyCommand=bad'})

    def test_rsync_preserves_immutable_directory_and_persistent_data(self):
        instance = deploy.Deployment(self.config, self.env)
        with patch.object(deploy.subprocess, 'run') as run:
            instance.rsync(self.output, instance.target, ['uploads', '.env*', 'assets'], True)
        command = run.call_args.args[0]
        self.assertIn('--delete-delay', command)
        self.assertIn('--exclude=/assets', command)
        self.assertIn('--exclude=/uploads', command)
        self.assertIn('--protect-args', command)
        self.assertIn('StrictHostKeyChecking=yes', command[command.index('-e') + 1])


BASH = os.environ.get('TEST_BASH') or (r'C:\Program Files\Git\bin\bash.exe' if os.name == 'nt' else shutil.which('bash'))


@unittest.skipUnless(BASH and Path(BASH).exists(), 'Bash is required for remote integration tests')
class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "application's files"
        (self.target / 'assets').mkdir(parents=True)
        self.script = (ROOT / 'bin/remote-deploy.sh').read_text()
        path = self.target.as_posix()
        self.posix_target = '/'+ path[0].lower() + path[2:] if os.name == 'nt' else path
        self.run = 'a' * 32
        self.state = self.root / ".application's files.hostinger-ci"
        # Remote integration tests exercise the shell's filesystem logic, not
        # transfer. Provide only the rsync capability missing from Git Bash.
        tools = self.root / 'tools'
        tools.mkdir()
        (tools / 'rsync').write_text('#!/usr/bin/env bash\nexit 0\n', newline='\n')
        (tools / 'rsync').chmod(0o700)
        tools_path = tools.as_posix()
        self.tools_path = '/' + tools_path[0].lower() + tools_path[2:] if os.name == 'nt' else tools_path

    def execute(self, phase, inventory='', target=None, max_files=10000, max_bytes=536870912,
                incoming_bytes=0, verified=False, profile='static-vite'):
        script = "incoming=$(cat <<'INVENTORY'\n" + inventory + '\nINVENTORY\n)\n' + 'PATH=' + shlex.quote(self.tools_path) + ':$PATH\n' + self.script
        return subprocess.run([BASH, '-s', '--', target or self.posix_target, phase, self.run,
                               'assets', '2', '7', profile, 'false', str(max_files), str(max_bytes),
                               str(incoming_bytes), '', str(verified).lower()], input=script,
                              text=True, capture_output=True)

    def asset(self, name, content):
        path = self.target / 'assets' / name
        path.write_text(content)
        return hashlib.sha256(path.read_bytes()).hexdigest() + '  assets/' + name

    def test_missing_destination_fails_without_writing(self):
        result = self.execute('prepare', target=self.posix_target + '/missing')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())

    def test_collision_preserves_existing_bytes(self):
        self.asset('app-12345678.js', 'old bytes')
        inventory = hashlib.sha256(b'new bytes').hexdigest() + '  assets/app-12345678.js'
        result = self.execute('prepare', inventory)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('collision', result.stderr)
        self.assertEqual((self.target / 'assets/app-12345678.js').read_text(), 'old bytes')

    def test_resource_budget_refuses_upload_before_metadata_mutation(self):
        old = self.asset('old-12345678.js', 'old')
        new = hashlib.sha256(b'new').hexdigest() + '  assets/new-12345678.js'
        result = self.execute('prepare', new, max_files=5, incoming_bytes=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('budget would be exceeded', result.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.target / 'assets/new-12345678.js').exists())
        self.assertTrue((self.target / 'assets/old-12345678.js').exists())
        result = self.execute('prepare', old, max_bytes=10, incoming_bytes=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())

    def test_missing_command_has_actionable_error_before_mutation(self):
        self.script = self.script.replace('for tool in find ', 'for tool in missing_hostinger_command find ')
        result = self.execute('prepare')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Required server command is unavailable: missing_hostinger_command', result.stderr)
        self.assertFalse(self.state.exists())

    def test_laravel_requires_production_environment_before_upload(self):
        # A fake PHP command models CLI availability; .env is intentionally absent.
        php = self.root / 'tools/php'
        php.write_text('#!/usr/bin/env bash\nexit 0\n', newline='\n')
        php.chmod(0o700)
        result = self.execute('prepare', profile='laravel-vite')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Create the production .env', result.stderr)
        self.assertFalse(self.state.exists())

    def test_http_failure_cleanup_preserves_last_verified_release(self):
        verified = self.asset('verified-12345678.js', 'verified')
        self.assertEqual(self.execute('prepare', verified).returncode, 0)
        result = self.execute('cleanup', verified=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        history = self.state / 'history'
        # Simulate more publications than KeepReleases and older than the grace
        # period; the previous verified application's chunks must still survive.
        for inventory in history.iterdir():
            os.utime(inventory, (1, 1))
            if inventory.name != '00000000000000-baseline.txt':
                inventory.rename(history / '19900101000000-verified.txt')
        latest = self.asset('latest-12345678.js', 'latest')
        for stamp in ('20200101000000', '20210101000000'):
            previous = history / f'{stamp}-unverified.txt'
            previous.write_text(latest + '\n', newline='\n')
            os.utime(previous, (1, 1))
        self.run = 'b' * 32
        self.assertEqual(self.execute('prepare', latest).returncode, 0)
        result = self.execute('cleanup', verified=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / 'assets/verified-12345678.js').exists())
        self.assertTrue((self.target / 'assets/latest-12345678.js').exists())
        self.assertEqual((self.state / 'last-verified.txt').read_text().strip(), verified)

    def test_cleanup_retains_previous_releases_and_grace_period(self):
        current = self.asset('current-12345678.js', 'current')
        ancient = self.asset('ancient-12345678.js', 'ancient')
        previous = self.asset('previous-12345678.js', 'previous')
        recent = self.asset('recent-12345678.js', 'recent')
        unmanaged = self.asset('unmanaged.js', 'not owned')
        self.assertEqual(self.execute('prepare', current).returncode, 0)
        history = self.state / 'history'
        baseline = history / '00000000000000-baseline.txt'
        baseline.write_text(ancient + '\n', newline='\n')
        os.utime(baseline, (1, 1))
        old = history / '20200101000000-previous.txt'
        old.write_text(previous + '\n', newline='\n')
        os.utime(old, (1, 1))
        grace = history / '20190101000000-recent.txt'
        grace.write_text(recent + '\n', newline='\n')
        result = self.execute('cleanup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.target / 'assets/ancient-12345678.js').exists())
        for name in ('current-12345678.js', 'previous-12345678.js', 'recent-12345678.js', 'unmanaged.js'):
            self.assertTrue((self.target / 'assets' / name).exists(), name)

    def test_symlinked_assets_fail_closed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.target / 'assets/link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation requires Windows privileges')
        result = self.execute('prepare', hashlib.sha256(b'x').hexdigest() + '  assets/app-12345678.js')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())

    def test_expired_failed_assets_pruned_but_changed_files_preserved(self):
        current = self.asset('current-12345678.js', 'current')
        failed = self.asset('failed-12345678.js', 'failed')
        changed = self.asset('changed-12345678.js', 'original')
        self.assertEqual(self.execute('prepare', current).returncode, 0)
        pending = self.state / 'pending' / ('b' * 32 + '.txt')
        pending.write_text(failed + '\n' + changed + '\n', newline='\n')
        os.utime(pending, (1, 1))
        # Expire the adopted baseline too so it does not protect these assets.
        baseline = self.state / 'history/00000000000000-baseline.txt'
        os.utime(baseline, (1, 1))
        # Fill the second retained-release slot, leaving the baseline eligible.
        previous = self.state / 'history/20200101000000-previous.txt'
        previous.write_text(current + '\n', newline='\n')
        (self.target / 'assets/changed-12345678.js').write_text('changed externally')
        result = self.execute('cleanup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.target / 'assets/failed-12345678.js').exists())
        self.assertEqual((self.target / 'assets/changed-12345678.js').read_text(), 'changed externally')
        self.assertFalse(pending.exists())


@unittest.skipUnless(os.name != 'nt' and shutil.which('rsync'), 'Linux rsync required for end-to-end transfer')
class RsyncIntegrationTests(unittest.TestCase):
    def test_two_releases_preserve_old_chunks_and_production_data(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory)
            output = root / 'dist'
            target = root / 'live site'
            (output / 'assets').mkdir(parents=True)
            (target / 'uploads').mkdir(parents=True)
            (target / 'uploads/user.txt').write_text('keep upload')
            (target / '.env').write_text('keep production secret')
            (target / 'obsolete.html').write_text('remove stale application file')
            ssh = root / 'local-ssh'
            ssh.write_text("#!/usr/bin/env python3\nimport os, shlex, sys\ncommands = sys.argv[2:]\ncommand = commands[0] if len(commands) == 1 else shlex.join(commands)\nos.execvp('bash', ['bash', '-c', command])\n")
            ssh.chmod(0o700)
            config = dict(profile='static-vite', output_dir=str(output), immutable_dirs=['assets'],
                          protected_paths=['uploads', '.env*'], keep_releases=2, retention_days=7, migrate=False)
            env = dict(HOSTINGER_TARGET_DIR=str(target), HOSTINGER_SSH_HOST='example.test',
                       HOSTINGER_SSH_USER='test', RUNNER_TEMP=str(root))
            instance = deploy.Deployment(config, env)
            instance.ssh = [str(ssh)]
            for name in ('first-12345678.js', 'second-12345678.js'):
                for asset in (output / 'assets').iterdir():
                    asset.unlink()
                (output / 'assets' / name).write_text('export const build = ' + repr(name))
                (output / 'index.html').write_text('<html>' + name + '</html>')
                instance.transfer()
                instance.cleanup()
            self.assertTrue((target / 'assets/first-12345678.js').exists())
            self.assertTrue((target / 'assets/second-12345678.js').exists())
            self.assertIn('second-', (target / 'index.html').read_text())
            self.assertFalse((target / 'obsolete.html').exists())
            self.assertEqual((target / 'uploads/user.txt').read_text(), 'keep upload')
            self.assertEqual((target / '.env').read_text(), 'keep production secret')


if __name__ == '__main__':
    unittest.main()

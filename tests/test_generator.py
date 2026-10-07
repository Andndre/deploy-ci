import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

from test_deployment import ROOT, SCRATCH

POWERSHELL = os.environ.get('POWERSHELL_EXE') or shutil.which('pwsh') or shutil.which('powershell')


@unittest.skipUnless(POWERSHELL, 'PowerShell required')
class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache.json'
        self.cache.write_text('{"SshHost":"previous-host"}')
        self.package = {'scripts': {'build': 'vite build', 'lint:check': 'eslint .'},
                        'devDependencies': {'vite': '6.0.0'}}
        self.write_package()
        (self.root / 'package-lock.json').write_text('{}')
        (self.root / 'artisan').write_text('<?php')
        (self.root / 'composer.json').write_text(json.dumps({'require': {'php': '^8.3'},
                                                          'require-dev': {'pestphp/pest': '^3.0'}}))
        (self.root / 'composer.lock').write_text('{}')
        (self.root / '.env.example').write_text('APP_KEY=')
        (self.root / '.nvmrc').write_text('22')
        (self.root / 'public/build/assets').mkdir(parents=True)
        (self.root / 'public/build/assets/old-12345678.js').write_text('old build')
        self.git('init', '-b', 'main')
        self.git('add', 'public/build')

    def write_package(self):
        (self.root / 'package.json').write_text(json.dumps(self.package))

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def snapshot(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*') if path.is_file()}

    def generate(self, *args, secrets=False, wrapper=None):
        command = [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(wrapper or ROOT / 'bin/deploy-ci.ps1'), '-NonInteractive',
                   '-ConfigCachePath', str(self.cache), '-TargetDir', '/home/test/app',
                   '-DeployUrl', 'https://example.test/']
        if not secrets:
            command.append('-SkipSecrets')
        return subprocess.run(command + list(args), cwd=self.root, encoding="utf-8", errors="replace", capture_output=True,
                              timeout=60)

    def workflow(self):
        # BaseLoader avoids YAML 1.1 treating GitHub's "on" as a boolean.
        text = (self.root / '.github/workflows/deploy.yml').read_text(encoding='utf-8-sig')
        workflow = yaml.load(text, Loader=yaml.BaseLoader)
        profile = json.loads((self.root / '.github/hostinger/profile.json').read_text())
        generated = SCRATCH / 'generated-workflows'
        generated.mkdir(exist_ok=True)
        mode = 'gated' if 'verify' in workflow['jobs'] else 'lean'
        (generated / f"{profile['profile']}-{profile['package_manager']}-{mode}.yml").write_text(text, encoding='utf-8')
        actionlint = os.environ.get('ACTIONLINT_EXE') or shutil.which('actionlint')
        if actionlint:
            result = subprocess.run([actionlint, '-shellcheck=', '-pyflakes=', str(self.root / '.github/workflows/deploy.yml')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return workflow

    def test_dry_run_leaves_cache_workflow_ignore_and_index_untouched(self):
        before = self.snapshot()
        result = self.generate('-DryRun', '-NodeVersion', '24')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("node-version: '24'", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_lean_and_gated_workflows_use_same_safe_transfer(self):
        for mode in ('-WithoutTests', '-WithTests'):
            result = self.generate(mode, '-Force', '-NodeVersion', '24')
            self.assertEqual(result.returncode, 0, result.stderr)
            workflow = self.workflow()
            deploy = workflow['jobs']['deploy']
            self.assertEqual(deploy['concurrency']['cancel-in-progress'], 'false')
            self.assertEqual(deploy['concurrency']['group'], 'hostinger-production')
            self.assertIn('workflow_dispatch', deploy['if'])
            steps = deploy['steps']
            build = workflow['jobs']['verify' if mode == '-WithTests' else 'build']
            node = next(step for step in build['steps'] if step.get('name') == 'Setup Node.js')
            self.assertEqual(node['with']['node-version'], '24')
            runs = [step.get('run', '') for step in steps]
            transfer = runs.index('python3 .github/hostinger/deploy.py transfer')
            check = runs.index('python3 .github/hostinger/measure.py http-verification -- python3 .github/hostinger/check-deploy.py')
            cleanup = runs.index('python3 .github/hostinger/deploy.py cleanup')
            self.assertLess(transfer, check)
            self.assertLess(check, cleanup)
            cleanup_step = next(s for s in steps if s.get('id') == 'cleanup')
            self.assertIn("steps.transfer.outcome == 'success'", cleanup_step['if'])
            self.assertIn('!cancelled()', cleanup_step['if'])
            self.assertNotIn("steps.verify.outcome == 'success'", cleanup_step['if'])
            self.assertIn("steps.verify.outcome == 'success'", cleanup_step['env']['HOSTINGER_HTTP_VERIFIED'])
            self.assertEqual('verify' in workflow['jobs'], mode == '-WithTests')
            self.assertNotIn('Setup Node.js', [s.get('name') for s in steps])
            self.assertNotIn('npm ci', '\n'.join(runs))
            self.assertEqual(sum(s.get('name') == 'Build frontend once' for job in workflow['jobs'].values() for s in job['steps']), 1)
            self.assertIn('--no-dev', next(s['run'] for s in steps if s.get('name') == 'Install production PHP dependencies'))
            quality = [s.get('name') for s in build['steps']]
            if mode == '-WithTests':
                self.assertLess(quality.index('Run PHP tests'), quality.index('Seal verified frontend build'))
            self.assertEqual(deploy['needs'], ['verify' if mode == '-WithTests' else 'build'])
            artifact = next(s for s in steps if s.get('uses') == 'actions/download-artifact@v4')
            self.assertIn('outputs.artifact_id', artifact['with']['artifact-ids'])
            cache = next(s for s in build['steps'] if s.get('id') == 'composer-cache')
            self.assertIn('runner.os', cache['with']['key'])
            self.assertIn('php8.3-composer2', cache['with']['key'])
            self.assertIn("hashFiles('composer.lock')", cache['with']['key'])
            self.assertNotIn('restore-keys', cache['with'])
            self.assertEqual(node['with']['cache'], 'npm')
            self.assertIn('package-lock.json', node['with']['cache-dependency-path'])
            self.assertIn('.node-cache-runtime', node['with']['cache-dependency-path'])
            for job in workflow['jobs'].values():
                checkout = next(s for s in job['steps'] if s.get('uses') == 'actions/checkout@v4')
                self.assertEqual(checkout['with']['ref'], '${{ github.sha }}')
            if mode == '-WithTests':
                verify_node = next(s for s in workflow['jobs']['verify']['steps'] if s.get('name') == 'Setup Node.js')
                self.assertEqual(verify_node['with']['node-version'], '24')
        self.assertEqual(self.git('ls-files', '--', 'public/build').strip(), '')
        self.assertTrue((self.root / 'public/build/assets/old-12345678.js').exists())

    def test_replacement_requires_force_and_shows_diff_before_any_mutation(self):
        self.assertEqual(self.generate('-WithoutTests').returncode, 0)
        before = self.snapshot()
        result = self.generate('-WithoutTests', '-NodeVersion', '24')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--- existing/.github/workflows/deploy.yml', result.stdout)
        self.assertIn("+          node-version: '24'", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_static_profiles_select_output_and_skip_php(self):
        (self.root / 'artisan').unlink()
        for profile, output, immutable in [('static-vite', 'dist', 'assets'),
                                           ('sveltekit-static', 'build', '_app/immutable')]:
            (self.root / 'svelte.config.js').write_text("import adapter from '@sveltejs/adapter-static'; export default {kit:{adapter:adapter()}}")
            result = self.generate('-Profile', profile, '-WithoutTests', '-Force')
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads((self.root / '.github/hostinger/profile.json').read_text())
            self.assertEqual(config['output_dir'], output)
            self.assertEqual(config['immutable_dirs'], [immutable])
            self.assertEqual(config['max_retained_files'], 10000)
            self.assertEqual(config['max_retained_bytes'], 512 * 1024 * 1024)
            self.assertNotIn('Setup PHP', (self.root / '.github/workflows/deploy.yml').read_text())
            self.assertTrue(config['require_build_artifact'])
            self.workflow()

    def test_sveltekit_accepts_current_vite_config_location(self):
        (self.root / 'vite.config.ts').write_text("import adapter from '@sveltejs/adapter-static'; export default {}")
        result = self.generate('-Profile', 'sveltekit-static', '-WithoutTests')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_static_profile_rejects_node_adapter(self):
        (self.root / 'svelte.config.js').write_text("import adapter from '@sveltejs/adapter-node';")
        before = self.snapshot()
        result = self.generate('-Profile', 'sveltekit-static')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.snapshot())

    def test_package_managers_are_pinned_and_use_frozen_locks(self):
        for manager, version, lockfile, install in [('pnpm', '10.0.0', 'pnpm-lock.yaml', 'pnpm install --frozen-lockfile'),
                                                   ('yarn', '4.0.0', 'yarn.lock', 'yarn install --immutable'),
                                                   ('bun', '1.2.0', 'bun.lock', 'bun install --frozen-lockfile')]:
            self.package['packageManager'] = f'{manager}@{version}'
            self.write_package()
            (self.root / lockfile).write_text('lock')
            result = self.generate('-WithoutTests', '-Force')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(install, (self.root / '.github/workflows/deploy.yml').read_text())
            steps = self.workflow()['jobs']['build']['steps']
            if manager in ('pnpm', 'yarn'):
                activation = next(i for i, s in enumerate(steps) if s.get('name') == 'Activate package manager')
                cache = next(i for i, s in enumerate(steps) if s.get('id') == 'node')
                self.assertLess(activation, cache)
                self.assertEqual(steps[cache]['with']['cache'], manager)
            else:
                cache = next(s for s in steps if s.get('id') == 'node')
                self.assertEqual(cache['with']['path'], '${{ runner.temp }}/bun-download-cache')
                self.assertNotIn('node_modules', cache['with']['path'])

    def test_missing_composer_lock_is_rejected_without_mutation(self):
        (self.root / 'composer.lock').unlink()
        before = self.snapshot()
        result = self.generate('-WithoutTests')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Commit composer.lock', result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_custom_build_command_and_php_web_user_are_preserved(self):
        command = "npm run build && printf '%s' done"
        result = self.generate('-WithTests', '-BuildCommand', command, '-PhpWebUser', 'audited-user')
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = self.workflow()['jobs']['verify']['steps']
        build = next(s for s in steps if s.get('name') == 'Build frontend once')
        self.assertEqual(build['env']['FRONTEND_BUILD_COMMAND'], command)
        profile = json.loads((self.root / '.github/hostinger/profile.json').read_text())
        self.assertEqual(profile['php_web_user'], 'audited-user')
        self.assertTrue((self.root / '.github/hostinger/build_artifact.py').exists())
        self.assertTrue((self.root / '.github/hostinger/measure.py').exists())
        ignore = (self.root / '.gitignore').read_text()
        for name in ('frontend-artifact', 'deploy-diagnostics', '.node-cache-runtime'):
            self.assertIn(name, ignore)

    def test_custom_static_profile_with_types_and_frontend_tests_is_gated(self):
        self.package['scripts'].update({'types:check': 'svelte-check', 'test:ci': 'node --test'})
        self.write_package()
        result = self.generate('-Profile', 'static', '-OutputDir', 'site/output', '-ImmutableDirs', 'chunks', '-WithTests')
        self.assertEqual(result.returncode, 0, result.stderr)
        workflow = self.workflow()
        steps = workflow['jobs']['verify']['steps']
        names = [s.get('name') for s in steps]
        for name in ('Check frontend lint', 'Check frontend types', 'Run frontend tests'):
            self.assertLess(names.index(name), names.index('Seal verified frontend build'))
        deploy = workflow['jobs']['deploy']
        self.assertFalse(any(s.get('uses') == 'actions/setup-node@v4' for s in deploy['steps']))
        self.assertNotIn('composer', '\n'.join(s.get('run', '') for s in deploy['steps']))

    def test_dependency_cache_scope_changes_with_runtime_and_lockfile(self):
        self.assertEqual(self.generate('-WithTests', '-NodeVersion', '22', '-PhpVersion', '8.3').returncode, 0)
        first = self.workflow()['jobs']['verify']['steps']
        self.assertEqual(self.generate('-WithTests', '-Force', '-NodeVersion', '24', '-PhpVersion', '8.4').returncode, 0)
        second = self.workflow()['jobs']['verify']['steps']
        composer_key = lambda steps: next(s['with']['key'] for s in steps if s.get('id') == 'composer-cache')
        node_marker = lambda steps: next(s['run'] for s in steps if s.get('name') == 'Scope frontend download cache to runtime')
        self.assertNotEqual(composer_key(first), composer_key(second))
        self.assertNotEqual(node_marker(first), node_marker(second))
        for filename in ('composer.lock', 'package-lock.json'):
            before = hashlib.sha256((self.root / filename).read_bytes()).hexdigest()
            (self.root / filename).write_text('{"changed":true}')
            after = hashlib.sha256((self.root / filename).read_bytes()).hexdigest()
            self.assertNotEqual(before, after)
        self.assertIn("hashFiles('composer.lock')", composer_key(second))
        node = next(s for s in second if s.get('id') == 'node')
        self.assertIn('package-lock.json', node['with']['cache-dependency-path'])
        installs = '\n'.join(s.get('run', '') for s in second if s.get('name', '').startswith('Install'))
        self.assertIn('npm ci', installs)
        self.assertIn('composer install', installs)
        self.assertNotIn('if:', installs)

    def test_moving_lts_selector_uses_resolved_runtime_in_download_cache(self):
        result = self.generate('-WithoutTests', '-NodeVersion', 'lts/*')
        self.assertEqual(result.returncode, 0, result.stderr)
        steps = self.workflow()['jobs']['build']['steps']
        setup = next(i for i, s in enumerate(steps) if s.get('name') == 'Setup Node.js')
        marker = next(i for i, s in enumerate(steps) if s.get('name') == 'Scope frontend download cache to runtime')
        cache = next(i for i, s in enumerate(steps) if s.get('id') == 'node')
        self.assertLess(setup, marker)
        self.assertLess(marker, cache)
        self.assertIn('$(node --version)', steps[marker]['run'])

    def test_failed_secret_command_stops_and_key_uses_stdin(self):
        key = self.root / 'fake-private-key'
        key.write_text('FAKE-KEY-FOR-TEST-ONLY')
        known = self.root / 'known_hosts'
        known.write_text('example.test ssh-ed25519 fake-host-key')
        log = self.root / 'gh-log.jsonl'
        wrapper = self.root / 'wrapper.ps1'
        # This fake command prevents any network request or real secret update.
        wrapper.write_text("""function global:gh {
  $value = $input | Out-String
  @{ args = @($args); stdin = $value } | ConvertTo-Json -Compress | Add-Content -LiteralPath '""" + str(log).replace("'", "''") + """'
  if ($args -contains 'HOSTINGER_SSH_KEY') { $global:LASTEXITCODE = 9 } else { $global:LASTEXITCODE = 0 }
}
& '""" + str(ROOT / 'bin/deploy-ci.ps1').replace("'", "''") + "' @args\n")
        result = self.generate('-WithoutTests', '-SshHost', 'example.test', '-SshUser', 'test',
                               '-SshKeyPath', str(key), '-SshKnownHostsPath', str(known),
                               secrets=True, wrapper=wrapper)
        self.assertNotEqual(result.returncode, 0)
        entries = [json.loads(line) for line in log.read_text(encoding='utf-8-sig').splitlines()]
        self.assertEqual(len(entries), 6)
        last = entries[-1]
        self.assertEqual(last['args'], ['secret', 'set', 'HOSTINGER_SSH_KEY'])
        self.assertIn('FAKE-KEY-FOR-TEST-ONLY', last['stdin'])
        self.assertNotIn('FAKE-KEY-FOR-TEST-ONLY', result.stdout + result.stderr)
        self.assertNotIn('All six SSH secrets configured successfully', result.stdout)

    def test_native_secret_stdin_has_no_bom_even_when_parent_encoding_has_one(self):
        key = self.root / 'fake-private-key'
        key.write_text('FAKE-KEY-\u00e9', encoding='utf-8-sig')
        known = self.root / 'known_hosts'
        known.write_text('example.test ssh-ed25519 FAKE-HOST-KEY', encoding='utf-8-sig')
        log = self.root / 'native-secret-log.jsonl'
        encoding_log = self.root / 'restored-encoding.txt'
        receiver = self.root / 'record-native-secret.py'
        wrapper = self.root / 'native-wrapper.ps1'
        quoted = lambda path: str(path).replace("'", "''")
        native_tools = self.root / 'native-tools'
        native_tools.mkdir()
        native_gh = native_tools / ('gh.cmd' if os.name == 'nt' else 'gh')
        if os.name == 'nt':
            native_gh.write_text(f'@echo off\n"{sys.executable}" "{receiver}" %*\n', newline='\r\n')
        else:
            native_gh.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(receiver))} "$@"\n')
            native_gh.chmod(0o700)
        for fail_name in ('', 'HOSTINGER_SSH_KEY'):
            with self.subTest(fail_name=fail_name):
                log.unlink(missing_ok=True)
                receiver.write_text(
                    "import json, sys\n"
                    "from pathlib import Path\n"
                    "data = sys.stdin.buffer.read()\n"
                    "entry = {'args': sys.argv[1:], 'stdin': data.decode('utf-8'), 'bom': data.startswith(bytes.fromhex('efbbbf'))}\n"
                    f"with Path({str(log)!r}).open('a', encoding='utf-8') as stream: stream.write(json.dumps(entry) + '\\n')\n"
                    f"sys.exit(9 if sys.argv[-1] == {fail_name!r} else 0)\n",
                    encoding='utf-8')
                wrapper.write_text(
                    "$global:OutputEncoding = [Text.Encoding]::UTF8\n"
                    "[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false\n"
                    f"$env:PATH = '{quoted(native_tools)}' + [IO.Path]::PathSeparator + $env:PATH\n"
                    "try {\n"
                    f"  & '{quoted(ROOT / 'bin/deploy-ci.ps1')}' @args\n"
                    "} finally {\n"
                    f"  [IO.File]::WriteAllText('{quoted(encoding_log)}', [Convert]::ToBase64String($OutputEncoding.GetPreamble()))\n"
                    "}\n", encoding='utf-8')
                result = self.generate('-WithoutTests', '-Force', '-SshHost', 'example.test', '-SshUser', 'test',
                                       '-SshKeyPath', str(key), '-SshKnownHostsPath', str(known),
                                       secrets=True, wrapper=wrapper)
                if fail_name:
                    self.assertNotEqual(result.returncode, 0)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                entries = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(len(entries), 6)
                self.assertTrue(all(not entry['bom'] for entry in entries))
                target = next(entry for entry in entries if entry['args'][-1] == 'HOSTINGER_TARGET_DIR')
                self.assertEqual(target['stdin'].strip(), '/home/test/app')
                self.assertEqual(entries[-1]['stdin'].strip(), 'FAKE-KEY-\u00e9')
                self.assertEqual(encoding_log.read_text(), '77u/')
                self.assertNotIn('FAKE-KEY-', result.stdout + result.stderr)

    def test_known_hosts_isolates_host_entry_and_protects_other_servers(self):
        key = self.root / 'fake-private-key'
        key.write_text('FAKE-KEY')
        known = self.root / 'known_hosts'
        known.write_text(
            'other-private-server.test ssh-ed25519 OTHER-SECRET-KEY\n'
            'example.test ssh-ed25519 HOSTINGER-KEY\n'
            'github.com ssh-rsa GITHUB-KEY\n'
        )
        log = self.root / 'gh-log.jsonl'
        wrapper = self.root / 'wrapper.ps1'
        wrapper.write_text("""function global:gh {
  $value = $input | Out-String
  @{ args = @($args); stdin = $value } | ConvertTo-Json -Compress | Add-Content -LiteralPath '""" + str(log).replace("'", "''") + """'
  $global:LASTEXITCODE = 0
}
& '""" + str(ROOT / 'bin/deploy-ci.ps1').replace("'", "''") + "' @args\n")
        result = self.generate('-WithoutTests', '-SshHost', 'example.test', '-SshUser', 'test',
                               '-SshKeyPath', str(key), '-SshKnownHostsPath', str(known),
                               '-MaintenanceMode', secrets=True, wrapper=wrapper)
        self.assertEqual(result.returncode, 0, result.stderr)
        entries = [json.loads(line) for line in log.read_text(encoding='utf-8-sig').splitlines()]
        known_secret = next(e for e in entries if e['args'] == ['secret', 'set', 'HOSTINGER_SSH_KNOWN_HOSTS'])
        self.assertIn('HOSTINGER-KEY', known_secret['stdin'])
        self.assertNotIn('OTHER-SECRET-KEY', known_secret['stdin'])
        self.assertNotIn('GITHUB-KEY', known_secret['stdin'])
        config = json.loads((self.root / '.github/hostinger/profile.json').read_text())
        self.assertTrue(config.get('maintenance'))


if __name__ == '__main__':
    unittest.main()

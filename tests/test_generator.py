import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
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
                   str(wrapper or ROOT / 'bin/setup-hostinger-ci.ps1'), '-NonInteractive',
                   '-ConfigCachePath', str(self.cache), '-TargetDir', '/home/test/app',
                   '-DeployUrl', 'https://example.test/']
        if not secrets:
            command.append('-SkipSecrets')
        return subprocess.run(command + list(args), cwd=self.root, text=True, capture_output=True,
                              timeout=60)

    def workflow(self):
        # BaseLoader avoids YAML 1.1 treating GitHub's "on" as a boolean.
        return yaml.load((self.root / '.github/workflows/deploy.yml').read_text(encoding='utf-8-sig'), Loader=yaml.BaseLoader)

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
            node = next(step for step in steps if step.get('name') == 'Setup Node.js')
            self.assertEqual(node['with']['node-version'], '24')
            runs = [step.get('run', '') for step in steps]
            transfer = runs.index('python3 .github/hostinger/deploy.py transfer')
            check = runs.index('python3 .github/hostinger/check-deploy.py')
            cleanup = runs.index('python3 .github/hostinger/deploy.py cleanup')
            self.assertLess(transfer, check)
            self.assertLess(check, cleanup)
            cleanup_step = next(s for s in steps if s.get('id') == 'cleanup')
            self.assertIn("steps.transfer.outcome == 'success'", cleanup_step['if'])
            self.assertIn('!cancelled()', cleanup_step['if'])
            self.assertNotIn("steps.verify.outcome == 'success'", cleanup_step['if'])
            self.assertIn("steps.verify.outcome == 'success'", cleanup_step['env']['HOSTINGER_HTTP_VERIFIED'])
            self.assertEqual('verify' in workflow['jobs'], mode == '-WithTests')
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
& '""" + str(ROOT / 'bin/setup-hostinger-ci.ps1').replace("'", "''") + "' @args\n")
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
& '""" + str(ROOT / 'bin/setup-hostinger-ci.ps1').replace("'", "''") + "' @args\n")
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

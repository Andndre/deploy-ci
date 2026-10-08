"""Runner regressions for vendor reuse, transfer diagnostics and bandwidth options."""
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_deployment import SCRATCH
import deploy


STATS = """Number of files: 43,918 (reg: 43,900, dir: 18)
Number of created files: 2 (reg: 2)
Number of deleted files: 1 (reg: 1)
Number of regular files transferred: 4
Total file size: 313,000,000 bytes
Total transferred file size: 1,234 bytes
Literal data: 1,200 bytes
Matched data: 34 bytes
Total bytes sent: 1,500
Total bytes received: 64
"""


class VendorReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        (self.source / 'public/build/assets').mkdir(parents=True)
        (self.source / 'public/build/assets/app-12345678.js').write_text('new asset')
        (self.source / 'public/build/manifest.json').write_text('{}')
        (self.source / 'composer.lock').write_text('{"fixture":"unchanged lock"}')
        self.config = dict(profile='laravel-vite', output_dir=str(self.source),
                           immutable_dirs=['public/build/assets'], protected_paths=['.env*', 'storage'],
                           keep_releases=3, retention_days=7, migrate=True)
        self.env = dict(HOSTINGER_TARGET_DIR='/home/test/app', HOSTINGER_SSH_HOST='example.test',
                        HOSTINGER_SSH_USER='test', RUNNER_TEMP=str(self.root))
        self.recording = patch.object(deploy, 'record')
        self.metrics = self.recording.start()
        self.addCleanup(self.recording.stop)

    def transfer(self, reusable, config=None, environment=None):
        instance = deploy.Deployment(config or self.config, environment or self.env)
        def remote(phase, *args, **kwargs):
            if phase == 'prepare':
                instance.vendor_reusable = reusable
        with patch.object(instance, 'remote', side_effect=remote) as phases, patch.object(instance, 'rsync') as sync:
            instance.transfer()
        return instance, phases, sync

    def test_warm_transfer_updates_autoloader_while_excluding_dependency_packages(self):
        instance, phases, sync = self.transfer(True)
        application = sync.call_args.kwargs
        self.assertEqual(application['includes'], ['vendor', 'vendor/autoload.php', 'vendor/composer/***'])
        self.assertIn('vendor/***', application['excludes'])
        self.assertIn('storage', application['excludes'])
        self.assertTrue(application['delete'])
        lock_hash = deploy.digest(self.source / 'composer.lock')
        self.assertEqual(phases.call_args_list[0].args[-2:], (lock_hash, False))
        self.assertEqual(phases.call_args.kwargs['composer_lock_hash'], lock_hash)
        self.assertEqual(sync.call_args_list[0].args[0], self.source / 'public/build/assets')

    def test_unverified_or_forced_transfer_keeps_full_checksum_sync(self):
        for forced in (False, True):
            with self.subTest(forced=forced):
                _, phases, sync = self.transfer(False, environment=self.env | {'HOSTINGER_FORCE_VENDOR_SYNC': str(forced).lower()})
                self.assertEqual(phases.call_args_list[0].args[-1], forced)
                self.assertEqual(sync.call_args.kwargs['includes'], [])
                self.assertNotIn('vendor/***', sync.call_args.kwargs['excludes'])

    def test_force_vendor_sync_environment_overrides_profile(self):
        for config_value, env_value, expected in ((True, '', True), (False, 'true', True), (True, 'false', False)):
            with self.subTest(config=config_value, environment=env_value):
                _, phases, _ = self.transfer(False, config=self.config | {'force_vendor_sync': config_value},
                                              environment=self.env | {'HOSTINGER_FORCE_VENDOR_SYNC': env_value})
                self.assertEqual(phases.call_args_list[0].args[-1], expected)

    def test_static_profile_never_applies_vendor_reuse_filters(self):
        (self.source / 'index.html').write_text('<html></html>')
        _, _, sync = self.transfer(True, config=self.config | {'profile': 'static-vite'})
        self.assertEqual(sync.call_args.kwargs['includes'], [])
        self.assertNotIn('vendor/***', sync.call_args.kwargs['excludes'])

    def test_rsync_stats_are_returned_and_recorded_with_transfer_duration(self):
        instance = deploy.Deployment(self.config, self.env)
        with patch.object(deploy.subprocess, 'run', return_value=subprocess.CompletedProcess(['rsync'], 0, stdout=STATS)):
            stats = instance.rsync(self.source, instance.target, delete=True)
        self.assertEqual(stats['files_listed'], 43918)
        self.assertEqual(stats['files_transferred'], 4)
        self.assertEqual(stats['total_file_size'], 313000000)
        self.assertEqual(stats['bytes_sent'], 1500)
        self.assertEqual(self.metrics.call_args.args[0], 'rsync-application')
        self.assertGreaterEqual(self.metrics.call_args.args[1], 0)
        self.assertEqual(self.metrics.call_args.args[2], 'success')
        self.assertEqual(self.metrics.call_args.kwargs, stats)
        self.assertEqual(deploy.parse_rsync_stats('no statistics'), {})

    def test_bandwidth_limits_use_environment_profile_and_disabled_values(self):
        for configured, environment, expected in (('250K', '', '250K'), ('250K', '100', '100'), ('250K', '0', None), (0, '', None)):
            with self.subTest(configured=configured, environment=environment):
                instance = deploy.Deployment(self.config | {'rsync_bwlimit': configured}, self.env | {'HOSTINGER_RSYNC_BWLIMIT': environment})
                with patch.object(deploy.subprocess, 'run', return_value=subprocess.CompletedProcess(['rsync'], 0, stdout='')) as run:
                    instance.rsync(self.source, instance.target, (), True)
                command = run.call_args.args[0]
                limits = [value for value in command if value.startswith('--bwlimit=')]
                self.assertEqual(limits, [f'--bwlimit={expected}'] if expected else [])
                self.assertIn('--delete-delay', command)

    def test_invalid_bandwidth_limit_is_rejected_before_transfer(self):
        for value in ('-1', '10.5', '250 --delete', 'unlimited'):
            with self.subTest(value=value):
                instance = deploy.Deployment(self.config, self.env | {'HOSTINGER_RSYNC_BWLIMIT': value})
                with patch.object(deploy.subprocess, 'run') as run, self.assertRaises(ValueError):
                    instance.rsync(self.source, instance.target)
                run.assert_not_called()

    def test_rsync_failure_exposes_output_preserves_exit_code_and_records_failure(self):
        instance = deploy.Deployment(self.config, self.env)
        output = io.StringIO()
        failure = subprocess.CompletedProcess(['rsync'], 23, stdout='rsync: Permission denied (13)')
        with patch.object(deploy.subprocess, 'run', return_value=failure), contextlib.redirect_stdout(output):
            with self.assertRaises(subprocess.CalledProcessError) as error:
                instance.rsync(self.source, instance.target)
        self.assertEqual(error.exception.returncode, 23)
        self.assertIn('Permission denied', error.exception.stdout)
        self.assertIn('Permission denied', output.getvalue())
        self.assertIn('exit code 23', output.getvalue())
        self.assertEqual(self.metrics.call_args.args[2], 'failure')


if __name__ == '__main__':
    unittest.main()

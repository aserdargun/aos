import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import call, patch

from scripts import install_usage_timer


class InstallUsageTimerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / 'repo space%$'
        (self.repo / '.venv/bin').mkdir(parents=True)
        interpreter = self.repo / '.venv/bin/python'
        interpreter.write_text('synthetic executable')
        interpreter.chmod(0o700)
        (self.repo / 'scripts').mkdir()
        (self.repo / 'scripts/record_usage.py').write_text('synthetic collector')
        self.home = self.root / 'home space%$'
        self.home.mkdir(mode=0o700)
        self.platform = patch.object(install_usage_timer.sys, 'platform', 'linux')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.effective = patch.object(install_usage_timer, 'check_effective_conflicts')
        self.effective_mock = self.effective.start()
        self.addCleanup(self.effective.stop)

    def plan(self):
        return install_usage_timer.build_plan(self.repo, self.home)

    def test_plan_is_readonly_and_uses_literal_pinned_paths(self):
        plan = self.plan()
        self.assertFalse(plan['directory'].exists())
        service = plan['units'][install_usage_timer.SERVICE]
        self.assertIn('repo space%%$/.venv/bin/python"', service)
        self.assertIn('repo space%%$$/scripts/record_usage.py"', service)
        self.assertNotIn('WorkingDirectory=', service)
        self.assertNotIn('--public-report', service)
        self.assertIn('UMask=0077', service)
        self.assertIn('Persistent=false', plan['units'][install_usage_timer.TIMER])

    def test_control_characters_are_denied_and_quotes_backslashes_escaped(self):
        for character in ('\n', '\r', '\0', '\t'):
            with self.assertRaises(ValueError):
                install_usage_timer.unit_argument('/synthetic' + character)
        self.assertEqual(install_usage_timer.unit_argument('a"b\\c%'), '"a\\"b\\\\c%%"')

    def test_unsupported_platform_is_denied(self):
        with patch.object(install_usage_timer.sys, 'platform', 'darwin'):
            with self.assertRaises(ValueError):
                self.plan()

    def test_missing_interpreter_is_denied(self):
        (self.repo / '.venv/bin/python').unlink()
        with self.assertRaises(ValueError):
            self.plan()

    @patch.object(install_usage_timer.subprocess, 'run')
    def test_install_private_and_idempotent_without_enable(self, run):
        plan = self.plan()
        install_usage_timer.install(plan)
        self.assertEqual(plan['directory'].stat().st_mode & 0o777, 0o700)
        for name in plan['units']:
            self.assertEqual((plan['directory'] / name).stat().st_mode & 0o777, 0o600)
        original = (plan['directory'] / install_usage_timer.SERVICE).stat().st_ino
        install_usage_timer.install(plan)
        self.assertEqual((plan['directory'] / install_usage_timer.SERVICE).stat().st_ino, original)
        self.assertEqual(run.call_count, 2)
        run.assert_called_with(['systemctl', '--user', 'daemon-reload'], check=True)

    @patch.object(install_usage_timer.subprocess, 'run')
    def test_conflict_refused_without_overwrite_or_systemctl(self, run):
        plan = self.plan()
        plan['directory'].mkdir(parents=True, mode=0o700)
        path = plan['directory'] / install_usage_timer.SERVICE
        path.write_text('unknown existing unit')
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            install_usage_timer.install(plan)
        self.assertEqual(path.read_text(), 'unknown existing unit')
        run.assert_not_called()

    @patch.object(install_usage_timer.subprocess, 'run')
    def test_enable_is_separate_requires_exact_units(self, run):
        plan = self.plan()
        with self.assertRaises(ValueError):
            install_usage_timer.enable(plan)
        run.assert_not_called()
        install_usage_timer.install(plan)
        run.reset_mock()
        install_usage_timer.enable(plan)
        self.assertEqual(run.call_args_list, [
            call(['systemctl', '--user', 'daemon-reload'], check=True),
            call(['systemctl', '--user', 'enable', '--now', install_usage_timer.TIMER], check=True)])

    def test_nonprivate_directory_and_symlink_unit_are_denied(self):
        plan = self.plan()
        plan['directory'].mkdir(parents=True, mode=0o755)
        install_usage_timer.check_existing(plan)
        plan['directory'].chmod(0o775)
        with self.assertRaises(ValueError):
            install_usage_timer.check_existing(plan)
        plan['directory'].chmod(0o700)
        (plan['directory'] / install_usage_timer.SERVICE).symlink_to(self.repo / 'scripts/record_usage.py')
        with self.assertRaises(ValueError):
            install_usage_timer.check_existing(plan)

    @patch.object(install_usage_timer.subprocess, 'run')
    def test_effective_same_name_and_generic_dropins_are_refused(self, run):
        self.effective.stop()
        plan = self.plan()
        other = self.root / 'synthetic-global-units'
        other.mkdir()
        run.return_value.stdout = str(other) + '\n' + str(plan['directory']) + '\n'
        install_usage_timer.check_effective_conflicts(plan)
        conflicting = other / install_usage_timer.SERVICE
        conflicting.write_text('synthetic global unit')
        with self.assertRaises(ValueError):
            install_usage_timer.check_effective_conflicts(plan)
        conflicting.unlink()
        for name in ('service.d', 'aos-.service.d', 'aos-usage-record.service.d'):
            directory = other / name
            directory.mkdir()
            configuration = directory / 'synthetic.conf'
            configuration.write_text('synthetic override')
            with self.assertRaises(ValueError):
                install_usage_timer.check_effective_conflicts(plan)
            configuration.unlink()
            directory.rmdir()

    def test_effective_conflict_prevents_install_mutation(self):
        self.effective_mock.side_effect = ValueError('synthetic conflict')
        plan = self.plan()
        with self.assertRaises(ValueError):
            install_usage_timer.install(plan)
        self.assertFalse(plan['directory'].exists())

    @patch.object(install_usage_timer.subprocess, 'run')
    def test_unreachable_default_directory_is_refused(self, run):
        self.effective.stop()
        run.return_value.stdout = str(self.root / 'synthetic-xdg/systemd/user') + '\n'
        with self.assertRaises(ValueError):
            install_usage_timer.check_effective_conflicts(self.plan())

    @patch.object(install_usage_timer.subprocess, 'run')
    def test_enable_does_not_activate_if_reload_fails(self, run):
        plan = self.plan()
        install_usage_timer.install(plan)
        run.reset_mock()
        run.side_effect = install_usage_timer.subprocess.CalledProcessError(1, 'synthetic-systemctl')
        with self.assertRaises(install_usage_timer.subprocess.CalledProcessError):
            install_usage_timer.enable(plan)
        run.assert_called_once_with(['systemctl', '--user', 'daemon-reload'], check=True)

    @unittest.skipUnless(os.environ.get('AOS_VERIFY_SYSTEMD') == '1'
                         and shutil.which('systemd-analyze'), 'Opt-in real unit syntax verification')
    def test_real_systemd_analyze_verifies_generated_units_without_install(self):
        plan = self.plan()
        staging = self.root / 'synthetic-unit-verification'
        staging.mkdir(mode=0o700)
        paths = []
        for name, content in plan['units'].items():
            path = staging / name
            path.write_text(content)
            paths.append(str(path))
        result = install_usage_timer.subprocess.run(
            ['systemd-analyze', '--user', 'verify', *paths], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(plan['directory'].exists())


if __name__ == '__main__':
    unittest.main()

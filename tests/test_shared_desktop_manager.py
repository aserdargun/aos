import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos import local_app
from aos.contracts import canonical, digest
from aos.lifecycle import process_identity
from aos.shared_desktop_manager import prepare_shared, provision_shared, shared_host_scope, start_shared, verify_predecessor
from aos.shared_desktop_plan import SharedDesktopLimits, SharedDesktopTemplate, load_plan
from test_shared_desktop_host import SyntheticSharedHostFixture


class SharedDesktopManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / 'local-app-v1'
        self.base_patch = patch('aos.local_app.BASE', self.base)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        source_files = {}
        for name in ('synthetic-python', 'aos_native_launch.py'):
            path = self.root / name
            path.write_text('Explicitly synthetic source pin; never executed\n')
            path.chmod(0o600)
            source_files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        configuration = self.root / 'synthetic-config.json'
        configuration.write_text('{"synthetic":true}')
        configuration.chmod(0o600)
        config_files = {str(configuration): hashlib.sha256(configuration.read_bytes()).hexdigest()}
        self.template = SharedDesktopTemplate(origin=local_app.ORIGIN, manager_base=str(self.base),
            session_root=str(self.base), python_path=str(self.root / 'synthetic-python'),
            python_sha256=source_files[str(self.root / 'synthetic-python')],
            launcher_path=str(self.root / 'aos_native_launch.py'),
            launcher_sha256=source_files[str(self.root / 'aos_native_launch.py')],
            source_files=source_files, source_sha256=digest(source_files),
            config_files=config_files, config_sha256=digest(config_files),
            broker_socket=str(self.root / 'synthetic-broker.sock'), broker_identity_sha256='a' * 64,
            limits=SharedDesktopLimits(cpu_quota_percent=100, memory_max_bytes=536870912,
                                      tasks_max=32, stop_timeout_seconds=5))
        self.template_path = self.root / 'shared-template.json'
        self.template_path.write_text(canonical(self.template.model_dump(mode='json')))
        self.template_path.chmod(0o600)
        self.template_sha = hashlib.sha256(self.template_path.read_bytes()).hexdigest()
        self.output = self.root / 'shared-plan.json'

    def predecessor(self):
        local_app.prepare_base()
        identity = process_identity(os.getpid())
        state = local_app.LocalAppState(session='app-' + 'a' * 32, mode='real', phase='running',
            supervisor=identity, backend=identity, token_name='desktop-console-' + 'b' * 16 + '.token', started_at='synthetic')
        local_app.write_state(state)
        return state

    def prepare(self, expected='none'):
        return prepare_shared(self.template_path, self.template_sha, self.output, expected_session=expected)

    def cli(self, arguments):
        output = StringIO()
        with patch('sys.argv', ['aos-v1', *arguments]), patch('sys.stdout', output):
            local_app.main()
        return json.loads(output.getvalue())

    def test_inert_preparation_does_not_allocate_runtime_or_change_live_predecessor(self):
        previous = self.predecessor()
        original = (self.base / 'current.json').read_bytes()
        with (patch('aos.local_app.start') as start, patch('aos.local_app.require_local_preflight') as preflight,
              patch('aos.local_app.socket.socket') as socket, patch('aos.local_app.subprocess.Popen') as spawn,
              patch('aos.local_app.signal_owned') as signal, patch('aos.local_app.prepare_base') as prepare_base):
            report = self.prepare(previous.session)
        for effect in (start, preflight, socket, spawn, signal, prepare_base):
            effect.assert_not_called()
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['runtime_started'])
        self.assertEqual((self.base / 'current.json').read_bytes(), original)
        self.assertFalse(Path(report['workspace']).exists())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        plan = load_plan(self.output, report['plan_sha256'])
        self.assertEqual(plan.predecessor_session, previous.session)
        self.assertEqual(plan.predecessor_snapshot_sha256, hashlib.sha256(original).hexdigest())

    def test_public_prepare_and_explicit_none_preserve_native_v1_defaults(self):
        report = self.cli(['prepare-shared', '--shared-template', str(self.template_path),
            '--shared-template-sha256', self.template_sha, '--shared-output', str(self.output), '--expected-session', 'none'])
        self.assertEqual(report['phase'], 'prepared')
        self.assertFalse(self.base.exists())
        self.assertIsNone(report['predecessor_session'])
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_public_provision_preserves_running_predecessor_without_runtime_effects(self):
        previous = self.predecessor()
        prepared = self.prepare(previous.session)
        original = (self.base / 'current.json').read_bytes()
        with (patch('aos.shared_desktop_manager._host') as host,
              patch('aos.local_app.start') as start, patch('aos.local_app.prepare_base') as prepare_base,
              patch('aos.local_app.require_local_preflight') as preflight,
              patch('aos.local_app.socket.socket') as socket, patch('aos.local_app.subprocess.Popen') as spawn,
              patch('aos.local_app.signal_owned') as signal):
            report = self.cli(['provision-shared', '--shared-plan', str(self.output),
                '--shared-plan-sha256', prepared['plan_sha256'], '--expected-session', previous.session])
        for effect in (host, start, prepare_base, preflight, socket, spawn, signal):
            effect.assert_not_called()
        self.assertEqual(report['phase'], 'provisioned')
        self.assertFalse(report['runtime_started'])
        self.assertFalse(report['execution_authorized'])
        self.assertEqual((self.base / 'current.json').read_bytes(), original)
        workspace = Path(report['workspace'])
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
        self.assertEqual(set(child.name for child in workspace.parent.iterdir()), {'workspace', 'shared-provision.json'})
        receipt = Path(report['provision_path'])
        self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(), report['provision_sha256'])
        with self.assertRaises(FileExistsError):
            provision_shared(self.output, prepared['plan_sha256'], expected_session=previous.session)
        self.assertEqual((self.base / 'current.json').read_bytes(), original)

    def test_provision_requires_existing_private_manager_base_and_explicit_none(self):
        prepared = self.prepare()
        with self.assertRaises(FileNotFoundError):
            provision_shared(self.output, prepared['plan_sha256'], expected_session='none')
        self.assertFalse(self.base.exists())
        local_app.prepare_base()
        with self.assertRaises(ValueError):
            provision_shared(self.output, prepared['plan_sha256'], expected_session=None)
        report = provision_shared(self.output, prepared['plan_sha256'], expected_session='none')
        self.assertIsNone(local_app.read_state())
        self.assertTrue(Path(report['workspace']).is_dir())

    def test_provision_allows_stable_phase_change_but_rejects_changed_owner(self):
        previous = self.predecessor()
        prepared = self.prepare(previous.session)
        changed = previous.model_copy(update={'supervisor': previous.supervisor.model_copy(
            update={'pid': previous.supervisor.pid + 1})})
        local_app.write_state(changed)
        with self.assertRaisesRegex(ValueError, 'stable predecessor'):
            provision_shared(self.output, prepared['plan_sha256'], expected_session=previous.session)
        self.assertFalse(Path(prepared['workspace']).parent.exists())
        stopped = previous.model_copy(update={'phase': 'stopped', 'token_name': None})
        local_app.write_state(stopped)
        provision_shared(self.output, prepared['plan_sha256'], expected_session=previous.session)
        self.assertEqual(local_app.read_state(), stopped)

    def test_provision_rejects_modified_source_before_allocating_scope(self):
        previous = self.predecessor()
        prepared = self.prepare(previous.session)
        (self.root / 'aos_native_launch.py').write_text('Changed source')
        with self.assertRaises(ValueError):
            provision_shared(self.output, prepared['plan_sha256'], expected_session=previous.session)
        self.assertFalse(Path(prepared['workspace']).parent.exists())
        self.assertEqual(local_app.read_state(), previous)

    def test_provision_uses_existing_manager_lock_without_interrupting_owner(self):
        previous = self.predecessor()
        prepared = self.prepare(previous.session)
        with local_app.instance_lock(), self.assertRaises(BlockingIOError):
            provision_shared(self.output, prepared['plan_sha256'], expected_session=previous.session)
        self.assertFalse(Path(prepared['workspace']).parent.exists())

    def test_wrong_session_modified_pin_and_foreign_scope_refuse_before_output(self):
        previous = self.predecessor()
        for expected in ('none', 'app-' + 'c' * 32, None):
            with self.assertRaises(ValueError):
                self.prepare(expected)
        source = self.root / 'aos_native_launch.py'
        source.write_text('Changed after review')
        with self.assertRaises(ValueError):
            self.prepare(previous.session)
        self.assertFalse(self.output.exists())

    def test_shared_start_refuses_running_predecessor_before_host_or_native_effects(self):
        previous = self.predecessor()
        prepared = self.prepare(previous.session)
        with (patch('aos.shared_desktop_manager._host') as host,
              patch('aos.local_app.prepare_base') as prepare_base,
              patch('aos.local_app.signal_owned') as signal, patch('aos.local_app.start') as native):
            with self.assertRaisesRegex(ValueError, 'already-stopped'):
                start_shared(self.output, prepared['plan_sha256'], self.root / 'absent-activation.json',
                             'd' * 64, provision_sha256='e' * 64, expected_session=previous.session)
        for effect in (host, prepare_base, signal, native):
            effect.assert_not_called()
        self.assertEqual(local_app.read_state(), previous)

    def test_same_identity_clean_stop_allows_plan_but_not_new_predecessor(self):
        previous = self.predecessor()
        prepared = self.prepare(previous.session)
        plan = load_plan(self.output, prepared['plan_sha256'])
        stopped = previous.model_copy(update={'phase': 'stopped', 'token_name': None})
        local_app.write_state(stopped)
        with (patch('aos.local_app.observe_process', return_value='not_observed'),
              patch('aos.local_app.clean_shutdown', return_value=True)):
            verify_predecessor(plan, stopped.model_dump(mode='json'))
        with (patch('aos.local_app.observe_process', return_value='not_observed'),
              patch('aos.local_app.clean_shutdown', return_value=False)):
            with self.assertRaisesRegex(ValueError, 'cleanup'):
                verify_predecessor(plan, stopped.model_dump(mode='json'))
        replacement = stopped.model_copy(update={'session': 'app-' + 'f' * 32})
        local_app.write_state(replacement)
        with self.assertRaises(ValueError):
            verify_predecessor(plan, stopped.model_dump(mode='json'))

    def test_cli_shared_flags_cannot_mix_modes_or_escape_command_scope(self):
        for command in (
            ['prepare-shared', '--fixture'],
            ['start', '--shared-plan', str(self.output), '--synthetic-learning'],
            ['status', '--shared-plan', str(self.output)],
            ['prepare-shared', '--shared-activation', str(self.output)],
            ['start', '--shared-template', str(self.template_path)],
            ['provision-shared', '--fixture'],
            ['provision-shared', '--shared-activation', str(self.output)],
            ['provision-shared', '--shared-output', str(self.output)],
            ['provision-shared', '--shared-provision-sha256', 'a' * 64],
            ['prepare-shared', '--shared-provision-sha256', 'a' * 64],
        ):
            with self.subTest(command=command), patch('sys.stderr', new_callable=StringIO), \
                    patch('aos.local_app.start') as native, self.assertRaises(SystemExit):
                self.cli(command)
            native.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_public_shared_start_dispatches_exact_paths_without_native_start(self):
        arguments = ['start', '--shared-plan', str(self.output), '--shared-plan-sha256', 'a' * 64,
            '--shared-activation', str(self.root / 'activation.json'), '--shared-activation-sha256', 'b' * 64,
            '--shared-provision-sha256', 'c' * 64,
            '--expected-session', 'none']
        with (patch('aos.shared_desktop_manager.start_shared', return_value={'phase': 'synthetic-dispatched'}) as shared,
              patch('aos.local_app.start') as native):
            report = self.cli(arguments)
        shared.assert_called_once_with(self.output, 'a' * 64, self.root / 'activation.json', 'b' * 64,
                                       provision_sha256='c' * 64, expected_session='none')
        native.assert_not_called()
        self.assertEqual(report, {'phase': 'synthetic-dispatched'})


class SharedDesktopManagerHostIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.fixture = SyntheticSharedHostFixture(self.root)
        for target, value in (
            ('aos.local_app.BASE', self.root / 'data/local-app-v1'),
            ('aos.local_app.REPO_ROOT', self.root),
            ('aos.shared_desktop_host.REPO_ROOT', self.root),
            ('aos.shared_desktop_host.SCIENTIST_ROOT', self.root),
        ):
            replacement = patch(target, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.template_path = self.fixture.file('manager-template.json',
            canonical(self.fixture.template.model_dump(mode='json')).encode())
        self.output = self.root / 'manager-plan.json'
        self.original_launch = self.fixture.launch
        self.fixture.transport.start.side_effect = self.launch_after_durable_manager_intent
        self.fixture.host.activation_claimer = self.claim_after_durable_manager_intent

    def claim_after_durable_manager_intent(self, plan, activation, state):
        recorded = local_app.read_state()
        self.assertEqual(recorded, state)
        self.fixture.states.append(recorded)
        self.fixture.claim_authority(plan, activation, state)

    def launch_after_durable_manager_intent(self, plan, activation):
        recorded = local_app.read_state()
        self.assertEqual(recorded.version, '2')
        self.assertEqual(recorded.phase, 'starting')
        self.assertEqual(recorded.session, plan.app_session)
        self.assertEqual(recorded.workspace_identity, self.fixture.host._workspace(plan.workspace))
        return self.original_launch(plan, activation)

    @staticmethod
    def cli(arguments):
        output = StringIO()
        with patch('sys.argv', ['aos-v1', *arguments]), patch('sys.stdout', output):
            local_app.main()
        return output.getvalue()

    def prepare(self):
        with patch('aos.shared_desktop_manager.identifier', return_value=self.fixture.plan.app_session):
            return json.loads(self.cli(['prepare-shared', '--shared-template', str(self.template_path),
                '--shared-template-sha256', self.fixture.sha(self.template_path.read_bytes()),
                '--shared-output', str(self.output), '--expected-session', 'none']))

    def start(self, prepared):
        return json.loads(self.cli(['start', '--shared-plan', str(self.output),
            '--shared-plan-sha256', prepared['plan_sha256'],
            '--shared-activation', str(self.fixture.activation_path),
            '--shared-provision-sha256', self.fixture.provision_sha,
            '--shared-activation-sha256', self.fixture.activation_sha, '--expected-session', 'none']))

    def test_public_cli_actual_host_prepare_start_status_token_stop(self):
        with shared_host_scope(lambda: self.fixture.host):
            prepared = self.prepare()
            self.assertFalse(prepared['runtime_started'])
            self.assertIsNone(local_app.read_state())
            self.assertTrue(Path(prepared['workspace']).is_dir())
            self.assertEqual(list(Path(prepared['workspace']).iterdir()), [])
            self.assertEqual(prepared['plan_sha256'], self.fixture.plan.plan_sha256())
            started = self.start(prepared)
            self.assertEqual(started['phase'], 'running', started)
            self.assertTrue(started['ui_online'])
            self.assertTrue(started['task_admission_enabled'])
            state = local_app.read_state()
            self.assertEqual(state.version, '2')
            self.assertEqual(state.service_binding, self.fixture.service)
            self.assertEqual(state.runtime_binding.birth.workspace, state.workspace_identity)
            self.assertEqual(json.loads(self.cli(['status']))['session'], state.session)
            token = self.cli(['token']).strip()
            self.assertEqual(token, self.fixture.host.token_value(state))
            stopped = json.loads(self.cli(['stop', '--expected-session', state.session]))
            self.assertEqual(stopped['phase'], 'stopped', stopped)
            self.assertTrue(stopped['cleanup_verified'])
            self.assertFalse(stopped['ui_online'])
            self.assertTrue(local_app.clean_shutdown(local_app.read_state()))
            self.fixture.transport.start.assert_called_once()
            self.fixture.transport.stop.assert_called_once()

    def test_v2_state_rejects_native_fallback_restart_and_recovery_without_effects(self):
        with shared_host_scope(lambda: self.fixture.host):
            state = self.start(self.prepare())
            self.assertEqual(state['phase'], 'running', state)
            original = local_app.read_state()
            with (patch('aos.local_app.subprocess.Popen') as spawn,
                  patch('aos.local_app.signal_owned') as signal,
                  patch('aos.local_app.require_local_preflight') as preflight):
                for operation in (
                    lambda: local_app.start(),
                    lambda: local_app.restart(expected_session=original.session),
                    lambda: local_app.recover_reboot(expected_session=original.session),
                    lambda: local_app.recover_clean_exit(expected_session=original.session),
                    lambda: local_app.release_restart_quiesce(expected_session=original.session),
                ):
                    with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, 'Shared'):
                        operation()
                    self.assertEqual(local_app.read_state(), original)
                for effect in (spawn, signal, preflight):
                    effect.assert_not_called()
            self.fixture.transport.start.assert_called_once()
            self.fixture.transport.stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()

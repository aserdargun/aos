import json
from io import StringIO
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aos import local_app
from aos.contracts import REPO_ROOT
from aos.lifecycle import process_identity
from aos.owned_learning_workspace import OwnedLearningWorkspace
from aos.owned_parameter_project import (
    APPLICATIONS, owned_parameter_project_review_sha256, provision_owned_parameter_project,
)


class LauncherArgumentTests(unittest.TestCase):
    def test_public_start_forwards_exact_project_pins(self):
        directory = Path('/tmp/synthetic-project')
        with (patch('sys.argv', ['aos-v1', 'start',
                                '--owned-parameter-project-directory', str(directory),
                                '--owned-parameter-project-manifest-sha256', 'a' * 64,
                                '--owned-parameter-project-engine', 'fixture']),
              patch('sys.stdout', new_callable=StringIO),
              patch('aos.local_app.start', return_value={'phase': 'running'}) as start):
            local_app.main()
        self.assertEqual(start.call_args.kwargs['owned_parameter_project_directory'], directory)
        self.assertEqual(start.call_args.kwargs['owned_parameter_project_manifest_sha256'], 'a' * 64)

    def test_project_options_are_start_only(self):
        with (patch('sys.argv', ['aos-v1', 'status',
                                '--owned-parameter-project-directory', '/tmp/synthetic-project']),
              patch('sys.stderr', new_callable=StringIO),
              patch('aos.local_app.current_status') as status,
              self.assertRaises(SystemExit)):
            local_app.main()
        status.assert_not_called()


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic certificate preparation')
class ParameterProjectLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aos-project-launcher-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / 'manager'
        self.base_patch = patch('aos.local_app.BASE', self.base)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.parameters = {'record-id': 'Synthetic Ada', 'note-text': 'Synthetic note'}

    def provision(self, application=APPLICATIONS[0], port=19443):
        return provision_owned_parameter_project(
            self.root / application, port, application_key=application, parameters=self.parameters,
            confirm_parameters_sha256=owned_parameter_project_review_sha256(application, self.parameters),
            human_confirmation=True)

    def options(self, bundle):
        return {'owned_parameter_project_directory': bundle['directory'],
                'owned_parameter_project_manifest_sha256': bundle['manifest_sha256'],
                'owned_parameter_project_engine': 'fixture'}

    def backend_options(self, bundle):
        return {key: value for key, value in self.options(bundle).items()
                if key != 'owned_parameter_project_engine'} | {
            'owned_form_listener_fd': 42, 'owned_learning_lock_fd': 43,
            'owned_form_manifest_sha256': bundle['manifest_sha256'],
            'owned_form_recipe_sha256': bundle['recipe_sha256']}

    def test_command_uses_exact_external_sources_and_manual_mode_for_both_apps(self):
        for application in APPLICATIONS:
            with self.subTest(application=application):
                bundle = self.provision(application)
                session = self.base / ('app-' + 'a' * 32)
                command = local_app.managed_backend_command(
                    session, 'real', 41, **self.backend_options(bundle))
                self.assertEqual(command[command.index('--owned-parameter-project-directory') + 1],
                                 str(bundle['directory']))
                self.assertEqual(command[command.index('--owned-parameter-project-manifest-sha256') + 1],
                                 bundle['manifest_sha256'])
                self.assertEqual(command[command.index('--workspace') + 1], str(session / 'workspace'))
                self.assertEqual(command[command.index('--database') + 1], str(session / 'store.sqlite'))
                self.assertIn('--owned-learning-lock-fd', command)
                self.assertNotIn('--owned-synthetic-form-recipe', command)
                self.assertNotIn('--owned-form-manifest-sha256', command)
                self.assertNotIn('--local-ui-auto-login', command)
                self.assertNotIn('--remote-entry-task-file', command)
                self.assertEqual(command[command.index('--engine') + 1], 'fixture')
                self.assertEqual(command[command.index('--vision-engine') + 1], 'fixture')
                self.assertFalse(any(option in command for option in (
                    '--reuse-decider', '--prewarm-decider', '--prewarm-idle-seconds', '--gpu-idle-seconds')))
                for change in ({'owned_learning_lock_fd': None},
                               {'owned_form_manifest_sha256': '0' * 64},
                               {'remote_routes_plan_sha256': '1' * 64},
                               {'owned_skill_reuse_sha256': '2' * 64}):
                    with self.subTest(change=change), self.assertRaises(ValueError):
                        local_app.managed_backend_command(
                            session, 'real', 41, **(self.backend_options(bundle) | change))

    def test_incomplete_conflicting_changed_and_application_port_sources_reject(self):
        bundle = self.provision()
        for options in (
                {'owned_parameter_project_directory': bundle['directory']},
                self.options(bundle) | {'owned_parameter_project_manifest_sha256': '0' * 64},
                self.options(bundle) | {'owned_parameter_project_engine': None},
                self.options(bundle) | {'owned_synthetic_form_invocation': True},
                self.options(bundle) | {'synthetic_learning': True},
                self.options(bundle) | {'remote_entry_profile_sha256': '1' * 64},
                self.options(bundle) | {'owned_parameter_project_directory': REPO_ROOT / 'data'},
                self.options(bundle) | {'owned_parameter_project_directory': Path('relative')}):
            with self.subTest(options=options), patch('aos.local_app.subprocess.Popen') as launch:
                with self.assertRaises((ValueError, OSError)):
                    local_app.start(check=False, **options)
                launch.assert_not_called()
        changed = bundle['directory'] / 'owned-record-config.json'
        changed.write_bytes(changed.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            local_app.managed_backend_command(self.base / ('app-' + 'a' * 32), 'real', 41,
                                              **self.backend_options(bundle))
        conflicting = self.provision(APPLICATIONS[1], port=8765)
        with self.assertRaisesRegex(ValueError, 'port conflicts'):
            local_app.prepare_owned_parameter_project(**{
                'directory': conflicting['directory'], 'manifest_sha256': conflicting['manifest_sha256']})

    def test_start_retains_exact_listener_lock_and_single_use_activation(self):
        bundle = self.provision()
        sockets = []
        state_holder = {}
        commands = []

        class SyntheticSocket:
            def __init__(self, *_arguments):
                self.descriptor = 41 + len(sockets)
                sockets.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                pass

            def setsockopt(self, *_arguments):
                pass

            def bind(self, address):
                self.address = address

            def listen(self, _backlog):
                pass

            def fileno(self):
                return self.descriptor

            def getsockname(self):
                return self.address

        def launch(command, **kwargs):
            commands.append(command)
            session = command[command.index('--session') + 1]
            self.assertIn(sockets[1].fileno(), kwargs['pass_fds'])
            lock_fd = int(command[command.index('--owned-learning-lock-fd') + 1])
            self.assertIn(lock_fd, kwargs['pass_fds'])
            with OwnedLearningWorkspace.adopt(bundle['directory'], lock_fd):
                with self.assertRaisesRegex(ValueError, 'busy'):
                    OwnedLearningWorkspace.acquire(bundle['directory'])
            state_holder['state'] = local_app.LocalAppState(
                session=session, mode='real', phase='running',
                supervisor=process_identity(os.getpid()), started_at='synthetic',
                owned_synthetic_parameter_project=True,
                owned_parameter_project_runtime='fixture_cpu',
                owned_form_manifest_sha256=bundle['manifest_sha256'],
                owned_form_invocation_sha256=bundle['invocation_sha256'],
                owned_form_recipe_sha256=bundle['recipe_sha256'],
                owned_parameter_project_directory=str(bundle['directory']),
                owned_parameter_project_manifest_sha256=bundle['manifest_sha256'])
            return SimpleNamespace(wait=lambda timeout: 0)

        with (patch('aos.local_app.socket.socket', SyntheticSocket),
              patch('aos.local_app.subprocess.Popen', side_effect=launch),
              patch('aos.local_app.read_state', side_effect=lambda: state_holder.get('state'))):
            result = local_app.start(check=False, **self.options(bundle))
        self.assertEqual(sockets[0].address, ('127.0.0.1', 8765))
        self.assertEqual(sockets[1].address, ('127.0.0.1', bundle['manifest']['port']))
        self.assertTrue(result['owned_synthetic_parameter_project'])
        self.assertFalse(result['owned_synthetic_form_recipe'])
        self.assertEqual(result['owned_parameter_project_manifest_sha256'], bundle['manifest_sha256'])
        self.assertFalse((self.base / result['session'] / 'owned-form').exists())
        activation = json.loads((bundle['directory'] / '.aos-parameter-activation.json').read_bytes())
        self.assertEqual(activation, {'session': result['session'], 'manifest_sha256': bundle['manifest_sha256']})
        with (patch('aos.local_app.subprocess.Popen') as launch_again,
              self.assertRaisesRegex(ValueError, 'already activated')):
            local_app.start(check=False, **self.options(bundle))
        launch_again.assert_not_called()
        self.assertEqual(len(commands), 1)
        self.assertNotIn('--owned-synthetic-form-recipe', commands[0])

    def test_previous_live_state_and_uncertain_activation_remain_denied(self):
        bundle = self.provision()
        previous = local_app.LocalAppState(
            session='app-' + 'b' * 32, mode='fixture', phase='running',
            supervisor=process_identity(os.getpid()), started_at='synthetic')
        local_app.prepare_base()
        local_app.write_state(previous)
        with (patch('aos.local_app.subprocess.Popen') as launch,
              self.assertRaisesRegex(ValueError, 'Previous session requires inspection')):
            local_app.start(check=False, **self.options(bundle))
        launch.assert_not_called()
        self.assertEqual(local_app.read_state(), previous)
        self.assertFalse((bundle['directory'] / '.aos-parameter-activation.json').exists())

    def test_supervisor_rejects_foreign_activation_session_before_runtime_start(self):
        bundle = self.provision()
        session = 'app-' + 'a' * 32
        local_app.prepare_base()
        (self.base / session).mkdir(mode=0o700)
        local_app.write_new_private_plan(
            bundle['directory'] / '.aos-parameter-activation.json',
            json.dumps({'session': 'app-' + 'b' * 32,
                        'manifest_sha256': bundle['manifest_sha256']}).encode())
        with (local_app.instance_lock() as manager_lock,
              OwnedLearningWorkspace.acquire(bundle['directory'], create=True) as source_lock,
              patch('aos.local_app.signal.signal'),
              patch('aos.local_app.subprocess.Popen') as launch,
              self.assertRaisesRegex(ValueError, 'another manager session')):
            local_app.supervise(
                session, 'real', manager_lock, 41, owned_form_listener_fd=42,
                owned_learning_lock_fd=source_lock.lock_fd,
                owned_form_manifest_sha256=bundle['manifest_sha256'],
                **{key: value for key, value in self.options(bundle).items()
                   if key != 'owned_parameter_project_engine'})
        launch.assert_not_called()
        self.assertIsNone(local_app.read_state())

    def test_running_manager_is_never_reused_for_project_activation(self):
        bundle = self.provision()
        local_app.prepare_base()
        with (local_app.instance_lock(),
              patch('aos.local_app.subprocess.Popen') as launch,
              self.assertRaisesRegex(ValueError, 'fresh session')):
            local_app.start(check=False, **self.options(bundle))
        launch.assert_not_called()
        self.assertFalse((bundle['directory'] / '.aos-parameter-activation.json').exists())

    def test_schema_honest_project_state_and_restart_guard(self):
        bundle = self.provision()
        state = local_app.LocalAppState(
            session='app-' + 'a' * 32, mode='real', phase='stopped',
            supervisor=process_identity(os.getpid()), started_at='synthetic',
            owned_synthetic_parameter_project=True,
            owned_parameter_project_runtime='fixture_cpu',
            owned_form_manifest_sha256=bundle['manifest_sha256'],
            owned_form_invocation_sha256=bundle['invocation_sha256'],
            owned_form_recipe_sha256=bundle['recipe_sha256'],
            owned_parameter_project_directory=str(bundle['directory']),
            owned_parameter_project_manifest_sha256=bundle['manifest_sha256'])
        schema = json.loads((REPO_ROOT / 'schemas/local_app_state.schema.json').read_text())
        self.assertEqual(schema, local_app.LocalAppState.model_json_schema())
        local_app.prepare_base()
        local_app.write_state(state)
        self.assertEqual(local_app.read_state(), state)
        with self.assertRaisesRegex(ValueError, 'plain local session'):
            local_app.restart()
        for change in ({'owned_synthetic_parameter_project': False},
                       {'owned_synthetic_form_recipe': True},
                       {'owned_parameter_project_manifest_sha256': '0' * 64}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                local_app.LocalAppState.model_validate(state.model_dump() | change)


if __name__ == '__main__':
    unittest.main()

"""Named-manager isolation with private synthetic files and mocked launches only."""

from contextlib import ExitStack
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from aos import local_app
from aos.contracts import REPO_ROOT, canonical, digest
from aos.lifecycle import LifecycleBirth, LifecycleEvent, process_identity
from aos.workspace_identity import open_existing_workspace, workspace_identity


class ReservedSocket:
    def __init__(self, descriptor, port):
        self.descriptor = descriptor
        self.port = port
        self.address = None

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
        return ('127.0.0.1', self.port)


class LocalAppProjectTests(unittest.TestCase):
    def setUp(self):
        self.project = local_app.LocalAppInstance.for_project('test-' + uuid4().hex, 31821)
        self.addCleanup(lambda: shutil.rmtree(self.project.base, ignore_errors=True))
        self.identity = process_identity(os.getpid())
        self.session = 'app-' + uuid4().hex
        self.globals = (local_app.BASE, local_app.URL, local_app.ORIGIN)

    def state(self, **changes):
        return local_app.LocalAppState(session=self.session, mode='fixture', phase='running',
            supervisor=self.identity, backend=self.identity, started_at='synthetic',
            project=self.project.project, url=self.project.url, **changes)

    def test_named_readiness_requires_exact_scope_and_manager_session(self):
        state = self.state()
        with local_app.instance_scope(self.project):
            expected = local_app._session_readiness(state, auto_login=False)
            self.assertEqual(expected, {'authenticated': False,
                'manager_scope': {'project': self.project.project, 'port': self.project.port},
                'manager_session': self.session})
            for changes in ({'project': 'foreign'}, {'url': 'http://127.0.0.1:8765/ui/'}):
                with self.assertRaises(ValueError):
                    local_app._session_readiness(state.model_copy(update=changes), auto_login=False)

    def test_default_readiness_retains_legacy_response(self):
        state = self.state().model_copy(update={'project': None, 'url': local_app.URL})
        with local_app.instance_scope(local_app.LocalAppInstance(base=local_app.BASE, port=8765)):
            self.assertEqual(local_app._session_readiness(state, auto_login=False), {'authenticated': False})
            self.assertEqual(local_app._session_readiness(state, auto_login=True),
                             {'authenticated': False, 'local_auto_login': True})

    def persist(self, state=None):
        with local_app.instance_scope(self.project):
            local_app.prepare_base()
            local_app.write_state(state or self.state())

    def cli(self, command, *arguments, project=None):
        instance = project or self.project
        output = StringIO()
        with patch('sys.argv', ['aos-v1', command, *instance.arguments(), *arguments]), patch('sys.stdout', output):
            local_app.main()
        self.assertEqual((local_app.BASE, local_app.URL, local_app.ORIGIN), self.globals)
        return output.getvalue()

    def test_typed_scope_is_immutable_strict_and_does_not_mutate_global_defaults(self):
        for name, port in (('../escape', 30000), ('Upper', 30000), ('', 30000), ('valid', True),
                           ('valid', '30000'), ('valid', 8765), ('valid', 1023), ('valid', 65536)):
            with self.subTest(name=name, port=port), self.assertRaises(ValueError):
                local_app.LocalAppInstance.for_project(name, port)
        with self.assertRaises(ValueError): self.project.port = 32000
        with self.assertRaises(ValueError):
            local_app.LocalAppInstance(base=local_app.BASE, project=self.project.project, port=31821)
        with self.assertRaises(ValueError):
            local_app.LocalAppInstance(base=self.project.base, port=31821)
        before = local_app.current_instance()
        with local_app.instance_scope(self.project): self.assertEqual(local_app.current_instance(), self.project)
        self.assertEqual(local_app.current_instance(), before)
        self.assertEqual((local_app.BASE, local_app.URL, local_app.ORIGIN), self.globals)

    def test_public_status_and_open_are_project_scoped_and_default_state_is_untouched(self):
        self.persist()
        with tempfile.TemporaryDirectory() as temporary:
            default = local_app.LocalAppInstance(base=Path(temporary) / 'default', port=8765)
            with local_app.instance_scope(default):
                local_app.prepare_base()
                original = local_app.LocalAppState(session='app-' + 'a' * 32, mode='fixture', phase='stopped',
                                                   supervisor=self.identity, started_at='synthetic-default')
                local_app.write_state(original)
                raw = (default.base / 'current.json').read_bytes()
                with patch('aos.local_app.observe_process', return_value='same_process'):
                    status = json.loads(self.cli('status'))
                    with patch('aos.local_app.subprocess.run') as opened: self.cli('open')
                self.assertEqual(status['url'], self.project.url)
                self.assertEqual(status['directory'], str(self.project.base / self.session))
                self.assertEqual(status['manager_scope'], {'project': self.project.project, 'port': self.project.port})
                self.assertEqual(opened.call_args.args[0], ['/usr/bin/xdg-open', self.project.url])
                self.assertEqual((default.base / 'current.json').read_bytes(), raw)
                self.assertEqual(local_app.read_state(), original)
                self.assertNotIn('project', original.model_dump())

    def test_port_drift_and_foreign_project_state_deny_before_token_signal_or_http(self):
        self.persist()
        changed = local_app.LocalAppInstance.for_project(self.project.project, self.project.port + 1)
        with ExitStack() as stack:
            effects = [stack.enter_context(patch('aos.local_app.' + name))
                       for name in ('signal_owned', 'token_value', 'subprocess.Popen', 'httpx.Client')]
            stack.enter_context(patch('sys.stderr', new_callable=StringIO))
            for command in ('start', 'status', 'stop', 'token', 'open', 'preview-owned-skill-reuse'):
                with self.subTest(command=command), self.assertRaises(SystemExit): self.cli(command, project=changed)
            foreign = self.state().model_copy(update={'project': 'other', 'url': self.project.url})
            (self.project.base / 'current.json').write_text(canonical(foreign.model_dump()))
            with self.assertRaises(SystemExit): self.cli('stop')
            for effect in effects: effect.assert_not_called()

    def test_pair_required_hidden_base_rejected_and_recovery_not_enabled(self):
        for arguments in (['status', '--project', self.project.project], ['status', '--project-port', '30000'],
                          ['status', '--base', str(self.project.base)],
                          ['status', '--base', str(self.project.base), *self.project.arguments()],
                          ['restart', *self.project.arguments()], ['recover-reboot', *self.project.arguments()],
                          ['start', '--remote-form-plan-file', '/synthetic/plan', *self.project.arguments()],
                          ['start', '--synthetic-learning', *self.project.arguments()]):
            with (self.subTest(arguments=arguments), patch('sys.argv', ['aos-v1', *arguments]),
                  patch('sys.stderr', new_callable=StringIO), patch('aos.local_app.subprocess.Popen') as launch,
                  patch('aos.local_app.signal_owned') as signal, self.assertRaises(SystemExit)):
                local_app.main()
            launch.assert_not_called()
            signal.assert_not_called()

    def test_supervisor_cli_requires_identical_explicit_project_and_base_without_global_rebinding(self):
        observed = []
        def supervise(*arguments, **options):
            observed.append((local_app.current_instance(), arguments))
        arguments = ['_supervise', '--session', self.session, '--base', str(self.project.base),
                     '--mode', 'fixture', '--lock-fd', '21', '--listen-fd', '22']
        with (patch('sys.argv', ['aos-v1', *arguments, *self.project.arguments()]),
              patch('aos.local_app.os.fork', return_value=0),
              patch('aos.local_app.supervise', side_effect=supervise)):
            local_app.main()
        self.assertEqual(observed[0][0], self.project)
        self.assertEqual(observed[0][1], (self.session, 'fixture', 21, 22))
        for changed in (arguments, [str(local_app.BASE) if value == str(self.project.base) else value
                                   for value in arguments] + self.project.arguments()):
            with (patch('sys.argv', ['aos-v1', *changed]), patch('sys.stderr', new_callable=StringIO),
                  patch('aos.local_app.os.fork') as fork, patch('aos.local_app.supervise') as runner,
                  self.assertRaises(SystemExit)):
                local_app.main()
            fork.assert_not_called()
            runner.assert_not_called()
        self.assertEqual((local_app.BASE, local_app.URL, local_app.ORIGIN), self.globals)

    def test_public_owned_start_uses_existing_provision_lock_and_scoped_child_arguments(self):
        listeners = [ReservedSocket(23, self.project.port), ReservedSocket(24, 41827)]
        bundle = {'manifest_sha256': 'a' * 64, 'invocation_sha256': 'b' * 64}
        def provision(directory, port):
            self.assertEqual(port, 41827)
            directory.mkdir(mode=0o700)
            return bundle
        def launch(command, **_options):
            session = command[command.index('--session') + 1]
            local_app.write_state(local_app.LocalAppState(session=session, mode='real', phase='running',
                supervisor=self.identity, backend=self.identity, started_at='synthetic', project=self.project.project,
                url=self.project.url, owned_synthetic_form_invocation=True,
                owned_form_manifest_sha256=bundle['manifest_sha256'],
                owned_form_invocation_sha256=bundle['invocation_sha256']))
            return Mock(wait=Mock(return_value=0))
        with (patch('aos.local_app.socket.socket', side_effect=listeners),
              patch('aos.local_app.subprocess.Popen', side_effect=launch) as launched,
              patch('aos.local_app.require_local_preflight'), patch('aos.local_app.observe_process', return_value='same_process'),
              patch('aos.owned_form_invocation_session.provision_owned_synthetic_form_invocation', side_effect=provision)):
            result = json.loads(self.cli('start', '--owned-synthetic-form-invocation'))
        self.assertEqual(result['phase'], 'running')
        self.assertEqual(listeners[0].address, ('127.0.0.1', self.project.port))
        command = launched.call_args.args[0]
        self.assertEqual(command[command.index('--base') + 1], str(self.project.base))
        self.assertEqual(command[command.index('--project') + 1], self.project.project)
        self.assertEqual(command[command.index('--project-port') + 1], str(self.project.port))
        self.assertIn('--owned-learning-lock-fd', command)
        self.assertEqual(launched.call_args.kwargs['pass_fds'][1:3], (23, 24))
        self.assertTrue((Path(result['directory']) / 'owned-form/.learning.lock').is_file())

    def test_backend_uses_project_port_private_roots_and_no_ordinary_retention_or_autologin(self):
        with local_app.instance_scope(self.project):
            command = local_app.managed_backend_command(self.project.base / self.session, 'fixture', 22)
            self.assertEqual(command[command.index('--port') + 1], str(self.project.port))
            self.assertNotIn('--managed-retention', command)
            self.assertNotIn('--local-ui-auto-login', command)
            for name in ('knowledge', 'web-profiles', 'web-task', 'web-form-plan', 'web-form-value',
                         'web-form-state', 'web-route', 'web-static', 'web-readonly-data'):
                self.assertEqual(command[command.index('--' + name + '-root') + 1], str(self.project.base / name))
            with self.assertRaises(ValueError):
                local_app.managed_backend_command(local_app.BASE / self.session, 'fixture', 22)

    def parameter_source(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-parameter-scope-')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        directory.chmod(0o700)
        bundle = {'directory': directory, 'profiles_root': directory / 'profiles',
                  'manifest': {'port': 41829}, 'manifest_sha256': 'a' * 64,
                  'invocation_sha256': 'b' * 64, 'recipe_sha256': 'c' * 64}
        return bundle

    def parameter_arguments(self, bundle):
        return ['--owned-parameter-project-directory', str(bundle['directory']),
                '--owned-parameter-project-manifest-sha256', bundle['manifest_sha256'],
                '--owned-parameter-project-engine', 'fixture']

    def test_named_parameter_activation_keeps_source_lock_manifest_scope_and_one_use(self):
        from aos.owned_learning_workspace import OwnedLearningWorkspace

        bundle = self.parameter_source()
        listeners = [ReservedSocket(23, self.project.port), ReservedSocket(24, bundle['manifest']['port'])]
        def launch(command, **options):
            session = command[command.index('--session') + 1]
            lock_fd = int(command[command.index('--owned-learning-lock-fd') + 1])
            self.assertIn(lock_fd, options['pass_fds'])
            with OwnedLearningWorkspace.adopt(bundle['directory'], lock_fd):
                with self.assertRaisesRegex(ValueError, 'busy'):
                    OwnedLearningWorkspace.acquire(bundle['directory'])
            local_app.write_state(local_app.LocalAppState(session=session, mode='real', phase='running',
                supervisor=self.identity, backend=self.identity, started_at='synthetic',
                project=self.project.project, url=self.project.url,
                owned_synthetic_parameter_project=True, owned_parameter_project_runtime='fixture_cpu',
                owned_parameter_project_directory=str(bundle['directory']),
                owned_parameter_project_manifest_sha256=bundle['manifest_sha256'],
                owned_form_manifest_sha256=bundle['manifest_sha256'],
                owned_form_invocation_sha256=bundle['invocation_sha256'],
                owned_form_recipe_sha256=bundle['recipe_sha256']))
            return Mock(wait=Mock(return_value=0))
        with (patch('aos.owned_parameter_project.read_owned_parameter_project', return_value=bundle),
              patch('aos.local_app.socket.socket', side_effect=listeners),
              patch('aos.local_app.subprocess.Popen', side_effect=launch) as launched,
              patch('aos.local_app.require_local_preflight') as preflight,
              patch('aos.local_app.observe_process', return_value='same_process')):
            result = json.loads(self.cli('start', *self.parameter_arguments(bundle)))
        preflight.assert_called_once_with('fixture')
        self.assertEqual(result['owned_parameter_project_runtime'], 'fixture_cpu')
        self.assertIs(result['owned_parameter_project_real_model'], False)
        self.assertEqual(listeners[0].address, ('127.0.0.1', self.project.port))
        self.assertEqual(listeners[1].address, ('127.0.0.1', bundle['manifest']['port']))
        command = launched.call_args.args[0]
        self.assertEqual(command[command.index('--project') + 1], self.project.project)
        self.assertEqual(command[command.index('--project-port') + 1], str(self.project.port))
        self.assertEqual(command[command.index('--mode') + 1], 'real')
        self.assertEqual(command[command.index('--owned-parameter-project-engine') + 1], 'fixture')
        self.assertNotIn('--owned-synthetic-form-recipe', command)
        self.assertEqual(launched.call_args.kwargs['pass_fds'][1:3], (23, 24))
        marker = bundle['directory'] / '.aos-parameter-activation.json'
        self.assertEqual(json.loads(marker.read_text()), {'session': result['session'],
            'manifest_sha256': bundle['manifest_sha256']})
        with (local_app.instance_scope(self.project),
              patch('aos.owned_parameter_project.read_owned_parameter_project', return_value=bundle),
              patch('aos.local_app.observe_process', return_value='not_observed'),
              patch('aos.local_app.subprocess.Popen') as second):
            state = local_app.read_state()
            local_app.write_state(state.model_copy(update={'phase': 'stopped'}))
            with self.assertRaisesRegex(ValueError, 'already activated'):
                local_app.start(check=False, owned_parameter_project_directory=bundle['directory'],
                    owned_parameter_project_manifest_sha256=bundle['manifest_sha256'],
                    owned_parameter_project_engine='fixture')
            with self.assertRaises(ValueError):
                local_app.restart(expected_session=state.session)
        second.assert_not_called()

    def test_named_parameter_backend_and_supervisor_keep_cpu_only_exact_source(self):
        bundle = self.parameter_source()
        with (local_app.instance_scope(self.project),
              patch('aos.owned_parameter_project.read_owned_parameter_project', return_value=bundle)):
            command = local_app.managed_backend_command(self.project.base / self.session, 'real', 23,
                owned_parameter_project_directory=bundle['directory'],
                owned_parameter_project_manifest_sha256=bundle['manifest_sha256'],
                owned_form_listener_fd=24, owned_learning_lock_fd=25,
                owned_form_manifest_sha256=bundle['manifest_sha256'], owned_form_recipe_sha256=bundle['recipe_sha256'])
        self.assertEqual(command[command.index('--engine') + 1], 'fixture')
        self.assertEqual(command[command.index('--vision-engine') + 1], 'fixture')
        self.assertEqual(command[command.index('--web-profiles-root') + 1], str(bundle['profiles_root']))
        self.assertEqual(command[command.index('--knowledge-root') + 1], str(self.project.base / 'knowledge'))
        self.assertEqual(command[command.index('--port') + 1], str(self.project.port))
        for option in ('--desktop-navigation-mcp-manifest', '--reuse-decider', '--prewarm-decider',
                       '--prewarm-idle-seconds', '--gpu-idle-seconds', '--managed-retention', '--local-ui-auto-login'):
            self.assertNotIn(option, command)
        observed = []
        def supervise(*arguments, **options):
            observed.append((local_app.current_instance(), arguments, options))
        with (patch('sys.argv', ['aos-v1', '_supervise', *self.project.arguments(),
                '--base', str(self.project.base), '--session', self.session, '--mode', 'real',
                '--lock-fd', '21', '--listen-fd', '23', '--owned-form-listener-fd', '24',
                '--owned-learning-lock-fd', '25', '--owned-form-manifest-sha256', bundle['manifest_sha256'],
                *self.parameter_arguments(bundle)]), patch('aos.local_app.os.fork', return_value=0),
              patch('aos.local_app.supervise', side_effect=supervise)):
            local_app.main()
        self.assertEqual(observed[0][0], self.project)
        self.assertEqual(observed[0][1], (self.session, 'real', 21, 23))
        self.assertEqual(observed[0][2]['owned_parameter_project_directory'], bundle['directory'])
        self.assertEqual(observed[0][2]['owned_parameter_project_manifest_sha256'], bundle['manifest_sha256'])
        self.assertEqual(observed[0][2]['owned_learning_lock_fd'], 25)

    def test_named_parameter_conflicting_flags_and_source_ports_deny_before_launch(self):
        bundle = self.parameter_source()
        arguments = self.parameter_arguments(bundle)
        invalid = [arguments[:-2], arguments[2:], arguments[:2] + arguments[4:],
                   arguments + ['--fixture'], arguments + ['--owned-synthetic-form-invocation'],
                   arguments + ['--owned-synthetic-form-recipe'], arguments + ['--synthetic-learning'],
                   arguments + ['--remote-entry-profile-sha256', 'd' * 64]]
        with (patch('aos.local_app.subprocess.Popen') as launch,
              patch('aos.local_app.socket.socket') as listener,
              patch('aos.owned_parameter_project.read_owned_parameter_project', return_value=bundle) as read,
              patch('sys.stderr', new_callable=StringIO)):
            for flags in invalid:
                with self.subTest(flags=flags), self.assertRaises(SystemExit):
                    self.cli('start', *flags)
            for operation in ('status', 'restart', 'doctor'):
                with self.subTest(operation=operation), self.assertRaises(SystemExit):
                    self.cli(operation, *arguments)
            read.assert_not_called()
            for port in (8765, self.project.port):
                bundle['manifest']['port'] = port
                with self.subTest(port=port), self.assertRaises(SystemExit):
                    self.cli('start', *arguments)
        launch.assert_not_called()
        listener.assert_not_called()
        self.assertFalse((bundle['directory'] / '.aos-parameter-activation.json').exists())

    def test_token_and_stop_use_only_exact_recorded_project_process(self):
        state = self.state(token_name='desktop-console-' + 'b' * 16 + '.token')
        self.persist(state)
        original_read = local_app.private_read
        def read(path, *arguments, **options):
            if path.name == state.token_name: return b'x' * 43
            return original_read(path, *arguments, **options)
        def signal(expected, wait):
            self.assertEqual(expected, self.identity)
            self.assertEqual(wait, 130)
            local_app.write_state(state.model_copy(update={'phase': 'stopped', 'token_name': None}))
            return True
        with (patch('aos.local_app.observe_process', return_value='same_process'),
              patch('aos.local_app.private_read', side_effect=read)):
            self.assertEqual(self.cli('token').strip(), 'x' * 43)
            with patch('aos.local_app.signal_owned', side_effect=signal) as signalled:
                self.assertEqual(json.loads(self.cli('stop', '--expected-session', self.session))['phase'], 'stopped')
            signalled.assert_called_once()

    def test_reuse_preview_pins_same_project_port_and_exact_retained_root(self):
        previous = self.state().model_copy(update={'phase': 'stopped', 'owned_skill_source_session': None})
        with (local_app.instance_scope(self.project),
              patch('aos.owned_skill_reuse.preview_owned_skill_reuse', return_value={'preview_sha256': 'd' * 64}) as preview):
            result = local_app.prepare_owned_skill_reuse(previous, 'a' * 64, 'b' * 64)
            self.assertEqual(result['preview_sha256'], 'd' * 64)
            self.assertEqual(preview.call_args.kwargs['owned_root'], self.project.base / self.session / 'owned-form')
            for changed in (previous.model_copy(update={'project': 'other'}),
                            previous.model_copy(update={'url': 'http://127.0.0.1:31822/ui/'})):
                with self.assertRaises(ValueError): local_app.prepare_owned_skill_reuse(changed, 'a' * 64, 'b' * 64)
            with self.assertRaises(ValueError):
                local_app.prepare_owned_skill_reuse(previous, 'a' * 64, 'b' * 64, base=local_app.BASE)
            self.assertEqual(preview.call_count, 1)

    def test_inherited_supervisor_listener_is_verified_before_state_or_backend(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            instance = local_app.LocalAppInstance.for_project(self.project.project, port)
            with (local_app.instance_scope(instance), patch('aos.local_app.private_directory', side_effect=ValueError('synthetic-next-gate')),
                  patch('aos.local_app.write_state') as write, patch('aos.local_app.subprocess.Popen') as launch):
                with self.assertRaisesRegex(ValueError, 'synthetic-next-gate'):
                    local_app.supervise(self.session, 'fixture', -1, listener.fileno())
            write.assert_not_called()
            launch.assert_not_called()
            wrong = local_app.LocalAppInstance.for_project(self.project.project, 1024 if port != 1024 else 1025)
            with (local_app.instance_scope(wrong), patch('aos.local_app.write_state') as write,
                  patch('aos.local_app.subprocess.Popen') as launch, self.assertRaisesRegex(ValueError, 'Inherited UI listener')):
                local_app.supervise(self.session, 'fixture', -1, listener.fileno())
            write.assert_not_called()
            launch.assert_not_called()

    def test_instance_contexts_do_not_leak_between_concurrent_callers(self):
        other = local_app.LocalAppInstance.for_project('test-' + uuid4().hex, 31822)
        barrier = threading.Barrier(2)
        observed = []
        def inspect(instance):
            with local_app.instance_scope(instance):
                barrier.wait(timeout=2)
                observed.append(local_app.current_instance())
        threads = [threading.Thread(target=inspect, args=(instance,)) for instance in (self.project, other)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(observed, [self.project, other])
        self.assertEqual((local_app.BASE, local_app.URL, local_app.ORIGIN), self.globals)

    def test_named_lifecycle_requires_exact_session_inside_each_callable_and_cli(self):
        self.persist()
        functions = {'stop': local_app.stop, 'restart': local_app.restart,
                     'release-restart': local_app.release_restart_quiesce,
                     'recover-clean-exit': local_app.recover_clean_exit, 'recover-reboot': local_app.recover_reboot}
        with (local_app.instance_scope(self.project), patch('aos.local_app.instance_lock') as lock,
              patch('aos.local_app.signal_owned') as signal, patch('aos.local_app.httpx.Client') as client,
              patch('aos.local_app.subprocess.run') as command, patch('aos.local_app.subprocess.Popen') as launch):
            for name, function in functions.items():
                for expected in (None, 'app-' + 'f' * 32, True):
                    with self.subTest(name=name, expected=expected), self.assertRaises(ValueError):
                        function(expected_session=expected)
                with patch('sys.stderr', new_callable=StringIO), self.assertRaises(SystemExit): self.cli(name)
            for effect in (lock, signal, client, command, launch): effect.assert_not_called()

    def test_named_cli_forwards_expected_session_for_every_lifecycle_operation(self):
        self.persist()
        for command, function in (('restart', 'restart'), ('release-restart', 'release_restart_quiesce'),
                                  ('recover-clean-exit', 'recover_clean_exit'), ('recover-reboot', 'recover_reboot')):
            with self.subTest(command=command), patch('aos.local_app.' + function, return_value={'phase': 'synthetic'}) as called:
                self.assertEqual(json.loads(self.cli(command, '--expected-session', self.session)), {'phase': 'synthetic'})
                called.assert_called_once_with(expected_session=self.session)

    def test_stop_rechecks_full_state_before_signal_and_refuses_replacement_after_wait(self):
        state = self.state()
        self.persist(state)
        replacement = state.model_copy(update={'session': 'app-' + 'f' * 32, 'phase': 'stopped'})
        def observation(_identity):
            local_app.write_state(replacement)
            return 'same_process'
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', side_effect=observation),
              patch('aos.local_app.signal_owned') as signal, self.assertRaisesRegex(ValueError, 'session changed')):
            local_app.stop(expected_session=self.session)

        signal.assert_not_called()
        self.persist(state)
        def signal_and_replace(*_arguments, **_options):
            local_app.write_state(replacement)
            return True
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', return_value='same_process'),
              patch('aos.local_app.signal_owned', side_effect=signal_and_replace),
              self.assertRaisesRegex(ValueError, 'session changed')):
            local_app.stop(expected_session=self.session)

    def test_stopped_noop_rejects_replacement_or_unverified_result_before_return(self):
        state = self.state().model_copy(update={'phase': 'stopped'})
        replacement = state.model_copy(update={'session': 'app-' + 'f' * 32})
        original_status = local_app.current_status
        for scenario in ('unchanged', 'replacement', 'wrong_phase'):
            self.persist(state)
            def status():
                if scenario == 'replacement': local_app.write_state(replacement)
                result = original_status()
                return result | {'phase': 'running'} if scenario == 'wrong_phase' else result
            with (self.subTest(scenario=scenario), local_app.instance_scope(self.project),
                  patch('aos.local_app.observe_process', return_value='not_observed'),
                  patch('aos.local_app.current_status', side_effect=status),
                  patch('aos.local_app.signal_owned') as signal):
                if scenario == 'unchanged':
                    self.assertEqual(local_app.stop(expected_session=self.session)['session'], self.session)
                else:
                    with self.assertRaisesRegex(ValueError, 'session changed'):
                        local_app.stop(expected_session=self.session)
            signal.assert_not_called()

    def test_restart_retains_exact_predecessor_through_verified_shutdown_and_start_lock(self):
        state = self.state()
        self.persist(state)
        stopped = state.model_copy(update={'phase': 'stopped', 'token_name': None})
        def stop(**options):
            self.assertEqual(options, {'expected_session': self.session, 'expected_state': state})
            local_app.write_state(stopped)
            return {'phase': 'stopped', 'session': self.session}
        with (local_app.instance_scope(self.project), patch('aos.local_app.require_local_preflight') as preflight,
              patch('aos.local_app.require_idle_restart') as idle, patch('aos.local_app.stop', side_effect=stop),
              patch('aos.local_app.start', return_value={'phase': 'running'}) as start):
            self.assertEqual(local_app.restart(expected_session=self.session), {'phase': 'running'})
        preflight.assert_called_once_with('fixture')
        idle.assert_called_once_with(state)
        start.assert_called_once_with('fixture', expected_previous_session=self.session, expected_previous_state=stopped)
        with (local_app.instance_scope(self.project), patch('aos.local_app.socket.socket') as socket_factory,
              patch('aos.local_app.subprocess.Popen') as launch, self.assertRaisesRegex(ValueError, 'session changed')):
            local_app.start('fixture', check=False, expected_previous_session='app-' + 'f' * 32)
        socket_factory.assert_not_called()
        launch.assert_not_called()
        with (local_app.instance_scope(self.project), patch('aos.local_app.instance_lock', side_effect=BlockingIOError),
              patch('aos.local_app.current_status') as status, self.assertRaisesRegex(ValueError, 'no replacement adopted')):
            local_app.start('fixture', check=False, expected_previous_session=self.session, expected_previous_state=stopped)
        status.assert_not_called()

    def test_restart_rejects_preflight_drift_failed_cleanup_and_implicit_recovery(self):
        state = self.state()
        replacement = state.model_copy(update={'session': 'app-' + 'f' * 32})
        for scenario in ('preflight_drift', 'cleanup_failure', 'interrupted'):
            self.persist(state.model_copy(update={'phase': 'failed'}) if scenario == 'interrupted' else state)
            def preflight(_mode):
                if scenario == 'preflight_drift': local_app.write_state(replacement)
            with (self.subTest(scenario=scenario), local_app.instance_scope(self.project),
                  patch('aos.local_app.require_local_preflight', side_effect=preflight),
                  patch('aos.local_app.require_idle_restart'),
                  patch('aos.local_app.stop', return_value={'phase': 'needs_inspection'}) as stop,
                  patch('aos.local_app.recover_reboot') as recover, patch('aos.local_app.start') as start,
                  self.assertRaises(ValueError)):
                local_app.restart(expected_session=self.session)
            start.assert_not_called()
            recover.assert_not_called()
            if scenario != 'cleanup_failure': stop.assert_not_called()

    def test_named_quiesce_and_release_require_current_http_owner_scope_and_unchanged_manager(self):
        state = self.state()
        desktop = 'desktop-session-' + 'c' * 32
        tasks = {'busy': False, 'reserved': False, 'approval': None, 'auto_approval': None, 'jobs': [],
                 'restart_quiesced': True, 'manager_session': self.session,
                 'manager_scope': {'project': self.project.project, 'port': self.project.port}}
        control = {'control': {'session_id': desktop, 'owner': 'AGENT', 'status': 'running'}}
        def response(value):
            return Mock(raise_for_status=Mock(return_value=None), json=Mock(return_value=value))
        for release in (False, True):
            for scenario in ('valid', 'foreign_http', 'human_owner', 'before_effect_drift', 'after_effect_drift'):
                self.persist(state)
                observed_tasks = tasks | ({'manager_session': 'app-' + 'f' * 32} if scenario == 'foreign_http' else {})
                observed_control = {'control': control['control'] | ({'owner': 'HUMAN'} if scenario == 'human_owner' else {})}
                posts = []
                def get(path):
                    if path == '/api/tasks': return response(observed_tasks)
                    if scenario == 'before_effect_drift':
                        local_app.write_state(state.model_copy(update={'session': 'app-' + 'f' * 32}))
                    return response(observed_control)
                def post(path, **_options):
                    posts.append(path)
                    if path == '/api/login': return response({})
                    if scenario == 'after_effect_drift':
                        local_app.write_state(state.model_copy(update={'session': 'app-' + 'f' * 32}))
                    return response({'quiesced': not release, 'session_id': desktop})
                with (self.subTest(release=release, scenario=scenario), local_app.instance_scope(self.project),
                      patch('aos.local_app.token_value', return_value='synthetic-token'),
                      patch('aos.local_app.observe_process', return_value='same_process'),
                      patch('aos.local_app.httpx.Client') as client):
                    requester = client.return_value.__enter__.return_value
                    requester.get.side_effect = get
                    requester.post.side_effect = post
                    function = (lambda: local_app.release_restart_quiesce(expected_session=self.session)) if release else (
                        lambda: local_app.require_idle_restart(state))
                    if scenario == 'valid': function()
                    else:
                        with self.assertRaises(ValueError): function()
                self.assertEqual(posts, ['/api/login', '/api/restart/' + ('release' if release else 'quiesce')]
                                 if scenario in ('valid', 'after_effect_drift') else ['/api/login'])

    def recovery_fixture(self, *, reboot):
        state = self.state().model_copy(update={'phase': 'failed'})
        if reboot:
            old = self.identity.model_copy(update={'boot_id': '00000000-0000-0000-0000-000000000000'})
            state = state.model_copy(update={'supervisor': old, 'backend': old})
        self.persist(state)
        directory = self.project.base / self.session
        directory.mkdir(mode=0o700)
        workspace = directory / 'workspace'
        workspace.mkdir(mode=0o700)
        descriptor = open_existing_workspace(workspace)
        try: identity = workspace_identity(workspace, descriptor)
        finally: os.close(descriptor)
        birth = LifecycleBirth(runtime_id='desktop-' + uuid4().hex, container_name='aos-desktop-' + uuid4().hex[:20],
            image_id='sha256:' + 'd' * 64, source_sha256='e' * 64, workspace=identity, process=state.backend)
        container = 'c' * 64
        journals = directory / '.aos-lifecycle'
        journals.mkdir(mode=0o700)
        events = []
        for ordinal, stage in enumerate(('intent', 'created', 'started', 'removed')):
            events.append(LifecycleEvent(birth=birth, sequence=ordinal, stage=stage, recorded_at='synthetic',
                previous_sha256=digest(events[-1].model_dump()) if events else None,
                container_id=container if ordinal else None))
        journal = journals / (birth.runtime_id + '.jsonl')
        journal.write_text(''.join(canonical(event.model_dump()) + '\n' for event in events))
        journal.chmod(0o600)
        token_name = 'desktop-console-' + uuid4().hex[:16] + '.token'
        log = directory / 'backend.log'
        log.write_text('Local token file (0600): ' + str(REPO_ROOT / 'runs' / token_name) + '\n')
        log.chmod(0o600)
        return state, birth, container

    def test_named_clean_exit_recovery_retains_physical_proofs_and_selected_port(self):
        state, _birth, container = self.recovery_fixture(reboot=False)
        absent = subprocess.CompletedProcess([], 1, b'', ('Error response from daemon: No such container: ' + container).encode())
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', return_value='not_observed'),
              patch('aos.local_app.subprocess.run', return_value=absent) as docker,
              patch('aos.local_app.socket.socket') as listener, patch('aos.local_app.start') as start):
            self.assertEqual(local_app.recover_clean_exit(expected_session=self.session)['phase'], 'stopped')
            self.assertEqual(local_app.read_state().backend, state.backend)
        start.assert_not_called()
        self.assertEqual(docker.call_args.args[0][:3], ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock'])
        listener.return_value.__enter__.return_value.bind.assert_called_once_with(('127.0.0.1', self.project.port))
        record = json.loads((self.project.base / ('.clean-exit-' + self.session + '.json')).read_text())
        self.assertEqual(record['previous_state'], state.model_dump())

    def test_named_reboot_recovery_refuses_running_container_then_records_stopped_predecessor(self):
        state, birth, container = self.recovery_fixture(reboot=True)
        def result(running):
            return subprocess.CompletedProcess([], 0, json.dumps([container, '/' + birth.container_name,
                'running' if running else 'exited', running, 1 if running else 0, birth.image_id, birth.runtime_id]).encode(), b'')
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', return_value='different_boot'),
              patch('aos.local_app.subprocess.run', return_value=result(True)),
              self.assertRaisesRegex(ValueError, 'not safely stopped')):
            local_app.recover_reboot(expected_session=self.session)
        self.assertFalse((self.project.base / ('.recovery-' + self.session + '.json')).exists())
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', return_value='different_boot'),
              patch('aos.local_app.subprocess.run', return_value=result(False)), patch('aos.local_app.socket.socket') as listener,
              patch('aos.local_app.start') as start):
            self.assertEqual(local_app.recover_reboot(expected_session=self.session)['phase'], 'stopped')
        start.assert_not_called()
        listener.return_value.__enter__.return_value.bind.assert_called_once_with(('127.0.0.1', self.project.port))
        record = json.loads((self.project.base / ('.recovery-' + self.session + '.json')).read_text())
        self.assertEqual(record['previous_state'], state.model_dump())

    def ui_builder(self, arguments, **options):
        self.assertEqual(arguments[:2], ['/usr/bin/node', str(REPO_ROOT / 'ui/node_modules/vite/bin/vite.js')])
        self.assertNotIn('--emptyOutDir', arguments)
        self.assertEqual(options['timeout'], 30)
        self.assertEqual(options['max_output'], 262144)
        self.assertEqual(options['env']['npm_config_offline'], 'true')
        self.assertEqual(options['env']['COREPACK_ENABLE_NETWORK'], '0')
        self.assertEqual(options['env']['CUDA_VISIBLE_DEVICES'], '')
        self.assertNotIn('NODE_OPTIONS', options['env'])
        target = Path(arguments[arguments.index('--outDir') + 1])
        self.assertEqual(target.parent, self.project.base)
        self.assertTrue(target.name.startswith('.prepare-ui-'))
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        assets = target / 'assets'
        assets.mkdir(mode=0o700)
        (assets / 'synthetic.js').write_text('export const synthetic = true;')
        (assets / 'synthetic.css').write_text('body {}')
        (target / 'index.html').write_text('<script src="/ui/assets/synthetic.js"></script>'
                                         '<link rel="stylesheet" href="/ui/assets/synthetic.css">')
        return subprocess.CompletedProcess(arguments, 0, b'synthetic build output', b'')

    def test_prepare_ui_public_command_publishes_fresh_private_assets_without_starting(self):
        with (patch('aos.local_app.run_bounded', side_effect=self.ui_builder) as builder,
              patch('aos.local_app.start') as start, patch('aos.local_app.signal_owned') as signal,
              patch('aos.local_app.httpx.Client') as client):
            result = json.loads(self.cli('prepare-ui'))
        self.assertEqual(result, {'status': 'prepared', 'manager_scope': {'project': self.project.project, 'port': self.project.port},
            'ui_root': str(self.project.base / 'ui'), 'source_sha256': digest(local_app._frontend_source_snapshot()),
            'runtime_started': False, 'execution_authorized': False})
        self.assertEqual(self.project.base.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.project.base / 'ui/index.html').is_file())
        self.assertFalse((self.project.base / 'current.json').exists())
        logs = list(self.project.base.glob('.prepare-ui-*.log'))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
        self.assertIn(b'synthetic build output', logs[0].read_bytes())
        builder.assert_called_once()
        for effect in (start, signal, client): effect.assert_not_called()

    def test_prepare_ui_default_scope_and_runtime_flags_are_rejected(self):
        with (patch('sys.argv', ['aos-v1', 'prepare-ui']), patch('sys.stderr', new_callable=StringIO),
              patch('aos.local_app.prepare_base') as prepare, patch('aos.local_app.run_bounded') as builder,
              self.assertRaises(SystemExit)):
            local_app.main()
        prepare.assert_not_called()
        builder.assert_not_called()
        with (patch('sys.stderr', new_callable=StringIO), patch('aos.local_app.run_bounded') as builder,
              self.assertRaises(SystemExit)):
            self.cli('prepare-ui', '--fixture')
        builder.assert_not_called()

    def test_prepare_ui_rejects_existing_directory_file_or_symlink_without_overwrite(self):
        with local_app.instance_scope(self.project): local_app.prepare_base()
        target = self.project.base / 'ui'
        for kind in ('directory', 'file', 'symlink'):
            if kind == 'directory': target.mkdir(mode=0o700)
            elif kind == 'file': target.write_text('synthetic unchanged')
            else: target.symlink_to(REPO_ROOT / 'ui/dist')
            with (self.subTest(kind=kind), local_app.instance_scope(self.project),
                  patch('aos.local_app.run_bounded') as builder, self.assertRaisesRegex(ValueError, 'never overwrites')):
                local_app.prepare_ui()
            builder.assert_not_called()
            if kind == 'directory': target.rmdir()
            else: target.unlink()

    def test_prepare_ui_requires_clean_stopped_processes_and_preserves_original_state(self):
        state, _birth, _container = self.recovery_fixture(reboot=False)
        stopped = state.model_copy(update={'phase': 'stopped'})
        self.persist(stopped)
        before = (self.project.base / 'current.json').read_bytes()
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', return_value='not_observed'),
              patch('aos.local_app.clean_shutdown', return_value=False), patch('aos.local_app.run_bounded') as builder,
              self.assertRaisesRegex(ValueError, 'cleanup is not proven')):
            local_app.prepare_ui()
        builder.assert_not_called()
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', return_value='same_process'),
              patch('aos.local_app.run_bounded') as builder, self.assertRaisesRegex(ValueError, 'both processes absent')):
            local_app.prepare_ui()
        builder.assert_not_called()
        with (local_app.instance_scope(self.project), patch('aos.local_app.observe_process', return_value='not_observed'),
              patch('aos.local_app.run_bounded', side_effect=self.ui_builder)):
            self.assertEqual(local_app.prepare_ui()['status'], 'prepared')
        self.assertEqual((self.project.base / 'current.json').read_bytes(), before)

    def test_prepare_ui_source_drift_leaves_unpublished_private_staging(self):
        with (local_app.instance_scope(self.project), patch('aos.local_app.run_bounded', side_effect=self.ui_builder),
              patch('aos.local_app._frontend_source_snapshot', side_effect=[{'synthetic': 'a' * 64}, {'synthetic': 'b' * 64}]),
              self.assertRaisesRegex(ValueError, 'sources changed')):
            local_app.prepare_ui()
        self.assertFalse((self.project.base / 'ui').exists())
        stages = [path for path in self.project.base.glob('.prepare-ui-*') if path.is_dir()]
        self.assertEqual(len(stages), 1)
        self.assertTrue((stages[0] / 'index.html').is_file())

    def test_prepare_ui_builder_failure_timeout_and_state_drift_never_publish(self):
        for scenario in ('failed', 'timeout', 'state_drift'):
            def builder(arguments, **options):
                if scenario == 'timeout': raise subprocess.TimeoutExpired(arguments, 30)
                if scenario == 'failed': return subprocess.CompletedProcess(arguments, 1, b'', b'synthetic builder failure')
                result = self.ui_builder(arguments, **options)
                local_app.write_state(self.state())
                return result
            with (self.subTest(scenario=scenario), local_app.instance_scope(self.project),
                  patch('aos.local_app.run_bounded', side_effect=builder),
                  self.assertRaises((ValueError, subprocess.TimeoutExpired))):
                local_app.prepare_ui()
            self.assertFalse((self.project.base / 'ui').exists())
        self.assertEqual(len([path for path in self.project.base.glob('.prepare-ui-*') if path.is_dir()]), 3)

    def test_prepare_ui_publication_race_cannot_replace_an_existing_empty_directory(self):
        def builder(arguments, **options):
            result = self.ui_builder(arguments, **options)
            (self.project.base / 'ui').mkdir(mode=0o700)
            return result
        with (local_app.instance_scope(self.project), patch('aos.local_app.run_bounded', side_effect=builder),
              self.assertRaises(FileExistsError)):
            local_app.prepare_ui()
        self.assertEqual(list((self.project.base / 'ui').iterdir()), [])
        self.assertTrue(any(path.is_dir() for path in self.project.base.glob('.prepare-ui-*')))

    def test_frontend_source_snapshot_rejects_links_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'src').mkdir()
            for name in ('package.json', 'pnpm-lock.yaml', 'index.html', 'src/main.tsx'):
                (root / name).write_text('synthetic')
            self.assertEqual(set(local_app._frontend_source_snapshot(root)),
                             {'package.json', 'pnpm-lock.yaml', 'index.html', 'src/main.tsx'})
            (root / 'src/link').symlink_to(root / 'index.html')
            with self.assertRaisesRegex(ValueError, 'links'): local_app._frontend_source_snapshot(root)
            (root / 'src/link').unlink()
            with (root / 'src/oversized').open('wb') as stream: stream.truncate(2097153)
            with self.assertRaisesRegex(ValueError, 'bound'): local_app._frontend_source_snapshot(root)


if __name__ == '__main__':
    unittest.main()

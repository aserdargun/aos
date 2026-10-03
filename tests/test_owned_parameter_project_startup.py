import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from aos.contracts import REPO_ROOT, digest
from aos.owned_learning_workspace import OwnedLearningWorkspace
from aos.owned_parameter_project import (
    APPLICATIONS, MODE, owned_parameter_project_review_sha256,
    provision_owned_parameter_project,
)
from aos.owned_parameter_project_startup import (
    load_owned_parameter_project_startup, verify_owned_parameter_project_listener,
)
from aos.site_skill_form_recipe import SiteSkillFormRecipeInvocation


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic certificate authoring')
class OwnedParameterProjectStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aos-parameter-startup-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]

    def provision(self, name='project', application=APPLICATIONS[0], parameters=None):
        parameters = parameters or {'record-id': 'Ada', 'note-text': 'Call tomorrow'}
        return provision_owned_parameter_project(
            self.root / name, self.port, application_key=application,
            parameters=parameters,
            confirm_parameters_sha256=owned_parameter_project_review_sha256(
                application, parameters), human_confirmation=True)

    def startup(self, bundle):
        startup = load_owned_parameter_project_startup(
            bundle['directory'], bundle['manifest_sha256'], self.listener.fileno())
        self.addCleanup(startup.close)
        return startup

    def script(self):
        spec = importlib.util.spec_from_file_location(
            'aos_parameter_startup_serve_desktop', REPO_ROOT / 'scripts/serve_desktop.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def arguments(self, bundle):
        return ['serve_desktop.py', '--engine', 'fixture', '--browser-tasks',
                '--desktop-browser', '--owned-parameter-project-directory',
                str(bundle['directory']), '--owned-parameter-project-manifest-sha256',
                bundle['manifest_sha256'], '--owned-form-listener-fd',
                str(self.listener.fileno()), '--workspace', str(self.root / 'workspace'),
                '--database', str(self.root / 'store.sqlite')]

    def test_two_app_sources_preserve_authored_identity_and_typed_recipe(self):
        for index, application in enumerate(APPLICATIONS):
            with self.subTest(application=application):
                bundle = self.provision(str(index), application)
                startup = self.startup(bundle)
                self.assertEqual(startup.source()['manifest'], bundle['manifest'])
                invocation = startup.revalidate()
                self.assertIsInstance(invocation, SiteSkillFormRecipeInvocation)
                self.assertEqual(digest(invocation.model_dump(mode='json')), bundle['invocation_sha256'])
                self.assertEqual(startup.bundle['mode'], MODE)
                self.assertEqual(len(startup.bundle['fields']), 2)
                self.assertEqual(startup.bundle['body'], bundle['bodies']['form_body'])
                self.assertFalse(any(value for key, value in bundle['manifest'].items()
                                     if key.endswith('_authorized') or key.endswith('_verified')
                                     or key in {'execution_performed', 'skill_reviewed', 'training_ready'}))
                self.assertNotIn('released_source', bundle)
                self.assertNotIn('candidate_sha256', bundle)

    def test_exact_retained_listener_required_without_descriptor_consumption(self):
        bundle = self.provision()
        descriptor = self.listener.fileno()
        self.startup(bundle)
        self.assertEqual(self.listener.getsockname(), ('127.0.0.1', self.port))
        self.assertEqual(self.listener.fileno(), descriptor)
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.bind(('127.0.0.1', 0))
        other.listen(1)
        with self.assertRaisesRegex(ValueError, 'listener_mismatch'):
            load_owned_parameter_project_startup(
                bundle['directory'], bundle['manifest_sha256'], other.fileno())
        for descriptor in (None, -1, True):
            with self.subTest(descriptor=descriptor), self.assertRaises(ValueError):
                verify_owned_parameter_project_listener(descriptor, bundle['manifest'])
        for change in ({'origin': bundle['manifest']['origin'] + '/'},
                       {'port': self.port + 1}, {'origin': 'https://other.invalid:19443'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_owned_parameter_project_listener(
                    self.listener.fileno(), bundle['manifest'] | change)
        unlistened = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(unlistened.close)
        unlistened.bind(('127.0.0.1', 0))
        manifest = bundle['manifest'] | {
            'port': unlistened.getsockname()[1],
            'origin': f'https://w3-owned-form.aos.invalid:{unlistened.getsockname()[1]}'}
        with self.assertRaisesRegex(ValueError, 'listener_mismatch'):
            verify_owned_parameter_project_listener(unlistened.fileno(), manifest)

    def test_changed_source_identity_pin_and_default_missing_pins_deny(self):
        bundle = self.provision()
        for directory, pin in ((None, bundle['manifest_sha256']),
                               (bundle['directory'], None),
                               (bundle['directory'], '0' * 64),
                               (bundle['directory'] / '..' / 'project', bundle['manifest_sha256'])):
            with self.subTest(directory=directory, pin=pin), self.assertRaises(ValueError):
                load_owned_parameter_project_startup(directory, pin, self.listener.fileno())
        startup = self.startup(bundle)
        path = bundle['directory'] / 'remote-form-skill-recipe.json'
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        os.chmod(replacement, 0o600)
        replacement.replace(path)
        with self.assertRaisesRegex(ValueError, 'source_identity_changed'):
            startup.revalidate()
        second = self.provision('second')
        startup = self.startup(second)
        (second['directory'] / 'owned-record-config.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'source_pin_changed'):
            startup.source()

    def test_replaced_listener_at_same_port_does_not_change_retained_identity(self):
        bundle = self.provision()
        startup = self.startup(bundle)
        self.listener.close()
        replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)
        replacement.bind(('127.0.0.1', self.port))
        replacement.listen(1)
        with OwnedLearningWorkspace.acquire(bundle['directory'], create=True) as lock:
            startup.retain_workspace(lock)
            with patch('aos.owned_parameter_project_startup.OwnedFormFixture') as fixture, \
                    self.assertRaisesRegex(ValueError, 'listener_identity_changed'):
                startup.create_fixture(replacement.fileno())
            fixture.assert_not_called()

    def test_workspace_lock_retained_and_record_fixture_uses_actual_fields(self):
        bundle = self.provision(application=APPLICATIONS[1])
        startup = self.startup(bundle)
        with self.assertRaisesRegex(ValueError, 'workspace_required'):
            startup.create_fixture(self.listener.fileno())
        with OwnedLearningWorkspace.acquire(bundle['directory'], create=True) as lock:
            startup.retain_workspace(lock)
            with self.assertRaisesRegex(ValueError, 'busy'):
                OwnedLearningWorkspace.acquire(bundle['directory'])
            with patch('aos.owned_parameter_project_startup.OwnedFormFixture') as fixture:
                startup.create_fixture(self.listener.fileno())
                arguments = fixture.call_args.kwargs
                self.assertEqual(arguments['record_config'], bundle['record_config'])
                self.assertEqual(arguments['expected_body'], bundle['body'])
                self.assertEqual(arguments['profile_sha256'], bundle['profile_sha256'])
                self.assertEqual(arguments['certificate_file'], bundle['certificate_file'])
                self.assertEqual(set(item['name'] for item in arguments['record_config']['fields']),
                                 {'item_code', 'note'})
            with patch('aos.owned_parameter_project_startup.audit_site_skill_form_recipe_execution',
                       return_value={'synthetic_test_audit': True}) as audit:
                startup.audit(self.root / 'store.sqlite', 'synthetic-run')
                self.assertEqual(audit.call_args.args[-2:],
                                 (self.root / 'store.sqlite', 'synthetic-run'))
                self.assertIsInstance(audit.call_args.args[-3], SiteSkillFormRecipeInvocation)
        with self.assertRaisesRegex(ValueError, 'workspace_closed'):
            startup.revalidate()

    def test_missing_trajectory_cannot_be_reported_as_actual_recipe_audit(self):
        bundle = self.provision()
        startup = self.startup(bundle)
        with self.assertRaises((OSError, ValueError)):
            startup.audit(self.root / 'absent.sqlite', 'synthetic-absent-run')
        self.assertFalse((self.root / 'absent.sqlite').exists())

    def test_cli_conflicting_sources_deny_before_runtime_or_token_creation(self):
        bundle = self.provision()
        module = self.script()
        conflicts = [
            ['--owned-form-manifest-sha256', bundle['manifest_sha256']],
            ['--owned-synthetic-form-recipe'], ['--remote-form-field-name', 'note'],
            ['--remote-entry-profile-sha256', bundle['profile_sha256']],
            ['--remote-form-public-plan-sha256', bundle['form_plan_sha256']],
            ['--remote-form-cookie-file', str(self.root / 'cookie')],
            ['--owned-skill-reuse-sha256', '1' * 64], ['--engine', 'scientist'],
            ['--engine', 'decider'], ['--vision-engine', 'bonsai'],
            ['--reuse-decider'], ['--prewarm-decider'],
            ['--prewarm-idle-seconds', '30'], ['--gpu-idle-seconds', '10'],
        ]
        for conflict in conflicts:
            with self.subTest(conflict=conflict), patch.object(sys, 'argv', self.arguments(bundle) + conflict), \
                    patch.object(module, 'DesktopRuntime') as runtime, \
                    patch.object(module, 'DeciderEngine') as native, \
                    patch.object(module, 'ReusableDeciderEngine') as reusable, \
                    patch.object(module, 'BonsaiVisionSupervisor') as bonsai, \
                    patch.object(module.secrets, 'token_urlsafe') as token, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                module.main()
            self.assertEqual(error.exception.code, 2)
            runtime.assert_not_called()
            native.assert_not_called()
            reusable.assert_not_called()
            bonsai.assert_not_called()
            token.assert_not_called()

    def test_cli_incomplete_project_pins_deny_and_default_does_not_load_project(self):
        bundle = self.provision()
        module = self.script()
        arguments = self.arguments(bundle)
        for missing in ('--owned-parameter-project-directory',
                        '--owned-parameter-project-manifest-sha256', '--owned-form-listener-fd'):
            index = arguments.index(missing)
            incomplete = arguments[:index] + arguments[index + 2:]
            with self.subTest(missing=missing), patch.object(sys, 'argv', incomplete), \
                    patch.object(module, 'DesktopRuntime') as runtime, \
                    patch.object(module.secrets, 'token_urlsafe') as token, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                module.main()
            runtime.assert_not_called()
            token.assert_not_called()
        with patch.object(sys, 'argv', ['serve_desktop.py']), \
                patch('aos.owned_parameter_project_startup.load_owned_parameter_project_startup') as load, \
                patch.object(module.secrets, 'token_urlsafe', side_effect=RuntimeError('synthetic-before-token')), \
                self.assertRaisesRegex(RuntimeError, 'synthetic-before-token'):
            module.main()
        load.assert_not_called()

    def test_actual_startup_composes_project_with_mocked_runtime_only(self):
        bundle = self.provision()
        module = self.script()
        runtime = MagicMock()
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.container_id = 'synthetic-not-started'
        controller = MagicMock()
        controller.state.return_value = {'status': 'stopped'}
        scheduler = MagicMock()
        captured = {}

        def configure(project, journal_directory, *, current_source, source_auditor):
            captured['project'] = project
            captured['journal_directory'] = journal_directory
            captured['current'] = current_source()
            captured['audit'] = source_auditor

        scheduler.configure_owned_parameter_project_execution.side_effect = configure
        app = SimpleNamespace(router=SimpleNamespace())
        descriptor = os.dup(self.listener.fileno())
        self.addCleanup(lambda: self.close_if_open(descriptor))
        arguments = self.arguments(bundle)
        arguments[arguments.index('--owned-form-listener-fd') + 1] = str(descriptor)
        with patch.object(sys, 'argv', arguments), patch.object(module, 'REPO_ROOT', self.root), \
                patch.object(module, 'DesktopRuntime', return_value=runtime), \
                patch.object(module, 'DesktopController', return_value=controller), \
                patch.object(module, 'TrajectoryStore'), \
                patch.object(module, 'DesktopScheduler', return_value=scheduler) as factory, \
                patch.object(module, 'create_console', return_value=app) as console, \
                patch.object(module.uvicorn, 'run') as serve, \
                patch('aos.owned_parameter_project_startup.OwnedFormFixture') as fixture, \
                contextlib.redirect_stdout(io.StringIO()):
            module.main()
        options = factory.call_args.kwargs
        self.assertEqual(options['remote_form_fields'],
                         [{'name': name, 'value': value} for name, value in bundle['fields']])
        self.assertIsNone(options['remote_form_field_name'])
        self.assertIsNone(options['remote_form_value'])
        self.assertEqual(options['remote_form_plan'], bundle['form_plan'])
        self.assertEqual(options['remote_form_state_plan'], bundle['state_plan'])
        self.assertEqual(options['remote_form_skill_invocation'], bundle['invocation'])
        self.assertEqual(options['remote_form_owned_manifest']['mode'], MODE)
        self.assertIsNone(options['remote_form_owned_candidate_session'])
        self.assertEqual(fixture.call_args.kwargs['record_config'], bundle['record_config'])
        self.assertEqual(captured['current']['manifest_sha256'], bundle['manifest_sha256'])
        self.assertEqual(captured['journal_directory'], self.root / 'owned-parameter-project-execution')
        self.assertTrue(console.call_args.kwargs['owned_form_recipe_mode'])
        serve.assert_called_once()
        fixture.return_value.close.assert_called_once()
        self.assertFalse(list((self.root / 'runs').glob('*.token')))

    @staticmethod
    def close_if_open(descriptor):
        try:
            os.close(descriptor)
        except OSError:
            pass

import asyncio
from copy import deepcopy
import importlib.util
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import jsonschema
import httpx

from aos.contracts import REPO_ROOT, digest
from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_profile_output import AdmissionProfileOutputValidator
from aos.storage import TrajectoryStore
from aos.scientist_transport import ScientistAdmissionError
from aos.scientist_bootstrap import CONTROL_DESCRIPTOR_SHA256, ScientistBootstrapCapture, ScientistBootstrapCodec
from aos.scientist_bootstrap_factory import ScientistBootstrapAdmissionFactory
from aos.scientist_lab_service import ScientistLabStartup, prepare_scientist_lab_startup
from aos.vision import BonsaiVisionSupervisor
from test_desktop_tasks import FixtureDesktop
from test_scientist_lab import SyntheticLabServer
from test_scientist_profile_output import OUTPUT_PIN
import test_scientist_bootstrap as bootstrap_cases


specification = importlib.util.spec_from_file_location('synthetic_serve_desktop', REPO_ROOT / 'scripts/serve_desktop.py')
SERVE = importlib.util.module_from_spec(specification)
specification.loader.exec_module(SERVE)


class ScientistStartupTests(unittest.TestCase):
    def test_retained_resolution_factory_is_rejected_before_desktop_start(self):
        factory = Mock()
        factory.verify_configuration.side_effect = ScientistAdmissionError('synthetic-private-configuration')
        for candidate in (object(), factory):
            with self.subTest(factory=type(candidate).__name__), \
                    patch('sys.argv', self.arguments()), patch('sys.stderr', StringIO()) as stderr, \
                    patch.object(SERVE, 'DesktopRuntime') as desktop:
                with self.assertRaises(SystemExit):
                    SERVE.main(scientist_retained_resolver_factory=candidate,
                               scientist_admission_factory=Mock(), scientist_output_contract={})
                desktop.assert_not_called()
                self.assertNotIn('synthetic-private-configuration', stderr.getvalue())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'models').mkdir(mode=0o700)
        self.pins = {'synthetic_cpu_fixture': True, 'model_files': {'model.safetensors': 'a' * 64},
                     'checkpoint_revision': 'synthetic', 'tokenizer_revision': 'synthetic'}
        self.manifest = self.root / 'models/decider-manifest.json'
        self.manifest.write_text(json.dumps(self.pins))

    def arguments(self, *extra):
        return ['serve_desktop', '--engine', 'scientist', '--scientist-broker-socket', str(self.root / 'broker.sock'),
                '--decider-manifest', str(self.manifest), '--workspace', str(self.root / 'workspace'),
                '--database', str(self.root / 'synthetic.sqlite3'), *extra]

    def denied(self, arguments, provider=None, lab_config=None, lab_capability=None, **startup_options):
        output = StringIO()
        with patch('sys.argv', arguments), patch('sys.stderr', output), patch.object(SERVE, 'REPO_ROOT', self.root), \
                patch.object(SERVE, 'DesktopRuntime') as runtime, patch.object(SERVE, 'DeciderEngine') as native, \
                patch.object(SERVE, 'ReusableDeciderEngine') as reusable, patch.object(SERVE.uvicorn, 'run') as run:
            with self.assertRaises(SystemExit) as stopped:
                SERVE.main(scientist_confirm_runtime=provider, scientist_lab_config=lab_config,
                           scientist_verify_lab_capability=lab_capability, **startup_options)
            self.assertEqual(stopped.exception.code, 2)
            runtime.assert_not_called()
            native.assert_not_called()
            reusable.assert_not_called()
            run.assert_not_called()
        self.assertFalse((self.root / 'synthetic.sqlite3').exists())
        self.assertFalse((self.root / 'runs').exists())
        return output.getvalue()

    def test_standalone_scientist_mode_fails_before_runtime_or_token_without_joint_provider(self):
        error = self.denied(self.arguments())
        self.assertIn('not jointly admitted', error)
        self.assertIn('no native fallback', error)

    def test_socket_and_incompatible_native_paths_are_rejected_before_startup(self):
        for extra in [['--reuse-decider'], ['--prewarm-decider'], ['--gpu-idle-seconds', '10'],
                      ['--vision-engine', 'fixture'], ['--owned-synthetic-form-recipe']]:
            with self.subTest(extra=extra):
                self.denied(self.arguments(*extra), lambda profiles: None)
        self.denied(['serve_desktop', '--engine', 'scientist'], lambda profiles: None)
        self.denied(['serve_desktop', '--scientist-broker-socket', str(self.root / 'broker.sock')])

    def test_boolean_capability_and_revoked_provider_cannot_enable_runtime(self):
        self.denied(self.arguments(), lambda profiles: True)
        def revoke(profiles):
            raise ScientistAdmissionError('Synthetic unsupported version')
        self.denied(self.arguments(), revoke)

    def test_duplicate_or_nonobject_manifest_is_rejected_before_startup(self):
        for raw in ['{"model_files":{},"model_files":{}}', 'true', '{"value":1e999}']:
            with self.subTest(raw=raw):
                self.manifest.write_text(raw)
                self.denied(self.arguments(), lambda profiles: None)

    def test_help_lists_explicit_broker_mode_without_starting_anything(self):
        output = StringIO()
        with patch('sys.argv', ['serve_desktop', '--help']), patch('sys.stdout', output), \
                patch.object(SERVE, 'DesktopRuntime') as runtime:
            with self.assertRaises(SystemExit) as stopped:
                SERVE.main()
            self.assertEqual(stopped.exception.code, 0)
            runtime.assert_not_called()
        self.assertIn('--scientist-broker-socket', output.getvalue())
        self.assertIn('scientist', output.getvalue())

    def test_admission_factory_and_output_contract_are_a_trusted_scientist_pair(self):
        factory = Mock()
        for options in ({'scientist_admission_factory': factory},
                        {'scientist_output_contract': OUTPUT_PIN},
                        {'scientist_admission_factory': True, 'scientist_output_contract': OUTPUT_PIN},
                        {'scientist_output_context_tokens': 512}):
            with self.subTest(options=options):
                self.denied(self.arguments(), lambda profiles: None, **options)
        self.denied(['serve_desktop', '--engine', 'fixture'], lambda profiles: None,
                    scientist_admission_factory=factory, scientist_output_contract=OUTPUT_PIN)
        factory.assert_not_called()

    def test_output_contract_and_context_are_strictly_validated_before_runtime(self):
        factory = Mock()
        for contract in (True, {}, OUTPUT_PIN | {'version': True}, OUTPUT_PIN | {'version': 2.0},
                         OUTPUT_PIN | {'bundle_sha256': 'invalid'}, OUTPUT_PIN | {'extra': True}):
            with self.subTest(contract=contract):
                self.denied(self.arguments(), lambda profiles: None,
                            scientist_admission_factory=factory, scientist_output_contract=contract)
        for context in (True, 256.0, 255, 16385):
            with self.subTest(context=context):
                self.denied(self.arguments(), lambda profiles: None,
                    scientist_admission_factory=factory, scientist_output_contract=OUTPUT_PIN,
                    scientist_output_context_tokens=context)
        factory.assert_not_called()

    def test_actual_startup_connects_same_store_history_and_frozen_output_validator(self):
        runtime = FixtureDesktop(self.root / 'workspace')
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.docker = Mock()
        runtime.container_id = 'synthetic'
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=None))
        expected = deepcopy(OUTPUT_PIN)
        configured = deepcopy(OUTPUT_PIN)
        histories = []
        controllers = []

        def factory(controller):
            history = ScientistAdmissionHistory(controller.store, record_version='2.0')
            histories.append(history)
            controllers.append(controller)
            return history

        def confirm(profiles):
            configured['bundle_sha256'] = 'f' * 64

        private_assets = self.root / 'private-console-assets'
        with patch('sys.argv', self.arguments('--console-assets-root', str(private_assets))), patch('sys.stdout', StringIO()), \
                patch.object(SERVE, 'REPO_ROOT', self.root), patch.object(SERVE, 'DesktopRuntime', return_value=runtime), \
                patch.object(SERVE, 'DeciderEngine') as native, patch.object(SERVE, 'ReusableDeciderEngine') as reusable, \
                patch.object(SERVE, 'create_console', return_value=app) as console, patch.object(SERVE.uvicorn, 'run') as run:
            SERVE.main(scientist_confirm_runtime=confirm, scientist_admission_factory=factory,
                       scientist_output_contract=configured, scientist_output_context_tokens=4096)
        self.assertEqual(len(histories), 1)
        self.assertEqual(console.call_args.args[3], private_assets / ('a' * 64))
        scheduler = console.call_args.args[5]
        controller = console.call_args.args[0]
        self.assertIs(controllers[0], controller)
        self.assertIs(histories[0].store, controller.store)
        self.assertIs(scheduler.scientist_binding.admission_history, histories[0])
        validator = scheduler.scientist_binding.output_validator
        self.assertIsInstance(validator, AdmissionProfileOutputValidator)
        self.assertIs(validator.history, histories[0])
        self.assertEqual(json.loads(validator.expected_json), expected)
        self.assertEqual(validator.context_tokens, 4096)
        self.assertIs(scheduler.engine.client._callbacks[3], validator)
        native.assert_not_called()
        reusable.assert_not_called()
        run.assert_called_once()
        self.assertEqual(list((self.root / 'runs').glob('*.token')), [])

    def test_factory_rejects_foreign_store_legacy_nonhistory_and_failure_before_console(self):
        foreign = TrajectoryStore(self.root / 'foreign.sqlite3')
        self.addCleanup(foreign.close)
        for kind in ('foreign', 'legacy', 'boolean', 'failure', 'untyped_bootstrap'):
            runtime = FixtureDesktop(self.root / 'workspace')
            runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
            runtime.docker = Mock()
            runtime.container_id = 'synthetic'

            def factory(controller):
                if kind == 'failure':
                    raise ScientistAdmissionError('Synthetic unavailable original source')
                if kind == 'boolean':
                    return True
                return ScientistAdmissionHistory(foreign if kind == 'foreign' else controller.store,
                                                   record_version='1.0' if kind == 'legacy' else '2.0')

            with self.subTest(kind=kind), patch('sys.argv', self.arguments()), patch('sys.stderr', StringIO()), \
                    patch.object(SERVE, 'REPO_ROOT', self.root), patch.object(SERVE, 'DesktopRuntime', return_value=runtime), \
                    patch.object(SERVE, 'DeciderEngine') as native, patch.object(SERVE, 'create_console') as console, \
                    patch.object(SERVE, 'create_scientist_desktop_scheduler') as scheduler, \
                    patch.object(SERVE.uvicorn, 'run') as run, patch.object(runtime, 'stop', wraps=runtime.stop) as stop:
                with self.assertRaises(SystemExit) as stopped:
                    SERVE.main(scientist_confirm_runtime=lambda profiles: None,
                               scientist_admission_factory=factory, scientist_output_contract=OUTPUT_PIN,
                               scientist_bootstrap_expected_peer=Mock() if kind == 'untyped_bootstrap' else None)
                self.assertEqual(stopped.exception.code, 2)
                native.assert_not_called()
                scheduler.assert_not_called()
                console.assert_not_called()
                run.assert_not_called()
                self.assertGreater(stop.call_count, 0)
                self.assertEqual(list((self.root / 'runs').glob('*.token')), [])

    def test_explicit_peer_option_wires_factory_capture_into_actual_async_client(self):
        runtime = FixtureDesktop(self.root / 'workspace')
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.docker = Mock()
        runtime.container_id = 'synthetic'
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=None))
        histories = []
        peer_reader = Mock()

        def factory(controller):
            capture = ScientistBootstrapCapture(controller.store, self.root / 'bootstrap.sock',
                codec=ScientistBootstrapCodec(control_descriptor_sha256=CONTROL_DESCRIPTOR_SHA256))
            history = ScientistAdmissionHistory(controller.store, capture=capture, record_version='2.0')
            histories.append(history)
            return history

        with patch('sys.argv', self.arguments()), patch('sys.stdout', StringIO()), \
                patch.object(SERVE, 'REPO_ROOT', self.root), patch.object(SERVE, 'DesktopRuntime', return_value=runtime), \
                patch.object(SERVE, 'DeciderEngine') as native, patch.object(SERVE, 'ReusableDeciderEngine') as reusable, \
                patch.object(SERVE, 'create_console', return_value=app) as console, patch.object(SERVE.uvicorn, 'run') as run:
            SERVE.main(scientist_confirm_runtime=lambda profiles: None, scientist_admission_factory=factory,
                       scientist_output_contract=OUTPUT_PIN, scientist_bootstrap_expected_peer=peer_reader)
        scheduler = console.call_args.args[5]
        binding = scheduler.scientist_binding
        self.assertIs(binding.bootstrap_capture, histories[0].capture)
        self.assertIs(binding.expected_bootstrap_peer, peer_reader)
        self.assertEqual(scheduler.engine.client._prepare_infer, binding.prepare_infer)
        peer_reader.assert_not_called()
        native.assert_not_called()
        reusable.assert_not_called()
        run.assert_called_once()

    def concrete_bootstrap_factory(self):
        fixture = bootstrap_cases.ScientistBootstrapTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        stable = deepcopy(fixture.stable)
        stable['profile_pin']['deployment_digest'] = digest(self.pins)
        source = Mock(return_value=None)
        factory = ScientistBootstrapAdmissionFactory({stable['profile_id']: stable}, self.root / 'bootstrap.sock',
            control_descriptor_sha256=CONTROL_DESCRIPTOR_SHA256, verify_source=source,
            authenticator=fixture.authenticator, clock=lambda: fixture.now)
        return factory, source, fixture.authenticator

    def test_single_bootstrap_factory_rejects_wrong_type_mixed_hooks_and_other_engine_before_runtime(self):
        for factory in (True, object(), Mock(spec=ScientistBootstrapAdmissionFactory)):
            with self.subTest(factory=factory):
                self.denied(self.arguments(), scientist_bootstrap_factory=factory)
        factory, source, _authenticator = self.concrete_bootstrap_factory()
        for options in ({'provider': lambda profiles: None}, {'scientist_admission_factory': Mock()},
                        {'scientist_bootstrap_expected_peer': Mock()}, {'scientist_output_contract': OUTPUT_PIN}):
            with self.subTest(options=options):
                self.denied(self.arguments(), scientist_bootstrap_factory=factory, **options)
        self.denied(['serve_desktop', '--engine', 'fixture'], scientist_bootstrap_factory=factory)
        source.assert_not_called()

    def test_actual_single_factory_startup_forwards_exact_hooks_without_prefetch_or_native_model(self):
        factory, source, authenticator = self.concrete_bootstrap_factory()
        runtime = FixtureDesktop(self.root / 'workspace')
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.docker = Mock()
        runtime.container_id = 'synthetic'
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=None))

        def inspect_startup(_app, **options):
            controller = console.call_args.args[0]
            self.assertEqual(controller.store.connection.execute('SELECT count(*) FROM scientist_turn_intents').fetchone()[0], 0)
            self.assertEqual(controller.store.connection.execute(
                "SELECT count(*) FROM desktop_events WHERE kind='scientist_bootstrap_intent'").fetchone()[0], 0)

        with patch('sys.argv', self.arguments()), patch('sys.stdout', StringIO()), \
                patch.object(SERVE, 'REPO_ROOT', self.root), patch.object(SERVE, 'DesktopRuntime', return_value=runtime), \
                patch.object(SERVE, 'DeciderEngine') as native, patch.object(SERVE, 'ReusableDeciderEngine') as reusable, \
                patch.object(SERVE, 'create_console', return_value=app) as console, \
                patch.object(SERVE.uvicorn, 'run', side_effect=inspect_startup) as run, \
                patch.object(ScientistBootstrapCapture, 'prepare') as prepare, \
                patch.object(ScientistBootstrapCapture, 'prepare_async') as prepare_async:
            SERVE.main(scientist_bootstrap_factory=factory, scientist_output_context_tokens=4096)
        scheduler = console.call_args.args[5]
        binding = scheduler.scientist_binding
        self.assertEqual(binding.confirm_runtime, factory.confirm_runtime)
        self.assertEqual(binding.expected_bootstrap_peer, factory.expected_peer)
        self.assertIs(binding.admission_history, factory._history)
        self.assertIs(binding.admission_history.store, console.call_args.args[0].store)
        self.assertIs(binding.bootstrap_capture, binding.admission_history.capture)
        self.assertEqual(binding.admission_history.record_version, '2.0')
        self.assertEqual(json.loads(binding.output_validator.expected_json), OUTPUT_PIN)
        self.assertEqual(binding.output_validator.context_tokens, 4096)
        self.assertEqual(scheduler.engine.client._prepare_infer, binding.prepare_infer)
        self.assertTrue(source.called)
        authenticator.authenticate.assert_not_called()
        prepare.assert_not_called()
        prepare_async.assert_not_called()
        native.assert_not_called()
        reusable.assert_not_called()
        run.assert_called_once()

    def test_single_factory_revoked_source_denies_before_runtime(self):
        factory, source, _authenticator = self.concrete_bootstrap_factory()
        source.side_effect = ScientistAdmissionError('Synthetic independent source revoked')
        self.denied(self.arguments(), scientist_bootstrap_factory=factory)
        source.assert_called_once()

    def test_injected_synthetic_joint_provider_wires_actual_factory_into_console_without_native_engine(self):
        server = SyntheticLabServer()
        self.addCleanup(server.close)
        token = self.root / 'synthetic-private.token'
        token.write_text('synthetic-private-token\n')
        token.chmod(0o600)
        lab_config = ScientistLabStartup(authority_url=server.url, token_file=token,
            principal_id='synthetic-aos', allowed_suites=frozenset({'synthetic.allowed.v1'}),
            program_version='director.v1', authorization_context_sha256='a' * 64)
        bonsai_manifest = self.root / 'models/bonsai-manifest.json'
        bonsai_manifest.write_text(json.dumps({'synthetic_cpu_fixture': True, 'temperature': 0.0, 'max_output_tokens': 128}))
        vision_digest = digest(BonsaiVisionSupervisor(bonsai_manifest).pins)
        runtime = FixtureDesktop(self.root / 'workspace')
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.docker = Mock()
        runtime.container_id = 'synthetic'
        app = SimpleNamespace(router=SimpleNamespace(lifespan_context=None))
        profiles_seen = []
        def confirm(profiles):
            profiles_seen.append(profiles)
            self.assertEqual(profiles, {'aos.decider.turn.v1': digest(self.pins),
                                       'aos.bonsai.vision.v1': vision_digest})
        output = StringIO()
        with patch('sys.argv', self.arguments('--browser-tasks', '--vision-engine', 'bonsai',
                                             '--bonsai-manifest', str(bonsai_manifest))), patch('sys.stdout', output), \
                patch.object(SERVE, 'REPO_ROOT', self.root), patch.object(SERVE, 'DesktopRuntime', return_value=runtime), \
                patch.object(SERVE, 'DeciderEngine') as native, patch.object(SERVE, 'BonsaiVisionSupervisor') as bonsai, \
                patch.object(SERVE, 'create_console', return_value=app) as console, patch.object(SERVE.uvicorn, 'run') as run:
            SERVE.main(scientist_confirm_runtime=confirm, scientist_lab_config=lab_config,
                       scientist_verify_lab_capability=lambda task, action: None)
        native.assert_not_called()
        bonsai.assert_not_called()
        run.assert_called_once()
        scheduler = console.call_args.args[5]
        self.assertEqual(scheduler.engine.identity['kind'], 'scientist_decider_broker')
        self.assertEqual(scheduler.vision_supervisor.identity['kind'], 'scientist_bonsai_broker')
        self.assertIs(scheduler.scientist_binding.controller, console.call_args.args[0])
        self.assertIsNone(console.call_args.kwargs['knowledge_answerer'])
        self.assertEqual(len(profiles_seen), 2)
        self.assertEqual(list((self.root / 'runs').glob('*.token')), [])
        self.assertIsNotNone(app.router.lifespan_context)
        lab = console.call_args.kwargs['scientist_lab']
        self.assertIs(lab.controller, console.call_args.args[0])
        self.assertEqual(lab.client.principal_id, 'synthetic-aos')
        self.assertEqual(lab.client.verify_authority, lab.journal.verify_authority)
        self.assertEqual(server.requests, [])
        self.assertNotIn('synthetic-private-token', output.getvalue())
        self.assertTrue(token.exists())

    def lab_config(self, **changes):
        token = self.root / 'synthetic-private.token'
        token.write_text('synthetic-private-token\n')
        token.chmod(0o600)
        return ScientistLabStartup(authority_url='http://127.0.0.1:1', token_file=token,
            principal_id='synthetic-aos', allowed_suites=frozenset({'synthetic.allowed.v1'}),
            program_version='director.v1', authorization_context_sha256='a' * 64).model_copy(update=changes)

    def test_lab_requires_explicit_configuration_verifier_and_broker_mode(self):
        config = self.lab_config()
        self.denied(self.arguments(), lambda profiles: None, lab_config=config)
        self.denied(['serve_desktop'], lab_config=config, lab_capability=lambda *arguments: None)
        self.denied(self.arguments(), lambda profiles: None, lab_capability=lambda *arguments: None)

    def test_lab_preflight_rejects_nonloopback_and_nonprivate_credential_before_runtime(self):
        config = self.lab_config(authority_url='http://192.0.2.1:1234')
        self.denied(self.arguments(), lambda profiles: None, config, lambda *arguments: None)
        config = self.lab_config()
        config.token_file.chmod(0o644)
        self.denied(self.arguments(), lambda profiles: None, config, lambda *arguments: None)

    def test_lab_config_is_frozen_and_token_is_never_exported_as_a_value(self):
        config = self.lab_config()
        with self.assertRaises(ValueError):
            config.principal_id = 'foreign'
        self.assertNotIn('synthetic-private-token', config.model_dump_json())
        with self.assertRaises(ScientistAdmissionError):
            prepare_scientist_lab_startup({'synthetic': True})

    def test_lab_startup_canonical_schema_matches_typed_host_configuration(self):
        schema = json.loads((REPO_ROOT / 'schemas/scientist_lab_startup.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                  **ScientistLabStartup.model_json_schema()})
        jsonschema.Draft202012Validator(schema).validate(json.loads(self.lab_config().model_dump_json()))

    def test_actual_startup_console_authenticated_lab_lifecycle_and_independent_report(self):
        server = SyntheticLabServer()
        self.addCleanup(server.close)
        config = self.lab_config(authority_url=server.url)
        runtime = FixtureDesktop(self.root / 'workspace')
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.docker = Mock()
        runtime.container_id = 'synthetic'

        async def drive(app):
            base = 'http://127.0.0.1:8765'
            token = next((self.root / 'runs').glob('*.token')).read_text()
            headers = {'Origin': base}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as client:
                self.assertEqual((await client.post('/api/login', headers=headers, json={'token': token})).status_code, 200)
                proposal = {'suite': 'synthetic.allowed.v1', 'track': 'anomaly', 'program_version': 'director.v1',
                            'budget': {'experiments': 1, 'wall_seconds': 30, 'model_tokens': 100}}
                approval = (await client.post('/api/scientist/propose', headers=headers, json=proposal)).json()
                self.assertEqual(server.requests, [])
                for operation, body in [('approve', {'action_id': approval['action_id'],
                    'envelope_sha256': approval['envelope_sha256'], 'accept': True}),
                    ('execute', {'action_id': approval['action_id']})]:
                    result = await client.post('/api/scientist/' + operation, headers=headers, json=body)
                    self.assertEqual(result.status_code, 200, result.text)
                run_id = approval['envelope']['task']['request']['external_run_id']
                status = await client.post('/api/scientist/status', headers=headers, json={'run_id': run_id})
                self.assertEqual(status.json()['state'], 'queued')
                server.state = 'completed'
                report = await client.post('/api/scientist/report', headers=headers, json={'run_id': run_id})
                self.assertEqual(report.status_code, 200, report.text)
                self.assertEqual(report.json()['run_id'], server.run_id)
                inventory = (await client.get('/api/scientist/jobs')).json()
                self.assertEqual(inventory['jobs'][0]['actions'][0]['state'], 'acknowledged')
                self.assertFalse(inventory['joint_runtime_admitted'])
                self.assertFalse(inventory['inference']['gpu_release_verified'])
                self.assertNotIn('synthetic-private-token', json.dumps(inventory))

        def serve(app, **options):
            async def lifecycle():
                async with app.router.lifespan_context(app):
                    await drive(app)
            asyncio.run(lifecycle())

        with patch('sys.argv', self.arguments()), patch('sys.stdout', StringIO()), \
                patch.object(SERVE, 'REPO_ROOT', self.root), patch.object(SERVE, 'DesktopRuntime', return_value=runtime), \
                patch.object(SERVE.uvicorn, 'run', side_effect=serve), patch.object(SERVE, 'DeciderEngine') as native:
            SERVE.main(scientist_confirm_runtime=lambda profiles: None, scientist_lab_config=config,
                       scientist_verify_lab_capability=lambda task, action: None)
        native.assert_not_called()
        self.assertEqual(len([request for request in server.requests if request[0] == 'POST']), 1)

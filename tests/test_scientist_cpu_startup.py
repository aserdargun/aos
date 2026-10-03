"""CPU startup composition tests; desktop and remote experiment observations are synthetic."""

import asyncio
from io import StringIO
import unittest
from unittest.mock import Mock, patch

import httpx

from aos.scientist_cpu_capability import ScientistCpuCapabilityVerifier
from aos.scientist_cpu_session import prepare_scientist_cpu_startup
from aos.scientist_lab_service import ScientistLabStartup
from aos.scientist_transport import ScientistAdmissionError
from test_desktop_tasks import FixtureDesktop
import test_scientist_cpu_capability as capability_fixtures
from test_scientist_startup import SERVE


class ScientistCpuStartupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capability_fixtures.ScientistCpuCapabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.config = ScientistLabStartup(authority_url=self.fixture.client.authority_url,
            token_file=self.fixture.client.token_file, principal_id=self.fixture.client.principal_id,
            allowed_suites=self.fixture.client.allowed_suites, program_version='mode-grid.v1',
            authorization_context_sha256=self.fixture.grant.authorization_context_sha256)

    def arguments(self, *extra):
        return ['serve_desktop', '--engine', 'fixture', '--port', '19001',
                '--workspace', str(self.root / 'workspace'), '--database', str(self.root / 'session.sqlite'),
                '--console-assets-root', str(self.root / 'console-assets'), *extra]

    def denied(self, *, extra=(), config=None, grant=None, **options):
        with patch('sys.argv', self.arguments(*extra)), patch('sys.stderr', StringIO()) as stderr, \
                patch.object(SERVE, 'REPO_ROOT', self.root), patch.object(SERVE, 'DesktopRuntime') as runtime, \
                patch.object(SERVE.uvicorn, 'run') as server:
            with self.assertRaises(SystemExit) as error:
                SERVE.main(scientist_lab_config=self.config if config is None else config,
                    scientist_cpu_grant=self.fixture.grant if grant is None else grant, **options)
            self.assertEqual(error.exception.code, 2)
            runtime.assert_not_called()
            server.assert_not_called()
            self.assertNotIn('synthetic-test-token', stderr.getvalue())
        self.assertFalse((self.root / 'session.sqlite').exists())

    def test_preparation_revalidates_without_network_or_binding_effect_callbacks(self):
        with patch('aos.scientist_lab.ScientistLabClient._request') as request:
            config, client, grant = prepare_scientist_cpu_startup(self.config, self.fixture.grant)
        self.assertEqual(config, self.config)
        self.assertEqual(grant, self.fixture.grant)
        self.assertIsNot(grant, self.fixture.grant)
        self.assertEqual(client.allowed_suites, frozenset({grant.capability.suite_id}))
        self.assertEqual(client.verify_authority, self.fixture.client.verify_authority)
        request.assert_not_called()

    def test_mismatched_or_untyped_configuration_denies_before_desktop(self):
        for changes in ({'authority_url': 'http://127.0.0.1:19998'}, {'principal_id': 'another-owner'},
                        {'allowed_suites': frozenset({'another-suite'})},
                        {'allowed_suites': self.config.allowed_suites | {'another-suite'}},
                        {'program_version': 'wrong.v1'}, {'authorization_context_sha256': '0' * 64}):
            with self.subTest(changes=changes):
                self.denied(config=self.config.model_copy(update=changes))
        self.denied(config={})
        self.denied(grant={})
        self.denied(grant=self.fixture.grant.model_copy(update={'capability_sha256': '0' * 64}))

    def test_missing_or_public_credential_denies_before_desktop(self):
        self.config.token_file.chmod(0o644)
        self.denied()
        self.config.token_file.unlink()
        self.denied()

    def test_native_shared_and_default_session_options_are_rejected(self):
        for extra in (('--engine', 'decider'), ('--engine', 'scientist'), ('--engine', 'disabled'),
                      ('--vision-engine', 'bonsai'), ('--reuse-decider',), ('--prewarm-decider',),
                      ('--prewarm-idle-seconds', '10'), ('--gpu-idle-seconds', '10'),
                      ('--owned-skill-reuse-sha256', 'a' * 64), ('--port', '8765'),
                      ('--workspace', str(self.root / 'data/desktop-workspace')),
                      ('--database', str(self.root / 'data/desktop-console.sqlite'))):
            with self.subTest(extra=extra):
                self.denied(extra=extra)
        for name in ('scientist_confirm_runtime', 'scientist_verify_lab_capability',
                     'scientist_admission_factory', 'scientist_bootstrap_factory',
                     'scientist_retained_resolver_factory', 'scientist_output_contract',
                     'scientist_bootstrap_expected_peer'):
            with self.subTest(hook=name):
                self.denied(**{name: Mock()})
        self.denied(scientist_output_context_tokens=True)

    def test_actual_startup_wires_exact_cpu_service_and_closes_it_without_remote_request(self):
        runtime = FixtureDesktop(self.root / 'workspace')
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.docker = Mock()
        runtime.container_id = 'synthetic'
        services = []

        def run(app, **options):
            async def lifecycle():
                async with app.router.lifespan_context(app):
                    service = console.call_args.kwargs['scientist_lab']
                    services.append(service)
                    self.assertIs(service.controller, console.call_args.args[0])
                    self.assertIsInstance(service.capability, ScientistCpuCapabilityVerifier)
                    self.assertEqual(service.capability.grant, self.fixture.grant)
                    self.assertIs(service.capability.client, service.client)
                    self.assertEqual(service.client.verify_authority, service.journal.verify_authority)
                    self.assertEqual(service.client.authorize_and_persist, service.journal.authorize_and_persist)
                    self.assertIsNone(console.call_args.kwargs['knowledge_answerer'])
                    self.assertEqual(service.store.connection.execute(
                        'SELECT count(*) FROM scientist_lab_actions').fetchone()[0], 0)
                    origin = console.call_args.args[2]
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                        self.assertEqual((await browser.get('/api/scientist/jobs')).status_code, 401)
                        login = await browser.post('/api/login', headers={'Origin': origin},
                            json={'token': console.call_args.args[1]})
                        self.assertEqual(login.status_code, 200)
                        inventory = await browser.get('/api/scientist/jobs')
                        self.assertEqual(inventory.status_code, 200)
                        self.assertTrue(inventory.json()['configured'])
                        self.assertFalse(inventory.json()['joint_runtime_admitted'])
                        self.assertEqual(inventory.json()['allowed_suites'], sorted(self.config.allowed_suites))
            asyncio.run(lifecycle())

        with patch('sys.argv', self.arguments()), patch('sys.stdout', StringIO()) as output, \
                patch.object(SERVE, 'REPO_ROOT', self.root), \
                patch.object(SERVE, 'DesktopRuntime', return_value=runtime), \
                patch.object(SERVE.DeciderEngine, '__init__', side_effect=AssertionError('Native forbidden')) as native, \
                patch.object(SERVE.ReusableDeciderEngine, '__init__', side_effect=AssertionError('Native forbidden')) as reusable, \
                patch.object(SERVE.BonsaiVisionSupervisor, '__init__', side_effect=AssertionError('Native forbidden')) as bonsai, \
                patch.object(SERVE, 'create_console', wraps=SERVE.create_console) as console, \
                patch('aos.scientist_lab.ScientistLabClient._request') as request, \
                patch.object(SERVE.uvicorn, 'run', side_effect=run):
            SERVE.main(scientist_lab_config=self.config, scientist_cpu_grant=self.fixture.grant)
        native.assert_not_called()
        reusable.assert_not_called()
        bonsai.assert_not_called()
        request.assert_not_called()
        self.assertTrue(services[0].client._closed)
        self.assertNotIn('synthetic-test-token', output.getvalue())


if __name__ == '__main__':
    unittest.main()

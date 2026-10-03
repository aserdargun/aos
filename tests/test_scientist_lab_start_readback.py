"""Owned CPU HTTP and journal/service START readback; no Scientist or GPU."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.desktop_control import DesktopController
from aos.scientist_lab import ScientistLabClient, ScientistLabUncertain
from aos.scientist_lab_service import ScientistLabService
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore

import test_scientist_lab as lab_fixtures


def configure_start(server, *, acknowledgement=None, readback=None, lost_get=False, invalid_get=False):
    if not hasattr(server, '_start_handler'):
        server._start_handler = server.server.RequestHandlerClass
        server._start_status = server.status
    original_handler, original_status = server._start_handler, server._start_status
    def status():
        return original_status() | (readback or {})
    class Handler(original_handler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            server.requests.append(('POST', self.path, body))
            self.send_json({'run_id': server.run_id, 'state': 'queued', 'reused': False}
                           | (acknowledgement or {}), 202)

        def do_GET(self):
            if lost_get or invalid_get:
                server.requests.append(('GET', self.path, b''))
                if lost_get:
                    self.connection.close()
                else:
                    self.send_json({'synthetic_invalid': True})
                return
            super().do_GET()
    server.status = status
    server.server.RequestHandlerClass = Handler


class ScientistLabStartReadbackTests(unittest.TestCase):
    def setUp(self):
        lab_fixtures.ScientistLabTests.setUp(self)

    def action(self, tool, task=None):
        return lab_fixtures.ScientistLabTests.action(self, tool, task)

    def fresh_client(self):
        return ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
            allowed_suites=self.client.allowed_suites, verify_authority=self.authority, authorize_and_persist=self.effect)

    def assert_routes(self):
        self.assertEqual([entry[:2] for entry in self.server.requests],
                         [('POST', '/v1/runs'), ('GET', '/v1/runs/' + self.server.run_id)])

    def uncertain(self, client=None):
        client = client or self.client
        action = self.action('lab.start')
        with self.assertRaises(ScientistLabUncertain): client.execute(self.task, action)
        self.assertEqual(client.uncertain_action_id, action.action_id)
        before = list(self.server.requests)
        with self.assertRaises(ScientistAdmissionError): client.execute(self.task, action)
        changed = self.task.model_copy(update={'request': self.task.request.model_copy(update={
            'external_action_id': 'action-' + 'f' * 32, 'idempotency_key': 'synthetic-new-start-key'})})
        with self.assertRaises(ScientistAdmissionError): client.execute(changed, self.action('lab.start', changed))
        self.assertEqual(self.server.requests, before)

    def test_start_returns_independent_state_preserving_run_and_reused_with_same_deadline(self):
        for state in ('queued', 'running', 'stop_requested', 'completed', 'stopped', 'failed'):
            self.server.requests.clear()
            configure_start(self.server, acknowledgement={'reused': True}, readback={'state': state})
            client = self.fresh_client()
            with self.subTest(state=state), patch.object(client, '_request', wraps=client._request) as request:
                result = client.execute(self.task, self.action('lab.start'))
            self.assertEqual(result.model_dump(), {'run_id': self.server.run_id, 'state': state, 'reused': True})
            self.assertEqual(request.call_args_list[0].args[3], request.call_args_list[1].args[3])
            self.assert_routes()

    def test_terminal_acknowledgement_cannot_regress_or_conflict(self):
        for terminal in ('completed', 'stopped', 'failed'):
            for observed in ('queued', 'running', 'stop_requested', 'completed', 'stopped', 'failed'):
                if terminal == observed:
                    continue
                self.server.requests.clear()
                configure_start(self.server, acknowledgement={'state': terminal}, readback={'state': observed})
                with self.subTest(ack=terminal, observed=observed):
                    self.uncertain(self.fresh_client())
                    self.assert_routes()

    def test_same_terminal_acknowledgement_and_readback_are_valid(self):
        for state in ('completed', 'stopped', 'failed'):
            self.server.requests.clear()
            configure_start(self.server, acknowledgement={'state': state, 'reused': True}, readback={'state': state})
            result = self.fresh_client().execute(self.task, self.action('lab.start'))
            self.assertEqual(result.state, state)
            self.assertTrue(result.reused)
            self.assert_routes()

    def test_lost_invalid_or_foreign_get_stays_uncertain_without_retry(self):
        for options in ({'lost_get': True}, {'invalid_get': True},
                        {'readback': {'run_id': 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee'}},
                        {'readback': {'origin': 'local'}}, {'readback': {'purpose': 'baseline'}}):
            self.server.requests.clear()
            configure_start(self.server, **options)
            with self.subTest(options=options):
                self.uncertain(self.fresh_client())
                self.assert_routes()

    def test_authority_revoked_after_post_or_get_never_returns_success(self):
        for phase in ('POST', 'GET'):
            self.server.requests.clear()
            configure_start(self.server)
            def revoke(*_arguments):
                if any(entry[0] == phase for entry in self.server.requests):
                    raise ScientistAdmissionError('Synthetic start authority revoked')
            self.authority.side_effect = revoke
            with self.subTest(phase=phase): self.uncertain(self.fresh_client())
            self.assertEqual([entry[0] for entry in self.server.requests], ['POST'] if phase == 'POST' else ['POST', 'GET'])


class ScientistLabStartServiceReadbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lab_fixtures.ScientistLabTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = Path(self.fixture.temporary.name) / 'synthetic-service.sqlite3'
        self.store = TrajectoryStore(self.path)
        self.addCleanup(self.store.close)
        runtime = SimpleNamespace(runtime_id='synthetic-runtime', pins={'image_id': 'synthetic-image'})
        self.controller = DesktopController(self.store, runtime)
        self.authority = Mock(return_value=None)
        self.service = ScientistLabService(self.controller, self.fixture.client,
            authorization_context_sha256='a' * 64, program_version='director.v1', verify_capability=self.authority)
        approval = self.service.propose(suite='synthetic.allowed.v1', track='anomaly', program_version='director.v1',
                                        budget=self.fixture.task.request.budget)
        self.action_id = approval['action_id']
        self.service.respond(self.action_id, envelope_sha256=approval['envelope_sha256'], accept=True)

    def test_actual_service_persists_observed_failure_not_queued_acknowledgement(self):
        configure_start(self.fixture.server, acknowledgement={'reused': True}, readback={'state': 'failed'})
        original = self.fixture.client._request
        def request(method, *arguments, **options):
            if method == 'POST':
                with closing(sqlite3.connect(self.path)) as reader:
                    self.assertEqual(reader.execute('SELECT state FROM scientist_lab_actions WHERE action_id=?',
                                                   (self.action_id,)).fetchone()[0], 'intent')
            return original(method, *arguments, **options)
        with patch.object(self.fixture.client, '_request', side_effect=request):
            result = self.service.execute(self.action_id)
        self.assertEqual(result, {'run_id': self.fixture.server.run_id, 'state': 'failed', 'reused': True})
        with closing(sqlite3.connect(self.path)) as reader:
            row = reader.execute('SELECT state,result_json FROM scientist_lab_actions WHERE action_id=?', (self.action_id,)).fetchone()
            self.assertEqual(row[0], 'acknowledged')
            self.assertEqual(json.loads(row[1]), result)
            self.assertEqual(reader.execute('SELECT lab_run_id FROM scientist_lab_jobs').fetchone()[0], self.fixture.server.run_id)

    def test_actual_service_terminal_regression_keeps_unresolved_original_intent(self):
        configure_start(self.fixture.server, acknowledgement={'state': 'completed'}, readback={'state': 'queued'})
        with self.assertRaises(ScientistLabUncertain): self.service.execute(self.action_id)
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute('SELECT state,result_json FROM scientist_lab_actions WHERE action_id=?',
                                           (self.action_id,)).fetchone(), ('intent', None))
            self.assertIsNone(reader.execute('SELECT lab_run_id FROM scientist_lab_jobs').fetchone()[0])
        before = list(self.fixture.server.requests)
        with self.assertRaises(ScientistAdmissionError): self.service.execute(self.action_id)
        self.assertEqual(self.fixture.server.requests, before)
        self.assertEqual(self.fixture.client.uncertain_action_id, self.action_id)


if __name__ == '__main__':
    unittest.main()

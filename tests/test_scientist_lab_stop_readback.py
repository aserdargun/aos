from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from aos.scientist_lab import ScientistLabClient, ScientistLabUncertain
from aos.scientist_lab_journal import ScientistLabJournal
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore
import test_scientist_lab as lab_fixtures
import test_scientist_lab_journal as journal_fixtures


def configure_stop_responses(server, *, acknowledgment=None, readback=None, lost_get=False):
    if not hasattr(server, '_stop_original_status'):
        server._stop_original_status = server.status
        server._stop_original_handler = server.server.RequestHandlerClass
    original_status = server._stop_original_status
    original_handler = server._stop_original_handler

    def status():
        value = original_status()
        phase = server.requests[-1][0]
        value.update((acknowledgment if phase == 'POST' else readback) or {})
        return value

    class Handler(original_handler):
        def do_GET(self):
            if lost_get:
                server.requests.append(('GET', self.path, b''))
                self.connection.close()
                return
            super().do_GET()

    server.status = status
    server.server.RequestHandlerClass = Handler


class ScientistLabStopReadbackTests(unittest.TestCase):
    def setUp(self):
        lab_fixtures.ScientistLabTests.setUp(self)

    def action(self, tool, task=None):
        return lab_fixtures.ScientistLabTests.action(self, tool, task)

    def bound(self):
        return lab_fixtures.ScientistLabTests.bound(self)

    def assert_stop_routes(self):
        self.assertEqual([entry[:2] for entry in self.server.requests], [
            ('POST', '/v1/runs/' + self.server.run_id + '/stop'),
            ('GET', '/v1/runs/' + self.server.run_id),
        ])
        self.assertEqual(self.server.requests[0][2], b'{}')

    def assert_uncertain_stop(self, client=None):
        client = client or self.client
        task = self.bound()
        action = self.action('lab.stop', task)
        with self.assertRaises(ScientistLabUncertain):
            client.execute(task, action)
        self.assertEqual(client.uncertain_action_id, action.action_id)
        dispatched = list(self.server.requests)
        for retry in [action, action.model_copy(update={
                'action_id': 'action-' + 'f' * 32,
                'idempotency_key': 'synthetic-new-stop-key-0002'})]:
            with self.assertRaises(ScientistAdmissionError):
                client.execute(task, retry)
        self.assertEqual(self.server.requests, dispatched)

    def test_stop_returns_independent_readback_under_same_deadline(self):
        configure_stop_responses(self.server, acknowledgment={'updated_at': 'synthetic-ack'},
                                 readback={'updated_at': 'synthetic-readback'})
        task = self.bound()
        with patch.object(self.client, '_request', wraps=self.client._request) as request:
            result = self.client.execute(task, self.action('lab.stop', task))
        self.assert_stop_routes()
        self.assertEqual(result.updated_at, 'synthetic-readback')
        self.assertEqual(result.state, 'stop_requested')
        self.assertTrue(result.stop_requested)
        self.assertIsNone(result.report_sha256)
        self.assertNotIn('gpu_released', result.model_dump())
        self.assertEqual(request.call_args_list[0].args[3], request.call_args_list[1].args[3])
        self.effect.assert_called_once()

    def test_stop_request_can_progress_to_any_terminal_state(self):
        for state in ['completed', 'stopped', 'failed']:
            with self.subTest(state=state):
                self.server.requests.clear()
                configure_stop_responses(self.server, readback={
                    'state': state, 'stop_requested': state == 'stopped',
                    'updated_at': 'synthetic-terminal', 'report_sha256': 'b' * 64})
                client = ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
                    allowed_suites=self.client.allowed_suites, verify_authority=self.authority,
                    authorize_and_persist=self.effect)
                task = self.bound()
                result = client.execute(task, self.action('lab.stop', task))
                self.assertEqual(result.state, state)
                self.assertEqual(result.updated_at, 'synthetic-terminal')
                self.assert_stop_routes()

    def test_acknowledgment_identity_and_baseline_are_rejected_before_get(self):
        for change in [{'run_id': 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee'},
                       {'origin': 'local'}, {'purpose': 'baseline'}]:
            with self.subTest(change=change):
                self.server.requests.clear()
                configure_stop_responses(self.server, acknowledgment=change)
                client = ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
                    allowed_suites=self.client.allowed_suites, verify_authority=self.authority,
                    authorize_and_persist=self.effect)
                self.assert_uncertain_stop(client)
                self.assertEqual([entry[0] for entry in self.server.requests], ['POST'])

    def test_readback_identity_and_baseline_are_uncertain(self):
        for change in [{'run_id': 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee'},
                       {'origin': 'local'}, {'purpose': 'baseline'}]:
            with self.subTest(change=change):
                self.server.requests.clear()
                configure_stop_responses(self.server, readback=change)
                client = ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
                    allowed_suites=self.client.allowed_suites, verify_authority=self.authority,
                    authorize_and_persist=self.effect)
                self.assert_uncertain_stop(client)
                self.assert_stop_routes()

    def test_acknowledgment_cannot_replace_independent_stop_request(self):
        configure_stop_responses(self.server, readback={'state': 'running', 'stop_requested': False})
        self.assert_uncertain_stop()
        self.assert_stop_routes()

    def test_terminal_acknowledgment_cannot_change_terminal_state(self):
        configure_stop_responses(self.server,
            acknowledgment={'state': 'completed', 'stop_requested': False, 'report_sha256': 'a' * 64},
            readback={'state': 'failed', 'stop_requested': False, 'report_sha256': 'b' * 64})
        self.assert_uncertain_stop()
        self.assert_stop_routes()

    def test_lost_get_after_valid_acknowledgment_remains_uncertain(self):
        configure_stop_responses(self.server, lost_get=True)
        self.assert_uncertain_stop()
        self.assert_stop_routes()

    def test_authority_revoked_after_post_prevents_readback(self):
        def verify(task, action):
            if self.server.requests:
                raise ScientistAdmissionError('synthetic generation revoked after POST')

        self.authority.side_effect = verify
        self.assert_uncertain_stop()
        self.assertEqual([entry[0] for entry in self.server.requests], ['POST'])

    def test_authority_revoked_after_get_prevents_success(self):
        def verify(task, action):
            if any(entry[0] == 'GET' for entry in self.server.requests):
                raise ScientistAdmissionError('synthetic generation revoked after GET')

        self.authority.side_effect = verify
        self.assert_uncertain_stop()
        self.assert_stop_routes()


class ScientistLabStopReadbackJournalTests(unittest.TestCase):
    def setUp(self):
        journal_fixtures.ScientistLabJournalTests.setUp(self)

    def action(self, tool, task=None):
        return journal_fixtures.ScientistLabJournalTests.action(self, tool, task)

    def test_lost_stop_get_retains_original_durable_intent_and_denies_replay(self):
        start = self.action('lab.start')
        self.journal.queue(self.task, start)
        self.journal.respond(start.action_id, approver='synthetic-human', accept=True)
        result = self.journal.execute(self.client, self.task, start)
        bound = self.task.model_copy(update={'lab_run_id': result.run_id})
        stop = self.action('lab.stop', bound)
        self.journal.queue(bound, stop)
        self.journal.respond(stop.action_id, approver='synthetic-human', accept=True)
        self.server.requests.clear()
        configure_stop_responses(self.server, lost_get=True)
        with self.assertRaises(ScientistLabUncertain):
            self.journal.execute(self.client, bound, stop)
        with closing(sqlite3.connect(self.path)) as reader:
            row = reader.execute('SELECT state,result_json FROM scientist_lab_actions WHERE action_id=?',
                                 (stop.action_id,)).fetchone()
            self.assertEqual(row, ('intent', None))
            self.assertEqual(reader.execute('SELECT lab_run_id FROM scientist_lab_jobs').fetchone()[0],
                             self.server.run_id)
        self.store.close()
        self.store = TrajectoryStore(self.path)
        restarted = ScientistLabJournal(self.store, authenticate_human=self.human,
                                        verify_capability=self.capability)
        client = ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
            allowed_suites=self.client.allowed_suites, verify_authority=restarted.verify_authority,
            authorize_and_persist=restarted.authorize_and_persist)
        with self.assertRaises(ScientistAdmissionError):
            restarted.execute(client, bound, stop)
        with self.assertRaises(ScientistAdmissionError):
            restarted.queue(bound, stop.model_copy(update={'action_id': 'action-' + 'f' * 32}))
        self.assertEqual([entry[:2] for entry in self.server.requests], [
            ('POST', '/v1/runs/' + self.server.run_id + '/stop'),
            ('GET', '/v1/runs/' + self.server.run_id),
        ])

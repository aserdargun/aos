import sqlite3
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from aos.contracts import canonical
from aos.scientist_lab import ScientistLabUncertain
from aos.scientist_lab_journal import ScientistLabJournal
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore
import test_scientist_lab as lab_fixtures


class ScientistLabJournalTests(unittest.TestCase):
    def setUp(self):
        lab_fixtures.ScientistLabTests.setUp(self)
        self.task = self.task.model_copy(update={'state_version': 0})
        self.path = Path(self.temporary.name) / 'synthetic.sqlite3'
        self.store = TrajectoryStore(self.path)
        self.addCleanup(lambda: self.store.close())
        with self.store.connection:
            self.store.connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                ('session', 'runtime', 'synthetic-image', 'AGENT', 'lease', 2, 'running', 'synthetic', 'synthetic'))
        self.human = Mock(return_value=None)
        self.capability = Mock(return_value=None)
        self.journal = ScientistLabJournal(self.store, authenticate_human=self.human,
                                           verify_capability=self.capability)
        self.client.verify_authority = self.journal.verify_authority
        self.client.authorize_and_persist = self.journal.authorize_and_persist

    def action(self, tool, task=None):
        return lab_fixtures.ScientistLabTests.action(self, tool, task).model_copy(update={'state_version': 0})

    def queue(self):
        action = self.action('lab.start')
        self.journal.queue(self.task, action)
        return action

    def approve(self, action):
        self.journal.respond(action.action_id, approver='synthetic-human', accept=True)

    def state(self, action):
        return self.store.connection.execute('SELECT state FROM scientist_lab_actions WHERE action_id=?',
                                             (action.action_id,)).fetchone()[0]

    def test_synthetic_start_readback_binding_then_approved_stop_is_durable(self):
        action = self.queue()
        with self.assertRaises(ScientistAdmissionError):
            self.journal.execute(self.client, self.task, action)
        self.assertEqual(self.server.requests, [])
        self.approve(action)
        request = self.client._request

        def read_committed_before_post(method, *arguments, **keywords):
            if method == 'POST':
                with sqlite3.connect(self.path) as reader:
                    self.assertEqual(reader.execute(
                        "SELECT count(*) FROM scientist_lab_actions WHERE state='intent'").fetchone()[0], 1)
            return request(method, *arguments, **keywords)

        self.client._request = read_committed_before_post
        result = self.journal.execute(self.client, self.task, action)
        self.assertEqual(self.state(action), 'acknowledged')
        with sqlite3.connect(self.path) as reader:
            self.assertEqual(reader.execute('SELECT lab_run_id FROM scientist_lab_jobs').fetchone()[0], result.run_id)
        bound = self.task.model_copy(update={'lab_run_id': result.run_id})
        stop = self.action('lab.stop', bound)
        self.journal.queue(bound, stop)
        self.approve(stop)
        stopped = self.journal.execute(self.client, bound, stop)
        self.assertEqual(stopped.state, 'stop_requested')
        self.assertEqual(self.state(stop), 'acknowledged')
        self.assertTrue(self.human.called)

    def test_default_human_and_capability_providers_deny(self):
        action = self.queue()
        default = ScientistLabJournal(self.store)
        with self.assertRaises(ScientistAdmissionError):
            default.respond(action.action_id, approver='not-authenticated', accept=True)
        default.verify_capability = self.capability
        with self.assertRaises(ScientistAdmissionError):
            default.respond(action.action_id, approver='not-authenticated', accept=True)
        self.assertEqual(self.state(action), 'pending')

    def test_rejection_duplicate_and_changed_body_do_not_send(self):
        action = self.queue()
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.queue(self.task, action)
        self.journal.respond(action.action_id, approver='synthetic-human', accept=False)
        with self.assertRaises(ScientistAdmissionError):
            self.approve(action)
        with self.assertRaises(ScientistAdmissionError):
            self.journal.execute(self.client, self.task, action)
        self.assertEqual(self.server.requests, [])

    def test_approved_exact_body_and_callback_boolean_are_required(self):
        action = self.queue()
        self.human.return_value = True
        with self.assertRaises(ScientistAdmissionError):
            self.approve(action)
        self.human.return_value = None
        self.approve(action)
        with self.assertRaises(ScientistAdmissionError):
            self.journal.authorize_and_persist(self.task, action, b'{}')
        self.assertEqual(self.state(action), 'approved')

    def test_old_generation_or_owner_cannot_consume_approval(self):
        action = self.queue()
        self.approve(action)
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET generation=3,owner='HUMAN',lease_id='new'")
        with self.assertRaises(ScientistAdmissionError):
            self.journal.execute(self.client, self.task, action)
        self.assertEqual(self.state(action), 'approved')
        self.assertEqual(self.server.requests, [])

    def test_lost_ack_survives_restart_and_blocks_replay(self):
        action = self.queue()
        self.approve(action)
        self.server.mode = 'lost_ack'
        with self.assertRaises(ScientistLabUncertain):
            self.journal.execute(self.client, self.task, action)
        self.assertEqual(self.state(action), 'intent')
        self.store.close()
        self.store = TrajectoryStore(self.path)
        restarted = ScientistLabJournal(self.store, authenticate_human=self.human,
                                        verify_capability=self.capability)
        with self.assertRaises(ScientistAdmissionError):
            restarted.authorize_and_persist(self.task, action, b'{}')
        with self.assertRaises(ScientistAdmissionError):
            restarted.queue(self.task, action)
        self.assertEqual(len(self.server.requests), 1)

    def test_result_cleanup_failure_retains_intent_and_cannot_bind_foreign_run(self):
        action = self.queue()
        self.approve(action)
        self.journal.record_result = Mock(side_effect=OSError('synthetic disk failure'))
        with self.assertRaises(ScientistLabUncertain):
            self.journal.execute(self.client, self.task, action)
        self.assertEqual(self.state(action), 'intent')
        foreign = self.task.model_copy(update={'lab_run_id': 'ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee'})
        with self.assertRaises(ScientistAdmissionError):
            self.journal.verify_authority(foreign, self.action('lab.status', foreign))

    def test_sql_audit_identity_and_deletion_are_protected(self):
        action = self.queue()
        for statement in ["DELETE FROM scientist_lab_actions", "DELETE FROM scientist_lab_jobs",
                          "UPDATE scientist_lab_actions SET body='{}'",
                          "UPDATE scientist_lab_actions SET state='intent',approver='forged'"]:
            with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
                self.store.connection.execute(statement)
        self.assertEqual(self.state(action), 'pending')

    def test_expired_approval_and_revoked_capability_cannot_send(self):
        action = self.queue()
        self.capability.side_effect = ScientistAdmissionError('synthetic capability revoked')
        with self.assertRaises(ScientistAdmissionError):
            self.approve(action)
        self.capability.side_effect = None
        self.approve(action)
        expired = action.model_copy(update={'deadline': time.time() - 1})
        with self.assertRaises(ScientistAdmissionError):
            self.journal.execute(self.client, self.task, expired)
        self.assertEqual(self.server.requests, [])

    def test_other_approved_job_cannot_send_while_session_has_unresolved_effect(self):
        first = self.queue()
        self.approve(first)
        request = self.task.request.model_copy(update={
            'external_run_id': 'run-' + 'e' * 32,
            'external_action_id': 'action-' + 'f' * 32,
            'idempotency_key': 'synthetic-second-key-0002'})
        second_task = self.task.model_copy(update={'request': request})
        second = self.action('lab.start', second_task)
        self.journal.queue(second_task, second)
        self.approve(second)
        self.journal.authorize_and_persist(self.task, first,
                                          canonical(self.task.request.model_dump(mode='json')).encode())
        with self.assertRaises(ScientistAdmissionError):
            self.journal.execute(self.client, second_task, second)
        self.assertEqual(self.state(second), 'approved')
        self.assertEqual(self.server.requests, [])

    def test_owned_cpu_writer_crash_preserves_committed_lab_intent(self):
        path = Path(self.temporary.name) / 'crash.sqlite3'
        action = self.action('lab.start')
        source = '''import sys, time
from pathlib import Path
from aos.contracts import canonical
from aos.scientist_lab import ScientistLabTask, ScientistLabAction
from aos.scientist_lab_journal import ScientistLabJournal
from aos.storage import TrajectoryStore
store = TrajectoryStore(Path(sys.argv[1]))
with store.connection:
    store.connection.execute("INSERT INTO desktop_sessions VALUES('session','runtime','synthetic','AGENT','lease',2,'running','synthetic','synthetic')")
task = ScientistLabTask.model_validate_json(sys.argv[2], strict=True)
action = ScientistLabAction.model_validate_json(sys.argv[3], strict=True)
journal = ScientistLabJournal(store, authenticate_human=lambda *arguments: None, verify_capability=lambda *arguments: None)
journal.queue(task, action)
journal.respond(action.action_id, approver='synthetic-human', accept=True)
journal.authorize_and_persist(task, action, canonical(task.request.model_dump(mode='json')).encode())
print('committed', flush=True)
time.sleep(30)
'''
        process = subprocess.Popen([sys.executable, '-c', source, str(path),
                                    self.task.model_dump_json(), action.model_dump_json()],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertEqual(process.stdout.readline(), b'committed\n')
            process.kill()
            process.communicate(timeout=5)
            self.assertLess(process.returncode, 0)
            restarted = TrajectoryStore(path)
            try:
                self.assertEqual(restarted.connection.execute(
                    'SELECT state FROM scientist_lab_actions').fetchone()[0], 'intent')
                with self.assertRaises(ScientistAdmissionError):
                    ScientistLabJournal(restarted).verify_authority(self.task, action)
            finally:
                restarted.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

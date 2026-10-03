import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from aos.desktop_control import DesktopController
from aos.scientist_lab import ScientistLabBudget, ScientistLabClient, ScientistLabUncertain
from aos.scientist_lab_service import ScientistLabService
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore
from test_scientist_lab import SyntheticLabServer


class ScientistLabSharedDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = TrajectoryStore(self.root / 'synthetic.sqlite3')
        runtime = SimpleNamespace(runtime_id='runtime-synthetic', pins={'image_id': 'synthetic-image'})
        self.controller = DesktopController(self.store, runtime)
        self.server = SyntheticLabServer()
        token = self.root / 'synthetic.token'
        token.write_text('synthetic-test-token\n')
        token.chmod(0o600)
        self.client = ScientistLabClient(self.server.url, token, principal_id='synthetic-aos',
                                         allowed_suites=frozenset({'synthetic.allowed.v1'}))
        self.service = ScientistLabService(self.controller, self.client,
            authorization_context_sha256='a' * 64, program_version='director.v1',
            verify_capability=lambda task, action: None)

    async def asyncTearDown(self):
        await self.client.close_async(timeout_seconds=3)
        self.server.close()
        self.store.close()
        self.temporary.cleanup()

    def propose(self):
        return self.service.propose(suite='synthetic.allowed.v1', track='anomaly',
            budget=ScientistLabBudget(experiments=1, wall_seconds=30, model_tokens=100),
            program_version='director.v1')

    def approve(self, approval):
        return self.service.respond(approval['action_id'], envelope_sha256=approval['envelope_sha256'], accept=True)

    def row(self, approval):
        return self.store.connection.execute('SELECT * FROM scientist_lab_actions WHERE action_id=?',
                                            (approval['action_id'],)).fetchone()

    async def started(self):
        approval = self.propose()
        self.approve(approval)
        await self.service.execute_async(approval['action_id'])
        return approval, approval['envelope']['task']['request']['external_run_id']

    async def test_latch_is_irreversible_and_new_proposal_approval_execution_are_denied(self):
        pending = self.propose()
        approved = self.propose()
        self.approve(approved)
        self.assertIsNone(self.service.latch_shared_drain())
        self.assertIsNone(self.service.latch_shared_drain())
        self.assertEqual(self.service.shared_drain_status(), {'admission_closed': True,
            'blockers': ['pending_actions', 'approved_actions']})
        with self.assertRaises(ScientistAdmissionError):
            self.propose()
        with self.assertRaises(ScientistAdmissionError):
            self.approve(pending)
        with self.assertRaises(ScientistAdmissionError):
            self.service.execute(approved['action_id'])
        with self.assertRaises(ScientistAdmissionError):
            await self.service.execute_async(approved['action_id'])
        _row, task, action = self.service._load_action(approved['action_id'])
        with self.assertRaises(ScientistAdmissionError):
            await self.service._execute_async(task, action)
        with self.assertRaises(ScientistAdmissionError):
            self.client.authorize_and_persist(task, action, self.row(approved)['body'].encode())
        self.assertEqual(self.row(pending)['state'], 'pending')
        self.assertEqual(self.row(approved)['state'], 'approved')
        self.assertEqual(self.server.requests, [])

    async def test_final_intent_check_rejects_latch_during_authority_callback(self):
        approval = self.propose()
        self.approve(approval)
        self.service.capability = lambda task, action: self.service.latch_shared_drain()
        with self.assertRaises(ScientistAdmissionError):
            self.service.execute(approval['action_id'])
        self.assertEqual(self.row(approval)['state'], 'approved')
        self.assertEqual(self.server.requests, [])

    async def test_accept_guard_rechecks_latch_after_authority_without_blocking_reject(self):
        approval = self.propose()
        self.service.capability = lambda task, action: self.service.latch_shared_drain()
        with self.assertRaises(ScientistAdmissionError):
            self.approve(approval)
        rejected = self.service.respond(approval['action_id'],
            envelope_sha256=approval['envelope_sha256'], accept=False)
        self.assertEqual(rejected['state'], 'rejected')
        self.assertEqual(self.service.shared_drain_status()['blockers'], [])

    async def test_already_sent_start_ack_remains_durably_recordable_after_latch(self):
        approval = self.propose()
        self.approve(approval)
        self.server.mode = 'hold_start'
        pending = asyncio.create_task(self.service.execute_async(approval['action_id']))
        try:
            for _attempt in range(100):
                if self.server.entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.server.entered.is_set())
            self.service.latch_shared_drain()
            status = self.service.shared_drain_status()
            self.assertTrue(status['admission_closed'])
            self.assertIn('active_controls', status['blockers'])
            self.assertIn('client_active', status['blockers'])
            self.assertIn('unresolved_intents', status['blockers'])
            with self.assertRaises(ScientistAdmissionError):
                self.service.seal_shared_drain_controls()
            self.assertFalse(self.service.cleanup_controls_closed)
            self.server.release.set()
            result = await pending
        finally:
            self.server.release.set()
            await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(result['run_id'], self.server.run_id)
        self.assertEqual(self.row(approval)['state'], 'acknowledged')
        self.assertEqual(self.service.shared_drain_status(), {'admission_closed': True, 'blockers': []})
        self.assertEqual(self.server.state, 'queued')
        self.assertEqual(len([request for request in self.server.requests if request[0] == 'POST']), 1)
        self.assertIsNone(self.service.seal_shared_drain_controls())
        self.assertTrue(self.service.cleanup_controls_closed)

    async def test_uncertain_client_and_durable_intent_remain_blockers_without_replay(self):
        approval = self.propose()
        self.approve(approval)
        self.server.mode = 'lost_ack'
        with self.assertRaises(ScientistLabUncertain):
            await self.service.execute_async(approval['action_id'])
        self.service.latch_shared_drain()
        status = self.service.shared_drain_status()
        self.assertIn('client_uncertain', status['blockers'])
        self.assertIn('unresolved_intents', status['blockers'])
        with self.assertRaises(ScientistAdmissionError):
            self.service.seal_shared_drain_controls()
        self.assertFalse(self.service.cleanup_controls_closed)
        self.assertEqual(self.row(approval)['state'], 'intent')
        with self.assertRaises(ScientistAdmissionError):
            await self.service.execute_async(approval['action_id'])
        self.assertEqual(len(self.server.requests), 1)

    async def test_latch_preserves_independently_approved_stop_status_and_report(self):
        _approval, run_id = await self.started()
        self.service.latch_shared_drain()
        stop = self.service.propose_stop(run_id)
        self.approve(stop)
        result = await self.service.execute_async(stop['action_id'])
        self.assertEqual(result['state'], 'stop_requested')
        self.assertEqual(self.row(stop)['state'], 'acknowledged')
        self.assertEqual((await self.service.read_async(run_id, 'lab.status'))['state'], 'stop_requested')
        self.server.state = 'stopped'
        self.assertEqual((await self.service.read_async(run_id, 'lab.report'))['report']['status'], 'stopped')
        self.assertEqual(self.service.shared_drain_status()['blockers'], [])

    async def test_stale_authority_remains_denied_for_cleanup_after_latch(self):
        _approval, run_id = await self.started()
        stop = self.service.propose_stop(run_id)
        self.approve(stop)
        self.service.latch_shared_drain()
        self.controller.control('take-control')
        with self.assertRaises(ScientistAdmissionError):
            await self.service.execute_async(stop['action_id'])
        self.assertEqual(self.row(stop)['state'], 'approved')
        self.assertEqual(len([request for request in self.server.requests if request[0] == 'POST']), 1)

    async def test_durable_action_blockers_are_scoped_to_original_controller_session(self):
        self.propose()
        other = DesktopController(self.store,
            SimpleNamespace(runtime_id='runtime-other', pins={'image_id': 'synthetic-image'}))
        client = ScientistLabClient(self.server.url, self.client.token_file, principal_id='synthetic-aos',
                                    allowed_suites=self.client.allowed_suites)
        service = ScientistLabService(other, client, authorization_context_sha256='a' * 64,
            program_version='director.v1', verify_capability=lambda task, action: None)
        service.latch_shared_drain()
        self.assertEqual(service.shared_drain_status(), {'admission_closed': True, 'blockers': []})

    async def test_final_seal_requires_latch_and_empty_local_controls_then_is_irreversible(self):
        self.assertFalse(self.service.cleanup_controls_closed)
        with self.assertRaises(ScientistAdmissionError):
            self.service.seal_shared_drain_controls()
        pending = self.propose()
        self.service.latch_shared_drain()
        with self.assertRaises(ScientistAdmissionError):
            self.service.seal_shared_drain_controls()
        self.service.respond(pending['action_id'], envelope_sha256=pending['envelope_sha256'], accept=False)
        self.assertIsNone(self.service.seal_shared_drain_controls())
        self.assertIsNone(self.service.seal_shared_drain_controls())
        self.assertTrue(self.service.cleanup_controls_closed)
        with self.assertRaises(AttributeError):
            self.service.cleanup_controls_closed = False
        with self.assertRaises(ScientistAdmissionError):
            self.propose()
        self.assertFalse(self.service._closing)
        self.assertFalse(self.client._closed)

    async def test_authorized_reads_before_seal_and_all_new_controls_after_seal(self):
        _approval, run_id = await self.started()
        self.service.latch_shared_drain()
        self.assertEqual((await self.service.read_async(run_id, 'lab.status'))['state'], 'queued')
        task = self.service._load_task(run_id)
        read_action = self.service._action(task, 'lab.status')
        self.service.seal_shared_drain_controls()
        original_requests = list(self.server.requests)
        for tool in ('lab.status', 'lab.report'):
            with self.subTest(tool=tool), self.assertRaises(ScientistAdmissionError):
                self.service.read(run_id, tool)
            with self.subTest(async_tool=tool), self.assertRaises(ScientistAdmissionError):
                await self.service.read_async(run_id, tool)
        with self.assertRaises(ScientistAdmissionError):
            self.service.propose_stop(run_id)
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(task, read_action)
        with self.assertRaises(ScientistAdmissionError):
            await self.client.execute_async(task, read_action)
        await asyncio.sleep(0)
        self.assertEqual(self.server.requests, original_requests)
        self.assertEqual(self.service.shared_drain_status(), {'admission_closed': True, 'blockers': []})

    async def test_final_seal_detects_synchronous_client_lock_and_reobserves_on_repeat(self):
        self.service.latch_shared_drain()
        self.client._lock.acquire()
        try:
            with self.assertRaises(ScientistAdmissionError):
                self.service.seal_shared_drain_controls()
        finally:
            self.client._lock.release()
        self.service.seal_shared_drain_controls()
        self.client._lock.acquire()
        try:
            with self.assertRaises(ScientistAdmissionError):
                self.service.seal_shared_drain_controls()
            self.assertTrue(self.service.cleanup_controls_closed)
        finally:
            self.client._lock.release()


if __name__ == '__main__':
    unittest.main()

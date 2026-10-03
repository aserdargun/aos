import asyncio
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aos.computer import WorkspaceRuntime
from aos.contracts import AOSFault, Settings, identifier, now
from aos.decision import FixtureDecisionEngine
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.storage import TrajectoryStore
from aos.task_sequence import SequencePlan, SequenceStart


class SyntheticDesktop(WorkspaceRuntime):
    pins = {'image_id': 'synthetic-shared-drain-image'}
    container_id = None

    def perform(self, tool, arguments):
        if tool == 'probe':
            return {'display': True, 'xfce': True, 'note': True, 'vnc': 'RFB synthetic'}
        raise AssertionError('Unexpected synthetic desktop tool')


class WaitingFailureEngine(FixtureDecisionEngine):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(self, state, options):
        self.started.set()
        await self.release.wait()
        raise ValueError('Synthetic task finishes without tool execution')


class SharedDrainSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='synthetic-shared-drain-')
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'store.sqlite')
        self.runtime = SyntheticDesktop(self.settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(self.settings.database)
        self.controller = DesktopController(self.store, self.runtime)
        self.scheduler = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine())

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.runtime.stop()
        self.store.close()
        self.temporary.cleanup()

    def start(self, **options):
        state = self.controller.state()
        return self.scheduler.start(state['lease_id'], state['generation'], **options)

    def seed_task(self, status):
        state = self.controller.state()
        job_id = identifier('job')
        with self.store.connection:
            self.store.insert('desktop_tasks', job_id=job_id, session_id=self.controller.session_id,
                              kind='hello', lease_id=state['lease_id'], generation=state['generation'],
                              status=status, real_model=0, created_at=now(), updated_at=now())
        return job_id

    async def test_latch_only_sets_flag_without_database_or_stop_and_status_holds_lock(self):
        state = self.controller.state()
        trace = []
        self.store.connection.set_trace_callback(lambda statement: trace.append(self.controller.lock._is_owned()))
        with (patch.object(self.scheduler, 'cancel', new_callable=AsyncMock) as cancel,
              patch.object(self.runtime, 'stop') as stop):
            self.assertIsNone(self.scheduler.latch_shared_drain())
            self.assertEqual(trace, [])
            self.assertEqual(self.scheduler.shared_drain_status(), {'admission_closed': True, 'blockers': []})
            self.assertTrue(trace)
            self.assertTrue(all(trace))
            cancel.assert_not_awaited()
            stop.assert_not_called()
        self.store.connection.set_trace_callback(None)
        self.assertEqual(self.controller.state(), state)
        self.assertTrue(self.runtime.status()['running'])
        self.assertIsNone(self.scheduler.latch_shared_drain())

    async def test_busy_latch_remains_closed_after_existing_task_settles(self):
        engine = WaitingFailureEngine()
        self.scheduler.engine = engine
        self.start()
        await asyncio.wait_for(engine.started.wait(), 1)
        active = self.scheduler.task
        self.scheduler.latch_shared_drain()
        status = self.scheduler.shared_drain_status()
        self.assertTrue(status['admission_closed'])
        self.assertIn('reserved', status['blockers'])
        self.assertIn('unfinished_tasks', status['blockers'])
        self.assertFalse(active.cancelled())
        engine.release.set()
        await asyncio.wait_for(asyncio.shield(active), 1)
        self.assertEqual(self.scheduler.shared_drain_status(), {'admission_closed': True, 'blockers': []})
        with self.assertRaises(AOSFault):
            self.start()

    async def test_restart_release_and_direct_false_assignment_never_reopen_latch(self):
        self.assertTrue(self.scheduler.quiesce_for_restart(self.controller.session_id)['quiesced'])
        self.assertFalse(self.scheduler.release_restart_quiesce(self.controller.session_id)['quiesced'])
        self.assertFalse(self.scheduler.restart_quiesced)
        self.assertEqual(self.scheduler.shared_drain_status(), {'admission_closed': False, 'blockers': []})
        self.scheduler.quiesce_for_restart(self.controller.session_id)
        self.scheduler.latch_shared_drain()
        with self.assertRaises(AOSFault):
            self.scheduler.release_restart_quiesce(self.controller.session_id)
        self.scheduler.restart_quiesced = False
        self.assertTrue(self.scheduler.restart_quiesced)
        self.assertEqual(self.scheduler.shared_drain_status(), {'admission_closed': True, 'blockers': []})

    async def test_new_task_resume_private_start_and_sequence_admission_are_blocked(self):
        state = self.controller.state()
        self.scheduler.latch_shared_drain()
        for operation in (
            lambda: self.start(),
            lambda: self.scheduler._start(state['lease_id'], state['generation'], 'hello'),
            lambda: self.scheduler.resume(state['lease_id'], state['generation']),
            lambda: self.scheduler.sequences.start(SequenceStart(
                plan=SequencePlan(kinds=['hello', 'browser_form']),
                lease_id=state['lease_id'], generation=state['generation'])),
        ):
            with self.assertRaises(AOSFault):
                operation()
        self.assertIsNone(self.scheduler.task)
        self.assertIsNone(self.scheduler.sequences.task)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)

    async def test_pending_actual_approval_cannot_be_consumed_directly(self):
        self.start()
        for attempt in range(100):
            approval = self.scheduler.status()['approval']
            if approval:
                break
            await asyncio.sleep(.001)
        else:
            self.fail('Synthetic approval was not produced')
        self.scheduler.latch_shared_drain()
        status = self.scheduler.shared_drain_status()
        self.assertIn('pending_approvals', status['blockers'])
        for accept in (True, False):
            for operation in (self.scheduler.respond, self.scheduler._respond):
                with self.assertRaises(AOSFault):
                    operation(approval['approval_id'], approval['action_sha256'], accept)
        self.assertEqual(self.store.connection.execute(
            'SELECT status FROM desktop_approvals WHERE approval_id=?',
            (approval['approval_id'],)).fetchone()[0], 'pending')
        self.assertFalse(self.scheduler.answer.done())
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_child_approval_delegation_and_automatic_grant_cannot_bypass_latch(self):
        engine = WaitingFailureEngine()
        self.scheduler.engine = engine
        self.start(approve_all=True)
        await asyncio.wait_for(engine.started.wait(), 1)
        self.assertTrue(self.scheduler.grant_current())
        grant = self.scheduler._approval_grant
        self.scheduler.latch_shared_drain()
        self.assertFalse(self.scheduler.grant_current())
        self.assertIs(self.scheduler._approval_grant, grant)
        self.assertIn('approval_grant', self.scheduler.shared_drain_status()['blockers'])
        child = Mock(busy=True)
        with patch.object(self.scheduler, '_parameter_skill_reuse_execution', SimpleNamespace(child_manager=child)):
            with self.assertRaises(AOSFault):
                self.scheduler.respond('synthetic-approval', 'a' * 64, True)
        child.respond.assert_not_called()
        engine.release.set()
        await asyncio.wait_for(asyncio.shield(self.scheduler.task), 1)

    async def test_durable_unfinished_tasks_block_even_without_in_memory_reservation(self):
        job_id = self.seed_task('queued')
        self.scheduler.latch_shared_drain()
        for status in ('queued', 'running', 'waiting_approval', 'waiting_human', 'paused'):
            with self.subTest(status=status), self.store.connection:
                self.store.connection.execute('UPDATE desktop_tasks SET status=? WHERE job_id=?', (status, job_id))
                self.assertIn('unfinished_tasks', self.scheduler.shared_drain_status()['blockers'])
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET status='cancelled' WHERE job_id=?", (job_id,))
        self.assertEqual(self.scheduler.shared_drain_status(), {'admission_closed': True, 'blockers': []})

    async def test_durable_pending_or_approved_approvals_block_terminal_job(self):
        job_id = self.seed_task('succeeded')
        approval_id = identifier('approval')
        with self.store.connection:
            self.store.insert('desktop_approvals', approval_id=approval_id, job_id=job_id,
                              envelope_json='{}', action_sha256='a' * 64, expires_at=1,
                              status='pending', created_at=now(), updated_at=now())
        self.scheduler.latch_shared_drain()
        for status in ('pending', 'approved'):
            with self.store.connection:
                self.store.connection.execute('UPDATE desktop_approvals SET status=? WHERE approval_id=?',
                                              (status, approval_id))
            self.assertIn('pending_approvals', self.scheduler.shared_drain_status()['blockers'])
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_approvals SET status='consumed' WHERE approval_id=?", (approval_id,))
        self.assertEqual(self.scheduler.shared_drain_status(), {'admission_closed': True, 'blockers': []})

    async def test_queued_running_and_uncertain_inputs_block_without_cancellation(self):
        state = self.controller.state()
        input_id = self.controller.enqueue(state['lease_id'], state['generation'])
        self.scheduler.latch_shared_drain()
        for status in ('queued', 'running', 'uncertain'):
            with self.store.connection:
                self.store.connection.execute('UPDATE desktop_inputs SET status=? WHERE input_id=?', (status, input_id))
            self.assertIn('pending_inputs', self.scheduler.shared_drain_status()['blockers'])
            self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_inputs WHERE input_id=?',
                                                         (input_id,)).fetchone()[0], status)
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_inputs SET status='ok' WHERE input_id=?", (input_id,))
        self.assertEqual(self.scheduler.shared_drain_status(), {'admission_closed': True, 'blockers': []})

    async def test_existing_planning_and_adaptation_reservations_are_observed(self):
        self.scheduler.latch_shared_drain()
        for name in ('owned_skill_planning', 'web_goal_planning', 'knowledge_answer'):
            with self.subTest(name=name), patch.object(self.scheduler, name, SimpleNamespace(reserved=True)):
                self.assertIn('reserved', self.scheduler.shared_drain_status()['blockers'])
        learning = SimpleNamespace(adaptation=SimpleNamespace(reserved=True))
        with patch.object(self.scheduler, 'owned_skill_planning', SimpleNamespace(reserved=False, episode_learning=learning)):
            self.assertIn('reserved', self.scheduler.shared_drain_status()['blockers'])

    async def test_new_planning_cannot_begin_after_latch(self):
        self.scheduler.latch_shared_drain()
        state = self.controller.state()
        planner = SimpleNamespace(reserved=False, begin=Mock())
        with (patch.object(self.scheduler, 'owned_skill_planning', planner),
              patch.object(self.scheduler, 'web_goal_planning', planner)):
            with self.assertRaises(ValueError):
                self.scheduler.begin_owned_skill_plan('synthetic goal', state['lease_id'], state['generation'])
            with self.assertRaises(ValueError):
                self.scheduler.begin_web_goal_plan('synthetic goal', state['lease_id'], state['generation'],
                                                   confirm_catalog_sha256='a' * 64)
        planner.begin.assert_not_called()

    async def test_database_failure_returns_no_closed_proof_but_latch_remains(self):
        broken_store = SimpleNamespace(connection=Mock())
        broken_store.connection.execute.side_effect = sqlite3.OperationalError('synthetic unavailable database')
        with patch.object(self.scheduler, 'store', broken_store):
            self.assertIsNone(self.scheduler.latch_shared_drain())
            broken_store.connection.execute.assert_not_called()
            status = self.scheduler.shared_drain_status()
        self.assertEqual(status, {'admission_closed': False, 'blockers': ['status_unavailable']})
        self.assertTrue(self.scheduler.restart_quiesced)
        with self.assertRaises(AOSFault):
            self.start()

    async def test_closed_scheduler_is_not_misreported_as_proven_idle(self):
        self.scheduler.closed = True
        self.scheduler.latch_shared_drain()
        self.assertEqual(self.scheduler.shared_drain_status(),
                         {'admission_closed': True, 'blockers': ['scheduler_closed']})


if __name__ == '__main__':
    unittest.main()

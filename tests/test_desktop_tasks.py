import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import jsonschema

from aos.computer import WorkspaceRuntime
from aos.contracts import AOSFault, HELLO_CONTENT, Phase, REPO_ROOT, Settings
from aos.decision import DeciderEngine, FixtureDecisionEngine
from aos.reusable_decider import ReusableDeciderEngine
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import Approval, DesktopScheduler
from aos.export import export_run
from aos.storage import TrajectoryStore
from aos.supervisor import BonsaiSupervisor


class FixtureDesktop(WorkspaceRuntime):
    pins = {'image_id': 'synthetic-image'}
    container_id = None

    def perform(self, tool, arguments):
        if tool == 'probe':
            return {'display': True, 'xfce': True, 'note': True, 'vnc': 'RFB synthetic'}
        raise AssertionError('Unexpected fixture tool')


class WaitingEngine(FixtureDecisionEngine):
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def decide(self, state, options):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


class DesktopTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'store.sqlite')
        self.runtime = FixtureDesktop(self.settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(self.settings.database)
        self.controller = DesktopController(self.store, self.runtime)
        self.scheduler = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine())

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.runtime.stop()
        self.store.close()
        self.temporary.cleanup()

    def start(self):
        state = self.controller.state()
        return self.scheduler.start(state['lease_id'], state['generation'])

    async def approval(self):
        for attempt in range(100):
            result = self.scheduler.status()['approval']
            if result:
                return result
            await asyncio.sleep(.001)
        self.fail('No approval request produced')

    async def settled(self):
        await asyncio.gather(self.scheduler.task, return_exceptions=True)
        return self.scheduler.status()['jobs'][0]

    def reusable_fixture(self):
        engine = object.__new__(ReusableDeciderEngine)
        engine.cpu_prewarm = False
        engine.gpu_idle = False
        engine.gpu_idle_seconds = 0
        engine.identity = FixtureDecisionEngine.identity
        engine.last_metrics = {}
        engine.decide = AsyncMock(side_effect=FixtureDecisionEngine().decide)
        engine.close = AsyncMock()
        self.scheduler.engine = engine
        return engine

    async def test_reusable_model_released_after_verified_success(self):
        engine = self.reusable_fixture()
        self.start()
        approval = await self.approval()
        engine.close.assert_not_awaited()
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        self.assertEqual((await self.settled())['status'], 'succeeded')
        engine.close.assert_awaited_once()
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)

    async def test_reusable_model_released_on_pause_then_fresh_decision(self):
        engine = self.reusable_fixture()
        self.start()
        previous = await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        engine.close.assert_awaited_once()
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        fresh = await self.approval()
        self.assertNotEqual(previous['action_sha256'], fresh['action_sha256'])
        self.assertEqual(engine.decide.await_count, 2)
        self.scheduler.respond(fresh['approval_id'], fresh['action_sha256'], False)
        self.assertEqual((await self.settled())['status'], 'cancelled')
        self.assertEqual(engine.close.await_count, 2)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_reusable_model_released_after_failure(self):
        engine = self.reusable_fixture()
        engine.decide.side_effect = ValueError('synthetic inference failure')
        self.start()
        self.assertEqual((await self.settled())['status'], 'failed')
        engine.close.assert_awaited_once()
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_approve_is_one_use_bound_and_independently_verified(self):
        self.start()
        approval = await self.approval()
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        schema = json.loads((REPO_ROOT / 'schemas/desktop_approval.schema.json').read_text())
        jsonschema.validate(approval, schema)
        self.assertEqual(Approval.model_validate_json(json.dumps(approval)).action.tool, 'filesystem.write')
        with self.assertRaises(AOSFault):
            self.scheduler.respond(approval['approval_id'], '0' * 64, True)
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        with self.assertRaises(AOSFault):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        job = await self.settled()
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'consumed')
        self.assertEqual(self.store.connection.execute('SELECT kind FROM human_interventions').fetchone()[0], 'approve')
        self.assertEqual(self.store.connection.execute('SELECT result FROM verifications').fetchone()[0], 'passed')
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        exported = export_run(self.store, job['run_id'])
        jsonschema.validate(exported, json.loads((REPO_ROOT / 'schemas/trajectory_export.schema.json').read_text()))
        self.assertEqual(exported['steps'][0]['human_interventions'][0]['kind'], 'approve')
        self.assertNotIn(approval['approval_id'], json.dumps(exported['steps'][0]['human_interventions']))

    async def test_pause_resume_preserves_run_and_observes_before_new_approval(self):
        self.start()
        old = await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertTrue(self.scheduler.paused)
        self.assertFalse(self.scheduler.busy)
        paused = self.store.state(old['action']['run_id'])
        self.assertEqual(paused.phase.value, 'PAUSED')
        self.assertEqual(self.store.connection.execute('SELECT status FROM runs').fetchone()[0], 'paused')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        with self.assertRaises(AOSFault):
            self.scheduler.respond(old['approval_id'], old['action_sha256'], True)
        state = self.controller.control('resume')
        with self.assertRaises(AOSFault):
            self.start()
        with self.assertRaises(AOSFault):
            self.scheduler.resume('stale', state['generation'])
        (self.runtime.root / 'hello.txt').write_text(HELLO_CONTENT)
        self.scheduler.resume(state['lease_id'], state['generation'])
        with self.assertRaises(AOSFault):
            self.scheduler.resume(state['lease_id'], state['generation'])
        fresh = await self.approval()
        for field in ('run_id', 'task_id', 'step_id', 'runtime_id'):
            self.assertEqual(fresh['action'][field], old['action'][field])
        self.assertEqual(fresh['action']['tool'], 'filesystem.read')
        self.assertNotEqual(fresh['action_sha256'], old['action_sha256'])
        self.assertNotEqual(fresh['action']['owner_lease_id'], old['action']['owner_lease_id'])
        self.assertGreater(fresh['action']['state_version'], paused.state_version)
        self.scheduler.respond(fresh['approval_id'], fresh['action_sha256'], True)
        self.assertEqual((await self.settled())['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM decisions').fetchone()[0], 2)

    async def test_pause_drains_model_and_shutdown_cancels_retained_run(self):
        engine = WaitingEngine()
        self.scheduler.engine = engine
        self.start()
        await engine.started.wait()
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertTrue(engine.cancelled.is_set())
        self.assertTrue(self.scheduler.paused)
        await self.scheduler.close()
        self.assertFalse(self.scheduler.reserved)
        self.assertEqual(self.store.connection.execute('SELECT status FROM runs').fetchone()[0], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_reconcile_cancels_paused_run_and_revokes_old_approval(self):
        self.start()
        await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        self.store.reconcile()
        self.assertEqual(self.store.connection.execute('SELECT status FROM runs').fetchone()[0], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_tasks').fetchone()[0], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'revoked')

    async def test_resume_rejects_changed_deployment_or_dead_runtime(self):
        self.start()
        await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        state = self.controller.control('resume')
        original = self.scheduler.engine
        self.scheduler.engine = SimpleNamespace(identity={**original.identity, 'deployment_id': 'changed'})
        with self.assertRaises(AOSFault):
            self.scheduler.resume(state['lease_id'], state['generation'])
        self.scheduler.engine = original
        self.runtime.stop()
        with self.assertRaises(AOSFault):
            self.scheduler.resume(state['lease_id'], state['generation'])

    async def test_pause_and_cancel_before_resumed_coroutine_starts(self):
        self.start()
        old = await self.approval()
        for command in ('pause', 'stop'):
            self.controller.control('pause')
            await self.scheduler.pause()
            state = self.controller.control('resume')
            self.scheduler.resume(state['lease_id'], state['generation'])
            self.controller.control('pause')
            if command == 'pause':
                await self.scheduler.pause()
                self.assertTrue(self.scheduler.paused)
            else:
                await self.scheduler.cancel('stop')
                self.assertFalse(self.scheduler.reserved)
                self.assertEqual(self.store.state(old['action']['run_id']).phase.value, 'CANCELLED')

    async def test_resume_transaction_rolls_back_if_state_changed(self):
        self.start()
        approval = await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        paused = self.store.state(approval['action']['run_id'])
        resumed = paused.advance(Phase.PAUSED, owner='AGENT', owner_lease_id='synthetic-new-lease')
        stale = paused.model_copy(update={'state_version': paused.state_version - 1})
        with self.assertRaises(AOSFault):
            self.store.save_state(stale, resumed, resume=True)
        self.assertEqual(self.store.state(paused.run_id), paused)
        self.assertEqual(self.store.connection.execute('SELECT status FROM runs').fetchone()[0], 'paused')

    async def test_reject_and_expiry_never_execute(self):
        self.start()
        approval = await self.approval()
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        self.assertEqual((await self.settled())['status'], 'cancelled')
        self.assertEqual(self.store.state(self.scheduler.status()['jobs'][0]['run_id']).owner, 'PAUSED')
        self.scheduler.approval_seconds = .01
        self.start()
        approval = await self.approval()
        self.assertEqual((await self.settled())['status'], 'failed')
        with self.assertRaises(AOSFault):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_takeover_revokes_approval_and_requires_fresh_run(self):
        self.start()
        approval = await self.approval()
        previous = self.controller.state()
        self.controller.control('pause')
        await self.scheduler.cancel()
        self.controller.control('take-control')
        with self.assertRaises(AOSFault):
            self.start()
        with self.assertRaises(AOSFault):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        self.controller.control('return-control')
        with self.assertRaises(AOSFault):
            self.scheduler.start(previous['lease_id'], previous['generation'])
        self.start()
        fresh = await self.approval()
        self.assertNotEqual(fresh['action']['owner_lease_id'], approval['action']['owner_lease_id'])
        self.assertNotEqual(fresh['action']['run_id'], approval['action']['run_id'])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_no_concurrent_tasks_queued_inputs_or_pre_start_cancellation(self):
        state = self.controller.state()
        queued = self.controller.enqueue(state['lease_id'], state['generation'])
        self.start()
        with self.assertRaises(AOSFault):
            self.start()
        with self.assertRaises(AOSFault):
            self.controller.execute(queued)
        await self.scheduler.cancel()
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    async def test_cancellation_drains_model_before_return(self):
        engine = WaitingEngine()
        self.scheduler.engine = engine
        self.start()
        await engine.started.wait()
        self.controller.control('pause')
        await self.scheduler.cancel()
        self.assertTrue(engine.cancelled.is_set())
        self.assertEqual(self.store.connection.execute('SELECT status FROM runs').fetchone()[0], 'cancelled')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_approved_action_cannot_cross_changed_lease_or_state(self):
        for change in ['lease', 'state']:
            self.start()
            approval = await self.approval()
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            if change == 'lease':
                self.controller.control('pause')
            else:
                state = self.store.state(approval['action']['run_id'])
                self.store.save_state(state, state.model_copy(update={'state_version': state.state_version + 1}))
            self.assertEqual((await self.settled())['status'], 'failed')
            self.controller.control('resume')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_reconciliation_revokes_pending_and_never_replays(self):
        self.start()
        approval = await self.approval()
        self.store.reconcile()
        with self.assertRaises(AOSFault):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'revoked')
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_authenticated_api_scope_pause_resume_and_takeover(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, self.settings.database, self.scheduler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
            self.assertEqual((await client.get('/api/tasks')).status_code, 401)
            self.assertEqual((await client.post('/api/tasks', json={})).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            state = self.controller.state()
            payload = {'kind': 'hello', 'lease_id': state['lease_id'], 'generation': state['generation']}
            for bad in [{**payload, 'kind': 'shell'}, {**payload, 'path': '/etc/passwd'}, {**payload, 'generation': True}]:
                self.assertEqual((await client.post('/api/tasks', json=bad)).status_code, 400)
            self.assertEqual((await client.post('/api/tasks', json=payload, headers={'Origin': 'http://evil.invalid'})).status_code, 403)
            self.assertEqual((await client.post('/api/tasks', json=payload)).status_code, 200)
            approval = await self.approval()
            response = {'action_sha256': approval['action_sha256'], 'accept': True}
            for bad in [{**response, 'accept': 'true'}, {**response, 'arguments': {'path': '/etc/passwd'}}]:
                self.assertEqual((await client.post('/api/approvals/' + approval['approval_id'], json=bad)).status_code, 400)
            self.assertEqual((await client.post('/api/input', json={'lease_id': state['lease_id'], 'generation': state['generation']})).status_code, 409)
            self.assertEqual((await client.post('/api/control', json={'command': 'pause'})).status_code, 200)
            self.assertTrue((await client.get('/api/tasks')).json()['paused'])
            self.assertEqual((await client.post('/api/input', json={'lease_id': state['lease_id'], 'generation': state['generation']})).status_code, 409)
            self.assertEqual((await client.post('/api/approvals/' + approval['approval_id'], json=response)).status_code, 409)
            self.assertEqual((await client.post('/api/control', json={'command': 'resume'})).status_code, 200)
            fresh = await self.approval()
            self.assertEqual(fresh['action']['run_id'], approval['action']['run_id'])
            self.assertEqual((await client.post('/api/control', json={'command': 'resume'})).status_code, 409)
            self.assertEqual((await client.post('/api/control', json={'command': 'take-control'})).status_code, 200)
            self.assertFalse(self.scheduler.busy)
            self.assertEqual(self.controller.state()['owner'], 'HUMAN')
            self.assertEqual((await client.post('/api/approvals/' + approval['approval_id'], json={'action_sha256': approval['action_sha256'], 'accept': True})).status_code, 409)
            self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_goal_preview_is_authenticated_read_only_and_not_task_authority(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, self.settings.database, self.scheduler)
        before = self.store.connection.total_changes
        control = self.controller.state()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
            payload = {'goal': 'hello görevini hazırla'}
            self.assertEqual((await client.post('/api/tasks/preview', json=payload)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post('/api/tasks/preview', json=payload, headers={'Origin': 'http://evil.invalid'})).status_code, 403)
            for bad in ({}, {'goal': 1}, {'goal': ''}, {'goal': 'x' * 1001}, {**payload, 'execution_authorized': True}):
                self.assertEqual((await client.post('/api/tasks/preview', json=bad)).status_code, 400)
            response = await client.post('/api/tasks/preview', json=payload)
            self.assertEqual(response.status_code, 200)
            report = response.json()
            self.assertEqual(report['task_kind'], 'hello')
            self.assertFalse(report['execution_authorized'])
            self.assertEqual((await client.post('/api/tasks', json=report)).status_code, 400)
            self.assertEqual((await client.post('/api/tasks/preview', json={'goal': 'hello görevini başlatma'})).json()['status'], 'negated')
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual(self.controller.state(), control)
        self.assertEqual(self.scheduler.status()['jobs'], [])
        self.assertIsNone(self.scheduler.task)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_plan_preview_is_authenticated_bounded_and_not_executable(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, self.settings.database, self.scheduler)
        before, control = self.store.connection.total_changes, self.controller.state()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
            payload = {'goal': 'yerel form görevini hazırla'}
            self.assertEqual((await client.post('/api/tasks/plan', json=payload)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            self.assertEqual((await client.post('/api/tasks/plan', json=payload, headers={'Origin': 'http://evil.invalid'})).status_code, 403)
            self.assertEqual((await client.get('/api/tasks/plan')).status_code, 405)
            self.assertEqual((await client.post('/api/tasks/plan?execute=true', json=payload)).status_code, 400)
            for bad in ({}, {'goal': 1}, {'goal': ''}, {'goal': 'x' * 1001}, {**payload, 'plan': {}},
                        {**payload, 'execution_authorized': True}, {**payload, 'lease_id': control['lease_id']}):
                self.assertEqual((await client.post('/api/tasks/plan', json=bad)).status_code, 400)
            report = (await client.post('/api/tasks/plan', json=payload)).json()
            self.assertEqual(report['plan']['task_kind'], 'browser_form')
            self.assertEqual(len(report['plan']['steps']), 8)
            self.assertFalse(report['execution_authorized'])
            self.assertEqual((await client.post('/api/tasks', json=report)).status_code, 400)
            self.assertEqual((await client.post('/api/tasks/plan', json={'goal': 'hello görevini başlatma'})).json()['plan'], None)
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual(self.controller.state(), control)
        self.assertEqual(self.scheduler.status()['jobs'], [])
        self.assertIsNone(self.scheduler.task)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_plan_preview_does_not_change_pending_approval_and_works_without_engine(self):
        origin = 'http://127.0.0.1:8765'
        self.start()
        pending = await self.approval()
        before, control = self.store.connection.total_changes, self.controller.state()
        for scheduler in (self.scheduler, None):
            app = create_console(self.controller, 'synthetic-token', origin, self.root, scheduler=scheduler)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                response = await client.post('/api/tasks/plan', json={'goal': 'görsel save görevini hazırla'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['plan']['task_kind'], 'vision_canvas')
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual(self.controller.state(), control)
        self.assertEqual(self.scheduler.status()['approval'], pending)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_recovery_api_is_authenticated_fixed_source_and_read_only(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, self.root / 'other-trace.sqlite', self.scheduler,
                             recovery_database=self.settings.database)
        self.start()
        await self.approval()
        before = self.store.connection.total_changes
        pending = self.scheduler.status()['approval']['approval_id']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
            self.assertEqual((await client.get('/api/recovery')).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            for query in ('?database=/etc/passwd', '?limit=100', '?resume=true'):
                self.assertEqual((await client.get('/api/recovery' + query)).status_code, 400)
            response = await client.get('/api/recovery')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['totals']['approval_pending'], 1)
            self.assertFalse(response.json()['resume_authorized'])
            self.assertEqual((await client.post('/api/recovery', json={})).status_code, 405)
            with patch('aos.desktop_console.recovery_inventory', side_effect=ValueError('synthetic-private-error')):
                response = await client.get('/api/recovery')
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('synthetic-private-error', response.text)
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual(self.scheduler.status()['approval']['approval_id'], pending)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_recovery_scan_is_off_loop_and_single_flight_even_after_client_cancellation(self):
        from aos.recovery import recovery_inventory

        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root, recovery_database=self.settings.database)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        report = recovery_inventory(self.settings.database)
        def blocked(database):
            entered.set()
            try:
                release.wait(5)
                return report
            finally:
                finished.set()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            with patch('aos.desktop_console.recovery_inventory', side_effect=blocked):
                first = asyncio.create_task(client.get('/api/recovery'))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual((await asyncio.wait_for(client.get('/api/tasks'), 1)).status_code, 200)
                    self.assertEqual((await client.get('/api/recovery')).status_code, 429)
                    first.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await first
                    self.assertEqual((await client.get('/api/recovery')).status_code, 429)
                finally:
                    release.set()
                    await asyncio.to_thread(finished.wait, 2)
                    await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(self.scheduler.status()['jobs'], [])

    async def test_native_worker_creation_cancellation_is_drained(self):
        manifest = self.root / 'synthetic-manifest.json'
        manifest.write_text('{}')
        engine = DeciderEngine(manifest, Path('/synthetic/python'))
        entered, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(pid=99999999, returncode=None, wait=AsyncMock())

        async def spawning(*arguments, **keywords):
            entered.set()
            await release.wait()
            return process

        with patch('aos.decision.asyncio.create_subprocess_exec', side_effect=spawning), patch('aos.decision.os.killpg') as kill:
            from aos.contracts import State, Option
            state = State(task_id='fixture-task', run_id='fixture-run', step_id='fixture-step',
                          runtime_id='fixture-runtime', deployment_id='fixture-deployment', owner_lease_id='fixture-lease')
            task = asyncio.create_task(engine.decide(state, [Option(id='fixture', label='Fixture')]))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_called_once()
            process.wait.assert_awaited_once()

    async def test_bonsai_creation_cancellation_reaps_process_and_key(self):
        manifest = self.root / 'synthetic-bonsai.json'
        manifest.write_text(json.dumps({'server_path': '/synthetic/server', 'model_path': '/synthetic/models',
                                        'weights_file': 'weights', 'projector_file': 'projector',
                                        'context_tokens': 16, 'gpu_layers': 0}))
        supervisor = BonsaiSupervisor(manifest)
        entered, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(pid=99999999, returncode=None, wait=AsyncMock())
        key_paths = []

        async def spawning(*arguments, **keywords):
            key_paths.append(Path(arguments[arguments.index('--api-key-file') + 1]))
            entered.set()
            await release.wait()
            return process

        with patch.object(supervisor, 'verify_pins'), patch('aos.supervisor.asyncio.create_subprocess_exec', side_effect=spawning), patch('aos.supervisor.os.killpg') as kill:
            task = asyncio.create_task(supervisor.plan('Synthetic test', []))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_called_once()
            process.wait.assert_awaited_once()
            self.assertFalse(key_paths[0].exists())

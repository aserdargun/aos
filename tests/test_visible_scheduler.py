import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from aos.browser import BROWSER_EXPECTED, BROWSER_VALUE
from aos.contracts import AOSFault, REPO_ROOT, Settings
from aos.decision import DeciderEngine, FixtureDecisionEngine
from aos.reusable_decider import ReusableDeciderEngine
from aos.desktop import DesktopRuntime
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.learning_events import review_learning_events
from aos.learning_candidate import review_learning_candidates
from aos.storage import TrajectoryStore
from aos.vision import BonsaiVisionSupervisor, FixtureVisionSupervisor, VISION_EXPECTED


class VisibleConfigurationTests(unittest.TestCase):
    def test_visible_vision_requires_both_explicit_capabilities(self):
        for value, browser, supervisor in ((True, False, FixtureVisionSupervisor()), (True, True, None),
                                            (1, True, FixtureVisionSupervisor()), ('desktop', True, FixtureVisionSupervisor())):
            with self.subTest(value=value, browser=browser), self.assertRaises(ValueError):
                DesktopScheduler(SimpleNamespace(), Settings(), FixtureDecisionEngine(),
                                 browser_manifest=Path('synthetic'), desktop_browser=browser,
                                 vision_supervisor=supervisor, desktop_vision=value)

    def test_visible_mode_requires_explicit_browser_configuration(self):
        for value, manifest in ((True, None), ('desktop', Path('synthetic')), (1, Path('synthetic'))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DesktopScheduler(SimpleNamespace(), Settings(), FixtureDecisionEngine(),
                                 browser_manifest=manifest, desktop_browser=value)


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Requires real pinned Docker desktop')
class VisibleSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='visible-scheduler-', dir=REPO_ROOT / 'data')
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'store.sqlite')
        self.desktop = DesktopRuntime(self.settings.workspace, REPO_ROOT / 'models/desktop-manifest.json')
        self.store = TrajectoryStore(self.settings.database)
        await asyncio.to_thread(self.desktop.start)
        self.controller = DesktopController(self.store, self.desktop)
        self.scheduler = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine(),
                                          browser_manifest=REPO_ROOT / 'models/browser-manifest.json', desktop_browser=True,
                                          vision_supervisor=FixtureVisionSupervisor(), desktop_vision=True)

    async def asyncTearDown(self):
        try:
            await self.scheduler.close()
        finally:
            try:
                await asyncio.to_thread(self.desktop.stop)
            finally:
                self.store.close()
                self.temporary.cleanup()

    def start(self, kind='browser_form'):
        state = self.controller.state()
        return self.scheduler.start(state['lease_id'], state['generation'], kind)

    async def approval(self, tool):
        async with asyncio.timeout(120):
            while True:
                status = self.scheduler.status()
                approval = status['approval']
                if approval and approval['action']['tool'] == tool:
                    return approval
                if not status['busy']:
                    self.fail('Task ended before requested approval: ' + json.dumps(status))
                await asyncio.sleep(.05)

    def accept(self, approval):
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)

    async def complete_form(self):
        self.start()
        first = await self.approval('browser.fill')
        self.accept(first)
        second = await self.approval('browser.submit')
        self.accept(second)
        await self.scheduler.task
        return first, second

    async def test_two_approvals_real_desktop_and_retained_independent_result(self):
        self.start()
        first = await self.approval('browser.fill')
        runtime = self.scheduler.active_runtime
        self.assertEqual(self.scheduler.status()['browser_display'], 'desktop')
        self.assertNotEqual(runtime.runtime_id, self.desktop.runtime_id)
        self.assertEqual(runtime.perform('browser.verify', {})['value'], '')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.accept(first)
        second = await self.approval('browser.submit')
        self.assertEqual(runtime.perform('browser.verify', {}), {'value': BROWSER_VALUE, 'receipt': '', 'submissions': 0})
        self.assertNotEqual(first['action_sha256'], second['action_sha256'])
        self.accept(second)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertIs(self.scheduler.completed_runtime, runtime)
        self.assertIsNone(self.scheduler.active_runtime)
        self.assertTrue(runtime.status()['running'])
        self.assertTrue(self.desktop.status()['running'])
        self.assertEqual(runtime.perform('browser.verify', {}), BROWSER_EXPECTED)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(AOSFault):
            self.accept(second)
        await self.scheduler.cancel('take_control')
        self.assertIsNone(self.scheduler.completed_runtime)
        self.assertFalse(runtime.status()['running'])
        self.assertTrue(self.desktop.status()['running'])

    async def test_pause_retains_window_but_resume_requires_new_approval(self):
        self.start()
        self.accept(await self.approval('browser.fill'))
        previous = await self.approval('browser.submit')
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertTrue(self.scheduler.paused)
        self.assertIs(self.scheduler.active_runtime, runtime)
        self.assertTrue(runtime.status()['running'])
        with self.assertRaises(AOSFault):
            self.accept(previous)
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        fresh = await self.approval('browser.submit')
        self.assertEqual(previous['action']['run_id'], fresh['action']['run_id'])
        self.assertNotEqual(previous['action_sha256'], fresh['action_sha256'])
        self.accept(fresh)
        await self.scheduler.task
        self.assertEqual(runtime.perform('browser.verify', {}), BROWSER_EXPECTED)

    async def test_takeover_revokes_pending_input_and_closes_only_browser(self):
        self.start()
        pending = await self.approval('browser.fill')
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.cancel('take_control')
        self.controller.control('take-control')
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        self.assertIsNone(self.scheduler.status()['approval'])
        self.assertFalse(runtime.status()['running'])
        self.assertTrue(self.desktop.status()['running'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        with self.assertRaises(AOSFault):
            self.accept(pending)

    async def test_next_task_releases_previous_visible_result(self):
        await self.complete_form()
        completed = self.scheduler.completed_runtime
        self.start('hello')
        await self.approval('filesystem.write')
        self.assertIsNone(self.scheduler.completed_runtime)
        self.assertFalse(completed.status()['running'])
        self.assertTrue(self.desktop.status()['running'])

    async def test_visible_vision_one_approval_independent_result_and_next_task_cleanup(self):
        self.start('vision_canvas')
        pending = await self.approval('vision.click')
        runtime = self.scheduler.active_runtime
        self.assertEqual(self.scheduler.status()['vision_display'], 'desktop')
        self.assertEqual(runtime.perform('vision.verify', {}), {'selected': '', 'clicks': 0})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.accept(pending)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertIs(self.scheduler.completed_runtime, runtime)
        self.assertEqual(runtime.perform('vision.verify', {}), VISION_EXPECTED)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 1)
        self.assertTrue(runtime.status()['running'])
        self.start()
        await self.approval('browser.fill')
        self.assertFalse(runtime.status()['running'])
        self.assertTrue(self.desktop.status()['running'])

    async def test_visible_vision_pause_requires_fresh_capture_and_takeover_revokes(self):
        self.start('vision_canvas')
        previous = await self.approval('vision.click')
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertIs(self.scheduler.active_runtime, runtime)
        self.assertTrue(runtime.status()['running'])
        with self.assertRaises(AOSFault):
            self.accept(previous)
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        fresh = await self.approval('vision.click')
        self.assertEqual(previous['action']['run_id'], fresh['action']['run_id'])
        self.assertNotEqual(previous['action']['arguments']['capture_id'], fresh['action']['arguments']['capture_id'])
        self.assertNotEqual(previous['action_sha256'], fresh['action_sha256'])
        self.assertEqual(runtime.perform('vision.verify', {}), {'selected': '', 'clicks': 0})
        self.controller.control('pause')
        await self.scheduler.cancel('take_control')
        self.controller.control('take-control')
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        self.assertFalse(runtime.status()['running'])
        self.assertTrue(self.desktop.status()['running'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        with self.assertRaises(AOSFault):
            self.accept(fresh)

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1', 'Explicit real Bonsai and Decider inference opt-in')
    async def test_real_bonsai_decider_visible_capture_and_independent_verification(self):
        self.scheduler.engine = DeciderEngine(REPO_ROOT / 'models/decider-manifest.json',
                                              Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.scheduler.vision_supervisor = BonsaiVisionSupervisor(REPO_ROOT / 'models/bonsai-manifest.json')
        self.start('vision_canvas')
        self.accept(await self.approval('vision.click'))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.status()['jobs'][0]['real_model'])
        self.assertEqual(self.scheduler.completed_runtime.perform('vision.verify', {}), VISION_EXPECTED)
        calls = self.store.connection.execute('SELECT role,status FROM model_calls ORDER BY rowid').fetchall()
        self.assertEqual([tuple(row) for row in calls], [('system2', 'ok'), ('system1', 'ok')])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT training_eligible FROM runs').fetchone()[0], 0)
        review = review_learning_events(self.settings.database, self.scheduler.status()['jobs'][0]['run_id'])
        self.assertEqual(review['event_count'], 2)
        self.assertEqual({event['role']: event['verified_outcome'] for event in review['events']},
                         {'system1': True, 'system2': False})
        system2 = next(event for event in review['events'] if event['role'] == 'system2')
        self.assertIsNotNone(system2['source']['scene_observation_id'])
        verification_id = self.store.connection.execute(
            "SELECT verification_id FROM verifications WHERE result='passed'").fetchone()[0]
        self.assertEqual(system2['source']['downstream_verification_ids'], [verification_id])
        self.assertTrue(all(event['training_ready'] is False for event in review['events']))
        gaps = review_learning_candidates(self.settings.database, self.scheduler.status()['jobs'][0]['run_id'])
        self.assertEqual((gaps['system1_count'], gaps['system2_count']), (1, 1))
        self.assertTrue(gaps['rows'][1]['has_scene_evidence'])
        self.assertTrue(all(row['status'] == 'blocked' and row['training_ready'] is False
                            for row in gaps['rows']))

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1', 'Explicit real Decider inference opt-in')
    async def test_real_decider_visible_form_with_independent_verification(self):
        self.scheduler.engine = DeciderEngine(REPO_ROOT / 'models/decider-manifest.json',
                                              Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        await self.complete_form()
        self.assertTrue(self.scheduler.status()['jobs'][0]['real_model'])
        self.assertEqual(self.scheduler.completed_runtime.perform('browser.verify', {}), BROWSER_EXPECTED)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM model_calls WHERE status='ok' AND role='system1'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM model_calls WHERE role='system2'").fetchone()[0], 0)
        review = review_learning_events(self.settings.database, self.scheduler.status()['jobs'][0]['run_id'])
        self.assertEqual(review['event_count'], 2)
        self.assertTrue(all(event['role'] == 'system1' and event['verified_outcome']
                            and event['training_ready'] is False for event in review['events']))

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real CPU-ready browser timing opt-in')
    async def test_real_decider_cpu_ready_form_baseline(self):
        engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))),
            cpu_prewarm=True)
        self.scheduler.engine = engine
        engine.prewarm_idle()
        async with asyncio.timeout(30):
            while engine.prewarm_status()['state'] != 'ready':
                await asyncio.sleep(.02)
        started = time.perf_counter()
        self.start()
        first = await self.approval('browser.fill')
        first_approval_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.accept(first)
        second = await self.approval('browser.submit')
        self.accept(second)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE status='ok' AND role='system1'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system2'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 2)
        self.assertEqual(self.scheduler.completed_runtime.perform('browser.verify', {}), BROWSER_EXPECTED)
        print('CPU-ready first approval:', round(first_approval_ms, 3))

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1', 'Explicit reusable real Decider inference opt-in')
    async def test_reusable_real_decider_two_fresh_decisions_and_job_release(self):
        engine = ReusableDeciderEngine(REPO_ROOT / 'models/decider-manifest.json',
                                      Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.scheduler.engine = engine
        self.start()
        first = await self.approval('browser.fill')
        process = engine.process
        self.assertFalse(engine.last_metrics['reused'])
        self.accept(first)
        second = await self.approval('browser.submit')
        self.assertIs(engine.process, process)
        self.assertTrue(engine.last_metrics['reused'])
        self.assertEqual(engine.last_metrics['load_ms'], 0)
        self.assertNotEqual(first['action_sha256'], second['action_sha256'])
        self.accept(second)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler.completed_runtime.perform('browser.verify', {}), BROWSER_EXPECTED)
        self.assertIsNone(engine.process)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM model_calls WHERE status='ok' AND role='system1'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 2)
        self.scheduler.vision_supervisor = BonsaiVisionSupervisor(REPO_ROOT / 'models/bonsai-manifest.json')
        self.start('vision_canvas')
        self.accept(await self.approval('vision.click'))
        self.assertFalse(engine.last_metrics['reused'])
        self.assertTrue(engine.last_metrics['prepared_cpu'])
        self.assertFalse(engine.preparation_metrics['cuda_initialized'])
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler.completed_runtime.perform('vision.verify', {}), VISION_EXPECTED)
        self.assertIsNone(engine.process)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM model_calls WHERE status='ok' AND role='system2'").fetchone()[0], 1)

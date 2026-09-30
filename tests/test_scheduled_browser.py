import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from aos.browser import BROWSER_VALUE
from aos.contracts import AOSFault, REPO_ROOT, Settings
from aos.decision import FixtureDecisionEngine
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.storage import TrajectoryStore
from aos.vision import FixtureVisionSupervisor, VISION_SCOPE
from test_desktop_tasks import FixtureDesktop


class ScheduledMigrationTests(unittest.TestCase):
    def test_cancel_during_browser_startup_waits_for_owned_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        instances = []

        class StartingBrowser:
            def __init__(self, manifest):
                self.running = False
                instances.append(self)

            def start(self):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Fixture startup release timed out')
                self.running = True

            def stop(self):
                self.running = False

        async def exercise(root):
            settings = Settings(workspace=root / 'workspace', database=root / 'store.sqlite')
            store = TrajectoryStore(settings.database)
            desktop = FixtureDesktop(settings.workspace)
            desktop.start()
            controller = DesktopController(store, desktop)
            scheduler = DesktopScheduler(controller, settings, FixtureDecisionEngine(), browser_manifest=Path('synthetic'))
            try:
                with patch('aos.desktop_tasks.BrowserRuntime', StartingBrowser):
                    state = controller.state()
                    scheduler.start(state['lease_id'], state['generation'], 'browser_form')
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    controller.control('pause')
                    cancelling = asyncio.create_task(scheduler.cancel())
                    await asyncio.sleep(.01)
                    self.assertFalse(cancelling.done())
                    release.set()
                    await cancelling
                self.assertFalse(instances[0].running)
                self.assertEqual(scheduler.status()['jobs'][0]['status'], 'cancelled')
                self.assertEqual(store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
            finally:
                release.set()
                await scheduler.close()
                desktop.stop()
                store.close()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(exercise(Path(temporary)))

    def test_upgrade_preserves_existing_jobs_approvals_and_foreign_keys(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        migrations = sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))
        for migration in migrations[:5]:
            connection.executescript(migration.read_text())
        connection.execute("INSERT INTO desktop_sessions VALUES('session','runtime','image','AGENT','lease',0,'running','synthetic','synthetic')")
        connection.execute("INSERT INTO desktop_tasks VALUES('job','session',NULL,'hello','lease',0,'waiting_approval',0,'synthetic','synthetic')")
        connection.execute("INSERT INTO desktop_approvals VALUES('approval','job','{}',?,123,'pending','synthetic','synthetic')", ('0' * 64,))
        connection.commit()
        connection.executescript(migrations[5].read_text())
        connection.executescript(migrations[6].read_text())
        self.assertEqual(connection.execute('SELECT job_id,kind,status,runtime_id FROM desktop_tasks').fetchall(), [('job', 'hello', 'waiting_approval', None)])
        self.assertEqual(connection.execute('SELECT approval_id,status FROM desktop_approvals').fetchall(), [('approval', 'pending')])
        self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO desktop_approvals VALUES('second','job','{}',?,123,'pending','synthetic','synthetic')", ('1' * 64,))
        connection.execute("UPDATE desktop_approvals SET status='consumed'")
        connection.execute("INSERT INTO desktop_approvals VALUES('second','job','{}',?,123,'pending','synthetic','synthetic')", ('1' * 64,))
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM desktop_tasks WHERE job_id='job'")
        connection.execute("UPDATE desktop_tasks SET status='paused'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO desktop_tasks VALUES('another','session',NULL,'hello','lease',0,'queued',0,'synthetic','synthetic',NULL)")


@unittest.skipUnless(os.environ.get('AOS_BROWSER_TESTS') == '1', 'Requires pinned real isolated Chromium')
class ScheduledBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(workspace=root / 'workspace', database=root / 'store.sqlite')
        self.store = TrajectoryStore(self.settings.database)
        self.desktop = FixtureDesktop(self.settings.workspace)
        self.desktop.start()
        self.controller = DesktopController(self.store, self.desktop)
        self.scheduler = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine(),
                                          browser_manifest=REPO_ROOT / 'models/browser-manifest.json',
                                          vision_supervisor=FixtureVisionSupervisor())

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.desktop.stop()
        self.store.close()
        self.temporary.cleanup()

    def start(self, kind):
        state = self.controller.state()
        return self.scheduler.start(state['lease_id'], state['generation'], kind)

    async def approval(self, previous=None):
        async with asyncio.timeout(30):
            while True:
                result = self.scheduler.status()['approval']
                if result and result['approval_id'] != previous:
                    return result
                if not self.scheduler.busy:
                    self.fail(str(self.scheduler.status()))
                await asyncio.sleep(.01)

    def answer(self, approval, accept=True):
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], accept)

    async def settled(self):
        await asyncio.gather(self.scheduler.task, return_exceptions=True)
        return self.scheduler.status()['jobs'][0]

    async def test_browser_requires_two_distinct_approvals_and_closes_runtime(self):
        self.start('browser_form')
        first = await self.approval()
        runtime = self.scheduler.active_runtime
        worker_pid = runtime.process.pid
        self.assertNotEqual(first['action']['runtime_id'], self.desktop.runtime_id)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.answer(first)
        second = await self.approval(first['approval_id'])
        self.assertEqual(second['action']['tool'], 'browser.submit')
        self.assertNotEqual(first['action_sha256'], second['action_sha256'])
        with self.assertRaises(AOSFault):
            self.answer(first)
        self.assertEqual(runtime.perform('browser.verify', {}), {'value': BROWSER_VALUE, 'receipt': '', 'submissions': 0})
        self.answer(second)
        self.assertEqual((await self.settled())['status'], 'succeeded')
        self.assertEqual([row[0] for row in self.store.connection.execute('SELECT status FROM desktop_approvals')], ['consumed', 'consumed'])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 2)
        self.assertIsNone(runtime.process)
        self.assertFalse(Path(f'/proc/{worker_pid}').exists())

    async def test_pause_after_fill_resumes_same_browser_without_replaying_fill(self):
        self.start('browser_form')
        first = await self.approval()
        self.answer(first)
        old = await self.approval(first['approval_id'])
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertTrue(self.scheduler.paused)
        self.assertTrue(runtime.status()['running'])
        with self.assertRaises(AOSFault):
            self.answer(old)
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        fresh = await self.approval()
        self.assertEqual(fresh['action']['run_id'], old['action']['run_id'])
        self.assertEqual(fresh['action']['runtime_id'], old['action']['runtime_id'])
        self.assertEqual(fresh['action']['tool'], 'browser.submit')
        self.assertNotEqual(fresh['action_sha256'], old['action_sha256'])
        self.answer(fresh)
        self.assertEqual((await self.settled())['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='browser.fill'").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='browser.submit'").fetchone()[0], 1)
        self.assertIsNone(runtime.process)

    async def test_vision_resume_recaptures_and_paused_takeover_reaps_runtime(self):
        self.start('vision_canvas')
        old = await self.approval()
        old_state = self.store.state(old['action']['run_id'])
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.pause()
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        fresh = await self.approval()
        fresh_state = self.store.state(fresh['action']['run_id'])
        self.assertEqual(fresh_state.run_id, old_state.run_id)
        self.assertNotEqual(fresh_state.capture_id, old_state.capture_id)
        self.assertNotEqual(fresh['action_sha256'], old['action_sha256'])
        with self.assertRaises(AOSFault):
            self.answer(old)
        self.controller.control('pause')
        await self.scheduler.pause()
        await self.scheduler.cancel('take_control')
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        self.assertIsNone(runtime.process)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_takeover_between_fill_and_submit_does_not_replay(self):
        self.start('browser_form')
        first = await self.approval()
        self.answer(first)
        second = await self.approval(first['approval_id'])
        runtime_id = second['action']['runtime_id']
        self.controller.control('pause')
        await self.scheduler.cancel('take_control')
        self.controller.control('take-control')
        self.assertIsNone(self.scheduler.active_runtime)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='browser.submit'").fetchone()[0], 0)
        with self.assertRaises(AOSFault):
            self.answer(second)
        self.controller.control('return-control')
        self.start('browser_form')
        fresh = await self.approval()
        self.assertEqual(fresh['action']['tool'], 'browser.fill')
        self.assertNotEqual(fresh['action']['runtime_id'], runtime_id)
        self.assertNotEqual(fresh['action']['run_id'], first['action']['run_id'])

    async def test_vision_fixture_approval_verification_and_capture_staleness(self):
        self.start('vision_canvas')
        first = await self.approval()
        self.assertEqual(first['action']['tool'], 'vision.click')
        self.assertEqual(self.scheduler.active_runtime.perform('vision.verify', {}), {'selected': '', 'clicks': 0})
        self.answer(first)
        self.assertEqual((await self.settled())['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.start('vision_canvas')
        stale = await self.approval()
        self.scheduler.active_runtime.read(VISION_SCOPE)
        self.answer(stale)
        self.assertEqual((await self.settled())['status'], 'failed')
        self.assertEqual(self.store.connection.execute("SELECT error_code FROM actions ORDER BY rowid DESC LIMIT 1").fetchone()[0], 'UI_CHANGED')

    async def test_reject_second_action_preserves_only_verified_fill(self):
        self.start('browser_form')
        first = await self.approval()
        self.answer(first)
        second = await self.approval(first['approval_id'])
        self.answer(second, False)
        job = await self.settled()
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='browser.submit'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 1)

    async def test_vision_model_wait_is_cancelled_and_browser_closed(self):
        started = asyncio.Event()

        class WaitingVision(FixtureVisionSupervisor):
            async def describe(self, capture, state_version):
                started.set()
                await asyncio.Event().wait()

        self.scheduler.vision_supervisor = WaitingVision()
        self.start('vision_canvas')
        await asyncio.wait_for(started.wait(), 30)
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.cancel()
        self.assertIsNone(runtime.process)
        self.assertEqual(self.store.connection.execute('SELECT status FROM runs').fetchone()[0], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

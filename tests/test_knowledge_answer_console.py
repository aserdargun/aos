import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import tempfile
import unittest

import httpx

from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.storage import TrajectoryStore
from test_desktop_tasks import FixtureDesktop
from test_knowledge_answer import FixtureAnswerer, SCOPE, TEXT


class KnowledgeAnswerConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'store.sqlite')
        self.runtime = FixtureDesktop(self.settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(self.settings.database)
        self.controller = DesktopController(self.store, self.runtime)
        self.scheduler = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine())
        self.answerer = FixtureAnswerer()
        self.app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
            scheduler=self.scheduler, recovery_database=self.settings.database,
            knowledge_root=self.root / 'knowledge', knowledge_answerer=self.answerer,
            web_profiles_root=self.root / 'profiles')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver',
                                      headers={'Origin': 'http://testserver'})
        await self.client.post('/api/login', json={'token': 'synthetic-token'})
        self.authority = {key: self.controller.state()[key] for key in ('lease_id', 'generation')}
        publication = self.scheduler.knowledge_answer.store.publish_preview(scope=SCOPE, source_id='manual',
            title='Synthetic password manual', text=TEXT, previous_sha256=None,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        source = self.scheduler.knowledge_answer.store.publish(preview=publication,
            confirm_sha256=publication['preview_sha256'])
        review = self.scheduler.knowledge_answer.store.review_preview(scope=SCOPE,
            document_sha256=source['document_sha256'], decision='accept')
        self.scheduler.knowledge_answer.store.review(preview=review, confirm_sha256=review['preview_sha256'])

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.scheduler.close()
        self.runtime.stop()
        self.store.close()
        self.temporary.cleanup()

    async def request(self, operation, payload):
        return await self.client.post('/api/knowledge-answer/' + operation, json=payload)

    async def preview(self):
        response = await self.request('preview', {'schema_version': '1.0', 'scope': SCOPE,
            'question': 'password reset', 'top_k': 4, 'context_chars': 4096} | self.authority)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def begin(self, preview, **changes):
        return await self.request('start', {'schema_version': '1.0', 'preview': preview,
            'confirm_sha256': preview['confirm_sha256'], 'consent': True} | self.authority | changes)

    async def test_authenticated_answer_source_hash_and_historical_report(self):
        self.assertTrue((await self.client.get('/api/tasks')).json()['knowledge_answer_available'])
        preview = await self.preview()
        self.assertEqual(self.answerer.calls, 0)
        response = await self.begin(preview)
        self.assertEqual(response.status_code, 200, response.text)
        answer_id = response.json()['answer_id']
        await self.scheduler.knowledge_answer.task
        report = await self.request('report', {'schema_version': '1.0', 'answer_id': answer_id})
        self.assertEqual(report.status_code, 200, report.text)
        result = report.json()
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['real_model'])
        self.assertTrue(result['historical_binding_verified'])
        self.assertTrue(result['current_source_valid'])
        self.assertFalse(result['semantic_relevance_verified'])
        self.assertEqual(result['model_response']['quotes'][0]['text'], self.answerer.response['quotes'][0]['text'])
        self.assertEqual(self.scheduler.status()['jobs'], [])
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())

    async def test_separate_consent_exact_body_and_authenticated_boundaries(self):
        preview = await self.preview()
        for consent in (False, 1, 'true'):
            response = await self.begin(preview, consent=consent)
            self.assertIn(response.status_code, (400, 409))
        response = await self.client.post('/api/knowledge-answer/preview', content='{"schema_version":"1.0","schema_version":"1.0"}')
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.post('/api/knowledge-answer/status?other=1', json={
            'schema_version': '1.0', 'answer_id': None})).status_code, 400)
        self.assertEqual(self.answerer.calls, 0)
        await self.client.post('/api/logout')
        self.assertEqual((await self.request('status', {'schema_version': '1.0', 'answer_id': None})).status_code, 401)

    async def test_model_lane_reserves_tasks_restart_and_cancel_releases(self):
        self.answerer.release = asyncio.Event()
        response = await self.begin(await self.preview())
        self.assertEqual(response.status_code, 200, response.text)
        answer_id = response.json()['answer_id']
        self.assertTrue(self.scheduler.reserved)
        with self.assertRaises(Exception):
            self.scheduler.start(**self.authority)
        with self.assertRaises(Exception):
            self.scheduler.quiesce_for_restart(self.controller.session_id)
        self.assertEqual((await self.request('cancel', {'schema_version': '1.0',
            'answer_id': 'answer-' + '0' * 32})).status_code, 409)
        cancelled = await self.request('cancel', {'schema_version': '1.0', 'answer_id': answer_id})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()['status'], 'cancelled')
        self.assertFalse(self.scheduler.reserved)
        self.assertEqual(self.scheduler.status()['jobs'], [])

    async def test_takeover_cancels_model_and_stale_preview_never_replays(self):
        self.answerer.release = asyncio.Event()
        preview = await self.preview()
        response = await self.begin(preview)
        self.assertEqual(response.status_code, 200, response.text)
        await self.answerer.started.wait()
        self.controller.control('take-control')
        await self.scheduler.cancel('take-control')
        self.assertTrue(self.answerer.cleaned)
        self.assertEqual(self.scheduler.knowledge_answer.status()['status'], 'cancelled')
        self.assertEqual((await self.begin(preview)).status_code, 409)
        self.assertFalse(self.scheduler.reserved)


@unittest.skipUnless(__import__('os').environ.get('AOS_UI_TESTS') == '1',
                     'Opt-in actual Chromium UI over isolated authenticated fixture backend; no real model')
class KnowledgeAnswerRenderedTests(KnowledgeAnswerConsoleTests):
    async def test_rendered_separate_consent_fixture_quotes_and_stale_scope(self):
        import uvicorn
        from playwright.async_api import async_playwright, expect
        from aos.contracts import REPO_ROOT

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(32)
        listener.setblocking(False)
        origin = 'http://127.0.0.1:' + str(listener.getsockname()[1])
        self.app = create_console(self.controller, 'synthetic-token', origin, self.root,
            scheduler=self.scheduler, recovery_database=self.settings.database,
            knowledge_root=self.root / 'knowledge', web_profiles_root=self.root / 'profiles')
        server = uvicorn.Server(uvicorn.Config(self.app, log_level='error', lifespan='off', ws='none'))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for attempt in range(100):
                if server.started:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            async with async_playwright() as playwright:
                browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
                browser = await playwright.chromium.launch(executable_path=str(browser_path))
                try:
                    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors, task_writes = [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: task_writes.append(request.url)
                            if request.method == 'POST' and '/api/tasks/' in request.url else None)
                    await page.goto(origin + '/ui/')
                    await page.get_by_label('Local session token', exact=True).fill('synthetic-token')
                    await page.get_by_role('button', name='Sign in', exact=True).click()
                    await expect(page.get_by_test_id('development-status')).to_be_visible()
                    await page.get_by_role('button', name='Knowledge', exact=True).click()
                    for field, value in [('application', SCOPE['application_id']), ('tenant', SCOPE['tenant_id']), ('role', SCOPE['account_role'])]:
                        await page.get_by_test_id('knowledge-' + field).fill(value)
                    await page.get_by_test_id('knowledge-answer-question').fill('password reset')
                    await page.get_by_test_id('knowledge-answer-preview').click()
                    await expect(page.get_by_test_id('knowledge-answer-preview-result')).to_be_visible()
                    await expect(page.get_by_test_id('knowledge-answer-start')).to_be_disabled()
                    await page.get_by_test_id('knowledge-answer-consent').check()
                    await expect(page.get_by_test_id('knowledge-answer-start')).to_be_disabled()
                    confirmation = await page.get_by_test_id('knowledge-answer-hash').inner_text()
                    await page.get_by_test_id('knowledge-answer-confirm').fill(confirmation)
                    await page.get_by_test_id('knowledge-answer-start').click()
                    await expect(page.get_by_test_id('knowledge-answer-report')).to_be_visible(timeout=10000)
                    await expect(page.get_by_test_id('knowledge-answer-report')).to_contain_text('Fixture response')
                    await expect(page.get_by_test_id('knowledge-answer-quote')).to_contain_text('open Settings and select Reset password.')
                    await page.screenshot(path='/tmp/aos-knowledge-answer-en.png', full_page=True)
                    await page.get_by_role('button', name='Türkçe', exact=True).click()
                    await page.set_viewport_size({'width': 390, 'height': 844})
                    await expect(page.get_by_test_id('knowledge-answer-report')).to_contain_text('Fixture yanıt')
                    self.assertLessEqual(await page.evaluate('document.documentElement.scrollWidth'), 390)
                    await page.screenshot(path='/tmp/aos-knowledge-answer-mobile-tr.png', full_page=True)
                    for mutation in ('real_model', 'model_response', 'bundle'):
                        async def corrupt(route, mutation=mutation):
                            response = await route.fetch()
                            payload = copy.deepcopy(await response.json())
                            if mutation == 'real_model':
                                payload['real_model'] = True
                            elif mutation == 'model_response':
                                payload['model_response']['quotes'][0]['text'] = 'Synthetic password reset instructions'
                            else:
                                payload['bundle']['model_request']['unexpected'] = True
                            await route.fulfill(response=response, json=payload)
                        await page.route('**/api/knowledge-answer/report', corrupt)
                        await page.get_by_test_id('knowledge-answer-question').fill('password reset ' + mutation)
                        await page.get_by_test_id('knowledge-answer-preview').click()
                        await expect(page.get_by_test_id('knowledge-answer-preview-result')).to_be_visible()
                        await page.get_by_test_id('knowledge-answer-consent').check()
                        await page.get_by_test_id('knowledge-answer-confirm').fill(await page.get_by_test_id('knowledge-answer-hash').inner_text())
                        await page.get_by_test_id('knowledge-answer-start').click()
                        await expect(page.get_by_test_id('knowledge-answer-error')).to_be_visible(timeout=10000)
                        await expect(page.get_by_test_id('knowledge-answer-report')).to_have_count(0)
                        await page.unroute('**/api/knowledge-answer/report', corrupt)
                    await page.get_by_test_id('knowledge-tenant').fill('other-tenant')
                    await expect(page.get_by_test_id('knowledge-answer-report')).to_have_count(0)
                    self.assertFalse(await page.get_by_test_id('knowledge-answer-consent').count())
                    self.assertEqual(errors, [])
                    self.assertEqual(task_writes, [])
                    self.assertEqual(self.scheduler.status()['jobs'], [])
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await running
            listener.close()

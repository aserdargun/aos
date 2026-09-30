import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from aos.contracts import REPO_ROOT
from aos.desktop_console import create_console
from aos.goal_plan import preview_goal_plan
from aos.recovery_session import inspect_session
import test_recovery_session as session_tests


class ParallelAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = session_tests.SessionInspectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.origin = 'http://127.0.0.1:8999'
        self.app = create_console(self.fixture.controller, 'synthetic-token', self.origin, REPO_ROOT / 'computer',
                                  trajectory_database=self.fixture.root / 'not-the-runtime-db.sqlite')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin,
                                       headers={'Origin': self.origin})

    async def asyncTearDown(self):
        await self.client.aclose()

    async def login(self):
        response = await self.client.post('/api/login', json={'token': 'synthetic-token'})
        self.assertEqual(response.status_code, 200)

    async def test_both_endpoints_require_authentication_and_origin_boundary(self):
        self.assertEqual((await self.client.get('/api/session/binding')).status_code, 401)
        self.assertEqual((await self.client.post('/api/tasks/compound-plan', json={'goal': 'hello görevini hazırla'})).status_code, 401)
        await self.login()
        self.assertEqual((await self.client.get('/api/session/binding', headers={'Host': 'evil.test'})).status_code, 403)
        self.assertEqual((await self.client.post('/api/tasks/compound-plan', json={'goal': 'hello görevini hazırla'},
                                               headers={'Origin': 'http://evil.test'})).status_code, 403)

    async def test_compound_preview_is_exact_read_only_and_does_not_echo_private_input(self):
        await self.login()
        before = self.fixture.store.connection.total_changes
        for goal in ('önce hello görevini hazırla; sonra yerel form görevini hazırla',
                     'hello görevini başlatma', 'private-secret unsupported task'):
            response = await self.client.post('/api/tasks/compound-plan', json={'goal': goal})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), preview_goal_plan(goal).model_dump())
            self.assertFalse(response.json()['execution_authorized'])
        self.assertNotIn('private-secret', response.text)
        self.assertEqual(self.fixture.store.connection.total_changes, before)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)
        self.assertFalse((self.fixture.runtime.root / 'hello.txt').exists())

    async def test_compound_endpoint_rejects_extra_fields_query_invalid_body_and_size(self):
        await self.login()
        for payload in ({'goal': 'hello', 'execute': True}, {'goal': None}, {'goal': []}, {}, {'goal': ''}, []):
            response = await self.client.post('/api/tasks/compound-plan', json=payload)
            self.assertEqual(response.status_code, 400)
        response = await self.client.post('/api/tasks/compound-plan?execute=true', json={'goal': 'hello görevini hazırla'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.post('/api/tasks/compound-plan', content='x' * 4097)).status_code, 413)

    async def test_binding_uses_main_database_host_paths_and_never_queries_live_docker(self):
        await self.login()
        before = self.fixture.store.connection.total_changes
        with patch('aos.recovery_lifecycle.observe_container') as docker:
            response = await self.client.get('/api/session/binding')
        docker.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['lifecycle']['owner_observation'], 'same_process')
        self.assertEqual(response.json()['lifecycle']['container_observation'], 'not_queried')
        self.assertIsNone(response.json()['job'])
        self.assertFalse(response.json()['lease_restored'])
        self.assertEqual(self.fixture.store.connection.total_changes, before)
        for private in (str(self.fixture.root), self.fixture.controller.session_id, self.fixture.runtime.runtime_id,
                        self.fixture.controller.state()['lease_id']):
            self.assertNotIn(private, response.text)
        self.assertFalse((self.fixture.root / 'not-the-runtime-db.sqlite').exists())

    async def test_binding_rejects_user_source_and_old_missing_records(self):
        await self.login()
        with patch('aos.desktop_console.inspect_session') as inspect:
            for query in ('workspace=/etc', 'session_id=other', 'job_id=other', 'database=other'):
                self.assertEqual((await self.client.get('/api/session/binding?' + query)).status_code, 400)
        inspect.assert_not_called()
        self.fixture.mutate('DELETE FROM desktop_events')
        response = await self.client.get('/api/session/binding')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': 'Session binding unavailable'})

    async def test_fixture_runtime_and_inspection_failures_are_generic(self):
        await self.login()
        with patch.object(self.fixture.controller, 'runtime', object()):
            self.assertEqual((await self.client.get('/api/session/binding')).status_code, 409)
        for error in (ValueError('private-secret'), OSError('private-secret'), sqlite3.DatabaseError('private-secret')):
            with patch('aos.desktop_console.inspect_session', side_effect=error):
                response = await self.client.get('/api/session/binding')
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('private-secret', response.text)

    async def test_busy_inspection_is_bounded_and_control_remains_available(self):
        await self.login()
        started, release = threading.Event(), threading.Event()

        def held(**arguments):
            report = inspect_session(**arguments)
            started.set()
            if not release.wait(5):
                raise ValueError('synthetic wait deadline')
            return report

        with patch('aos.desktop_console.inspect_session', side_effect=held):
            request = asyncio.create_task(self.client.get('/api/session/binding'))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                self.assertEqual((await self.client.get('/api/session/binding')).status_code, 429)
                response = await asyncio.wait_for(self.client.post('/api/control', json={'command': 'pause'}), 2)
                self.assertEqual(response.status_code, 200)
            finally:
                release.set()
            self.assertEqual((await request).status_code, 409)
        self.assertEqual((await self.client.get('/api/session/binding')).status_code, 200)


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1', 'Requires real Docker and built UI')
class ParallelUITests(unittest.TestCase):
    def test_compound_preview_and_session_binding_are_explicit_read_only_and_clear_stale_results(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='parallel-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, requests = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: requests.append(request.url))
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Plan', exact=True).click()
                    result = page.get_by_test_id('compound-plan-result')
                    field = page.get_by_label('Birleşik Türkçe görev', exact=True)
                    submit = page.get_by_role('button', name='Birleşimi önizle', exact=True)
                    expect(submit).to_be_disabled()
                    self.assertFalse(any(url.endswith('/api/tasks/compound-plan') for url in requests))
                    goal = 'önce hello görevini hazırla; sonra yerel form görevini hazırla; sonra görsel save görevini hazırla'
                    field.fill(goal)
                    expect(result).to_have_count(0)
                    submit.click()
                    expect(page.get_by_test_id('compound-plan-task')).to_have_count(3)
                    expect(result).to_contain_text('hiçbir görev seçilmedi veya başlatılmadı')
                    page.screenshot(path='/tmp/aos-compound-plan-desktop.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-compound-plan-mobile.png', full_page=True)
                    field.fill('hello görevini başlatma')
                    expect(result).to_have_count(0)
                    submit.click()
                    expect(result).to_contain_text('Olumsuzluk veya kontrol isteği')
                    field.fill('hello görevini hazırla ve özel verileri yükle')
                    submit.click()
                    expect(result).to_contain_text('Bu birleşim desteklenmiyor')
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    page.get_by_role('button', name='Kurtarma', exact=True).click()
                    binding = page.get_by_test_id('session-binding-report')
                    expect(binding).to_have_count(0)
                    self.assertFalse(any(url.endswith('/api/session/binding') for url in requests))
                    page.get_by_role('button', name='Oturum bağını oku', exact=True).click()
                    expect(binding).to_contain_text('Kalıcı oturum bağı eşleşti')
                    expect(binding).to_contain_text('Aynı host süreci gözlendi')
                    expect(binding).to_contain_text('Sorgulanmadı')
                    page.screenshot(path='/tmp/aos-session-binding-desktop.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-session-binding-mobile.png', full_page=True)
                    page.get_by_role('button', name='Duraklat', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('PAUSED')
                    expect(binding).to_have_count(0)
                    page.get_by_role('button', name='Oturum bağını oku', exact=True).click()
                    expect(binding).to_contain_text('Kalıcı oturum bağı eşleşti')
                    held_binding = []
                    saved_binding = client.get('/api/session/binding').json()
                    page.route('**/api/session/binding', lambda route: held_binding.append(route))
                    page.get_by_role('button', name='Oturum bağını oku', exact=True).click()
                    expect(page.get_by_role('button', name='Oturum bağı okunuyor…', exact=True)).to_be_disabled()
                    expect(page.get_by_role('button', name='Yeniden başlat', exact=True)).to_be_enabled()
                    page.get_by_role('button', name='Yeniden başlat', exact=True).click()
                    expect(page.get_by_role('button', name='Oturum bağını oku', exact=True)).to_be_enabled()
                    self.assertEqual(len(held_binding), 1)
                    held_binding.pop().fulfill(json=saved_binding)
                    expect(binding).to_have_count(0)
                    page.unroute('**/api/session/binding')
                    page.get_by_role('button', name='Oturum bağını oku', exact=True).click()
                    expect(binding).to_contain_text('Kalıcı oturum bağı eşleşti')
                    page.get_by_role('button', name='Plan', exact=True).click()
                    expect(page.get_by_test_id('compound-plan-result')).to_have_count(0)
                    held_plan = []
                    page.route('**/api/tasks/compound-plan', lambda route: held_plan.append(route))
                    field.fill(goal)
                    submit.click()
                    expect(page.get_by_role('button', name='Birleşim okunuyor…', exact=True)).to_be_disabled()
                    field.fill('hello görevini başlatma')
                    self.assertEqual(len(held_plan), 1)
                    held_plan.pop().fulfill(json=preview_goal_plan(goal).model_dump())
                    expect(page.get_by_test_id('compound-plan-result')).to_have_count(0)
                    field.fill(goal)
                    submit.click()
                    expect(page.get_by_role('button', name='Birleşim okunuyor…', exact=True)).to_be_disabled()
                    page.get_by_role('button', name='Kurtarma', exact=True).click()
                    held_plan.pop().fulfill(json=preview_goal_plan(goal).model_dump())
                    expect(page.get_by_test_id('compound-plan-panel')).to_have_count(0)
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

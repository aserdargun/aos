import asyncio
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import jsonschema
from pydantic import ValidationError

from aos.contracts import HELLO_CONTENT, REPO_ROOT
from aos.desktop_console import create_console
from aos.goal_plan import CompoundSequenceStart, preview_goal_plan
from aos.task_plan import plan_payload
import test_desktop_tasks as fixtures


GOAL = 'önce hello görevini hazırla; sonra yerel form görevini hazırla'


def start_payload(goal, state):
    report = preview_goal_plan(goal)
    return {'goal': goal, 'input_sha256': report.input_sha256,
            'composition_sha256': report.composition_sha256,
            'lease_id': state['lease_id'], 'generation': state['generation']}


class CompoundExecutionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.DesktopTaskTests.asyncTearDown
    approval = fixtures.DesktopTaskTests.approval

    async def asyncSetUp(self):
        await fixtures.DesktopTaskTests.asyncSetUp(self)
        self.browser_manifest = self.root / 'synthetic-browser-manifest.json'
        self.browser_manifest.write_text(json.dumps({
            'synthetic': True, 'purpose': 'sequence_identity_only_no_browser_runtime'}))

    async def client(self, *, enabled=True):
        self.scheduler.browser_manifest = self.browser_manifest
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root,
                             scheduler=self.scheduler if enabled else None)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                   headers={'Origin': origin})
        self.addAsyncCleanup(client.aclose)
        return client

    async def login(self, client):
        self.assertEqual((await client.post('/api/login', json={'token': 'synthetic-token'})).status_code, 200)

    def request(self, goal=GOAL):
        return start_payload(goal, self.controller.state())

    def test_canonical_schema_fixture_and_strict_request_shape(self):
        schema = json.loads((REPO_ROOT / 'schemas/compound_sequence_start.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                  **CompoundSequenceStart.model_json_schema()})
        fixture = json.loads((REPO_ROOT / 'examples/compound_execution.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.validate(fixture['request'], schema)
        self.assertEqual(CompoundSequenceStart.model_validate(fixture['request']).sequence_start().model_dump(),
                         fixture['sequence_request'])
        request = self.request()
        for invalid in ({**request, 'generation': True}, {**request, 'execute': True},
                        {**request, 'plan': {'kinds': ['hello', 'shell']}}, {**request, 'goal': None},
                        {**request, 'lease_id': ''}, {**request, 'input_sha256': 'not-a-hash'},
                        {**request, 'goal': 'x' * 3001}):
            with self.assertRaises(ValidationError):
                CompoundSequenceStart.model_validate(invalid)

    def test_all_catalog_orders_bind_exact_source_and_reject_partial_or_single_tasks(self):
        fragments = ['hello görevini hazırla', 'yerel form görevini hazırla', 'görsel save görevini hazırla']
        for count in (2, 3):
            for ordered in itertools.permutations(fragments, count):
                goal = 'önce ' + '; sonra '.join(ordered)
                self.assertEqual(CompoundSequenceStart.model_validate(self.request(goal)).sequence_start().plan,
                                 preview_goal_plan(goal).sequence)
        for goal in ('hello görevini hazırla', 'önce hello görevini hazırla; sonra özel dosyayı sil',
                     GOAL + ' başlatma', 'önce hello görevini hazırla; sonra hello görevini hazırla'):
            with self.assertRaises(ValueError):
                CompoundSequenceStart.model_validate(self.request(goal)).sequence_start()

    async def test_authentication_origin_query_body_and_scope_boundaries(self):
        client = await self.client()
        request = self.request()
        self.assertEqual((await client.post('/api/tasks/compound-start', json=request)).status_code, 401)
        await self.login(client)
        for headers in ({'Origin': 'http://evil.invalid'}, {'Host': 'evil.invalid'}):
            self.assertEqual((await client.post('/api/tasks/compound-start', json=request, headers=headers)).status_code, 403)
        for invalid in ({}, [], {**request, 'generation': True}, {**request, 'goal': []},
                        {**request, 'approve': True}, {**request, 'runtime_id': 'other'},
                        {**request, 'kinds': ['hello', 'shell']}, {**request, 'generation': -1}):
            self.assertEqual((await client.post('/api/tasks/compound-start', json=invalid)).status_code, 400)
        self.assertEqual((await client.post('/api/tasks/compound-start?execute=true', json=request)).status_code, 400)
        self.assertEqual((await client.post('/api/tasks/compound-start', content='x' * 4097)).status_code, 413)
        self.assertEqual(self.scheduler.status()['jobs'], [])
        self.assertIsNone(self.scheduler.sequences.report)

    async def test_changed_input_plan_hash_catalog_and_negation_are_rejected_without_writes(self):
        client = await self.client()
        await self.login(client)
        request = self.request()
        before = self.store.connection.total_changes
        for invalid in ({**request, 'goal': ' ' + GOAL}, {**request, 'composition_sha256': '0' * 64},
                        {**request, 'input_sha256': '0' * 64}, self.request('hello görevini başlatma'),
                        self.request('önce hello görevini hazırla; sonra özel-secret görevi'),
                        self.request('hello görevini hazırla')):
            response = await client.post('/api/tasks/compound-start', json=invalid)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('özel-secret', response.text)
        def changed_catalog(kind):
            payload = plan_payload(kind)
            payload['steps'][0]['description'] = 'Changed synthetic catalog description.'
            return payload
        with patch('aos.task_plan.plan_payload', side_effect=changed_catalog):
            self.assertEqual((await client.post('/api/tasks/compound-start', json=request)).status_code, 409)
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertIsNone(self.scheduler.sequences.report)

    async def test_unavailable_scheduler_or_task_kind_never_admits(self):
        client = await self.client(enabled=False)
        await self.login(client)
        before = self.store.connection.total_changes
        self.assertEqual((await client.post('/api/tasks/compound-start', json=self.request())).status_code, 409)
        enabled = await self.client()
        await self.login(enabled)
        self.scheduler.browser_manifest = None
        self.assertEqual((await enabled.post('/api/tasks/compound-start', json=self.request())).status_code, 409)
        self.assertEqual(self.store.connection.total_changes, before)

    async def test_stale_lease_and_generation_reject_after_control_change(self):
        client = await self.client()
        await self.login(client)
        request = self.request()
        for command in ('pause', 'resume', 'take-control', 'return-control'):
            self.assertEqual((await client.post('/api/control', json={'command': command})).status_code, 200)
            self.assertEqual((await client.post('/api/tasks/compound-start', json=request)).status_code, 409)
        self.assertEqual(self.scheduler.status()['jobs'], [])
        self.assertIsNone(self.scheduler.sequences.report)

    async def test_concurrent_admission_has_one_sequence_and_no_implicit_action_approval(self):
        client = await self.client()
        await self.login(client)
        request = self.request()
        responses = await asyncio.gather(*(client.post('/api/tasks/compound-start', json=request) for attempt in range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        accepted = next(response.json() for response in responses if response.status_code == 200)
        self.assertEqual(accepted['plan']['kinds'], ['hello', 'browser_form'])
        pending = await self.approval()
        self.assertEqual(pending['action']['tool'], 'filesystem.write')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual((await client.post('/api/sequences', json=CompoundSequenceStart.model_validate(request).sequence_start().model_dump())).status_code, 409)
        response = await client.post('/api/approvals/' + pending['approval_id'],
                                     json={'action_sha256': pending['action_sha256'], 'accept': False})
        self.assertEqual(response.status_code, 200)
        await self.scheduler.sequences.task
        self.assertEqual(self.scheduler.sequences.report.status, 'failed')
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_control_lock_denies_admission_and_takeover_revokes_pending(self):
        client = await self.client()
        await self.login(client)
        request = self.request()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.scheduler.pause
        async def held_pause():
            entered.set()
            await release.wait()
            await original()
        with patch.object(self.scheduler, 'pause', side_effect=held_pause):
            control = asyncio.create_task(client.post('/api/control', json={'command': 'pause'}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual((await client.post('/api/tasks/compound-start', json=request)).status_code, 409)
            finally:
                release.set()
                await control
        await client.post('/api/control', json={'command': 'resume'})
        self.assertEqual((await client.post('/api/tasks/compound-start', json=self.request())).status_code, 200)
        pending = await self.approval()
        await client.post('/api/control', json={'command': 'take-control'})
        response = await client.post('/api/approvals/' + pending['approval_id'],
                                     json={'action_sha256': pending['action_sha256'], 'accept': True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.scheduler.sequences.report.status, 'cancelled')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1', 'Requires real Docker and built UI')
class CompoundExecutionUITests(unittest.TestCase):
    def test_exact_compound_start_requires_four_fresh_approvals_and_clears_stale_preview(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='compound-execution-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture', ('--browser-tasks', '--vision-engine', 'fixture')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, starts = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: starts.append(request.post_data_json) if request.url.endswith('/api/tasks/compound-start') else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Plan', exact=True).click()
                    field = page.get_by_label('Birleşik Türkçe görev', exact=True)
                    preview = page.get_by_role('button', name='Birleşimi önizle', exact=True)
                    start = page.get_by_role('button', name='Bu birleşik sırayı başlat', exact=True)
                    goal = GOAL + '; sonra görsel save görevini hazırla'
                    field.fill(goal)
                    preview.click()
                    expect(start).to_be_enabled()
                    self.assertEqual(starts, [])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    page.get_by_role('button', name='Duraklat', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('PAUSED')
                    expect(page.get_by_test_id('compound-plan-result')).to_have_count(0)
                    field.fill(goal)
                    preview.click()
                    expect(start).to_be_disabled()
                    page.get_by_role('button', name='Devam et', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('AGENT')
                    expect(page.get_by_test_id('compound-plan-result')).to_have_count(0)
                    field.fill(goal)
                    preview.click()
                    expect(start).to_be_enabled()
                    page.screenshot(path='/tmp/aos-compound-start-desktop.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-compound-start-mobile.png', full_page=True)
                    start.click()
                    expect(page.get_by_test_id('compound-started')).to_contain_text('hiçbir eylem otomatik onaylanmadı')
                    expect(page.get_by_test_id('compound-plan-result')).to_have_count(0)
                    self.assertEqual(len(starts), 1)
                    self.assertEqual(starts[0], start_payload(goal, client.get('/api/state').json()['control']))
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    for tool in ('filesystem.write', 'browser.fill', 'browser.submit', 'vision.click'):
                        expect(page.get_by_test_id('approval')).to_contain_text(tool, timeout=30000)
                        expect(page.get_by_role('button', name='Hello görevi başlat', exact=True)).to_be_disabled()
                        page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('sequence-status')).to_contain_text('succeeded', timeout=30000)
                    expect(page.get_by_test_id('sequence-status')).to_contain_text('3 / 3')
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                    self.assertEqual(len(client.get('/api/sequences').json()['sequence']['job_ids']), 3)
                    page.get_by_role('button', name='Plan', exact=True).click()
                    field.fill(goal)
                    preview.click()
                    expect(start).to_be_enabled()
                    field.fill('hello görevini başlatma')
                    expect(start).to_have_count(0)
                    preview.click()
                    expect(page.get_by_test_id('compound-plan-result')).to_contain_text('Olumsuzluk veya kontrol isteği')
                    expect(start).to_have_count(0)
                    self.assertEqual(len(starts), 1)
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

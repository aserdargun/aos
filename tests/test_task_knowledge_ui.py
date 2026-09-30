from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT, digest
from aos.task_knowledge import PREFIX


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker and built Chromium UI; decision model is a synthetic fixture')
class TaskKnowledgeUITests(unittest.TestCase):
    def test_reviewed_unicode_context_requires_separate_consent_and_manual_effect(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        scope = {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant', 'account_role': 'reader'}
        text = ('Synthetic hello manual: Türkçe 😀 use the fresh observation. '
                '<script>window.taskKnowledgeInjected=true</script>'
                '<img src=x onerror="window.taskKnowledgeInjected=true">')
        browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
        with tempfile.TemporaryDirectory(prefix='task-knowledge-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            with task_server(root, 'fixture', ('--knowledge-root', str(root / 'knowledge'))) as (
                    origin, token, client, server), sync_playwright() as playwright:
                self.assertTrue(client.get('/api/tasks').json()['task_knowledge_available'])
                control = client.get('/api/state').json()['control']
                authority = {key: control[key] for key in ('lease_id', 'generation')}
                publication = client.post('/api/knowledge/publish-preview', json={
                    'schema_version': '1.0', 'scope': scope, 'source_id': 'synthetic-manual',
                    'title': 'Synthetic Türkçe hello manual', 'text': text, 'previous_sha256': None,
                    'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                    'rights_attested': True, 'storage_consent': True, 'synthetic': True})
                self.assertEqual(publication.status_code, 200, publication.text)
                preview = publication.json()
                published = client.post('/api/knowledge/publish', json={
                    'schema_version': '1.0', 'preview': preview,
                    'confirm_sha256': preview['preview_sha256'], **authority})
                self.assertEqual(published.status_code, 200, published.text)
                review = client.post('/api/knowledge/review-preview', json={
                    'schema_version': '1.0', 'scope': scope,
                    'document_sha256': published.json()['document_sha256'], 'decision': 'accept'})
                self.assertEqual(review.status_code, 200, review.text)
                reviewed = client.post('/api/knowledge/review', json={
                    'schema_version': '1.0', 'preview': review.json(),
                    'confirm_sha256': review.json()['preview_sha256'], **authority})
                self.assertEqual(reviewed.status_code, 200, reviewed.text)
                browser = playwright.chromium.launch(executable_path=str(browser_path))
                errors, writes = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'en')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text)
                            if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: writes.append((request.url, request.post_data_json))
                            if request.method == 'POST' else None)
                    page.goto(origin + '/ui/')
                    expect(page).to_have_title('AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('hello')
                    expect(page.get_by_test_id('approve-all')).to_be_checked()
                    panel = page.get_by_test_id('task-knowledge')
                    expect(panel).to_be_visible()
                    panel.locator('summary').click()
                    for field, value in (('application', scope['application_id']),
                                         ('tenant', scope['tenant_id']), ('role', scope['account_role']),
                                         ('query', 'hello')):
                        page.get_by_test_id('task-knowledge-' + field).fill(value)
                    with page.expect_response(lambda response: response.url.endswith('/tasks/knowledge/preview')) as response:
                        page.get_by_test_id('task-knowledge-preview').click()
                    self.assertEqual(response.value.status, 200, response.value.text())
                    context_preview = response.value.json()
                    expect(page.get_by_test_id('task-knowledge-context')).to_be_visible()
                    self.assertEqual(page.get_by_test_id('task-knowledge-context').inner_text(),
                                     context_preview['context_text'])
                    self.assertEqual(json.loads(context_preview['context_text'][len(PREFIX):])['citations'][0]['text'], text)
                    self.assertEqual(context_preview['confirm_sha256'], digest({
                        key: value for key, value in context_preview.items() if key != 'confirm_sha256'}))
                    expect(page.get_by_test_id('task-knowledge-start')).to_be_disabled()
                    page.get_by_test_id('task-knowledge-confirm').fill(context_preview['confirm_sha256'])
                    expect(page.get_by_test_id('task-knowledge-start')).to_be_disabled()
                    page.get_by_test_id('task-knowledge-consent').check()
                    page.get_by_test_id('task-knowledge-confirm').fill('0' * 64)
                    expect(page.get_by_test_id('task-knowledge-start')).to_be_disabled()
                    page.get_by_test_id('task-knowledge-confirm').fill(context_preview['confirm_sha256'])
                    expect(page.get_by_test_id('task-knowledge-start')).to_be_enabled()
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    self.assertIsNone(page.evaluate('window.taskKnowledgeInjected'))
                    self.assertEqual(panel.locator('script, img').count(), 0)
                    page.screenshot(path='/tmp/aos-task-knowledge-en.png', full_page=True)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('Tek görev için incelenmiş belge bağlamı')
                    expect(page.get_by_test_id('task-knowledge-consent')).to_be_checked()
                    expect(page.get_by_test_id('approve-all')).to_be_checked()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    page.screenshot(path='/tmp/aos-task-knowledge-mobile-tr.png', full_page=True)
                    with page.expect_response(lambda response: response.url.endswith('/tasks/knowledge/start')) as response:
                        page.get_by_test_id('task-knowledge-start').click()
                    self.assertEqual(response.value.status, 200, response.value.text())
                    started = response.value.json()
                    self.assertTrue(started['manual_approval_required'])
                    expect(page.get_by_test_id('task-knowledge-started')).to_contain_text(started['job_id'])
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write', timeout=30000)
                    status = client.get('/api/tasks').json()
                    self.assertIsNone(status['auto_approval'])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    starts = [body for url, body in writes if url.endswith('/tasks/knowledge/start')]
                    self.assertEqual(len(starts), 1)
                    self.assertIs(starts[0]['consent'], True)
                    self.assertEqual(starts[0]['preview'], context_preview)
                    self.assertNotIn('approve_all', starts[0])
                    self.assertNotIn('learning_metadata', starts[0])
                    self.assertFalse(any(url == origin + '/api/tasks' for url, body in writes))
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                    report = client.post('/api/tasks/knowledge/report', json={
                        'schema_version': '1.0', 'job_id': started['job_id']})
                    self.assertEqual(report.status_code, 200, report.text)
                    evidence = report.json()
                    self.assertTrue(evidence['historical_binding_verified'])
                    self.assertTrue(evidence['context_binding_verified'])
                    self.assertFalse(evidence['model_request_verified'])
                    self.assertFalse(evidence['knowledge_applied'])
                    self.assertEqual(evidence['prepared_context_count'], 1)
                    self.assertEqual(evidence['successful_model_call_count'], 0)
                    self.assertEqual(evidence['outcome']['status'], 'verified')
                    self.assertFalse(evidence['training_ready'])
                    self.assertFalse(evidence['gold'])
                    status = client.get('/api/tasks').json()
                    self.assertFalse(status['jobs'][0]['real_model'])
                    trace = client.get('/api/runs/' + status['jobs'][0]['run_id']).json()
                    self.assertEqual([action['tool'] for action in trace['actions']], ['filesystem.write', 'filesystem.read'])
                    self.assertEqual(trace['model_calls'], [])
                    page.get_by_test_id('task-knowledge-query').fill('hello refreshed')
                    page.get_by_test_id('task-knowledge-preview').click()
                    expect(page.get_by_test_id('task-knowledge-context')).to_be_visible()
                    page.get_by_test_id('task-knowledge-consent').check()
                    page.get_by_test_id('task-knowledge-confirm').fill(page.get_by_test_id('task-knowledge-hash').inner_text())
                    expect(page.get_by_test_id('task-knowledge-start')).to_be_enabled()
                    page.get_by_test_id('task-knowledge-query').fill('hello changed')
                    expect(page.get_by_test_id('task-knowledge-context')).to_have_count(0)
                    expect(page.get_by_test_id('task-knowledge-consent')).to_have_count(0)
                    expect(page.get_by_test_id('task-knowledge-start')).to_have_count(0)
                    self.assertEqual(len(client.get('/api/tasks').json()['jobs']), 1)
                    self.assertIsNone(page.evaluate('window.taskKnowledgeInjected'))
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

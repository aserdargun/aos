from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT, digest


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker, authenticated backend and built Chromium UI; synthetic text only')
class KnowledgeUITests(unittest.TestCase):
    def test_authenticated_document_review_lookup_revocation_and_fail_closed(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        scope = {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant', 'account_role': 'reader'}
        source_text = ('Synthetic Türkçe 😀 cedar document. <script>window.knowledgeInjected=true</script>'
                       '<img src=x onerror="window.knowledgeInjected=true">\n' + 'cedar ' * 180)
        expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
        with tempfile.TemporaryDirectory(prefix='knowledge-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            flags = ('--knowledge-root', str(root / 'knowledge'))
            with task_server(root, 'fixture', flags) as (origin, token, client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(browser_path))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors, calls, task_writes = [], [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: calls.append(request.url.rsplit('/', 1)[-1])
                            if '/api/knowledge/' in request.url else None)
                    page.on('request', lambda request: task_writes.append(request.url)
                            if request.method == 'POST' and '/api/tasks/' in request.url else None)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Knowledge', exact=True).click()
                    panel = page.get_by_test_id('knowledge-panel')
                    expect(panel).to_be_visible()
                    expect(page.get_by_test_id('knowledge-application')).to_be_visible()
                    self.assertEqual(calls, [])
                    for field, value in [('application', scope['application_id']), ('tenant', scope['tenant_id']),
                                         ('role', scope['account_role']), ('source', 'synthetic-manual'),
                                         ('title', 'Synthetic cedar manual'), ('expires', expires)]:
                        page.get_by_test_id('knowledge-' + field).fill(value)
                    for invalid_bytes in (b'\xff\xfe', b'x' * 65537):
                        page.get_by_test_id('knowledge-file').set_input_files({
                            'name': 'invalid-synthetic.txt', 'mimeType': 'text/plain', 'buffer': invalid_bytes})
                        expect(page.get_by_test_id('knowledge-error')).to_be_visible()
                        expect(page.get_by_test_id('knowledge-text')).to_have_value('')
                        self.assertEqual(calls, [])
                    page.get_by_test_id('knowledge-file').set_input_files({
                        'name': 'synthetic-manual.txt', 'mimeType': 'text/plain', 'buffer': source_text.encode('utf-8')})
                    expect(page.get_by_test_id('knowledge-text')).to_have_value(source_text)
                    self.assertEqual(calls, [])
                    expect(page.get_by_test_id('knowledge-preview')).to_be_disabled()
                    page.get_by_test_id('knowledge-rights').check()
                    expect(page.get_by_test_id('knowledge-preview')).to_be_disabled()
                    page.get_by_test_id('knowledge-storage').check()
                    page.get_by_test_id('knowledge-synthetic').check()
                    page.get_by_test_id('knowledge-preview').click()
                    preview = page.get_by_test_id('knowledge-publication')
                    expect(preview).to_be_visible()
                    expect(page.get_by_test_id('knowledge-publish')).to_be_disabled()
                    self.assertEqual(client.post('/api/knowledge/catalog', json={
                        'schema_version': '1.0', 'scope': scope}).json()['documents'], [])
                    preview_hash = page.get_by_test_id('knowledge-publication-hash').inner_text()
                    page.get_by_test_id('knowledge-publish-confirm').fill('0' * 64)
                    expect(page.get_by_test_id('knowledge-publish')).to_be_disabled()
                    page.get_by_test_id('knowledge-publish-confirm').fill(preview_hash)
                    with page.expect_response(lambda response: response.url.endswith('/knowledge/publish')) as published:
                        page.get_by_test_id('knowledge-publish').click()
                    self.assertEqual(published.value.status, 200)
                    inspection = published.value.json()
                    document_hash = inspection['document_sha256']
                    self.assertEqual(inspection['document']['text'], source_text)
                    self.assertEqual(inspection['review_status'], 'pending')
                    expect(page.get_by_test_id('knowledge-review-status')).to_contain_text('Awaiting separate review')
                    page.get_by_test_id('knowledge-query').fill('cedar')
                    page.get_by_test_id('knowledge-search').click()
                    expect(page.get_by_test_id('knowledge-search-result')).to_contain_text('No accepted')
                    self.assertEqual(page.get_by_test_id('knowledge-hit').count(), 0)
                    page.get_by_test_id('knowledge-accept-preview').click()
                    expect(page.get_by_test_id('knowledge-review')).to_be_visible()
                    expect(page.get_by_test_id('knowledge-review-save')).to_be_disabled()
                    page.get_by_test_id('knowledge-review-confirm').fill(page.get_by_test_id('knowledge-review-hash').inner_text())
                    expect(page.get_by_test_id('knowledge-review-save')).to_be_disabled()
                    page.get_by_test_id('knowledge-review-consent').check()
                    page.get_by_test_id('knowledge-review-save').click()
                    expect(page.get_by_test_id('knowledge-review-status')).to_contain_text('Accepted for lookup')
                    page.get_by_test_id('knowledge-search').click()
                    expect(page.get_by_test_id('knowledge-hit')).to_have_count(2)
                    expect(page.get_by_test_id('knowledge-hit').first).to_contain_text(document_hash)
                    expect(page.get_by_test_id('knowledge-hit').first).to_contain_text('Chunk SHA-256')
                    self.assertIsNone(page.evaluate('window.knowledgeInjected'))
                    self.assertEqual(page.locator('[data-testid=knowledge-hit] script, [data-testid=knowledge-hit] img').count(), 0)
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-knowledge-en-viewport.png')
                    page.screenshot(path='/tmp/aos-knowledge-en.png', full_page=True)
                    page.get_by_test_id('knowledge-search-result').screenshot(path='/tmp/aos-knowledge-citations-en.png',
                                                                           style='.controlbar{visibility:hidden}')
                    call_count = len(calls)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('İncelenmiş belgeleri atıflarla arayın')
                    self.assertEqual(len(calls), call_count)
                    page.screenshot(path='/tmp/aos-knowledge-tr.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-knowledge-mobile-viewport.png')
                    page.screenshot(path='/tmp/aos-knowledge-mobile-tr.png', full_page=True)
                    page.get_by_test_id('knowledge-search-result').screenshot(path='/tmp/aos-knowledge-citations-mobile-tr.png',
                                                                           style='.controlbar{visibility:hidden}')
                    page.get_by_role('button', name='English', exact=True).click()

                    def malformed_search(route):
                        response = route.fetch()
                        payload = response.json()
                        payload['hits'][0]['review_sha256'] = '0' * 64
                        route.fulfill(response=response, json=payload)

                    page.route('**/api/knowledge/search', malformed_search)
                    page.get_by_test_id('knowledge-search').click()
                    expect(page.get_by_test_id('knowledge-error')).to_be_visible()
                    expect(page.get_by_test_id('knowledge-hit')).to_have_count(0)
                    page.unroute('**/api/knowledge/search')
                    page.get_by_test_id('knowledge-catalog').click()
                    expect(page.get_by_test_id('knowledge-catalog-result')).to_contain_text('Synthetic cedar manual')
                    page.get_by_test_id('knowledge-inspect').click()
                    expect(page.get_by_test_id('knowledge-review-status')).to_contain_text('Accepted for lookup')
                    page.get_by_test_id('knowledge-revoke-preview').click()
                    expect(page.get_by_test_id('knowledge-review')).to_be_visible()
                    page.get_by_test_id('knowledge-review-confirm').fill(page.get_by_test_id('knowledge-review-hash').inner_text())
                    page.get_by_test_id('knowledge-review-consent').check()
                    page.get_by_test_id('knowledge-review-save').click()
                    expect(page.get_by_test_id('knowledge-review-status')).to_contain_text('Review revoked')
                    page.get_by_test_id('knowledge-search').click()
                    expect(page.get_by_test_id('knowledge-search-result')).to_contain_text('No accepted')
                    self.assertEqual(client.post('/api/knowledge/inspect', json={
                        'schema_version': '1.0', 'scope': scope, 'document_sha256': document_hash}).json()['document']['text'], source_text)
                    page.get_by_test_id('knowledge-tenant').fill('different-tenant')
                    expect(page.get_by_test_id('knowledge-inspection')).to_have_count(0)
                    expect(page.get_by_test_id('knowledge-search-result')).to_have_count(0)
                    page.get_by_test_id('knowledge-catalog').click()
                    expect(page.get_by_test_id('knowledge-catalog-result')).to_contain_text('No documents')
                    page.get_by_test_id('knowledge-tenant').fill(scope['tenant_id'])
                    page.get_by_test_id('knowledge-previous').fill(document_hash)
                    page.get_by_test_id('knowledge-text').fill('Synthetic cedar revised text. Türkçe 😀')
                    page.get_by_test_id('knowledge-preview').click()
                    expect(page.get_by_test_id('knowledge-publication')).to_contain_text('Revision 2')
                    page.get_by_test_id('knowledge-publish-confirm').fill(page.get_by_test_id('knowledge-publication-hash').inner_text())
                    page.get_by_test_id('knowledge-publish').click()
                    expect(page.get_by_test_id('knowledge-inspection')).to_contain_text('Revision 2')
                    page.get_by_test_id('knowledge-reject-preview').click()
                    expect(page.get_by_test_id('knowledge-review')).to_be_visible()
                    page.get_by_test_id('knowledge-review-confirm').fill(page.get_by_test_id('knowledge-review-hash').inner_text())
                    page.get_by_test_id('knowledge-review-consent').check()
                    page.get_by_test_id('knowledge-review-save').click()
                    expect(page.get_by_test_id('knowledge-review-status')).to_contain_text('Rejected')
                    page.get_by_test_id('knowledge-catalog').click()
                    expect(page.get_by_test_id('knowledge-catalog-result')).to_contain_text('superseded')
                    page.get_by_test_id('knowledge-previous').fill('')
                    page.get_by_test_id('knowledge-source').fill('synthetic-second')
                    page.evaluate("""() => {
                        window.knowledgeOriginalFetch = window.fetch;
                        window.fetch = async (...argumentsList) => {
                            const response = await window.knowledgeOriginalFetch(...argumentsList);
                            if (String(argumentsList[0]).endsWith('/knowledge/publish-preview')) {
                                await new Promise(resolve => {window.knowledgeDelayResolve = resolve;});
                            }
                            return response;
                        };
                    }""")
                    page.get_by_test_id('knowledge-preview').click()
                    page.wait_for_function('() => typeof window.knowledgeDelayResolve === "function"')
                    page.get_by_test_id('knowledge-tenant').fill('different-tenant')
                    page.evaluate('window.knowledgeDelayResolve()')
                    expect(page.get_by_test_id('knowledge-publication')).to_have_count(0)
                    page.evaluate('window.fetch = window.knowledgeOriginalFetch')
                    page.get_by_test_id('knowledge-tenant').fill(scope['tenant_id'])
                    for change in ['hash', 'scope', 'flags', 'schema']:
                        def malformed_preview(route, change=change):
                            response = route.fetch()
                            payload = response.json()
                            if change == 'hash':
                                payload['document_sha256'] = '0' * 64
                            elif change == 'scope':
                                payload['document']['scope']['tenant_id'] = 'forged-tenant'
                                payload['document_sha256'] = digest(payload['document'])
                            elif change == 'flags':
                                payload['document']['training_ready'] = True
                                payload['document_sha256'] = digest(payload['document'])
                            else:
                                payload['schema_version'] = '9.0'
                            payload['preview_sha256'] = digest({key: value for key, value in payload.items() if key != 'preview_sha256'})
                            route.fulfill(response=response, json=payload)

                        page.route('**/api/knowledge/publish-preview', malformed_preview)
                        page.get_by_test_id('knowledge-preview').click()
                        expect(page.get_by_test_id('knowledge-error')).to_be_visible()
                        expect(page.get_by_test_id('knowledge-publication')).to_have_count(0)
                        page.unroute('**/api/knowledge/publish-preview')
                    page.get_by_test_id('knowledge-preview').click()
                    expect(page.get_by_test_id('knowledge-publication')).to_be_visible()
                    page.get_by_test_id('knowledge-publish-confirm').fill(page.get_by_test_id('knowledge-publication-hash').inner_text())
                    publishes_before = calls.count('publish')

                    def stale_control(route):
                        response = route.fetch()
                        payload = response.json()
                        payload['control']['lease_id'] = 'different-lease'
                        route.fulfill(response=response, json=payload)

                    page.route('**/api/state', stale_control)
                    page.get_by_test_id('knowledge-publish').click()
                    expect(page.get_by_test_id('knowledge-error')).to_be_visible()
                    self.assertEqual(calls.count('publish'), publishes_before)
                    expect(page.get_by_test_id('knowledge-publication')).to_have_count(0)
                    page.unroute('**/api/state')
                    self.assertEqual(task_writes, [])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(page.evaluate('Object.keys(localStorage)'), ['aos.ui.language'])
                finally:
                    browser.close()

    def test_old_backend_capability_absence_makes_no_knowledge_requests(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='knowledge-legacy-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, _client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    calls = []
                    page.on('request', lambda request: calls.append(request.url) if '/api/knowledge/' in request.url else None)

                    def legacy_tasks(route):
                        response = route.fetch()
                        payload = response.json()
                        payload.pop('knowledge_available', None)
                        route.fulfill(response=response, json=payload)

                    page.route('**/api/tasks', legacy_tasks)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Knowledge', exact=True).click()
                    expect(page.get_by_test_id('knowledge-unavailable')).to_contain_text('does not provide')
                    expect(page.get_by_test_id('knowledge-preview')).to_have_count(0)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_test_id('knowledge-unavailable')).to_contain_text('özelliğini sunmuyor')
                    self.assertEqual(calls, [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

import os
from pathlib import Path
import tempfile
import time
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker, Chromium and built UI')
class FailureGuidanceUITests(unittest.TestCase):
    def test_reviewed_guidance_needs_separate_consent_and_fresh_manual_approval(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='failure-guidance-ui-', dir=REPO_ROOT / 'data', delete=False) as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, unused_server), sync_playwright() as playwright:
                control = client.get('/api/state').json()['control']
                source_response = client.post('/api/tasks', json={'kind': 'hello', 'approve_all': False,
                    'lease_id': control['lease_id'], 'generation': control['generation']})
                self.assertEqual(source_response.status_code, 200, source_response.text)
                source_id = source_response.json()['job_id']

                def wait_for(job_id, approval=False):
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        current = client.get('/api/tasks').json()
                        job = next(item for item in current['jobs'] if item['job_id'] == job_id)
                        if approval and current['approval'] is not None:
                            self.assertEqual(current['approval']['job_id'], job_id)
                            return current
                        if approval and job['status'] in {'succeeded', 'failed', 'cancelled'} and not current['reserved']:
                            self.fail(f"Task settled before manual approval: {job['status']}; private evidence: {root}")
                        if not approval and job['status'] in {'succeeded', 'failed', 'cancelled'} and not current['reserved']:
                            return current
                        time.sleep(.02)
                    self.fail('Isolated fixture task did not reach the required boundary')

                original = wait_for(source_id, approval=True)['approval']
                rejected = client.post('/api/approvals/' + original['approval_id'], json={
                    'action_sha256': original['action_sha256'], 'accept': False})
                self.assertEqual(rejected.status_code, 200, rejected.text)
                wait_for(source_id)
                self.assertFalse((root / 'workspace/hello.txt').exists())
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    fixture = {'preview_count': 0, 'inspect_count': 0, 'preview': None, 'start': None}

                    def route_guidance(route):
                        operation = route.request.url.rsplit('/', 1)[-1]
                        response = route.fetch()
                        self.assertEqual(response.status, 200, response.text())
                        value = response.json()
                        if operation == 'preview':
                            fixture['preview_count'] += 1
                            if fixture['preview_count'] == 1:
                                value['followup']['target']['session_id'] = 'foreign-session'
                            else:
                                fixture['preview'] = value
                        elif operation == 'start':
                            body = route.request.post_data_json
                            self.assertEqual(body['preview'], fixture['preview'])
                            self.assertEqual(body['confirm_sha256'], fixture['preview']['confirm_sha256'])
                            self.assertIs(body['consent'], True)
                            self.assertNotIn('approve_all', body)
                            self.assertNotIn('learning_metadata', body)
                            fixture['start'] = value
                        elif operation == 'inspect':
                            fixture['inspect_count'] += 1
                            if fixture['inspect_count'] == 1:
                                value['context_binding_verified'] = False
                                value['guidance_applied'] = True
                        route.fulfill(response=response, json=value)

                    page.route('**/api/tasks/failure-guidance/*', route_guidance)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('failure-guidance-panel')).to_have_count(0)
                    page.get_by_test_id('failure-preview').click()
                    expect(page.get_by_test_id('failure-consent')).to_be_visible()
                    page.get_by_test_id('failure-consent').check()
                    page.get_by_test_id('failure-save').click()
                    expect(page.get_by_test_id('failure-review-confirm')).to_be_visible()
                    page.get_by_test_id('failure-review-confirm').check()
                    page.get_by_test_id('failure-review-accept').click()
                    panel = page.get_by_test_id('failure-guidance-panel')
                    expect(panel).to_be_visible()
                    with page.expect_response(lambda response: response.url.endswith('/failure-guidance/preview')):
                        page.get_by_test_id('failure-guidance-preview').click()
                    expect(panel).to_contain_text('unavailable or changed')
                    expect(page.get_by_test_id('failure-guidance-preview-result')).to_have_count(0)
                    with page.expect_response(lambda response: response.url.endswith('/failure-guidance/preview')):
                        page.get_by_test_id('failure-guidance-preview').click()
                    expect(page.get_by_test_id('failure-guidance-preview-result')).to_be_visible()
                    expect(page.get_by_test_id('failure-guidance-start')).to_be_disabled()
                    page.get_by_test_id('failure-guidance-consent').check()
                    page.get_by_test_id('failure-guidance-confirm').fill('0' * 64)
                    expect(page.get_by_test_id('failure-guidance-start')).to_be_disabled()
                    page.get_by_test_id('failure-guidance-confirm').fill(fixture['preview']['confirm_sha256'])
                    expect(page.get_by_test_id('failure-guidance-start')).to_be_enabled()
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    with page.expect_response(lambda response: response.url.endswith('/failure-guidance/start')):
                        page.get_by_test_id('failure-guidance-start').click()
                    expect(page.get_by_test_id('failure-guidance-started')).to_be_visible()
                    guided_id = fixture['start']['job_id']
                    current = wait_for(guided_id, approval=True)
                    self.assertIsNone(current['auto_approval'])
                    self.assertNotEqual(current['approval']['approval_id'], original['approval_id'])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    page.get_by_role('button', name='Approve', exact=True).click()
                    completed = wait_for(guided_id)
                    self.assertEqual(next(job['status'] for job in completed['jobs'] if job['job_id'] == guided_id), 'succeeded')
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('failure-guidance-job')).to_contain_text(guided_id[:12])
                    page.get_by_test_id('failure-guidance-job').select_option(guided_id)
                    with page.expect_response(lambda response: response.url.endswith('/failure-guidance/inspect')):
                        page.get_by_test_id('failure-guidance-inspect').click()
                    expect(panel).to_contain_text('does not match this task')
                    expect(page.get_by_test_id('failure-guidance-report')).to_have_count(0)
                    with page.expect_response(lambda response: response.url.endswith('/failure-guidance/inspect')):
                        page.get_by_test_id('failure-guidance-inspect').click()
                    report = page.get_by_test_id('failure-guidance-report')
                    expect(report).to_contain_text('Context binding verified: Yes')
                    expect(report).to_contain_text('Model request verified: No')
                    expect(report).to_contain_text('Guidance applied: No')
                    expect(report).to_contain_text('Task outcome verified: Yes')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-guidance-panel-en.png', full_page=True, clip=panel.bounding_box())
                    page.screenshot(path='/tmp/aos-failure-guidance-en-viewport.png')
                    page.screenshot(path='/tmp/aos-failure-guidance-en.png', full_page=True)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('İncelenmiş yönlendirmeyi kullan')
                    expect(report).to_contain_text('Yönlendirme uygulandı: Hayır')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-guidance-panel-mobile.png', full_page=True, clip=panel.bounding_box())
                    page.screenshot(path='/tmp/aos-failure-guidance-mobile-viewport.png')
                    page.screenshot(path='/tmp/aos-failure-guidance-mobile.png', full_page=True)
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

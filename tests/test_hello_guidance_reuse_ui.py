import os
from pathlib import Path
import tempfile
import time
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker, Chromium and built UI')
class HelloGuidanceReuseUITests(unittest.TestCase):
    def test_publication_reuse_and_history_with_separate_permissions(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='hello-reuse-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, unused_server), sync_playwright() as playwright:
                control = client.get('/api/state').json()['control']
                response = client.post('/api/tasks', json={'kind': 'hello', 'approve_all': False,
                    'lease_id': control['lease_id'], 'generation': control['generation']})
                self.assertEqual(response.status_code, 200, response.text)
                source_id = response.json()['job_id']

                def wait_for(job_id, approval=False):
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        current = client.get('/api/tasks').json()
                        job = next(item for item in current['jobs'] if item['job_id'] == job_id)
                        if approval and current['approval'] is not None:
                            self.assertEqual(current['approval']['job_id'], job_id)
                            return current
                        if job['status'] in {'succeeded', 'failed', 'cancelled'} and not current['reserved']:
                            self.assertFalse(approval, 'Task settled before fresh manual approval')
                            return current
                        time.sleep(.02)
                    self.fail('Fixture task did not reach boundary')

                original = wait_for(source_id, approval=True)['approval']
                client.post('/api/approvals/' + original['approval_id'], json={
                    'action_sha256': original['action_sha256'], 'accept': False}).raise_for_status()
                wait_for(source_id)
                self.assertFalse((root / 'workspace/hello.txt').exists())
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    fixture = {'publication': None, 'reuse': None, 'start': None, 'entry': None, 'tamper': None, 'count': 0, 'revoked': False}

                    def route_reuse(route):
                        operation = route.request.url.rsplit('/', 1)[-1]
                        fixture['count'] += 1
                        response = route.fetch()
                        if operation == 'reuse-preview' and fixture['revoked']:
                            self.assertEqual(response.status, 409)
                            route.fulfill(response=response)
                            return
                        self.assertEqual(response.status, 200, response.text())
                        value = response.json()
                        if operation in {'publish', 'start'}:
                            expected = fixture['publication' if operation == 'publish' else 'reuse']
                            body = route.request.post_data_json
                            self.assertEqual(set(body), {'schema_version', 'preview', 'confirm_sha256', 'consent', 'lease_id', 'generation'})
                            self.assertEqual(body['preview'], expected)
                            self.assertEqual(body['confirm_sha256'], expected['confirm_sha256'])
                            self.assertIs(body['consent'], True)
                        if fixture['tamper'] == 'schema':
                            value['schema_version'] = '2.0'
                        elif fixture['tamper'] == 'flag':
                            value['entry']['execution_authorized'] = True
                        elif fixture['tamper'] == 'identity':
                            value['entry']['scope']['session_id'] = 'foreign-session'
                        elif fixture['tamper'] == 'guidance':
                            value['guidance']['followup']['target']['system1_deployment_id'] = 'foreign-deployment'
                        elif fixture['tamper'] == 'report':
                            value['guidance']['context_binding_verified'] = False
                            value['guidance']['guidance_applied'] = True
                        elif operation == 'publish-preview':
                            fixture['publication'] = value
                        elif operation == 'reuse-preview':
                            fixture['reuse'] = value
                        elif operation == 'start':
                            fixture['start'] = value
                        elif operation == 'publish':
                            fixture['entry'] = value
                        route.fulfill(response=response, json=value)

                    page.route('**/api/tasks/hello-guidance-reuse/*', route_reuse)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_test_id('failure-preview').click()
                    page.get_by_test_id('failure-consent').check()
                    page.get_by_test_id('failure-save').click()
                    page.get_by_test_id('failure-review-confirm').check()
                    page.get_by_test_id('failure-review-accept').click()
                    panel = page.get_by_test_id('hello-reuse-panel')
                    expect(panel).to_be_visible()
                    for tamper in ('schema', 'flag', 'identity'):
                        fixture['tamper'] = tamper
                        page.get_by_test_id('hello-reuse-publish-preview').click()
                        expect(panel).to_contain_text('response or scope changed')
                        expect(page.get_by_test_id('hello-reuse-preview-result')).to_have_count(0)
                    fixture['tamper'] = None
                    page.get_by_test_id('hello-reuse-publish-preview').click()
                    expect(page.get_by_test_id('hello-reuse-publish')).to_be_disabled()
                    page.get_by_test_id('hello-reuse-consent').check()
                    page.get_by_test_id('hello-reuse-confirm').fill('0' * 64)
                    expect(page.get_by_test_id('hello-reuse-publish')).to_be_disabled()
                    page.get_by_test_id('hello-reuse-confirm').fill(fixture['publication']['confirm_sha256'])
                    page.get_by_test_id('hello-reuse-publish').click()
                    expect(page.get_by_test_id('hello-reuse-entry-result')).to_contain_text('valid: Yes')
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    self.assertEqual(len(client.get('/api/tasks').json()['jobs']), 1)
                    page.get_by_test_id('hello-reuse-inspect').click()
                    expect(page.get_by_test_id('hello-reuse-entry-result')).to_contain_text('valid: Yes')
                    page.get_by_test_id('hello-reuse-preview').click()
                    expect(page.get_by_test_id('hello-reuse-start')).to_be_disabled()
                    page.get_by_test_id('hello-reuse-consent').check()
                    page.get_by_test_id('hello-reuse-confirm').fill(fixture['reuse']['confirm_sha256'])
                    expect(page.get_by_test_id('hello-reuse-start')).to_be_enabled()
                    client.post('/api/control', json={'command': 'pause'}).raise_for_status()
                    client.post('/api/control', json={'command': 'resume'}).raise_for_status()
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('hello-reuse-preview-result')).to_have_count(0)
                    self.assertEqual(len(client.get('/api/tasks').json()['jobs']), 1)
                    fixture['tamper'] = 'guidance'
                    page.get_by_test_id('hello-reuse-preview').click()
                    expect(panel).to_contain_text('response or scope changed')
                    expect(page.get_by_test_id('hello-reuse-preview-result')).to_have_count(0)
                    fixture['tamper'] = None
                    page.get_by_test_id('hello-reuse-preview').click()
                    expect(page.get_by_test_id('hello-reuse-start')).to_be_disabled()
                    expect(page.get_by_test_id('hello-reuse-consent')).not_to_be_checked()
                    page.get_by_test_id('hello-reuse-consent').check()
                    page.get_by_test_id('hello-reuse-confirm').fill(fixture['reuse']['confirm_sha256'])
                    page.get_by_test_id('hello-reuse-start').click()
                    expect(page.get_by_test_id('hello-reuse-started')).to_be_visible()
                    reused_id = fixture['start']['job_id']
                    current = wait_for(reused_id, approval=True)
                    self.assertIsNone(current['auto_approval'])
                    self.assertNotEqual(current['approval']['approval_id'], original['approval_id'])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    page.get_by_role('button', name='Approve', exact=True).click()
                    completed = wait_for(reused_id)
                    self.assertEqual(next(job['status'] for job in completed['jobs'] if job['job_id'] == reused_id), 'succeeded')
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    page.get_by_test_id('hello-reuse-job').select_option(reused_id)
                    fixture['tamper'] = 'report'
                    page.get_by_test_id('hello-reuse-report').click()
                    expect(panel).to_contain_text('response or scope changed')
                    expect(page.get_by_test_id('hello-reuse-report-result')).to_have_count(0)
                    fixture['tamper'] = None
                    page.get_by_test_id('hello-reuse-report').click()
                    report = page.get_by_test_id('hello-reuse-report-result')
                    expect(report).to_contain_text('Historical entry binding verified: Yes')
                    expect(report).to_contain_text('Current entry valid: Yes')
                    expect(report).to_contain_text('Context binding verified: Yes')
                    expect(report).to_contain_text('Model request verified: No')
                    expect(report).to_contain_text('Guidance applied: No')
                    expect(report).to_contain_text('Task outcome verified: Yes')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-hello-guidance-reuse-panel-en.png', full_page=True, clip=panel.bounding_box())
                    page.screenshot(path='/tmp/aos-hello-guidance-reuse-en.png', full_page=True)
                    page.get_by_test_id('failure-preview').click()
                    expect(page.get_by_test_id('failure-revoke')).to_be_enabled()
                    page.get_by_test_id('failure-revoke').click()
                    expect(page.get_by_test_id('failure-review-status')).to_contain_text('revoked')
                    page.get_by_test_id('hello-reuse-inspect').click()
                    expect(page.get_by_test_id('hello-reuse-entry-result')).to_contain_text('valid: No')
                    page.get_by_test_id('hello-reuse-report').click()
                    expect(report).to_contain_text('Historical entry binding verified: Yes')
                    expect(report).to_contain_text('Current entry valid: No')
                    expect(report).to_contain_text('Task outcome verified: Yes')
                    fixture['revoked'] = True
                    page.get_by_test_id('hello-reuse-preview').click()
                    expect(panel).to_contain_text('response or scope changed')
                    expect(page.get_by_test_id('hello-reuse-preview-result')).to_have_count(0)
                    page.get_by_test_id('hello-reuse-report').click()
                    expect(report).to_contain_text('Current entry valid: No')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('Yeniden kullanılabilir Hello yönlendirmesi')
                    expect(report).to_contain_text('Yönlendirme uygulandı: Hayır')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-hello-guidance-reuse-panel-tr.png', full_page=True, clip=panel.bounding_box())
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-hello-guidance-reuse-panel-mobile.png', full_page=True, clip=panel.bounding_box())
                    page.screenshot(path='/tmp/aos-hello-guidance-reuse-mobile.png', full_page=True)
                    self.assertEqual(errors, [])

                    def old_backend(route):
                        response = route.fetch()
                        value = response.json()
                        value.pop('hello_guidance_reuse_available', None)
                        route.fulfill(response=response, json=value)

                    count = fixture['count']
                    page.route('**/api/tasks', old_backend)
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(panel).to_have_count(0)
                    self.assertEqual(fixture['count'], count)
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

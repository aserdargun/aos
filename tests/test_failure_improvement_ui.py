import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires built UI and the local desktop fixture')
class FailureImprovementUITests(unittest.TestCase):
    def test_failed_task_diagnostic_actions_are_explicit_metadata_only_and_scoped(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        job_id = 'failed-synthetic-job'
        cancelled_job_id = 'cancelled-synthetic-job'
        candidate = {
            'schema_version': '1.0', 'kind': 'failed_task_improvement', 'job_id': job_id,
            'session_id': 'session-synthetic', 'task_kind': 'browser_remote_form', 'run_ref': 'a' * 64,
            'source_sha256': 'b' * 64, 'scope_sha256': 'c' * 64, 'outcome': 'failed',
            'failure_code': 'RUNTIME_CRASH', 'synthetic': True,
            'model_roles': {
                'system1': {'deployment_id': 'decider-' + '1' * 64, 'real_model': True,
                            'calls': {'ok': 1, 'error': 1, 'timeout': 0, 'cancelled': 0}},
                'system2': {'deployment_id': 'bonsai-' + '2' * 64, 'real_model': False,
                            'calls': {'ok': 1, 'error': 0, 'timeout': 0, 'cancelled': 0}}},
            'approval_counts': {'consumed': 2, 'rejected': 0, 'expired': 1, 'revoked': 0},
            'action_counts': {'ok': 1, 'error': 1, 'denied': 0, 'cancelled': 0},
            'metadata_only': True, 'retrospective': True, 'training_ready': False, 'gold': False,
            'execution_authorized': False, 'failure_attribution_verified': False}
        option_codes = ('refresh_observation', 'inspect_before_retry', 'request_clarification', 'repair_environment')
        review_options = ([{'decision': 'accept', 'correction_code': code, 'confirm_sha256': str(index + 1) * 64}
                           for index, code in enumerate(option_codes)]
                          + [{'decision': 'reject', 'correction_code': None, 'confirm_sha256': '5' * 64}])
        receipt = {'receipt_sha256': '6' * 64, 'decision': 'accept',
                   'correction_code': 'refresh_observation', 'revoked': False}
        review_options[0]['confirm_sha256'] = receipt['receipt_sha256']
        state = {'saved': False, 'review': None, 'source_current': True, 'mismatch_review': True}
        calls = []
        consent_value = 'synthetic-confirmation-token'

        def envelope():
            return {'schema_version': '1.0', 'job_id': job_id, 'available': True,
                    'candidate_sha256': 'd' * 64, 'candidate': candidate,
                    'saved': state['saved'], 'review': state['review'],
                    'review_options': review_options, 'source_current': state['source_current']}

        def route_tasks(route):
            response = route.fetch()
            payload = response.json()
            payload.update({'busy': False, 'reserved': False, 'approval': None,
                'failure_improvement_available': True,
                'jobs': [
                    {'job_id': job_id, 'run_id': 'run-synthetic', 'kind': candidate['task_kind'],
                     'real_model': 1, 'runtime_id': 'runtime-synthetic', 'status': 'failed'},
                    {'job_id': cancelled_job_id, 'run_id': None, 'kind': 'hello',
                     'real_model': 0, 'runtime_id': None, 'status': 'cancelled'},
                    {'job_id': 'successful-job', 'run_id': 'run-ok', 'kind': 'hello',
                     'real_model': 0, 'runtime_id': None, 'status': 'succeeded'}]})
            route.fulfill(response=response, json=payload)

        def route_failure(route):
            operation = route.request.url.rsplit('/', 1)[-1]
            body = route.request.post_data_json
            calls.append((operation, body))
            if operation == 'preview':
                route.fulfill(json=envelope())
            elif operation == 'save':
                self.assertEqual(body, {'schema_version': '1.0', 'job_id': job_id,
                    'confirm_sha256': 'd' * 64, 'consent': True,
                    'lease_id': body['lease_id'], 'generation': body['generation']})
                state['saved'] = True
                route.fulfill(json=envelope())
            elif operation == 'review':
                self.assertEqual(body['candidate_sha256'], 'd' * 64)
                self.assertEqual(body['decision'], 'accept')
                self.assertEqual(body['correction_code'], 'refresh_observation')
                self.assertEqual(body['confirm_sha256'], review_options[0]['confirm_sha256'])
                if state['mismatch_review']:
                    state['mismatch_review'] = False
                    route.fulfill(json=envelope() | {'review': receipt | {'receipt_sha256': '7' * 64}})
                    return
                state['review'] = receipt.copy()
                route.fulfill(json=envelope())
            elif operation == 'revoke':
                self.assertEqual(body['candidate_sha256'], 'd' * 64)
                self.assertEqual(body['receipt_sha256'], receipt['receipt_sha256'])
                state['review'] = receipt | {'revoked': True}
                route.fulfill(json=envelope())
            else:
                route.fulfill(status=404, json={'detail': 'unexpected fixture route'})

        with tempfile.TemporaryDirectory(prefix='failure-improvement-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, _client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    writes = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text)
                            if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: writes.append(request.url)
                            if request.method == 'POST' and '/api/tasks/failure-improvement/' in request.url else None)
                    page.route('**/api/tasks', route_tasks)
                    page.route('**/api/tasks/failure-improvement/*', route_failure)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    panel = page.get_by_test_id('failure-improvement-panel')
                    expect(panel).to_be_visible()
                    self.assertGreater(len(panel.inner_text()), 100)
                    page.screenshot(path='/tmp/aos-failure-improvement-en-viewport.png')
                    self.assertEqual(calls, [])
                    self.assertEqual(writes, [])
                    self.assertNotIn(consent_value, page.locator('body').inner_text())
                    expect(page.get_by_test_id('failure-save')).to_have_count(0)
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/preview')) as previewed:
                        page.get_by_test_id('failure-preview').click()
                    self.assertEqual(previewed.value.status, 200)
                    expect(page.get_by_test_id('failure-improvement-result')).to_be_visible()
                    expect(page.get_by_test_id('failure-no-training')).to_contain_text('not available')
                    self.assertIn('RUNTIME_CRASH', page.get_by_test_id('failure-improvement-result').inner_text())
                    self.assertNotIn('private model prompt', page.locator('body').inner_text())
                    expect(page.get_by_test_id('failure-save')).to_be_disabled()
                    page.get_by_test_id('failure-consent').check()
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/save')) as saved:
                        page.get_by_test_id('failure-save').click()
                    self.assertEqual(saved.value.status, 200)
                    expect(page.get_by_test_id('failure-review-accept')).to_be_disabled()
                    page.get_by_test_id('failure-review-confirm').check()
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/review')) as reviewed:
                        page.get_by_test_id('failure-review-accept').click()
                    self.assertEqual(reviewed.value.status, 200)
                    expect(page.get_by_test_id('failure-review-status')).to_have_count(0)
                    expect(page.get_by_test_id('failure-improvement-panel')).to_contain_text('unavailable or changed')
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/preview')) as refreshed:
                        page.get_by_test_id('failure-preview').click()
                    self.assertEqual(refreshed.value.status, 200)
                    page.get_by_test_id('failure-review-confirm').check()
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/review')) as reviewed:
                        page.get_by_test_id('failure-review-accept').click()
                    self.assertEqual(reviewed.value.status, 200)
                    expect(page.get_by_test_id('failure-review-status')).to_contain_text('accept')
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/revoke')) as revoked:
                        page.get_by_test_id('failure-revoke').click()
                    self.assertEqual(revoked.value.status, 200)
                    expect(page.get_by_test_id('failure-review-status')).to_contain_text('revoked')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-improvement-en.png', full_page=True)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    expect(panel).to_contain_text('Başarısız görev')
                    expect(page.get_by_test_id('failure-preview')).to_contain_text('Tanı metadata bilgisini incele')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-improvement-tr-viewport.png')
                    page.screenshot(path='/tmp/aos-failure-improvement-tr.png', full_page=True)
                    self.assertEqual(len(writes), 6)
                    self.assertEqual([operation for operation, _ in calls],
                                     ['preview', 'save', 'review', 'preview', 'review', 'revoke'])
                    self.assertEqual([operation for operation, _ in calls if operation != 'preview'],
                                     ['save', 'review', 'review', 'revoke'])
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    expect(panel).to_contain_text('Başarısız görev')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-improvement-mobile-viewport.png')
                    page.screenshot(path='/tmp/aos-failure-improvement-mobile-tr.png', full_page=True)
                    page.get_by_test_id('failure-job-select').select_option(cancelled_job_id)
                    expect(page.get_by_test_id('failure-improvement-result')).to_have_count(0)
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                finally:
                    browser.close()

    def test_legacy_backend_fails_closed_without_posting(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='failure-improvement-old-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, _client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    writes = []
                    page.on('request', lambda request: writes.append(request.url)
                            if request.method == 'POST' and '/api/tasks/failure-improvement/' in request.url else None)

                    def legacy_tasks(route):
                        response = route.fetch()
                        payload = response.json()
                        payload.update({'busy': False, 'reserved': False, 'approval': None,
                            'failure_improvement_available': False,
                            'jobs': [{'job_id': 'failed-legacy-job', 'run_id': None, 'kind': 'hello',
                                      'real_model': 0, 'runtime_id': None, 'status': 'failed'}]})
                        route.fulfill(response=response, json=payload)

                    page.route('**/api/tasks', legacy_tasks)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('failure-improvement-panel')).to_be_visible()
                    expect(page.get_by_test_id('failure-preview')).to_have_count(0)
                    expect(page.get_by_test_id('failure-improvement-panel')).to_contain_text('does not provide')
                    self.assertEqual(writes, [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

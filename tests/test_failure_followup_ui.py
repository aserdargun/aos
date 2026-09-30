import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires built UI and the local desktop fixture')
class FailureFollowupUITests(unittest.TestCase):
    def test_reviewed_followup_requires_fresh_opt_in_and_keeps_report_historical(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        source_job_id = 'failed-followup-source'
        followup_job_id = 'fresh-followup-job'
        candidate = {
            'schema_version': '1.0', 'kind': 'failed_task_improvement', 'job_id': source_job_id,
            'session_id': 'session-followup', 'task_kind': 'browser_remote_form', 'run_ref': 'a' * 64,
            'source_sha256': 'b' * 64, 'scope_sha256': 'c' * 64, 'outcome': 'failed',
            'failure_code': 'RUNTIME_CRASH', 'synthetic': True,
            'model_roles': {
                'system1': {'deployment_id': 'decider-' + '1' * 64, 'real_model': True,
                            'calls': {'ok': 1, 'error': 1, 'timeout': 0, 'cancelled': 0}},
                'system2': {'deployment_id': None, 'real_model': False,
                            'calls': {'ok': 0, 'error': 0, 'timeout': 0, 'cancelled': 0}}},
            'approval_counts': {'consumed': 1, 'rejected': 0, 'expired': 0, 'revoked': 0},
            'action_counts': {'ok': 0, 'error': 1, 'denied': 0, 'cancelled': 0},
            'metadata_only': True, 'retrospective': True, 'training_ready': False, 'gold': False,
            'execution_authorized': False, 'failure_attribution_verified': False}
        receipt_sha256 = 'd' * 64
        improvement_candidate_sha256 = 'e' * 64
        review_options = [
            {'decision': 'accept', 'correction_code': 'refresh_observation', 'confirm_sha256': receipt_sha256},
            {'decision': 'accept', 'correction_code': 'inspect_before_retry', 'confirm_sha256': '1' * 64},
            {'decision': 'accept', 'correction_code': 'request_clarification', 'confirm_sha256': '2' * 64},
            {'decision': 'accept', 'correction_code': 'repair_environment', 'confirm_sha256': '3' * 64},
            {'decision': 'reject', 'correction_code': None, 'confirm_sha256': '4' * 64}]
        review = {'receipt_sha256': receipt_sha256, 'decision': 'accept',
                  'correction_code': 'refresh_observation', 'revoked': False}
        state = {'saved': False, 'review': None, 'followup_started': False, 'bad_preview': True, 'preview_attempts': 0,
                 'snapshot': None, 'followup_start_body': None, 'calls': []}

        def envelope():
            return {'schema_version': '1.0', 'job_id': source_job_id, 'available': True,
                    'candidate_sha256': improvement_candidate_sha256, 'candidate': candidate,
                    'saved': state['saved'], 'review': state['review'], 'review_options': review_options,
                    'source_current': True}

        def route_state(route):
            response = route.fetch()
            payload = response.json()
            payload['runtime']['runtime_id'] = 'runtime-followup'
            payload['runtime']['image_id'] = 'image-followup'
            payload['control'].update({'owner': 'AGENT', 'status': 'running',
                                       'lease_id': 'lease-followup', 'generation': 9})
            state['snapshot'] = payload
            route.fulfill(response=response, json=payload)

        def route_tasks(route):
            response = route.fetch()
            payload = response.json()
            jobs = [{'job_id': source_job_id, 'run_id': 'run-source', 'kind': candidate['task_kind'],
                     'real_model': 1, 'runtime_id': 'runtime-followup', 'status': 'failed',
                     'failure_followup': None}]
            if state['followup_started']:
                jobs.append({'job_id': followup_job_id, 'run_id': 'run-followup', 'kind': candidate['task_kind'],
                             'real_model': 1, 'runtime_id': 'runtime-followup', 'status': 'waiting_approval',
                             'failure_followup': {'intent_sha256': 'f' * 64, 'source_job_id': source_job_id}})
            payload.update({'busy': False, 'reserved': False, 'approval': None,
                'failure_improvement_available': True, 'failure_followup_available': True,
                'jobs': jobs, 'auto_approval': None})
            route.fulfill(response=response, json=payload)

        preview = None
        def route_improvement(route):
            operation = route.request.url.rsplit('/', 1)[-1]
            body = route.request.post_data_json
            if operation == 'preview':
                route.fulfill(json=envelope())
            elif operation == 'save':
                state['saved'] = True
                route.fulfill(json=envelope())
            elif operation == 'review':
                state['review'] = review.copy()
                route.fulfill(json=envelope())
            else:
                route.fulfill(status=404, json={'detail': 'unexpected fixture request'})

        def route_followup(route):
            nonlocal preview
            operation = route.request.url.rsplit('/', 1)[-1]
            body = route.request.post_data_json
            state['calls'].append(operation)
            if operation == 'preview':
                state['preview_attempts'] += 1
                self.assertEqual(body, {'schema_version': '1.0', 'source_job_id': source_job_id,
                    'candidate_sha256': improvement_candidate_sha256, 'receipt_sha256': receipt_sha256})
                target = {'session_id': candidate['session_id'], 'parent_runtime_id': 'runtime-followup',
                    'image_id': 'image-followup', 'lease_id': 'lease-followup', 'generation': 9,
                    'task_kind': candidate['task_kind'], 'configuration_sha256': '5' * 64,
                    'system1_deployment_id': 'decider-' + '1' * 64, 'system2_deployment_id': None}
                attempt = state['preview_attempts']
                preview = {'schema_version': '1.0', 'attempt_id': 'followup-' + f'{attempt:032x}',
                    'source_job_id': source_job_id, 'candidate_sha256': improvement_candidate_sha256,
                    'receipt_sha256': receipt_sha256, 'source_sha256': candidate['source_sha256'],
                    'target': target, 'source_contract': {'semantic_sha256': '7' * 64, 'binding_sha256': '8' * 64},
                    'correction_code': 'refresh_observation', 'manual_approval_required': True,
                    'guidance_applied': False, 'causality_verified': False, 'gold': False,
                    'training_ready': False, 'confirm_sha256': str(attempt) * 64}
                if state['bad_preview']:
                    state['bad_preview'] = False
                    route.fulfill(json=preview | {'candidate_sha256': 'a' * 64})
                    return
                route.fulfill(json=preview)
            elif operation == 'start':
                self.assertEqual(set(body), {'schema_version', 'preview', 'confirm_sha256', 'consent', 'lease_id', 'generation'})
                self.assertEqual(body['schema_version'], '1.0')
                self.assertEqual(body['preview'], preview)
                self.assertEqual(body['confirm_sha256'], preview['confirm_sha256'])
                self.assertIs(body['consent'], True)
                self.assertEqual((body['lease_id'], body['generation']), ('lease-followup', 9))
                state['followup_start_body'] = body
                state['followup_started'] = True
                route.fulfill(json={'schema_version': '1.0', 'job_id': followup_job_id,
                    'intent_sha256': 'f' * 64, 'manual_approval_required': True})
            elif operation == 'inspect':
                self.assertEqual(body, {'schema_version': '1.0', 'job_id': followup_job_id})
                route.fulfill(json={'schema_version': '1.0', 'job_id': followup_job_id,
                    'source_job_id': source_job_id, 'intent_sha256': 'f' * 64,
                    'historical_binding_verified': True, 'current_source_valid': False,
                    'current_review_valid': False,
                    'outcome': {'status': 'verified', 'verification_count': 1,
                        'scope': 'fixed_task_outcome', 'reason': 'independent_evidence_verified'},
                    'manual_approval_required': True, 'guidance_applied': False,
                    'causality_verified': False, 'gold': False, 'training_ready': False})
            else:
                route.fulfill(status=404, json={'detail': 'unexpected fixture request'})

        with tempfile.TemporaryDirectory(prefix='failure-followup-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, _client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text)
                            if message.type in ('error', 'warning') else None)
                    page.route('**/api/state', route_state)
                    page.route('**/api/tasks', route_tasks)
                    page.route('**/api/tasks/failure-improvement/*', route_improvement)
                    page.route('**/api/tasks/failure-followup/*', route_followup)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    followup = page.get_by_test_id('failure-followup-panel')
                    self.assertEqual(state['calls'], [])
                    expect(followup).to_have_count(0)
                    page.screenshot(path='/tmp/aos-failure-followup-en-viewport.png')
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/preview')):
                        page.get_by_test_id('failure-preview').click()
                    page.get_by_test_id('failure-consent').check()
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/save')):
                        page.get_by_test_id('failure-save').click()
                    page.get_by_test_id('failure-review-confirm').check()
                    with page.expect_response(lambda response: response.url.endswith('/failure-improvement/review')):
                        page.get_by_test_id('failure-review-accept').click()
                    expect(followup).to_be_visible()
                    expect(page.get_by_test_id('failure-followup-preview')).to_be_enabled()
                    with page.expect_response(lambda response: response.url.endswith('/failure-followup/preview')):
                        page.get_by_test_id('failure-followup-preview').click()
                    expect(page.get_by_test_id('failure-followup-preview-result')).to_have_count(0)
                    expect(followup).to_contain_text('unavailable or changed')
                    with page.expect_response(lambda response: response.url.endswith('/failure-followup/preview')):
                        page.get_by_test_id('failure-followup-preview').click()
                    expect(page.get_by_test_id('failure-followup-preview-result')).to_be_visible()
                    expect(page.get_by_test_id('failure-followup-start')).to_be_disabled()
                    page.get_by_test_id('failure-followup-consent').check()
                    page.get_by_test_id('failure-followup-confirm').fill('0' * 64)
                    expect(page.get_by_test_id('failure-followup-start')).to_be_disabled()
                    page.get_by_test_id('failure-followup-confirm').fill(preview['confirm_sha256'])
                    expect(page.get_by_test_id('failure-followup-start')).to_be_enabled()
                    with page.expect_response(lambda response: response.url.endswith('/failure-followup/start')) as started:
                        page.get_by_test_id('failure-followup-start').click()
                    self.assertEqual(started.value.status, 200)
                    expect(page.get_by_test_id('failure-followup-started')).to_contain_text(followup_job_id)
                    self.assertIsNotNone(state['followup_start_body'])
                    self.assertNotIn('approve_all', state['followup_start_body'])
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('failure-followup-job')).to_contain_text(followup_job_id[:12], timeout=5000)
                    with page.expect_response(lambda response: response.url.endswith('/failure-followup/inspect')):
                        page.get_by_test_id('failure-followup-inspect').click()
                    expect(page.get_by_test_id('failure-followup-report')).to_contain_text('Verified')
                    expect(page.get_by_test_id('failure-followup-report')).to_contain_text('Current source valid: No')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-followup-en.png', full_page=True)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(followup).to_contain_text('Açık takip görevi')
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-followup-tr-viewport.png')
                    page.screenshot(path='/tmp/aos-failure-followup-tr.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-failure-followup-mobile-viewport.png')
                    page.screenshot(path='/tmp/aos-failure-followup-mobile-tr.png', full_page=True)
                    self.assertEqual(state['calls'], ['preview', 'preview', 'start', 'inspect'])
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                finally:
                    browser.close()

    def test_old_backend_does_not_offer_followup_controls(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='failure-followup-old-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, _client, _server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    writes = []
                    page.on('request', lambda request: writes.append(request.url)
                            if request.method == 'POST' and '/failure-followup/' in request.url else None)
                    def route_tasks(route):
                        response = route.fetch()
                        payload = response.json()
                        payload['failure_improvement_available'] = True
                        payload['failure_followup_available'] = False
                        payload['jobs'] = [{'job_id': 'failed-job', 'run_id': 'run-failed', 'kind': 'hello',
                            'real_model': 0, 'runtime_id': None, 'status': 'failed', 'failure_followup': None}]
                        route.fulfill(response=response, json=payload)
                    page.route('**/api/tasks', route_tasks)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('failure-followup-panel')).to_have_count(0)
                    expect(page.get_by_test_id('failure-followup-preview')).to_have_count(0)
                    self.assertEqual(writes, [])
                finally:
                    browser.close()

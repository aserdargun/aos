import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires Docker and built UI; candidate endpoints are synthetic route fixtures')
class OwnedSkillCandidateUITests(unittest.TestCase):
    def test_explicit_preview_publication_reinspection_and_invalid_results(self):
        self._check_flow(execution=False)

    def test_execution_confirmation_stale_preview_and_audit_claims(self):
        self._check_flow(execution=True)

    def test_review_confirmation_revocation_and_no_unreviewed_fallback(self):
        self._check_flow(execution=True, review=True)

    def _check_flow(self, *, execution, review=False):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        annotation = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_candidate.json').read_text())['candidate']['annotation']
        invocation = {
            'mode': 'owned_synthetic_form_invocation', 'lifecycle': 'audited',
            'profile_sha256': 'e' * 64, 'skill_sha256': 'b' * 64,
            'invocation_sha256': 'a' * 64, 'run_ref': '4' * 64, 'report_sha256': '9' * 64,
        }
        context = {
            'schema_version': '1.0', 'available': True, 'mode': invocation['mode'],
            'lifecycle': 'audited', 'source_run_ref': invocation['run_ref'],
            'invocation_sha256': invocation['invocation_sha256'],
            'profile_sha256': invocation['profile_sha256'], 'task_sha256': 'd' * 64,
            'page_draft_sha256': annotation['page_draft_sha256'],
            'task_key': annotation['task_key'], 'form_fields': ['message'], 'annotation_seed': annotation,
        }
        calls = []
        phase = {'invalid': None, 'runtime_drift': False, 'execution': None}
        operations = ('open_entry', 'read_state_before', 'fill_form', 'submit_form', 'read_receipt', 'read_state_after')

        with tempfile.TemporaryDirectory(prefix='owned-candidate-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def console_message(message):
                        if message.type in ('error', 'warning'):
                            if (message.location.get('url') == origin + '/api/remote-form/repeats'
                                    and '404 (Not Found)' in message.text):
                                return
                            errors.append(message.text)

                    page.on('console', console_message)

                    def route_tasks(route):
                        self.assertEqual(route.request.method, 'GET')
                        route.fulfill(json={
                            'available': True, 'busy': False, 'paused': False, 'reserved': False,
                            'supports_approve_all': True, 'supports_learning_metadata': False,
                            'kinds': ['browser_remote_form'], 'browser_display': 'desktop',
                            'owned_form_invocation': invocation,
                            'owned_form_candidate_execution': phase['execution'],
                            'remote_form': {
                                'plan_sha256': '1' * 64, 'entry_url': 'https://form.example.invalid/',
                                'submit_url': 'https://form.example.invalid/submit',
                                'receipt_url': 'https://form.example.invalid/receipt',
                                'field_name': 'message', 'field_names': ['message'], 'body_sha256': '2' * 64,
                                'state_plan_sha256': '3' * 64, 'state_url': 'https://form.example.invalid/state',
                                'mode': 'synthetic_one_post',
                            },
                            'jobs': [{'job_id': 'owned-job', 'run_id': invocation['run_ref'],
                                      'kind': 'browser_remote_form', 'status': 'succeeded',
                                      'real_model': 0, 'runtime_id': 'runtime-owned'}], 'approval': None,
                        })

                    def route_candidate(route):
                        action = route.request.url.rsplit('/', 1)[-1]
                        body = route.request.post_data_json if route.request.method == 'POST' else None
                        calls.append((action, body))
                        if action.startswith('review-'):
                            current = phase['execution']
                            summary = {'skill_key': 'save-record', 'expected_outcome_key': 'record-saved',
                                'parameter_key': 'record-query', 'form_field_name': 'message', 'steps': current['steps']}
                            receipt = {key: current[key] for key in ('candidate_sha256', 'source_run_ref',
                                'source_invocation_sha256', 'source_group_sha256', 'candidate_execution_sha256',
                                'invocation_sha256', 'recipe_sha256', 'skill_sha256', 'profile_sha256',
                                'case_key', 'parameter_variant_sha256')}
                            receipt.update(schema_version='1.0', synthetic=True, decision='accept',
                                purpose='development_review', reviewer='local_authenticated_user',
                                source_fingerprint_sha256='d' * 64, execution_run_ref=current['run_ref'],
                                summary=summary, activation_authorized=False, training_ready=False, independent_held_out=False)
                            if action in {'review-accept', 'review-revoke'}:
                                self.assertEqual(body['review_sha256'], body['confirm_sha256'])
                            response = {'schema_version': '1.0', 'available': True,
                                'status': 'preview' if action == 'review-preview' else 'revoked' if action == 'review-revoke' else 'accepted',
                                'review_sha256': 'b' * 64, 'receipt': receipt, 'summary': summary,
                                'revocation_sha256': 'c' * 64 if action == 'review-revoke' else None}
                            if phase['invalid'] == 'review_claim':
                                receipt['activation_authorized'] = True
                            route.fulfill(json=response)
                            return
                        if action.startswith('execution-'):
                            common = {
                                'schema_version': '1.0', 'candidate_sha256': 'c' * 64,
                                'source_run_ref': context['source_run_ref'],
                                'source_invocation_sha256': context['invocation_sha256'],
                                'source_group_sha256': 'f' * 64, 'profile_sha256': context['profile_sha256'],
                                'skill_sha256': 'f' * 64, 'case_key': 'dev-beta',
                                'parameter_variant_sha256': '7' * 64, 'recipe_sha256': 'f' * 64,
                                'invocation_sha256': '8' * 64,
                                'steps': [next(item for item in annotation['operation_step_keys']
                                               if item['operation'] == operation) for operation in operations],
                            }
                            if action == 'execution-preview':
                                self.assertEqual(set(body), {'schema_version', 'candidate_sha256', 'source_run_ref',
                                    'source_invocation_sha256', 'case_key', 'development_value'}
                                    | ({'review_sha256'} if 'review_sha256' in body else set()))
                                self.assertEqual(body['development_value'], 'beta')
                                response = {**common, 'available': True, 'status': 'preview', 'preview_sha256': '2' * 64,
                                            'form_plan_sha256': '3' * 64, 'state_plan_sha256': '5' * 64,
                                            'purpose': 'development_variation', 'independent_held_out': False, 'report': None}
                                if phase['invalid'] == 'preview_claim':
                                    response['independent_held_out'] = True
                                if 'review_sha256' in body and phase['invalid'] != 'review_downgrade':
                                    response.update(schema_version='1.1', review_sha256=body['review_sha256'])
                                route.fulfill(json=response)
                            elif action == 'execution-start':
                                self.assertEqual(body['confirm_sha256'], '2' * 64)
                                self.assertEqual(body['preview_sha256'], body['confirm_sha256'])
                                self.assertEqual(body['development_value'], 'beta')
                                control = client.get('/api/state').json()['control']
                                self.assertEqual(body['lease_id'], control['lease_id'])
                                self.assertEqual(body['generation'], control['generation'])
                                phase['execution'] = {**common, 'mode': 'owned_candidate_development',
                                    'lifecycle': 'completed', 'candidate_execution_sha256': '6' * 64,
                                    'job_id': 'candidate-job', 'run_id': 'candidate-run', 'run_ref': '9' * 64,
                                    'report_sha256': None}
                                route.fulfill(json={**common, 'accepted': True, 'lifecycle': 'running',
                                    'candidate_execution_sha256': '6' * 64, 'job_id': 'candidate-job', 'run_id': 'candidate-run'})
                            else:
                                self.assertEqual(body, {'candidate_execution_sha256': '6' * 64})
                                report = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_candidate_execution.json').read_text())['report']
                                report.update(source_run_ref=common['source_run_ref'], execution_run_ref='9' * 64,
                                              source_group_sha256=common['source_group_sha256'])
                                for key in ('candidate_sha256', 'source_group_sha256', 'source_run_ref', 'parameter_variant_sha256',
                                            'invocation_sha256', 'recipe_sha256', 'skill_sha256', 'profile_sha256'):
                                    report['admission'][key] = common[key]
                                if phase['invalid'] == 'audit_claim':
                                    report['training_ready'] = True
                                phase['execution'].update(lifecycle='audited', report_sha256='a' * 64)
                                route.fulfill(json={**phase['execution'], 'available': True, 'status': 'verified', 'report': report})
                            return
                        if action == 'context':
                            route.fulfill(json=context)
                            return
                        self.assertEqual(body['source_run_ref'], context['source_run_ref'])
                        self.assertEqual(body['invocation_sha256'], context['invocation_sha256'])
                        expected = {'schema_version', 'source_run_ref', 'invocation_sha256'}
                        expected |= {'candidate_sha256'} if action == 'inspect' else {'annotation'}
                        if action == 'publish':
                            expected.add('confirm_sha256')
                            self.assertEqual(body['confirm_sha256'], 'c' * 64)
                        self.assertEqual(set(body), expected)
                        names = body.get('annotation', annotation)['operation_step_keys']
                        summary = {
                            'schema_version': '1.0', 'candidate_schema_version': '1.1',
                            'candidate_sha256': 'c' * 64,
                            **{key: context[key] for key in ('source_run_ref', 'invocation_sha256', 'profile_sha256')},
                            **{key: 'f' * 64 for key in ('source_group_sha256', 'source_fingerprint_sha256',
                                                       'source_parameter_variant_sha256', 'skill_sha256', 'recipe_sha256')},
                            'steps': [next(item for item in names if item['operation'] == operation) for operation in operations],
                            'status': 'unreviewed_executable_recipe_candidate',
                            **{key: False for key in ('reviewed', 'activation_authorized', 'training_ready',
                                                    'site_outcome_verified', 'skill_executed')},
                        }
                        response = {
                            'schema_version': '1.0', 'available': True,
                            'status': {'preview': 'preview', 'publish': 'published', 'inspect': 'reinspected'}[action],
                            'persisted': action != 'preview', 'candidate_sha256': 'c' * 64, 'summary': summary,
                        }
                        if phase['invalid'] == 'source':
                            summary['source_run_ref'] = '0' * 64
                        elif phase['invalid'] == 'claim':
                            summary['training_ready'] = True
                        elif phase['invalid'] == 'persisted':
                            response['persisted'] = True
                        elif phase['invalid'] == 'steps':
                            summary['steps'] = list(reversed(summary['steps']))
                        elif phase['invalid'] == 'hash':
                            summary['candidate_sha256'] = '0' * 64
                        route.fulfill(json=response)

                    page.route('**/api/tasks', route_tasks)
                    page.route('**/api/tasks/owned-form-candidate/*', route_candidate)

                    def route_state(route):
                        response = route.fetch()
                        payload = response.json()
                        if phase['runtime_drift']:
                            payload['runtime']['runtime_id'] = 'replacement-runtime'
                        route.fulfill(response=response, json=payload)

                    page.route('**/api/state', route_state)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Control center')
                    page.get_by_label('Local session token').fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    expect(page.get_by_test_id('development-status')).to_contain_text('Saved candidate execution with new inputs')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_test_id('development-status')).to_contain_text('Kaydedilmiş adayın yeni girdilerle yürütülmesi')
                    page.get_by_role('button', name='English', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    panel = page.get_by_test_id('owned-skill-candidate')
                    expect(panel).to_be_visible()
                    self.assertEqual(calls, [])
                    page.get_by_test_id('candidate-context').click()
                    expect(page.get_by_test_id('candidate-preview')).to_be_disabled()
                    self.assertEqual([action for action, body in calls], ['context'])
                    page.get_by_test_id('candidate-skill-key').fill('operator-declared-skill')
                    page.get_by_test_id('candidate-names-confirmed').check()
                    page.get_by_test_id('candidate-preview').click()
                    expect(page.get_by_test_id('candidate-result')).to_contain_text('Preview')
                    expect(page.get_by_test_id('candidate-publish')).to_be_disabled()
                    self.assertEqual(calls[-1][1]['annotation']['skill_key'], 'operator-declared-skill')
                    self.assertEqual([item['operation'] for item in calls[-1][1]['annotation']['operation_step_keys']], sorted(operations))
                    page.get_by_test_id('candidate-publish-confirm').check()
                    page.get_by_test_id('candidate-publish').click()
                    expect(page.get_by_test_id('candidate-result')).to_contain_text('Unreviewed candidate saved')
                    expect(page.get_by_test_id('candidate-inspect-hash')).to_have_value('c' * 64)
                    page.get_by_test_id('candidate-inspect').click()
                    expect(page.get_by_test_id('candidate-result')).to_contain_text('revalidated')
                    if execution:
                        execution_panel = page.get_by_test_id('candidate-execution')
                        expect(execution_panel).to_be_visible()
                        expect(page.get_by_test_id('execution-candidate-hash')).to_have_value('c' * 64)
                        expect(page.get_by_test_id('execution-preview')).to_be_disabled()
                        page.get_by_test_id('execution-case-key').fill('dev-beta')
                        page.get_by_test_id('execution-value').fill(' beta')
                        expect(page.get_by_test_id('execution-preview')).to_be_disabled()
                        page.get_by_test_id('execution-value').fill('beta')
                        phase['invalid'] = 'preview_claim'
                        page.get_by_test_id('execution-preview').click()
                        expect(page.get_by_test_id('execution-unavailable')).to_be_visible()
                        expect(page.get_by_test_id('execution-preview-result')).to_have_count(0)
                        phase['invalid'] = None
                        page.get_by_test_id('execution-preview').click()
                        expect(page.get_by_test_id('execution-start')).to_be_disabled()
                        page.get_by_test_id('execution-confirm').check()
                        page.get_by_test_id('execution-value').fill('changed')
                        expect(page.get_by_test_id('execution-preview-result')).to_have_count(0)
                        self.assertFalse(any(action == 'execution-start' for action, body in calls))
                        page.get_by_test_id('execution-value').fill('beta')
                        page.get_by_test_id('execution-preview').click()
                        page.get_by_test_id('execution-confirm').check()
                        page.get_by_test_id('execution-start').click()
                        expect(page.get_by_test_id('execution-audit')).to_be_visible(timeout=15000)
                        expect(page.get_by_test_id('execution-audit-result')).to_have_count(0)
                        self.assertFalse(any(action == 'execution-audit' for action, body in calls))
                        phase['invalid'] = 'audit_claim'
                        page.get_by_test_id('execution-audit').click()
                        expect(page.get_by_test_id('execution-unavailable')).to_be_visible()
                        expect(page.get_by_test_id('execution-audit-result')).to_have_count(0)
                        phase['invalid'] = None
                        page.get_by_test_id('execution-audit').click()
                        expect(page.get_by_test_id('execution-audit-result')).to_contain_text('Source-bound development execution verified')
                        if review:
                            phase['invalid'] = 'review_claim'
                            page.get_by_test_id('review-preview').click()
                            expect(page.get_by_test_id('review-unavailable')).to_be_visible()
                            expect(page.get_by_test_id('review-result')).to_have_count(0)
                            phase['invalid'] = None
                            page.get_by_test_id('review-preview').click()
                            expect(page.get_by_test_id('review-accept')).to_be_disabled()
                            self.assertFalse(any(action == 'review-accept' for action, body in calls))
                            page.get_by_test_id('review-confirm').check()
                            page.get_by_test_id('review-accept').click()
                            expect(page.get_by_test_id('review-result')).to_contain_text('accepted')
                            expect(page.get_by_test_id('execution-review-hash')).to_have_value('b' * 64)
                            expect(page.get_by_test_id('execution-use-review')).not_to_be_checked()
                            self.assertEqual(sum(action == 'execution-start' for action, body in calls), 1)
                            page.get_by_test_id('execution-use-review').check()
                            phase['invalid'] = 'review_downgrade'
                            page.get_by_test_id('execution-preview').click()
                            expect(page.get_by_test_id('execution-unavailable')).to_be_visible()
                            expect(page.get_by_test_id('execution-preview-result')).to_have_count(0)
                            phase['invalid'] = None
                            page.get_by_test_id('execution-preview').click()
                            expect(page.get_by_test_id('execution-preview-result')).to_be_visible()
                            self.assertEqual(calls[-1][1]['schema_version'], '1.1')
                            self.assertEqual(calls[-1][1]['review_sha256'], 'b' * 64)
                            page.get_by_test_id('execution-use-review').uncheck()
                            expect(page.get_by_test_id('execution-preview-result')).to_have_count(0)
                            expect(page.get_by_test_id('review-revoke')).to_be_disabled()
                            page.get_by_test_id('review-revoke-confirm').check()
                            page.get_by_test_id('review-revoke').click()
                            expect(page.get_by_test_id('review-result')).to_contain_text('revoked')
                            page.get_by_test_id('candidate-review').scroll_into_view_if_needed()
                            page.screenshot(path='/tmp/aos-review-ui-en.png')
                        execution_panel.scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-candidate-execution-ui-en.png')
                        page.get_by_role('button', name='Türkçe', exact=True).click()
                        page.set_viewport_size({'width': 390, 'height': 844})
                        expect(execution_panel).to_contain_text('Kaynak kökenine bağlı gelişim yürütmesi doğrulandı')
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                        execution_panel.scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-candidate-execution-ui-tr-mobile.png')
                        if review:
                            expect(page.get_by_test_id('candidate-review')).to_contain_text('İnceleme geri çekildi')
                            page.get_by_test_id('candidate-review').scroll_into_view_if_needed()
                            page.screenshot(path='/tmp/aos-review-ui-tr-mobile.png')
                        self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                        self.assertEqual(errors, [])
                        self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                        return
                    panel.scroll_into_view_if_needed()
                    page.screenshot(path='/tmp/aos-owned-candidate-ui-en.png')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    expect(panel).to_contain_text('Bu deneyimden skill adayı oluştur')
                    panel.scroll_into_view_if_needed()
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    page.screenshot(path='/tmp/aos-owned-candidate-ui-tr-mobile.png')
                    page.get_by_test_id('candidate-skill-key').fill('renamed-skill')
                    expect(page.get_by_test_id('candidate-result')).to_have_count(0)
                    expect(page.get_by_test_id('candidate-preview')).to_be_disabled()
                    page.get_by_test_id('candidate-names-confirmed').check()
                    for invalid in ('source', 'claim', 'persisted', 'steps', 'hash'):
                        with self.subTest(invalid=invalid):
                            phase['invalid'] = invalid
                            page.get_by_test_id('candidate-preview').click()
                            expect(page.get_by_test_id('candidate-unavailable')).to_be_visible()
                            expect(page.get_by_test_id('candidate-result')).to_have_count(0)
                    phase['invalid'] = None
                    previous_calls = len(calls)
                    page.reload()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    expect(panel).to_be_visible()
                    self.assertEqual(len(calls), previous_calls)
                    page.get_by_test_id('candidate-context').click()
                    expect(page.get_by_test_id('candidate-inspect-hash')).to_have_value('')
                    page.get_by_test_id('candidate-inspect-hash').fill('c' * 64)
                    page.get_by_test_id('candidate-inspect').click()
                    expect(page.get_by_test_id('candidate-result')).to_contain_text('yeniden doğrulandı')
                    page.get_by_test_id('candidate-names-confirmed').check()
                    phase['runtime_drift'] = True
                    page.get_by_test_id('candidate-preview').click()
                    expect(page.get_by_test_id('candidate-result')).to_have_count(0)
                    expect(page.get_by_test_id('candidate-preview')).to_be_enabled()
                    invocation.update(mode='owned_synthetic_form_recipe', recipe_sha256='8' * 64,
                                      steps=[next(item for item in annotation['operation_step_keys']
                                                  if item['operation'] == operation) for operation in operations])
                    page.reload()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    expect(page.get_by_test_id('owned-form-recipe')).to_be_visible()
                    expect(panel).to_have_count(0)
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                finally:
                    browser.close()

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT, digest


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires Docker and built UI; release APIs use synthetic route fixtures')
class OwnedSkillReleaseUITests(unittest.TestCase):
    def test_reviewed_release_selection_rollback_and_run_binding(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        invocation = {'mode': 'owned_synthetic_form_invocation', 'lifecycle': 'audited',
            'profile_sha256': 'e' * 64, 'skill_sha256': 'b' * 64,
            'invocation_sha256': 'a' * 64, 'run_ref': '4' * 64, 'report_sha256': '9' * 64}
        candidate = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_candidate.json').read_text())['candidate']
        annotation = candidate['annotation']
        operations = ('open_entry', 'read_state_before', 'fill_form', 'submit_form', 'read_receipt', 'read_state_after')
        steps = [next(item for item in annotation['operation_step_keys'] if item['operation'] == op) for op in operations]
        execution = {'schema_version': '1.0', 'mode': 'owned_candidate_development', 'lifecycle': 'audited',
            'candidate_execution_sha256': '6' * 64, 'candidate_sha256': 'c' * 64,
            'source_run_ref': invocation['run_ref'], 'source_invocation_sha256': invocation['invocation_sha256'],
            'source_group_sha256': 'f' * 64, 'profile_sha256': invocation['profile_sha256'],
            'skill_sha256': 'b' * 64, 'case_key': 'dev-alpha', 'parameter_variant_sha256': '7' * 64,
            'recipe_sha256': '8' * 64, 'steps': steps, 'invocation_sha256': '5' * 64,
            'job_id': 'owned-job', 'run_id': 'execution-run', 'run_ref': '3' * 64, 'report_sha256': '2' * 64}
        context = {'schema_version': '1.0', 'available': True, 'mode': invocation['mode'],
            'lifecycle': 'audited', 'source_run_ref': invocation['run_ref'],
            'invocation_sha256': invocation['invocation_sha256'], 'profile_sha256': invocation['profile_sha256'],
            'task_sha256': 'd' * 64, 'page_draft_sha256': annotation['page_draft_sha256'],
            'task_key': annotation['task_key'], 'form_fields': ['message'], 'annotation_seed': annotation}
        review_hash = '1' * 64
        review_summary = {'skill_key': 'save-record', 'expected_outcome_key': 'record-saved',
            'parameter_key': 'record-query', 'form_field_name': 'message', 'steps': steps}
        receipt = {'candidate_sha256': execution['candidate_sha256'], 'source_run_ref': invocation['run_ref'],
            'source_invocation_sha256': invocation['invocation_sha256'],
            'candidate_execution_sha256': execution['candidate_execution_sha256'],
            'schema_version': '1.0', 'synthetic': True, 'decision': 'accept',
            'purpose': 'development_review', 'reviewer': 'local_authenticated_user',
            'source_group_sha256': execution['source_group_sha256'], 'source_fingerprint_sha256': '9' * 64,
            'execution_run_ref': execution['run_ref'], 'invocation_sha256': execution['invocation_sha256'],
            'recipe_sha256': execution['recipe_sha256'], 'skill_sha256': execution['skill_sha256'],
            'profile_sha256': invocation['profile_sha256'], 'case_key': execution['case_key'],
            'parameter_variant_sha256': execution['parameter_variant_sha256'], 'summary': review_summary,
            'activation_authorized': False, 'training_ready': False, 'independent_held_out': False}
        receipt.update(candidate_sha256=execution['candidate_sha256'])
        review = {'schema_version': '1.0', 'available': True, 'status': 'preview',
            'review_sha256': review_hash, 'revocation_sha256': None, 'summary': review_summary, 'receipt': receipt}

        family = {'profile_sha256': invocation['profile_sha256'], 'application_key': 'demo-app',
            'tenant_key': 'demo-tenant', 'account_role': 'demo-role', 'task_key': annotation['task_key'],
            'task_sha256': context['task_sha256'], 'page_draft_sha256': annotation['page_draft_sha256'],
            'skill_key': 'save_record', 'model_role': 'system1', 'field_binding_sha256': 'a' * 64}
        family_hash = digest(family)
        old_release = self._release(family, family_hash, '5' * 64, '4' * 64, 1, None, '9' * 64)
        old_hash = digest(old_release)
        current_release = None
        current_hash = None
        release_published = False
        selected_hash = '2' * 64
        sequence = 1
        selected_release_hash = old_hash
        catalog_mutation = {'selection': False, 'revoked': False}
        task_busy = {'value': False}
        invalid = {'cas': False, 'downgrade': False}
        calls = []
        writes = []

        def catalog():
            releases = [{'release_sha256': old_hash, 'release': old_release,
                'review_status': 'revoked' if catalog_mutation['revoked'] else 'accepted',
                'rollback_eligible': release_published and not catalog_mutation['revoked']}]
            if current_release and release_published:
                releases.append({'release_sha256': current_hash, 'release': current_release,
                    'review_status': 'accepted', 'rollback_eligible': False})
            return {'schema_version': '1.0', 'available': True, 'families': [{
                'family_sha256': family_hash, 'family': family,
                'selection_sha256': '0' * 64 if catalog_mutation['selection'] else selected_hash,
                'selected_release_sha256': selected_release_hash, 'sequence': sequence, 'releases': releases}]}

        def route_tasks(route):
            response = route.fetch()
            data = response.json()
            data.update(owned_form_invocation=invocation, owned_form_candidate_execution=execution,
                busy=task_busy['value'], reserved=False, kinds=['browser_remote_form'], browser_display='desktop',
                jobs=[{'job_id': 'owned-job', 'run_id': invocation['run_ref'], 'kind': 'browser_remote_form',
                       'status': 'succeeded', 'real_model': 0, 'runtime_id': 'runtime-owned'}], approval=None)
            route.fulfill(response=response, json=data)

        def route_owned(route):
            nonlocal current_release, current_hash, release_published, selected_hash, sequence, selected_release_hash
            action = route.request.url.rsplit('/', 1)[-1]
            body = route.request.post_data_json if route.request.method == 'POST' else None
            calls.append((action, copy.deepcopy(body)))
            if route.request.method == 'POST':
                writes.append(action)
            if action == 'context':
                route.fulfill(json=context); return
            if action == 'review-preview':
                route.fulfill(json=review); return
            if action == 'review-accept':
                route.fulfill(json={**review, 'status': 'accepted'}); return
            if action == 'release-catalog':
                route.fulfill(json=catalog()); return
            if action == 'release-preview':
                self.assertEqual(body, {'schema_version': '1.0', 'review_sha256': review_hash,
                                        'parent_release_sha256': old_hash})
                current_release = self._release(family, family_hash, execution['candidate_sha256'],
                    invocation['run_ref'], 2, old_hash, review_hash)
                current_hash = digest(current_release)
                result = {'schema_version': '1.0', 'available': True, 'status': 'preview',
                    'persisted': False, 'release_sha256': current_hash, 'release': current_release}
                if invalid['downgrade']:
                    result['schema_version'] = '1.1'
                route.fulfill(json=result); return
            if action == 'release-publish':
                self.assertEqual(body['confirm_sha256'], current_hash)
                release_published = True
                route.fulfill(json={'schema_version': '1.0', 'available': True, 'status': 'published',
                    'persisted': True, 'release_sha256': current_hash, 'release': current_release}); return
            if action.startswith('selection-'):
                entry_hash = body['release_sha256']
                operation = body['operation']
                target = next(item for item in catalog()['families'][0]['releases'] if item['release_sha256'] == entry_hash)
                prior_selection = body['expected_selection_sha256']
                if invalid['cas']:
                    prior_selection = '0' * 64
                event = {'schema_version': '1.0', 'synthetic': True, 'purpose': 'development_selected',
                    'family_sha256': family_hash, 'sequence': sequence + 1,
                    'previous_selection_sha256': prior_selection,
                    'previous_release_sha256': selected_release_hash, 'release_sha256': entry_hash,
                    'operation': operation,
                    'rollback_evidence_execution_sha256': '6' * 64 if operation == 'rollback' else None,
                    'actor': 'local_authenticated_user', 'activation_authorized': False, 'training_ready': False}
                checksum = digest(event)
                status = 'selected' if action == 'selection-commit' else 'preview'
                persisted = action == 'selection-commit'
                if persisted:
                    sequence += 1; selected_hash = checksum; selected_release_hash = entry_hash
                route.fulfill(json={'schema_version': '1.0', 'available': True, 'status': status,
                    'persisted': persisted, 'selection_sha256': checksum, 'selection': event,
                    'release_sha256': entry_hash, 'review_sha256': target['release']['review_sha256'],
                    'family_sha256': family_hash}); return
            if action == 'execution-preview':
                self.assertEqual(body['schema_version'], '1.2')
                self.assertEqual(body['release_sha256'], current_hash if selected_release_hash == current_hash else old_hash)
                self.assertEqual(body['selection_sha256'], selected_hash)
                selected_item = next(item for item in catalog()['families'][0]['releases']
                                     if item['release_sha256'] == selected_release_hash)
                execution.update(schema_version='1.2', lifecycle='previewed',
                    candidate_execution_sha256=None, job_id=None, run_id=None, run_ref=None,
                    report_sha256=None, preview_sha256='c' * 64,
                    candidate_sha256=body['candidate_sha256'], case_key=body['case_key'],
                    review_sha256=selected_item['release']['review_sha256'], review_status='accepted',
                    release_sha256=body['release_sha256'], selection_sha256=body['selection_sha256'],
                    family_sha256=family_hash, selection_status='current')
                route.fulfill(json={'schema_version': '1.2', 'available': True, 'status': 'preview',
                    'preview_sha256': 'c' * 64, 'candidate_sha256': body['candidate_sha256'],
                    'source_run_ref': body['source_run_ref'], 'source_invocation_sha256': body['source_invocation_sha256'],
                    'source_group_sha256': execution['source_group_sha256'], 'profile_sha256': invocation['profile_sha256'],
                    'skill_sha256': execution['skill_sha256'], 'case_key': body['case_key'],
                    'parameter_variant_sha256': execution['parameter_variant_sha256'],
                    'recipe_sha256': execution['recipe_sha256'], 'steps': steps,
                    'invocation_sha256': '5' * 64, 'form_plan_sha256': '3' * 64, 'state_plan_sha256': '4' * 64,
                    'purpose': 'development_variation', 'independent_held_out': False, 'report': None,
                    'review_sha256': selected_item['release']['review_sha256'], 'release_sha256': body['release_sha256'],
                    'selection_sha256': body['selection_sha256']}); return
            if action == 'execution-start':
                self.fail('A release selection must not auto-start execution')
            route.fulfill(status=404, json={'detail': 'unexpected synthetic fixture request'})

        with tempfile.TemporaryDirectory(prefix='owned-skill-release-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.route('**/api/tasks', route_tasks)
                    page.route('**/api/tasks/owned-form-candidate/*', route_owned)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token').fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.locator('#task-kind').select_option('browser_remote_form')
                    panel = page.get_by_test_id('skill-releases')
                    expect(panel).to_be_visible()
                    page.get_by_test_id('review-preview').click()
                    expect(page.get_by_test_id('review-result')).to_contain_text('preview')
                    page.get_by_test_id('review-confirm').check()
                    page.get_by_test_id('review-accept').click()
                    expect(page.get_by_test_id('review-result')).to_contain_text('accepted')
                    page.get_by_test_id('release-catalog-load').click()
                    expect(page.get_by_test_id('release-catalog')).to_be_visible()
                    page.get_by_test_id('release-parent').select_option(old_hash)
                    invalid['downgrade'] = True
                    page.get_by_test_id('release-preview').click()
                    expect(page.get_by_test_id('release-unavailable')).to_be_visible()
                    invalid['downgrade'] = False
                    page.get_by_test_id('release-catalog-load').click()
                    expect(page.get_by_test_id('release-catalog')).to_be_visible()
                    page.get_by_test_id('release-parent').select_option(old_hash)
                    page.get_by_test_id('release-preview').click()
                    expect(page.get_by_test_id('release-preview-result')).to_be_visible()
                    expect(page.get_by_test_id('release-publish')).to_be_disabled()
                    page.get_by_test_id('release-confirm').check()
                    page.get_by_test_id('release-publish').click()
                    expect(page.get_by_test_id('release-published')).to_contain_text('separate')
                    expect(page.get_by_test_id('release-entry')).to_have_count(2)

                    invalid['cas'] = True
                    page.get_by_test_id('release-select').nth(1).click()
                    expect(page.get_by_test_id('release-unavailable')).to_be_visible()
                    invalid['cas'] = False
                    page.get_by_test_id('release-catalog-load').click()
                    expect(page.get_by_test_id('release-catalog')).to_be_visible()
                    page.get_by_test_id('release-select').nth(1).click()
                    expect(page.get_by_test_id('selection-preview-result')).to_be_visible()
                    expect(page.get_by_test_id('selection-commit')).to_be_disabled()
                    page.get_by_test_id('selection-confirm').check()
                    page.get_by_test_id('selection-commit').click()
                    expect(page.get_by_test_id('release-catalog')).to_be_visible()

                    page.get_by_test_id('release-rollback').nth(0).click()
                    expect(page.get_by_test_id('selection-preview-result')).to_contain_text('rollback')
                    page.get_by_test_id('selection-confirm').check()
                    page.get_by_test_id('selection-commit').click()
                    writes_before_reuse = list(writes)
                    page.get_by_test_id('release-prepare-reuse').click()
                    guide = page.get_by_test_id('skill-reuse-guide')
                    expect(guide).to_be_visible()
                    expected_preview = (f'./scripts/aos-v1 preview-owned-skill-reuse --owned-skill-release-sha256 '
                        f'{old_hash} --owned-skill-selection-sha256 {selected_hash}')
                    expected_start = (f'./scripts/aos-v1 start --owned-skill-release-sha256 {old_hash} '
                        f'--owned-skill-selection-sha256 {selected_hash} '
                        '--owned-skill-reuse-confirm-sha256 PREVIEW_SHA256')
                    self.assertEqual(page.get_by_test_id('reuse-preview-command').inner_text(), expected_preview)
                    self.assertEqual(page.get_by_test_id('reuse-start-command').inner_text(), expected_start)
                    expect(guide).to_contain_text('copy')
                    expect(guide).to_contain_text('six separate manual approvals')
                    self.assertEqual(writes, writes_before_reuse)
                    expect(page.get_by_test_id('execution-release-binding')).to_have_count(0)
                    guide.screenshot(path='/tmp/aos-reuse-guide-fixture-en.png')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    expect(guide).to_contain_text('Yeni oturumda yeniden kullanım')
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    guide.screenshot(path='/tmp/aos-reuse-guide-fixture-tr.png')
                    page.get_by_role('button', name='English', exact=True).click()
                    page.set_viewport_size({'width': 1440, 'height': 1000})

                    catalog_mutation['selection'] = True
                    page.get_by_test_id('release-prepare-reuse').click()
                    expect(page.get_by_test_id('reuse-guide-unavailable')).to_be_visible()
                    expect(page.get_by_test_id('skill-reuse-guide')).to_have_count(0)
                    catalog_mutation['selection'] = False
                    page.get_by_test_id('release-catalog-load').click()
                    expect(page.get_by_test_id('release-catalog')).to_be_visible()
                    catalog_mutation['revoked'] = True
                    page.get_by_test_id('release-prepare-reuse').click()
                    expect(page.get_by_test_id('reuse-guide-unavailable')).to_be_visible()
                    expect(page.get_by_test_id('skill-reuse-guide')).to_have_count(0)
                    catalog_mutation['revoked'] = False
                    page.get_by_test_id('release-catalog-load').click()
                    expect(page.get_by_test_id('release-catalog')).to_be_visible()
                    page.get_by_test_id('release-prepare-reuse').click()
                    expect(page.get_by_test_id('skill-reuse-guide')).to_be_visible()
                    task_busy['value'] = True
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('skill-reuse-guide')).to_have_count(0)
                    task_busy['value'] = False
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    page.get_by_test_id('release-catalog-load').click()
                    expect(page.get_by_test_id('release-catalog')).to_be_visible()
                    page.get_by_test_id('release-prepare-reuse').click()
                    guide = page.get_by_test_id('skill-reuse-guide')
                    expect(guide).to_be_visible()
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(guide).to_contain_text('Yalnızca ./scripts/aos-v1 ile açılmış')
                    expect(guide.get_by_test_id('reuse-preview-command')).to_have_text(expected_preview)
                    expect(guide.get_by_test_id('reuse-start-command')).to_have_text(expected_start)
                    page.get_by_role('button', name='English', exact=True).click()

                    page.get_by_test_id('release-use-selected').click()
                    expect(page.get_by_test_id('execution-release-binding')).to_be_visible()
                    page.get_by_test_id('execution-case-key').fill('dev-next')
                    page.get_by_test_id('execution-value').fill('beta')
                    page.get_by_test_id('execution-preview').click()
                    expect(page.get_by_test_id('execution-preview-result')).to_be_visible()
                    page.get_by_test_id('execution-confirm').check()
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('execution-preview-result')).to_be_visible()
                    expect(page.get_by_test_id('execution-confirm')).to_be_checked()
                    expect(page.get_by_test_id('execution-start')).to_be_enabled()
                    execution['preview_sha256'] = 'd' * 64
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(page.get_by_test_id('execution-preview-result')).to_have_count(0)
                    self.assertFalse(any(action == 'execution-start' for action, _ in calls))
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)

                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('Seçim görev başlatmaz')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    panel.scroll_into_view_if_needed()
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    @staticmethod
    def _release(family, family_hash, candidate_hash, source_run_ref, revision, parent_hash, review_hash):
        return {'schema_version': '1.0', 'synthetic': True, 'purpose': 'development_release',
            'family': family, 'family_sha256': family_hash, 'revision': revision,
            'parent_release_sha256': parent_hash, 'candidate_sha256': candidate_hash,
            'skill_sha256': 'b' * 64, 'recipe_sha256': '8' * 64, 'review_sha256': review_hash,
            'source_run_ref': source_run_ref, 'source_group_sha256': 'f' * 64,
            'source_fingerprint_sha256': '9' * 64, 'evidence_execution_sha256': '6' * 64,
            'activation_authorized': False, 'training_ready': False, 'independent_held_out': False}

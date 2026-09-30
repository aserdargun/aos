from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest

from aos.contracts import REPO_ROOT
from aos.owned_form_invocation_session import provision_owned_synthetic_form_invocation
import test_owned_candidate_execution_managed as execution_helpers
from test_owned_form_invocation_managed import isolated_supervisor


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_OWNED_CANDIDATE_REVIEW_TESTS') == '1',
                     'Explicit owned review/execute/revoke with real Decider and Chromium')
class OwnedCandidateReviewManagedTests(unittest.TestCase):
    def test_review_receipt_binds_new_execution_and_revoke_stops_pending_fill(self):
        from playwright.sync_api import expect, sync_playwright

        helper = execution_helpers.OwnedCandidateExecutionManagedTests()
        prefix = '/api/tasks/owned-form-candidate/'
        with isolated_supervisor(provision_owned_synthetic_form_invocation) as (
                origin, token, client, session_directory, fixture_port, bundle, signal_handlers
        ), sync_playwright() as playwright:
            context, candidate_hash, source_job = helper.create_source_candidate(client)
            source = session_directory / 'owned-form'
            source_hashes = helper.source_bytes(source)
            database = session_directory / 'store.sqlite'
            selection = {'schema_version': '1.0', 'candidate_sha256': candidate_hash,
                         'source_run_ref': context['source_run_ref'],
                         'source_invocation_sha256': context['invocation_sha256'],
                         'case_key': 'dev-beta', 'development_value': 'beta'}
            preview = client.post(prefix + 'execution-preview', json=selection)
            preview.raise_for_status()
            control = client.get('/api/state').json()['control']
            started = client.post(prefix + 'execution-start', json={**selection,
                'preview_sha256': preview.json()['preview_sha256'], 'confirm_sha256': preview.json()['preview_sha256'],
                'lease_id': control['lease_id'], 'generation': control['generation']})
            started.raise_for_status()
            beta_job, approvals = helper.approve_job(client)
            self.assertEqual(beta_job['status'], 'succeeded', beta_job)
            self.assertEqual(len(approvals), 6)
            beta_execution = started.json()['candidate_execution_sha256']
            audited = client.post(prefix + 'execution-audit', json={'candidate_execution_sha256': beta_execution})
            audited.raise_for_status()
            self.assertTrue(audited.json()['available'], audited.text)
            browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                repeat_rejections = []
                repeat_console_rejections = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: repeat_rejections.append(response)
                        if response.url == origin + '/api/remote-form/repeats'
                        and response.status == 409 else None)

                def console_message(message):
                    if (message.location.get('url') == origin + '/api/remote-form/repeats'
                            and '409 (Conflict)' in message.text):
                        repeat_console_rejections.append(message.text)
                        return
                    if message.type in {'error', 'warning'} and not (
                            message.location.get('url') == origin + '/api/remote-form/repeats'
                            and '404 (Not Found)' in message.text):
                        errors.append(message.text + ' ' + str(message.location))

                page.on('console', console_message)
                page.goto(origin + '/ui/')
                self.assertEqual(page.title(), 'AOS · Control center')
                page.get_by_label('Local session token').fill(token)
                page.get_by_role('button', name='Sign in', exact=True).click()
                page.get_by_role('button', name='Tasks', exact=True).click()
                page.locator('#task-kind').select_option('browser_remote_form')
                counts = helper.database_counts(database)
                with page.expect_response(lambda response: response.url.endswith('/review-preview')) as preview_response:
                    page.get_by_test_id('review-preview').click()
                self.assertEqual(preview_response.value.status, 200, preview_response.value.text())
                proposal = preview_response.value.json()
                review_hash = proposal['review_sha256']
                review_selection = {key: proposal['receipt'][key] for key in (
                    'candidate_sha256', 'source_run_ref', 'source_invocation_sha256', 'candidate_execution_sha256')}
                self.assertEqual(review_selection['candidate_execution_sha256'], beta_execution)
                self.assertEqual(helper.database_counts(database), counts)
                self.assertFalse((source / 'candidate-reviews').exists())
                expect(page.get_by_test_id('review-accept')).to_be_disabled()
                denied = client.post(prefix + 'review-accept', json={**review_selection,
                    'review_sha256': review_hash, 'confirm_sha256': '0' * 64})
                self.assertEqual(denied.status_code, 409)
                self.assertFalse((source / 'candidate-reviews').exists())
                page.get_by_test_id('review-confirm').check()
                with page.expect_response(lambda response: response.url.endswith('/review-accept')) as accepted_response:
                    page.get_by_test_id('review-accept').click()
                self.assertEqual(accepted_response.value.status, 200, accepted_response.value.text())
                self.assertEqual(accepted_response.value.json()['review_sha256'], review_hash)
                expect(page.get_by_test_id('review-result')).to_contain_text('accepted')
                self.assertEqual(helper.database_counts(database), counts)
                self.assertEqual(helper.source_bytes(source), source_hashes)
                page.reload()
                page.get_by_role('button', name='Tasks', exact=True).click()
                page.locator('#task-kind').select_option('browser_remote_form')
                page.get_by_test_id('review-hash').fill(review_hash)
                page.get_by_test_id('review-inspect').click()
                expect(page.get_by_test_id('review-result')).to_contain_text('accepted')
                expect(page.get_by_test_id('execution-review-hash')).to_have_value(review_hash)
                expect(page.get_by_test_id('execution-use-review')).not_to_be_checked()
                page.get_by_test_id('execution-use-review').check()
                page.get_by_test_id('execution-case-key').fill('dev-gamma')
                page.get_by_test_id('execution-value').fill('gamma')
                gamma_execution = self.start_reviewed(page, review_hash)
                gamma_job, approvals = helper.approve_job(client, page=page)
                self.assertEqual(gamma_job['status'], 'succeeded', gamma_job)
                self.assertEqual(len(approvals), 6)
                expect(page.get_by_test_id('execution-audit')).to_be_visible()
                with page.expect_response(lambda response: response.url.endswith('/execution-audit')) as audit_response:
                    page.get_by_test_id('execution-audit').click()
                self.assertEqual(audit_response.value.status, 200, audit_response.value.text())
                report = audit_response.value.json()
                self.assertTrue(report['available'], report)
                self.assertEqual(report['schema_version'], '1.1')
                self.assertEqual(report['review_sha256'], review_hash)
                self.assertTrue(report['review_admission_verified'])
                self.assertEqual(report['review_status'], 'accepted')
                self.assertFalse(report['report']['activation_authorized'])
                expect(page.get_by_test_id('execution-audit-result')).to_be_visible()
                rechecked = client.post(prefix + 'review-preview', json=review_selection)
                self.assertEqual(rechecked.status_code, 200, rechecked.text)
                self.assertEqual(rechecked.json()['review_sha256'], review_hash)
                self.inspect_offline(source, database, bundle['manifest_sha256'], review_hash, 'accepted')
                page.get_by_test_id('candidate-review').scroll_into_view_if_needed()
                page.screenshot(path='/tmp/aos-review-real-accepted-en.png')
                page.get_by_test_id('execution-case-key').fill('dev-delta')
                page.get_by_test_id('execution-value').fill('delta')
                delta_execution = self.start_reviewed(page, review_hash)
                deadline = time.monotonic() + 60
                approved = set()
                while time.monotonic() < deadline:
                    tasks = client.get('/api/tasks').json()
                    approval = tasks.get('approval')
                    if approval and approval['action']['tool'] == 'browser.form.fill':
                        break
                    if approval and approval['approval_id'] not in approved:
                        client.post('/api/approvals/' + approval['approval_id'], json={
                            'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                        approved.add(approval['approval_id'])
                    time.sleep(.1)
                else:
                    self.fail('Reviewed run did not reach pending fill')
                self.assertEqual(len(approved), 2)
                self.assertEqual(client.post(prefix + 'review-revoke', json={
                    'review_sha256': review_hash, 'confirm_sha256': '0' * 64}).status_code, 409)
                self.assertEqual(client.get('/api/tasks').json()['approval']['approval_id'], approval['approval_id'])
                page.get_by_test_id('review-revoke-confirm').check()
                revoked_at = time.monotonic()
                with page.expect_response(lambda response: response.url.endswith('/review-revoke')) as revoke_response:
                    page.get_by_test_id('review-revoke').click()
                self.assertEqual(revoke_response.value.status, 200, revoke_response.value.text())
                expect(page.get_by_test_id('review-result')).to_contain_text('revoked')
                while time.monotonic() - revoked_at < 10:
                    tasks = client.get('/api/tasks').json()
                    if (tasks['jobs'][0]['status'] == 'failed'
                            and not tasks['busy'] and not tasks.get('reserved')):
                        break
                    time.sleep(.1)
                self.assertEqual(tasks['jobs'][0]['status'], 'failed')
                self.assertFalse(tasks['busy'])
                self.assertFalse(tasks.get('reserved'))
                self.assertIsNone(tasks['approval'])
                self.assertEqual(tasks['owned_form_candidate_execution']['candidate_execution_sha256'], delta_execution)
                self.assertEqual(tasks['owned_form_candidate_execution']['review_status'], 'revoked')
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'",
                        (tasks['jobs'][0]['run_id'],)).fetchone()[0], 0)
                historical = client.post(prefix + 'execution-audit', json={'candidate_execution_sha256': gamma_execution})
                self.assertEqual(historical.status_code, 200, historical.text)
                self.assertTrue(historical.json()['available'], historical.text)
                self.assertTrue(historical.json()['review_admission_verified'])
                self.assertEqual(historical.json()['review_status'], 'revoked')
                self.inspect_offline(source, database, bundle['manifest_sha256'], review_hash, 'revoked')
                denied = client.post(prefix + 'execution-preview', json={**selection, 'schema_version': '1.1',
                    'review_sha256': review_hash, 'case_key': 'dev-epsilon', 'development_value': 'epsilon'})
                self.assertEqual(denied.status_code, 409)
                denied = client.post(prefix + 'review-accept', json={**review_selection,
                    'review_sha256': review_hash, 'confirm_sha256': review_hash})
                self.assertEqual(denied.status_code, 409)
                page.get_by_role('button', name='Türkçe', exact=True).click()
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.get_by_test_id('review-result')).to_contain_text('İnceleme geri çekildi')
                page.get_by_test_id('candidate-review').scroll_into_view_if_needed()
                page.screenshot(path='/tmp/aos-review-real-revoked-tr-mobile.png')
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                self.assertEqual(errors, [])
                self.assertEqual(len(repeat_console_rejections), len(repeat_rejections))
                for response in repeat_rejections:
                    self.assertEqual(response.json(), {'detail': 'HTTPS form repeat source unavailable'})
                self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                self.assertEqual(helper.source_bytes(source), source_hashes)
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute("DELETE FROM observations WHERE run_id=? AND kind='skill.recipe_candidate_review_admission'",
                                       (gamma_job['run_id'],))
                    connection.commit()
                missing_admission = client.post(prefix + 'execution-audit', json={'candidate_execution_sha256': gamma_execution})
                self.assertEqual(missing_admission.status_code, 200, missing_admission.text)
                self.assertFalse(missing_admission.json()['available'])
            finally:
                browser.close()

    def inspect_offline(self, source, database, manifest_hash, review_hash, status):
        inspected = subprocess.run([sys.executable, '-c', '''
import json,sys
from pathlib import Path
from unittest.mock import patch
from aos.site_skill_form_recipe_candidate import OwnedSiteSkillFormRecipeCandidateSession
from aos.owned_candidate_review import inspect_owned_candidate_review_evidence
from aos.web_application import WebApplicationProfiles
from aos.site_knowledge import SiteKnowledgeStore
source,database=Path(sys.argv[1]),Path(sys.argv[2])
profiles=WebApplicationProfiles(source/'profiles')
session=OwnedSiteSkillFormRecipeCandidateSession(directory=source,manifest_sha256=sys.argv[3],
    candidate_directory=source/'site-skill-recipe-candidates',database=database,
    profiles=profiles,pages=SiteKnowledgeStore(source/'site-knowledge',profiles))
with patch('socket.socket',side_effect=RuntimeError('network_forbidden')):
    result=inspect_owned_candidate_review_evidence(source,sys.argv[4],candidate_session=session,database=database)
print(json.dumps({key:result[key] for key in ('status','review_sha256')}))
''', str(source), str(database), manifest_hash, review_hash], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout), {'status': status, 'review_sha256': review_hash})

    def start_reviewed(self, page, review_hash):
        from playwright.sync_api import expect

        with page.expect_response(lambda response: response.url.endswith('/execution-preview')) as preview_response:
            page.get_by_test_id('execution-preview').click()
        self.assertEqual(preview_response.value.status, 200, preview_response.value.text())
        self.assertEqual(preview_response.value.json()['review_sha256'], review_hash)
        self.assertEqual(preview_response.value.json()['schema_version'], '1.1')
        expect(page.get_by_test_id('execution-start')).to_be_disabled()
        page.get_by_test_id('execution-confirm').check()
        with page.expect_response(lambda response: response.url.endswith('/execution-start')) as start_response:
            page.get_by_test_id('execution-start').click()
        self.assertEqual(start_response.value.status, 200, start_response.value.text())
        self.assertEqual(start_response.value.json()['review_sha256'], review_hash)
        return start_response.value.json()['candidate_execution_sha256']

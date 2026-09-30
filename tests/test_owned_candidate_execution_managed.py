from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import unittest

from aos.contracts import REPO_ROOT
from aos.owned_form_invocation_session import provision_owned_synthetic_form_invocation
from test_owned_form_invocation_managed import isolated_supervisor


STAGE_TOOLS = ('browser.form.open', 'browser.form.state_before', 'browser.form.fill',
               'browser.form.submit', 'browser.form.receipt', 'browser.form.state_after')


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_OWNED_CANDIDATE_EXECUTION_TESTS') == '1',
                     'Explicit managed candidate reuse with real Decider and owned Chromium')
class OwnedCandidateExecutionManagedTests(unittest.TestCase):
    def test_saved_candidate_runs_two_fresh_cases_and_changed_candidate_blocks_post(self):
        from playwright.sync_api import expect, sync_playwright

        with isolated_supervisor(provision_owned_synthetic_form_invocation) as (
                origin, token, client, session_directory, fixture_port, bundle, signal_handlers
        ), sync_playwright() as playwright:
            context, checksum, source_job = self.create_source_candidate(client)
            source_directory = session_directory / 'owned-form'
            source_hashes = self.source_bytes(source_directory)
            source_manifest = (source_directory / 'manifest.json').read_bytes()
            database = session_directory / 'store.sqlite'
            browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                expected_rejections = set()
                observed_rejections = set()
                page.on('pageerror', lambda error: errors.append(str(error)))

                def console_message(message):
                    if message.type not in ('error', 'warning'):
                        return
                    if (message.location.get('url') == origin + '/api/remote-form/repeats'
                            and '404 (Not Found)' in message.text):
                        return
                    if (message.location.get('url') in expected_rejections
                            and '409 (Conflict)' in message.text):
                        observed_rejections.add(message.location['url'])
                        return
                    errors.append(message.text + ' ' + str(message.location))

                page.on('console', console_message)
                page.goto(origin + '/ui/')
                self.assertEqual(page.url, origin + '/ui/')
                self.assertEqual(page.title(), 'AOS · Control center')
                page.get_by_label('Local session token').fill(token)
                page.get_by_role('button', name='Sign in', exact=True).click()
                page.get_by_role('button', name='Tasks', exact=True).click()
                page.locator('#task-kind').select_option('browser_remote_form')
                panel = page.get_by_test_id('candidate-execution')
                expect(panel).to_be_visible()
                page.get_by_test_id('execution-candidate-hash').fill(checksum)
                original_counts = self.database_counts(database)
                rejected_source = client.post('/api/tasks/owned-form-candidate/execution-preview', json={
                    'schema_version': '1.0', 'candidate_sha256': checksum,
                    'source_run_ref': context['source_run_ref'],
                    'source_invocation_sha256': context['invocation_sha256'],
                    'case_key': 'dev-source-reuse', 'development_value': 'alpha'})
                self.assertEqual(rejected_source.status_code, 409)
                self.assertEqual(self.database_counts(database), original_counts)
                executions = []
                for value in ('beta', 'gamma'):
                    counts_before = self.database_counts(database)
                    if not executions:
                        self.assertFalse((source_directory / 'candidate-execution-bundles').exists())
                    page.get_by_test_id('execution-case-key').fill('dev-' + value)
                    page.get_by_test_id('execution-value').fill(value)
                    with page.expect_response(lambda response: response.url.endswith('/execution-preview')) as preview_response:
                        page.get_by_test_id('execution-preview').click()
                    self.assertEqual(preview_response.value.status, 200, preview_response.value.text())
                    preview = preview_response.value.json()
                    self.assertEqual(preview['candidate_sha256'], checksum)
                    self.assertEqual(preview['source_run_ref'], context['source_run_ref'])
                    self.assertEqual(self.database_counts(database), counts_before)
                    if not executions:
                        self.assertFalse((source_directory / 'candidate-execution-bundles').exists())
                    self.assertEqual(self.source_bytes(source_directory), source_hashes)
                    control = client.get('/api/state').json()['control']
                    start_selection = {
                        'schema_version': '1.0', 'candidate_sha256': checksum,
                        'source_run_ref': context['source_run_ref'],
                        'source_invocation_sha256': context['invocation_sha256'],
                        'case_key': 'dev-' + value, 'development_value': value,
                        'preview_sha256': preview['preview_sha256'], 'confirm_sha256': preview['preview_sha256'],
                        'lease_id': control['lease_id'], 'generation': control['generation'],
                    }
                    for change in ({'confirm_sha256': '0' * 64}, {'generation': control['generation'] + 1}):
                        rejected = client.post('/api/tasks/owned-form-candidate/execution-start', json={**start_selection, **change})
                        self.assertEqual(rejected.status_code, 409, rejected.text)
                        self.assertEqual(self.database_counts(database), counts_before)
                    expect(page.get_by_test_id('execution-start')).to_be_disabled()
                    page.get_by_test_id('execution-confirm').check()
                    with page.expect_response(lambda response: response.url.endswith('/execution-start')) as start_response:
                        page.get_by_test_id('execution-start').click()
                    self.assertIn(start_response.value.status, (200, 202), start_response.value.text())
                    self.assertTrue(start_response.value.json()['accepted'])
                    job, approvals = self.approve_job(client, page=page)
                    self.assertEqual(job['status'], 'succeeded', job)
                    self.assertEqual(len(approvals), 6)
                    self.assertNotEqual(job['run_id'], source_job['run_id'])
                    expect(page.get_by_test_id('execution-audit')).to_be_visible(timeout=15000)
                    expect(page.get_by_test_id('execution-audit-result')).to_have_count(0)
                    with page.expect_response(lambda response: response.url.endswith('/execution-audit')) as audit_response:
                        page.get_by_test_id('execution-audit').click()
                    self.assertEqual(audit_response.value.status, 200, audit_response.value.text())
                    audited = audit_response.value.json()
                    self.assertTrue(audited['available'], audited)
                    self.assertEqual(audited['report']['status'], 'source_bound_development_execution')
                    self.assertTrue(audited['report']['source_and_execution_events_disjoint'])
                    self.assertFalse(audited['report']['held_out_independence_verified'])
                    expect(page.get_by_test_id('execution-audit-result')).to_contain_text('Source-bound development execution verified')
                    executions.append(audited)
                    self.assertEqual((source_directory / 'manifest.json').read_bytes(), source_manifest)
                    self.assertEqual(self.source_bytes(source_directory), source_hashes)
                    original = client.get('/api/tasks').json()['owned_form_invocation']
                    self.assertEqual(original['run_ref'], context['source_run_ref'])
                    self.assertEqual(original['invocation_sha256'], context['invocation_sha256'])
                    self.assertEqual(original['lifecycle'], 'audited')
                    completed_counts = self.database_counts(database)
                    replay = client.post('/api/tasks/owned-form-candidate/execution-start', json=start_selection)
                    self.assertEqual(replay.status_code, 409)
                    self.assertEqual(self.database_counts(database), completed_counts)
                self.assertEqual(executions[0]['source_group_sha256'], executions[1]['source_group_sha256'])
                self.assertEqual(executions[0]['recipe_sha256'], executions[1]['recipe_sha256'])
                self.assertNotEqual(executions[0]['parameter_variant_sha256'], executions[1]['parameter_variant_sha256'])
                self.assertNotEqual(executions[0]['run_id'], executions[1]['run_id'])
                with self.assertRaises(OSError):
                    socket.create_connection(('127.0.0.1', fixture_port), timeout=.2)
                inspected = subprocess.run([sys.executable, '-c', '''
import json, sys
from pathlib import Path
from unittest.mock import patch
from aos.site_skill_form_recipe_candidate import OwnedSiteSkillFormRecipeCandidateSession
from aos.owned_form_candidate_execution import audit_persisted_candidate_execution
from aos.web_application import WebApplicationProfiles
from aos.site_knowledge import SiteKnowledgeStore
source, database = Path(sys.argv[1]), Path(sys.argv[2])
profiles = WebApplicationProfiles(source / 'profiles')
session = OwnedSiteSkillFormRecipeCandidateSession(directory=source,
    manifest_sha256=sys.argv[3], candidate_directory=source / 'site-skill-recipe-candidates',
    database=database, profiles=profiles, pages=SiteKnowledgeStore(source / 'site-knowledge', profiles))
reports = []
with patch('socket.socket', side_effect=RuntimeError('network_forbidden')):
    for checksum in sys.argv[4:]:
        report = audit_persisted_candidate_execution(source / 'candidate-execution-bundles',
            checksum, candidate_session=session, database=database)
        reports.append({key: report[key] for key in ('status', 'execution_run_ref', 'source_group_sha256')})
print(json.dumps(reports))
''', str(source_directory), str(database), bundle['manifest_sha256'],
                    *(result['candidate_execution_sha256'] for result in executions)],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(inspected.returncode, 0, inspected.stderr)
                self.assertEqual(json.loads(inspected.stdout), [
                    {key: result['report'][key] for key in ('status', 'execution_run_ref', 'source_group_sha256')}
                    for result in executions])
                panel.scroll_into_view_if_needed()
                page.screenshot(path='/tmp/aos-candidate-execution-real-en.png')
                page.get_by_role('button', name='Türkçe', exact=True).click()
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.get_by_test_id('execution-audit-result')).to_contain_text('Kaynak kökenine bağlı gelişim yürütmesi doğrulandı')
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                panel.scroll_into_view_if_needed()
                page.screenshot(path='/tmp/aos-candidate-execution-real-tr-mobile.png')
                page.get_by_role('button', name='English', exact=True).click()
                for value in ('delta', 'epsilon'):
                    changed_artifacts = {}
                    page.get_by_test_id('execution-case-key').fill('dev-' + value)
                    page.get_by_test_id('execution-value').fill(value)
                    page.get_by_test_id('execution-preview').click()
                    expect(page.get_by_test_id('execution-preview-result')).to_be_visible()
                    page.get_by_test_id('execution-confirm').check()
                    page.get_by_test_id('execution-start').click()
                    expect(page.get_by_test_id('candidate-execution-status')).to_contain_text('running')

                    def mutate_artifact(approval):
                        if approval['action']['tool'] == 'browser.form.fill':
                            expected_rejections.add(origin + '/api/approvals/' + approval['approval_id'])
                            if value == 'delta':
                                current_execution = client.get('/api/tasks').json()['owned_form_candidate_execution']
                                target = (source_directory / 'candidate-execution-bundles'
                                          / current_execution['candidate_execution_sha256'] / 'case-inputs.json')
                            else:
                                target = source_directory / 'site-skill-recipe-candidates' / (checksum + '.json')
                            changed_artifacts[target] = target.read_bytes()
                            with target.open('ab') as stream:
                                stream.write(b' ')
                            return True
                        return False

                    failed, accepted = self.approve_job(client, page=page, before_approval=mutate_artifact)
                    self.assertEqual(failed['status'], 'failed', failed)
                    self.assertLessEqual(len(accepted), 3)
                    with closing(sqlite3.connect(database)) as connection:
                        posts = connection.execute("SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'",
                                                   (failed['run_id'],)).fetchone()[0]
                    self.assertEqual(posts, 0)
                    expect(page.get_by_test_id('execution-audit-result')).to_have_count(0)
                    if value == 'delta':
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            task_state = client.get('/api/tasks').json()
                            if not task_state['busy'] and not task_state['reserved']:
                                break
                            time.sleep(.05)
                        else:
                            self.fail('Failed candidate cleanup did not release the scheduler')
                        selection = {
                            'schema_version': '1.0', 'candidate_sha256': checksum,
                            'source_run_ref': context['source_run_ref'],
                            'source_invocation_sha256': context['invocation_sha256'],
                            'case_key': 'dev-history-check', 'development_value': 'history-check',
                        }
                        counts_before = self.database_counts(database)
                        preview = client.post('/api/tasks/owned-form-candidate/execution-preview', json=selection)
                        self.assertEqual(preview.status_code, 200, preview.text)
                        control = client.get('/api/state').json()['control']
                        rejected = client.post('/api/tasks/owned-form-candidate/execution-start', json={
                            **selection, 'preview_sha256': preview.json()['preview_sha256'],
                            'confirm_sha256': preview.json()['preview_sha256'],
                            'lease_id': control['lease_id'], 'generation': control['generation'],
                        })
                        self.assertEqual(rejected.status_code, 409)
                        self.assertEqual(self.database_counts(database), counts_before)
                        for target, original_bytes in changed_artifacts.items():
                            target.write_bytes(original_bytes)
                self.assertEqual(len(expected_rejections), 2)
                self.assertEqual(observed_rejections, expected_rejections)
                self.assertEqual(errors, [])
                self.assertEqual(page.locator('vite-error-overlay').count(), 0)
            finally:
                browser.close()

    def approve_job(self, client, *, page=None, before_approval=None, approve_label='Approve'):
        accepted = []
        rejected_at = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get('/api/tasks')
            response.raise_for_status()
            tasks = response.json()
            job = tasks['jobs'][0]
            approval = tasks.get('approval')
            if approval is not None and approval['approval_id'] not in accepted:
                self.assertEqual(approval['action']['tool'], STAGE_TOOLS[len(accepted)])
                expect_rejection = before_approval(approval) if before_approval is not None else False
                if page is None:
                    client.post('/api/approvals/' + approval['approval_id'], json={
                        'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                else:
                    from playwright.sync_api import expect

                    expect(page.get_by_test_id('approval')).to_contain_text(approval['action']['tool'])
                    with page.expect_response(lambda response: response.url.endswith('/api/approvals/' + approval['approval_id'])) as response:
                        page.get_by_role('button', name=approve_label, exact=True).click()
                    self.assertEqual(response.value.status, 409 if expect_rejection else 200)
                    if expect_rejection:
                        rejected_at = time.monotonic()
                accepted.append(approval['approval_id'])
            if job['status'] in {'succeeded', 'failed', 'cancelled'}:
                if rejected_at is not None:
                    self.assertLess(time.monotonic() - rejected_at, 10, 'Invalid source must fail without waiting for approval timeout')
                return job, accepted
            time.sleep(.1)
        self.fail('Candidate execution did not reach a terminal job within 180 seconds')

    def create_source_candidate(self, client):
        control = client.get('/api/state').json()['control']
        client.post('/api/tasks', json={
            'kind': 'browser_remote_form', 'lease_id': control['lease_id'],
            'generation': control['generation']}).raise_for_status()
        job, approvals = self.approve_job(client)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(len(approvals), 6)
        audited = client.get('/api/tasks/owned-form-invocation-audit')
        audited.raise_for_status()
        self.assertEqual(audited.json()['status'], 'verified')
        context = client.get('/api/tasks/owned-form-candidate/context').json()
        self.assertTrue(context['available'], context)
        selection = {
            'schema_version': '1.0', 'source_run_ref': context['source_run_ref'],
            'invocation_sha256': context['invocation_sha256'],
            'annotation': context['annotation_seed'],
        }
        preview = client.post('/api/tasks/owned-form-candidate/preview', json=selection)
        preview.raise_for_status()
        candidate_sha256 = preview.json()['candidate_sha256']
        published = client.post('/api/tasks/owned-form-candidate/publish', json={
            **selection, 'confirm_sha256': candidate_sha256})
        published.raise_for_status()
        self.assertTrue(published.json()['persisted'])
        return context, candidate_sha256, job

    def source_bytes(self, source_directory):
        manifest = json.loads((source_directory / 'manifest.json').read_bytes())
        paths = {source_directory / name for name in manifest['sources']}
        for name in ('profiles', 'site-knowledge', 'site-skills'):
            paths.update(path for path in (source_directory / name).rglob('*') if path.is_file())
        return {str(path.relative_to(source_directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in paths}

    def database_counts(self, database):
        with closing(sqlite3.connect(database)) as connection:
            return tuple(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                         for table in ('runs', 'model_calls', 'actions'))

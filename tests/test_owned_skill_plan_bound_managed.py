import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from contextlib import closing

from aos import local_app
from aos.contracts import REPO_ROOT
import test_owned_candidate_execution_managed as execution_helpers
import test_owned_skill_release_managed as release_helpers
from test_owned_skill_reuse_managed import owned_subprocess_session, read_isolated_state


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_OWNED_PLAN_BOUND_TESTS') == '1',
                     'Explicit real Bonsai to Decider managed UI acceptance')
class OwnedPlanBoundManagedTests(unittest.TestCase):
    def test_real_bilingual_proposals_execute_and_audit_with_six_fresh_approvals(self):
        from playwright.sync_api import expect, sync_playwright

        base = REPO_ROOT / 'data' / ('local-app-test-' + uuid4().hex)
        base.mkdir(mode=0o700)
        helper = execution_helpers.OwnedCandidateExecutionManagedTests()
        releases = release_helpers.OwnedSkillReleaseManagedTests()
        complete = False

        def post(client, suffix, value):
            response = client.post('/api/tasks/owned-form-candidate/' + suffix, json=value)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        try:
            with owned_subprocess_session(base) as first:
                context, candidate, _job = helper.create_source_candidate(first.client)
                review = releases.evidence_and_review(first.client, helper, context, candidate, 'beta')
                request = {'schema_version': '1.0', 'review_sha256': review, 'parent_release_sha256': None}
                release = post(first.client, 'release-preview', request)['release_sha256']
                post(first.client, 'release-publish', request | {'confirm_sha256': release})
                request = {'schema_version': '1.0', 'release_sha256': release,
                           'expected_selection_sha256': None, 'operation': 'select'}
                selection = post(first.client, 'selection-preview', request)['selection_sha256']
                post(first.client, 'selection-commit', request | {'confirm_sha256': selection})
                database = first.source / 'store.sqlite'
            previous = read_isolated_state(base)
            with patch('socket.socket', side_effect=AssertionError('offline_preview')):
                material = local_app.prepare_owned_skill_reuse(previous, release, selection, base=base)
            audits = []
            with owned_subprocess_session(base, previous=previous, material=material) as second, sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                    'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def console(message):
                        if message.type not in {'error', 'warning'}:
                            return
                        if (message.location.get('url') == second.origin + '/api/remote-form/repeats'
                                and any(code in message.text for code in ('404 (Not Found)', '409 (Conflict)'))):
                            return
                        errors.append(message.text)

                    page.on('console', console)
                    page.goto(second.origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Control center')
                    page.get_by_label('Local session token').fill(second.token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.locator('#task-kind').select_option('browser_remote_form')
                    for language, goal in (('en', 'Save message "gamma"'), ('tr', 'Mesaj alanına "delta" kaydet')):
                        counts = helper.database_counts(database)
                        if language == 'tr':
                            page.get_by_role('button', name='Türkçe', exact=True).click()
                            page.set_viewport_size({'width': 390, 'height': 844})
                        page.get_by_test_id('planning-goal').fill(goal)
                        with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan')) as proposed:
                            page.get_by_test_id('planning-begin').click()
                        self.assertEqual(proposed.value.status, 202)
                        expect(page.get_by_test_id('planning-bind')).to_be_enabled(timeout=180000)
                        self.assertEqual(helper.database_counts(database), counts)
                        with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan/bind')) as bound:
                            page.get_by_test_id('planning-bind').click()
                        self.assertEqual(bound.value.status, 200)
                        preview = bound.value.json()
                        self.assertEqual(preview['schema_version'], '1.4')
                        self.assertEqual(len(preview['steps']), 6)
                        self.assertEqual(helper.database_counts(database), counts)
                        expect(page.get_by_test_id('planning-start')).to_be_disabled()
                        control = second.client.get('/api/state').json()['control']
                        rejected = second.client.post('/api/tasks/owned-skill-plan/start', json={
                            'schema_version': '1.3', 'planning_bundle_sha256': preview['planning_bundle_sha256']})
                        self.assertEqual(rejected.status_code, 400)
                        manual = {'schema_version': '1.3', 'candidate_sha256': candidate,
                            'source_run_ref': context['source_run_ref'], 'source_invocation_sha256': context['invocation_sha256'],
                            'case_key': 'manual-while-bound', 'development_value': 'never-submit',
                            'review_sha256': review, 'release_sha256': release, 'selection_sha256': selection,
                            'reuse_admission_sha256': preview['reuse_admission_sha256']}
                        self.assertEqual(second.client.post('/api/tasks/owned-form-candidate/execution-preview',
                                                          json=manual).status_code, 409)
                        self.assertEqual(helper.database_counts(database), counts)
                        page.get_by_test_id('planning-confirm').check()
                        with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan/start')) as started:
                            page.get_by_test_id('planning-start').click()
                        self.assertEqual(started.value.status, 200)
                        job, approvals = helper.approve_job(
                            second.client, page=page,
                            approve_label='Approve' if language == 'en' else 'Onayla')
                        self.assertEqual(job['status'], 'succeeded', job)
                        self.assertEqual(len(approvals), 6)
                        releases.wait_idle(second.client)
                        expect(page.get_by_test_id('planning-audit')).to_be_enabled()
                        with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan/audit')) as audited:
                            page.get_by_test_id('planning-audit').click()
                        self.assertEqual(audited.value.status, 200)
                        audit = audited.value.json()
                        self.assertTrue(audit['available'], audit)
                        self.assertTrue(audit['planning_admission_verified'])
                        self.assertEqual(audit['planning_bundle_sha256'], preview['planning_bundle_sha256'])
                        audits.append(audit)
                        expect(page.get_by_test_id('planning-verified')).to_be_visible()
                        page.get_by_test_id('skill-planning').screenshot(
                            path='/tmp/aos-plan-bound-panel-' + language + '.png')
                        self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),
                                             1440 if language == 'en' else 390)
                        page.get_by_test_id('skill-planning').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-plan-bound-' + language + '.png', full_page=True)
                        audit_route = '**/api/tasks/owned-skill-plan/audit'
                        page.route(audit_route, lambda route: route.fulfill(
                            status=200, content_type='application/json',
                            body=json.dumps({'schema_version': '1.4', 'available': False})))
                        page.get_by_test_id('planning-audit').click()
                        expect(page.get_by_test_id('planning-verified')).to_have_count(0)
                        expect(page.get_by_test_id('skill-planning').get_by_role('alert')).to_be_visible()
                        page.unroute(audit_route)
                        replay = second.client.post('/api/tasks/owned-skill-plan/start', json={
                            'schema_version': '1.4', 'planning_bundle_sha256': preview['planning_bundle_sha256'],
                            'confirm_plan_sha256': preview['planning_bundle_sha256'],
                            'preview_sha256': preview['preview_sha256'], 'confirm_sha256': preview['preview_sha256'],
                            'lease_id': control['lease_id'], 'generation': control['generation']})
                        self.assertEqual(replay.status_code, 409)

                    page.get_by_role('button', name='English', exact=True).click()
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    page.get_by_test_id('planning-goal').fill('Save message "epsilon"')
                    with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan')) as proposed:
                        page.get_by_test_id('planning-begin').click()
                    self.assertEqual(proposed.value.status, 202)
                    expect(page.get_by_test_id('planning-bind')).to_be_enabled(timeout=180000)
                    with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan/bind')) as bound:
                        page.get_by_test_id('planning-bind').click()
                    self.assertEqual(bound.value.status, 200)
                    tamper_preview = bound.value.json()
                    self.assertEqual(tamper_preview['schema_version'], '1.4')
                    page.get_by_test_id('planning-confirm').check()
                    with page.expect_response(lambda response: response.url.endswith('/owned-skill-plan/start')) as started:
                        page.get_by_test_id('planning-start').click()
                    self.assertEqual(started.value.status, 200)

                    approved = set()
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        tasks = second.client.get('/api/tasks').json()
                        approval = tasks.get('approval')
                        if approval and approval['action']['tool'] == 'browser.form.fill':
                            break
                        if approval and approval['approval_id'] not in approved:
                            response = second.client.post(
                                '/api/approvals/' + approval['approval_id'], json={
                                    'action_sha256': approval['action_sha256'], 'accept': True})
                            self.assertEqual(response.status_code, 200, response.text)
                            approved.add(approval['approval_id'])
                        time.sleep(.1)
                    else:
                        self.fail('Planned epsilon execution did not reach fill approval')
                    self.assertEqual(len(approved), 2)
                    execution = tasks['owned_form_candidate_execution']
                    execution_id = execution['candidate_execution_sha256']
                    planning_path = (first.source / 'owned-form' / 'candidate-execution-bundles'
                                     / execution_id / 'planning-bundle.json')
                    original_planning_bytes = planning_path.read_bytes()
                    try:
                        planning_path.write_bytes(original_planning_bytes + b' ')
                        planning_path.chmod(0o600)
                        response = second.client.post(
                            '/api/approvals/' + approval['approval_id'], json={
                                'action_sha256': approval['action_sha256'], 'accept': True})
                        self.assertEqual(response.status_code, 409, response.text)
                        self.assertEqual(response.json()['code'], 'UNSAFE_ACTION')
                        releases.wait_idle(second.client)
                        failed_tasks = second.client.get('/api/tasks').json()
                        failed = next(job for job in failed_tasks['jobs']
                                      if job['job_id'] == execution['job_id'])
                        self.assertEqual(failed['status'], 'failed', failed)
                        self.assertIsNone(failed_tasks['approval'])
                        with closing(sqlite3.connect(database)) as connection:
                            submit_count = connection.execute(
                                "SELECT COUNT(*) FROM actions WHERE run_id=? "
                                "AND tool='browser.form.submit'", (failed['run_id'],)).fetchone()[0]
                        self.assertEqual(submit_count, 0)
                    finally:
                        planning_path.write_bytes(original_planning_bytes)
                        planning_path.chmod(0o600)
                    self.assertEqual(planning_path.read_bytes(), original_planning_bytes)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
            self.assertEqual(read_isolated_state(base).phase, 'stopped')
            for audit in audits:
                self.audit_offline(first.source / 'owned-form', database, audit)
                with closing(sqlite3.connect(database)) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = connection.execute(
                        "SELECT * FROM observations WHERE kind='skill.owned_planning_admission'").fetchall()
                    row = next(dict(item) for item in rows if json.loads(item['payload_json'])[
                        'candidate_execution_sha256'] == audit['candidate_execution_sha256'])
                    connection.execute('DELETE FROM observations WHERE observation_id=?', (row['observation_id'],))
                    connection.commit()
                    try:
                        self.audit_offline(first.source / 'owned-form', database, audit, available=False)
                    finally:
                        connection.execute('INSERT INTO observations (' + ','.join(row) + ') VALUES ('
                                           + ','.join('?' for key in row) + ')', tuple(row.values()))
                        connection.commit()
            complete = True
        finally:
            if complete:
                shutil.rmtree(base)
            else:
                print('Preserved private plan-bound evidence:', base, flush=True)

    def audit_offline(self, source, database, expected, *, available=True):
        result = subprocess.run([sys.executable, '-c', '''
import json,sqlite3,sys
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch
from aos.desktop_tasks import DesktopScheduler
from aos.site_skill_form_recipe_candidate import OwnedSiteSkillFormRecipeCandidateSession
from aos.web_application import WebApplicationProfiles
from aos.site_knowledge import SiteKnowledgeStore
source,database=Path(sys.argv[1]),Path(sys.argv[2])
manifest=json.loads((source/'manifest.json').read_text())
from aos.contracts import digest
profiles=WebApplicationProfiles(source/'profiles')
session=OwnedSiteSkillFormRecipeCandidateSession(directory=source,manifest_sha256=digest(manifest),
 candidate_directory=source/'site-skill-recipe-candidates',database=database,
 profiles=profiles,pages=SiteKnowledgeStore(source/'site-knowledge',profiles))
with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as connection:
 connection.row_factory=sqlite3.Row
 scheduler=DesktopScheduler.__new__(DesktopScheduler)
 scheduler.task=None
 scheduler.job_id=None
 scheduler.owned_skill_planning=None
 scheduler.sequences=SimpleNamespace(reserved=False)
 scheduler.store=SimpleNamespace(connection=connection)
 scheduler.settings=SimpleNamespace(database=database)
 scheduler.remote_form_owned_candidate_session=session
 scheduler.remote_form_owned_manifest=manifest
 scheduler._owned_candidate_execution_history={}
 with patch('socket.socket',side_effect=AssertionError('network_forbidden')):
  report=scheduler.audit_owned_form_candidate_execution(sys.argv[3])
  assert report['available'] is (sys.argv[5]=='true'), report
  assert report['schema_version']=='1.4', report
  assert report['planning_admission_verified'] is (sys.argv[5]=='true'), report
  assert report['planning_bundle_sha256']==sys.argv[4], report
print('offline plan-bound audit verified')
''', str(source), str(database), expected['candidate_execution_sha256'], expected['planning_bundle_sha256'],
            'true' if available else 'false'],
            capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

from contextlib import closing
import copy
import json
import os
import signal
import socket
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
                     and os.environ.get('AOS_OWNED_SKILL_RELEASE_TESTS') == '1',
                     'Explicit real Decider/Chromium release selection and rollback acceptance')
class OwnedSkillReleaseManagedTests(unittest.TestCase):
    prefix = '/api/tasks/owned-form-candidate/'

    def test_selected_versions_execute_and_explicit_rollback_survives_reinspection(self):
        from playwright.sync_api import expect, sync_playwright

        helper = execution_helpers.OwnedCandidateExecutionManagedTests()
        with isolated_supervisor(provision_owned_synthetic_form_invocation) as (
                origin, token, client, session_directory, fixture_port, bundle, signal_handlers
        ), sync_playwright() as playwright:
            context, candidate_a, source_job = helper.create_source_candidate(client)
            source = session_directory / 'owned-form'
            database = session_directory / 'store.sqlite'
            source_bytes = helper.source_bytes(source)
            review_a = self.evidence_and_review(client, helper, context, candidate_a, 'beta')
            browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('request', lambda request: print('release-request', request.url.rsplit('/', 1)[-1], flush=True)
                        if '/api/tasks/owned-form-candidate/' in request.url else None)
                page.on('response', lambda response: print('release-response', response.url.rsplit('/', 1)[-1], response.status, flush=True)
                        if '/api/tasks/owned-form-candidate/' in response.url else None)
                errors, repeat_rejections = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def completed_request(request):
                    if request.url != origin + '/api/remote-form/repeats':
                        return
                    response = request.response()
                    if response is not None and response.status == 409:
                        repeat_rejections.append(response.json())

                page.on('requestfinished', completed_request)

                def console_message(message):
                    if (message.location.get('url') == origin + '/api/remote-form/repeats'
                            and any(code in message.text for code in ('404 (Not Found)', '409 (Conflict)'))):
                        return
                    if message.type in {'error', 'warning'}:
                        errors.append(message.text + ' ' + str(message.location))

                page.on('console', console_message)
                page.goto(origin + '/ui/')
                self.assertEqual(page.title(), 'AOS · Control center')
                page.get_by_label('Local session token').fill(token)
                page.get_by_role('button', name='Sign in', exact=True).click()
                page.get_by_role('button', name='Tasks', exact=True).click()
                page.locator('#task-kind').select_option('browser_remote_form')
                page.get_by_test_id('review-hash').fill(review_a)
                page.get_by_test_id('review-inspect').click()
                expect(page.get_by_test_id('execution-review-hash')).to_have_value(review_a)
                page.get_by_test_id('release-catalog-load').click()
                expect(page.get_by_test_id('release-catalog')).to_contain_text('No versions published yet.')
                counts = helper.database_counts(database)
                release_a = self.publish_version(page, client, review_a, None)
                self.assertEqual(helper.database_counts(database), counts)
                selection_a = self.select_version(page, client, release_a, 'select')
                self.assertEqual(helper.database_counts(database), counts)
                run_a = self.execute_selected(page, client, helper, release_a, selection_a, review_a, 'gamma')
                self.assertTrue(run_a['release_admission_verified'])
                self.assertEqual(run_a['selection_status'], 'current')
                candidate_selection = {'schema_version': '1.0', 'source_run_ref': context['source_run_ref'],
                    'invocation_sha256': context['invocation_sha256'], 'annotation': copy.deepcopy(context['annotation_seed'])}
                for step in candidate_selection['annotation']['operation_step_keys']:
                    step['step_key'] += '-v2'
                proposal_b = client.post(self.prefix + 'preview', json=candidate_selection)
                proposal_b.raise_for_status()
                candidate_b = proposal_b.json()['candidate_sha256']
                self.assertNotEqual(candidate_a, candidate_b)
                client.post(self.prefix + 'publish', json={**candidate_selection,
                    'confirm_sha256': candidate_b}).raise_for_status()
                review_b = self.evidence_and_review(client, helper, context, candidate_b, 'delta')
                page.get_by_test_id('review-hash').fill(review_b)
                page.get_by_test_id('review-inspect').click()
                expect(page.get_by_test_id('execution-review-hash')).to_have_value(review_b)
                page.get_by_test_id('release-parent').select_option(release_a)
                release_b = self.publish_version(page, client, review_b, release_a)
                selection_b = self.select_version(page, client, release_b, 'select')
                self.assertNotEqual(selection_a, selection_b)
                stale = client.post(self.prefix + 'selection-preview', json={'schema_version': '1.0',
                    'release_sha256': release_a, 'expected_selection_sha256': selection_a, 'operation': 'rollback'})
                self.assertEqual(stale.status_code, 409)
                run_b = self.execute_selected(page, client, helper, release_b, selection_b, review_b, 'epsilon')
                self.assertNotEqual(run_a['recipe_sha256'], run_b['recipe_sha256'])
                self.assertNotEqual(run_a['candidate_sha256'], run_b['candidate_sha256'])
                before_rollback = client.get(self.prefix + 'release-catalog').json()
                family = before_rollback['families'][0]
                self.assertEqual(family['selected_release_sha256'], release_b)
                self.assertEqual(family['selection_sha256'], selection_b)
                bypass = client.post(self.prefix + 'selection-preview', json={'schema_version': '1.0',
                    'release_sha256': release_a, 'expected_selection_sha256': selection_b, 'operation': 'select'})
                self.assertEqual(bypass.status_code, 409)
                page.reload()
                page.get_by_role('button', name='Tasks', exact=True).click()
                page.locator('#task-kind').select_option('browser_remote_form')
                page.get_by_test_id('release-catalog-load').click()
                expect(page.get_by_test_id('release-catalog')).to_contain_text(selection_b)
                self.assertEqual(helper.database_counts(database)[0], 5)
                rollback_selection = self.select_version(page, client, release_a, 'rollback')
                self.assertNotIn(rollback_selection, {selection_a, selection_b})
                run_restored = self.execute_selected(page, client, helper, release_a,
                    rollback_selection, review_a, 'zeta', blocked_release=release_b)
                for key in ('candidate_sha256', 'recipe_sha256', 'release_sha256', 'review_sha256'):
                    self.assertEqual(run_restored[key], run_a[key])
                self.assertNotEqual(run_restored['run_ref'], run_a['run_ref'])
                history = client.post(self.prefix + 'execution-audit', json={
                    'candidate_execution_sha256': run_a['candidate_execution_sha256']})
                history.raise_for_status()
                self.assertTrue(history.json()['available'])
                self.assertEqual(history.json()['selection_status'], 'superseded')
                current_catalog = client.get(self.prefix + 'release-catalog')
                current_catalog.raise_for_status()
                family = current_catalog.json()['families'][0]
                self.assertEqual(family['sequence'], 3)
                self.assertEqual(family['selection_sha256'], rollback_selection)
                self.assertEqual(family['selected_release_sha256'], release_a)
                self.assertEqual(len(family['releases']), 2)
                self.inspect_offline(source, family['family_sha256'],
                    [release_a, release_b, release_a], [selection_a, selection_b, rollback_selection])
                self.assertEqual(helper.source_bytes(source), source_bytes)
                page.get_by_test_id('skill-releases').scroll_into_view_if_needed()
                page.screenshot(path='/tmp/aos-release-real-en.png')
                page.get_by_role('button', name='Türkçe', exact=True).click()
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.get_by_test_id('skill-releases')).to_contain_text('Skill sürümleri ve gelişim seçimi')
                page.get_by_test_id('skill-releases').scroll_into_view_if_needed()
                page.screenshot(path='/tmp/aos-release-real-tr-mobile.png')
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                self.assertEqual(errors, [])
                for response in repeat_rejections:
                    self.assertEqual(response, {'detail': 'HTTPS form repeat source unavailable'})
                self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                self.revoke_pending_selected(client, context, candidate_a, release_a,
                    rollback_selection, review_a, database)
                denied = client.post(self.prefix + 'execution-preview', json={
                    'schema_version': '1.2', 'candidate_sha256': candidate_a,
                    'source_run_ref': context['source_run_ref'], 'source_invocation_sha256': context['invocation_sha256'],
                    'case_key': 'dev-denied', 'development_value': 'eta', 'review_sha256': review_a,
                    'release_sha256': release_a, 'selection_sha256': rollback_selection})
                self.assertEqual(denied.status_code, 409)
                final_catalog = client.get(self.prefix + 'release-catalog')
                final_catalog.raise_for_status()
                self.assertEqual(final_catalog.json()['families'][0]['selected_release_sha256'], release_a)
                self.assertEqual(final_catalog.json()['families'][0]['selection_sha256'], rollback_selection)
                with closing(sqlite3.connect(database)) as connection:
                    observations = connection.execute("SELECT COUNT(*) FROM observations WHERE run_id=? "
                        "AND kind='skill.owned_release_selection_admission'", (run_restored['run_id'],)).fetchone()[0]
                    self.assertEqual(observations, 1)
                    connection.execute("DELETE FROM observations WHERE run_id=? "
                        "AND kind='skill.owned_release_selection_admission'", (run_restored['run_id'],))
                    connection.commit()
                missing_admission = client.post(self.prefix + 'execution-audit', json={
                    'candidate_execution_sha256': run_restored['candidate_execution_sha256']})
                missing_admission.raise_for_status()
                self.assertFalse(missing_admission.json()['available'])
            finally:
                browser.close()
            from aos import local_app

            signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                stopped = local_app.read_state()
                if stopped is not None and stopped.phase == 'stopped':
                    break
                time.sleep(.1)
            else:
                self.fail('Isolated supervisor did not stop before offline release audit')
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', fixture_port), timeout=.2)
            self.audit_offline(source, database, bundle['manifest_sha256'],
                [run_a, run_b, run_restored], rollback_selection)

    def audit_offline(self, source, database, manifest_hash, runs, selection_hash):
        checked = subprocess.run([sys.executable, '-c', '''
import json,sqlite3,sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from aos.desktop_tasks import DesktopScheduler
from aos.site_skill_form_recipe_candidate import OwnedSiteSkillFormRecipeCandidateSession
from aos.web_application import WebApplicationProfiles
from aos.site_knowledge import SiteKnowledgeStore
source,database=Path(sys.argv[1]),Path(sys.argv[2])
profiles=WebApplicationProfiles(source/'profiles')
session=OwnedSiteSkillFormRecipeCandidateSession(directory=source,manifest_sha256=sys.argv[3],
    candidate_directory=source/'site-skill-recipe-candidates',database=database,
    profiles=profiles,pages=SiteKnowledgeStore(source/'site-knowledge',profiles))
with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as connection:
    connection.row_factory=sqlite3.Row
    scheduler=DesktopScheduler.__new__(DesktopScheduler)
    scheduler.owned_skill_planning=None
    scheduler.task=None
    scheduler.job_id=None
    scheduler.sequences=SimpleNamespace(reserved=False)
    scheduler.store=SimpleNamespace(connection=connection)
    scheduler.settings=SimpleNamespace(database=database)
    scheduler.remote_form_owned_candidate_session=session
    scheduler.remote_form_owned_manifest=json.loads((source/'manifest.json').read_text())
    scheduler._owned_candidate_execution_history={}
    with patch('socket.socket',side_effect=RuntimeError('network_forbidden')):
        reports=[scheduler.audit_owned_form_candidate_execution(checksum) for checksum in sys.argv[5:]]
        for report,review in zip(reports[:2],('revoked','accepted')):
            assert report['available'] is True,report
            assert report['schema_version']=='1.2'
            assert report['release_admission_verified'] is True
            assert report['review_admission_verified'] is True
            assert report['review_status']==review
            assert report['selection_status']=='superseded'
        assert reports[2]['available'] is False
        catalog=scheduler.catalog_owned_skill_releases()
        assert catalog['families'][0]['selection_sha256']==sys.argv[4]
print('closed-supervisor full release audit and selection verified')
''', str(source), str(database), manifest_hash, selection_hash,
            *(run['candidate_execution_sha256'] for run in runs)], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=45)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('closed-supervisor full release audit and selection verified', checked.stdout)

    def evidence_and_review(self, client, helper, context, candidate, value):
        selection = {'schema_version': '1.0', 'candidate_sha256': candidate,
            'source_run_ref': context['source_run_ref'], 'source_invocation_sha256': context['invocation_sha256'],
            'case_key': 'dev-evidence-' + value, 'development_value': value}
        preview = client.post(self.prefix + 'execution-preview', json=selection)
        preview.raise_for_status()
        control = client.get('/api/state').json()['control']
        start = client.post(self.prefix + 'execution-start', json={**selection,
            'preview_sha256': preview.json()['preview_sha256'], 'confirm_sha256': preview.json()['preview_sha256'],
            'lease_id': control['lease_id'], 'generation': control['generation']})
        start.raise_for_status()
        job, approvals = helper.approve_job(client)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(len(approvals), 6)
        self.wait_idle(client)
        execution = start.json()['candidate_execution_sha256']
        audit = client.post(self.prefix + 'execution-audit', json={'candidate_execution_sha256': execution})
        audit.raise_for_status()
        self.assertTrue(audit.json()['available'], audit.text)
        review_selection = {key: selection[key] for key in ('candidate_sha256', 'source_run_ref', 'source_invocation_sha256')}
        review_selection['candidate_execution_sha256'] = execution
        review = client.post(self.prefix + 'review-preview', json=review_selection)
        review.raise_for_status()
        review_hash = review.json()['review_sha256']
        client.post(self.prefix + 'review-accept', json={**review_selection,
            'review_sha256': review_hash, 'confirm_sha256': review_hash}).raise_for_status()
        return review_hash

    def revoke_pending_selected(self, client, context, candidate, release, selection, review, database):
        request = {'schema_version': '1.2', 'candidate_sha256': candidate,
            'source_run_ref': context['source_run_ref'],
            'source_invocation_sha256': context['invocation_sha256'],
            'case_key': 'dev-selected-revoke', 'development_value': 'theta',
            'release_sha256': release, 'selection_sha256': selection, 'review_sha256': review}
        preview = client.post(self.prefix + 'execution-preview', json=request)
        preview.raise_for_status()
        control = client.get('/api/state').json()['control']
        started = client.post(self.prefix + 'execution-start', json={**request,
            'preview_sha256': preview.json()['preview_sha256'],
            'confirm_sha256': preview.json()['preview_sha256'],
            'lease_id': control['lease_id'], 'generation': control['generation']})
        started.raise_for_status()
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
            self.fail('Selected execution did not reach pending fill')
        self.assertEqual(len(approved), 2)
        client.post(self.prefix + 'review-revoke', json={
            'review_sha256': review, 'confirm_sha256': review}).raise_for_status()
        self.wait_idle(client)
        tasks = client.get('/api/tasks').json()
        self.assertEqual(tasks['jobs'][0]['status'], 'failed')
        self.assertIsNone(tasks['approval'])
        self.assertEqual(tasks['owned_form_candidate_execution']['selection_sha256'], selection)
        self.assertEqual(tasks['owned_form_candidate_execution']['review_status'], 'revoked')
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'",
                (tasks['jobs'][0]['run_id'],)).fetchone()[0], 0)

    def publish_version(self, page, client, review_hash, parent):
        from playwright.sync_api import expect

        with page.expect_response(lambda response: response.url.endswith('/release-preview')) as response:
            page.get_by_test_id('release-preview').click()
        self.assertEqual(response.value.status, 200)
        proposal = response.value.json()
        checksum = proposal['release_sha256']
        expect(page.get_by_test_id('release-publish')).to_be_disabled()
        self.assertEqual(client.post(self.prefix + 'release-publish', json={'schema_version': '1.0',
            'review_sha256': review_hash, 'parent_release_sha256': parent,
            'confirm_sha256': '0' * 64}).status_code, 409)
        page.get_by_test_id('release-confirm').check()
        with page.expect_response(lambda response: response.url.endswith('/release-publish')) as response:
            page.get_by_test_id('release-publish').click()
        self.assertEqual(response.value.status, 200)
        self.assertEqual(response.value.json()['release_sha256'], checksum)
        expect(page.get_by_test_id('release-catalog')).to_contain_text(checksum)
        return checksum

    def select_version(self, page, client, release_hash, operation):
        from playwright.sync_api import expect

        row = page.locator(f'[data-release="{release_hash}"]')
        with page.expect_response(lambda response: response.url.endswith('/selection-preview')) as response:
            row.get_by_test_id('release-rollback' if operation == 'rollback' else 'release-select').click()
        self.assertEqual(response.value.status, 200)
        checksum = response.value.json()['selection_sha256']
        expect(page.get_by_test_id('selection-commit')).to_be_disabled()
        page.get_by_test_id('selection-confirm').check()
        with page.expect_response(lambda response: response.url.endswith('/selection-commit')) as response:
            page.get_by_test_id('selection-commit').click()
        self.assertEqual(response.value.status, 200)
        self.assertEqual(response.value.json()['selection_sha256'], checksum)
        catalog = client.get(self.prefix + 'release-catalog')
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertTrue(any(group['selection_sha256'] == checksum
            and group['selected_release_sha256'] == release_hash for group in catalog.json()['families']))
        expect(page.get_by_test_id('release-catalog')).to_contain_text(checksum)
        return checksum

    def execute_selected(self, page, client, helper, release_hash, selection_hash, review_hash, value,
                         blocked_release=None):
        from playwright.sync_api import expect

        blocked_request = None
        if blocked_release is not None:
            blocked_request = {'schema_version': '1.0', 'release_sha256': blocked_release,
                'expected_selection_sha256': selection_hash, 'operation': 'select'}
            proposed = client.post(self.prefix + 'selection-preview', json=blocked_request)
            proposed.raise_for_status()
            blocked_request['confirm_sha256'] = proposed.json()['selection_sha256']
        page.get_by_test_id('release-use-selected').click()
        expect(page.get_by_test_id('execution-use-release')).to_be_checked()
        expect(page.get_by_test_id('execution-release-binding')).to_contain_text(selection_hash)
        page.get_by_test_id('execution-case-key').fill('dev-selected-' + value)
        page.get_by_test_id('execution-value').fill(value)
        with page.expect_response(lambda response: response.url.endswith('/execution-preview')) as response:
            page.get_by_test_id('execution-preview').click()
        self.assertEqual(response.value.status, 200)
        for key, expected in {'schema_version': '1.2', 'release_sha256': release_hash,
                'selection_sha256': selection_hash, 'review_sha256': review_hash}.items():
            self.assertEqual(response.value.json()[key], expected)
        expect(page.get_by_test_id('execution-start')).to_be_disabled()
        page.get_by_test_id('execution-confirm').check()
        with page.expect_response(lambda response: response.url.endswith('/execution-start')) as response:
            page.get_by_test_id('execution-start').click()
        self.assertEqual(response.value.status, 200)
        if blocked_request is not None:
            self.assertEqual(client.post(self.prefix + 'selection-commit', json=blocked_request).status_code, 409)
        job, approvals = helper.approve_job(client, page=page)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(len(approvals), 6)
        self.wait_idle(client)
        with page.expect_response(lambda response: response.url.endswith('/execution-audit')) as response:
            page.get_by_test_id('execution-audit').click()
        self.assertEqual(response.value.status, 200)
        report = response.value.json()
        self.assertTrue(report['available'], report)
        for key, expected in {'schema_version': '1.2', 'release_sha256': release_hash,
                'selection_sha256': selection_hash, 'review_sha256': review_hash,
                'release_admission_verified': True, 'review_admission_verified': True}.items():
            self.assertEqual(report[key], expected)
        return report

    def inspect_offline(self, source, family_hash, releases, selections):
        checked = subprocess.run([sys.executable, '-c', '''
import json,sys
from pathlib import Path
from unittest.mock import patch
from aos.contracts import digest
from aos.owned_skill_release import (load_owned_skill_release,current_owned_skill_selection,
    owned_skill_selection_chain,list_owned_skill_release_families)
source=Path(sys.argv[1]); family=sys.argv[2]
expected_releases=json.loads(sys.argv[3]); expected_selections=json.loads(sys.argv[4])
with patch('socket.socket',side_effect=RuntimeError('network_forbidden')):
    head,event=current_owned_skill_selection(source,family)
    chain=owned_skill_selection_chain(source,family)
    assert head['selection_sha256']==expected_selections[-1] and head['sequence']==3
    assert event['release_sha256']==expected_releases[-1]
    assert [checksum for checksum,event in chain]==expected_selections
    assert [event['release_sha256'] for checksum,event in chain]==expected_releases
    assert [event['operation'] for checksum,event in chain]==['select','select','rollback']
    for checksum in set(expected_releases):
        release=load_owned_skill_release(source,checksum)
        assert digest(release)==checksum and release['family_sha256']==family
    catalog=list_owned_skill_release_families(source)
    assert len(catalog)==1 and catalog[0]['selection_sha256']==expected_selections[-1]
print('offline release history and current selection verified')
''', str(source), family_hash, json.dumps(releases), json.dumps(selections)], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('offline release history and current selection verified', checked.stdout)

    def wait_idle(self, client):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            tasks = client.get('/api/tasks').json()
            if not tasks['busy'] and not tasks.get('reserved'):
                return
            time.sleep(.1)
        self.fail('Selected execution did not settle within 10 seconds')

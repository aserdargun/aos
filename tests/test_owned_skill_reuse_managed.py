from contextlib import closing, contextmanager, ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from aos import local_app
from aos.contracts import REPO_ROOT, canonical, digest
from aos.owned_form_invocation_session import provision_owned_synthetic_form_invocation
from aos.owned_learning_workspace import OwnedLearningWorkspace
import test_owned_candidate_execution_managed as execution_helpers
import test_owned_skill_release_managed as release_helpers


SUPERVISOR_PROGRAM = '''
import json,sys
from pathlib import Path
from aos import local_app
base,session,port,lock,listener,options=sys.argv[1:]
local_app.BASE=Path(base)
local_app.ORIGIN='http://127.0.0.1:'+port
local_app.URL=local_app.ORIGIN+'/ui/'
original=local_app.managed_backend_command
def command(*args,**kwargs):
    result=original(*args,**kwargs)
    return [value for value in result if value!='--managed-retention']+['--port',port,'--knowledge-root',str(Path(base)/'document-knowledge')]
local_app.managed_backend_command=command
local_app.supervise(session,'real',int(lock),int(listener),**json.loads(options))
'''


def read_isolated_state(base):
    with patch('aos.local_app.BASE', base):
        return local_app.read_state()


@contextmanager
def owned_subprocess_session(base, *, previous=None, material=None):
    session = 'app-' + uuid4().hex
    directory = base / session
    directory.mkdir(mode=0o700)
    source = base / (previous.owned_skill_source_session or previous.session) if previous else directory
    process = None
    client = None
    with ExitStack() as resources:
        manager_lock = os.open(base / 'manager.lock', os.O_RDWR | os.O_CREAT, 0o600)
        resources.callback(os.close, manager_lock)
        fcntl.flock(manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        listener = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        api_port = listener.getsockname()[1]
        fixture = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        fixture.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        fixture_port = int(material['preview']['origin'].rsplit(':', 1)[1]) if material else 0
        fixture.bind(('127.0.0.1', fixture_port))
        fixture.listen(32)
        fixture_port = fixture.getsockname()[1]
        if material is None:
            bundle = provision_owned_synthetic_form_invocation(source / 'owned-form', fixture_port)
        else:
            local_app.write_new_private_plan(directory / 'owned-skill-reuse-previous-state.json',
                                             canonical(previous.model_dump()).encode())
            local_app.write_new_private_plan(directory / 'owned-skill-reuse-preview.json',
                                             canonical(material['preview']).encode())
            bundle = {'manifest_sha256': previous.owned_form_manifest_sha256}
        source_lock = resources.enter_context(OwnedLearningWorkspace.acquire(source / 'owned-form', create=True))
        options = {'owned_form_listener_fd': fixture.fileno(),
                   'owned_form_manifest_sha256': bundle['manifest_sha256'],
                   'owned_learning_lock_fd': source_lock.lock_fd}
        if material is not None:
            options['owned_skill_reuse_sha256'] = material['preview_sha256']
        log = resources.enter_context((directory / 'manager.log').open('xb'))
        os.chmod(directory / 'manager.log', 0o600)
        process = subprocess.Popen(
            [sys.executable, '-c', SUPERVISOR_PROGRAM, str(base), session, str(api_port),
             str(manager_lock), str(listener.fileno()), json.dumps(options)],
            cwd=REPO_ROOT, env={**os.environ, 'PYTHONPATH': str(REPO_ROOT / 'src')},
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            pass_fds=(manager_lock, listener.fileno(), fixture.fileno(), source_lock.lock_fd))
    origin = f'http://127.0.0.1:{api_port}'
    try:
        client = httpx.Client(base_url=origin, headers={'Origin': origin}, trust_env=False, timeout=10)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                logs = '\n'.join(path.read_text(errors='replace')[-6000:]
                                 for path in (directory / 'manager.log', directory / 'backend.log')
                                 if path.is_file())
                raise AssertionError('Owned subprocess exited before readiness: ' + logs)
            state = read_isolated_state(base)
            try:
                if (state is not None and state.session == session and state.phase == 'running'
                        and client.get('/api/session').status_code == 200):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.2)
        else:
            raise AssertionError('Owned subprocess did not become ready')
        with patch('aos.local_app.BASE', base):
            token = local_app.token_value(state)
        client.post('/api/login', json={'token': token}).raise_for_status()
        yield SimpleNamespace(client=client, origin=origin, token=token, state=state,
                              directory=directory, source=source, fixture_port=fixture_port,
                              process=process)
    finally:
        failed = sys.exc_info()[0] is not None
        if client is not None:
            client.close()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        process.wait(timeout=150)
        final_state = read_isolated_state(base)
        if not failed:
            if (process.returncode != 0 or final_state is None or final_state.session != session
                    or final_state.phase != 'stopped'):
                raise AssertionError('Owned subprocess did not prove clean shutdown: ' + str(final_state))
            if local_app.observe_process(final_state.supervisor) == 'same_process':
                raise AssertionError('Supervisor process remains live')
            try:
                connection = socket.create_connection(('127.0.0.1', fixture_port), timeout=.2)
            except OSError:
                pass
            else:
                connection.close()
                raise AssertionError('Owned fixture listener remains live')


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_OWNED_SKILL_REUSE_TESTS') == '1',
                     'Explicit two-process real Decider/Chromium selected skill reuse acceptance')
class OwnedSkillReuseManagedTests(unittest.TestCase):
    prefix = '/api/tasks/owned-form-candidate/'

    def retained_rows(self, database):
        with closing(sqlite3.connect(database)) as connection:
            return {table: connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
                    for table in ('runs', 'model_calls', 'actions', 'desktop_tasks', 'desktop_approvals')}

    def revoke_before_fill(self, client, selection, database):
        preview = self.post(client, 'execution-preview', selection)
        control = client.get('/api/state').json()['control']
        self.post(client, 'execution-start', selection | {
            'preview_sha256': preview['preview_sha256'], 'confirm_sha256': preview['preview_sha256'],
            'lease_id': control['lease_id'], 'generation': control['generation']})
        approved = set()
        deadline = time.monotonic() + 60
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
            self.fail('Reused selected execution did not reach pending fill')
        self.assertEqual(len(approved), 2)
        self.post(client, 'review-revoke', {'review_sha256': selection['review_sha256'],
                                          'confirm_sha256': selection['review_sha256']})
        release_helpers.OwnedSkillReleaseManagedTests().wait_idle(client)
        tasks = client.get('/api/tasks').json()
        self.assertEqual(tasks['jobs'][0]['status'], 'failed')
        self.assertIsNone(tasks['approval'])
        with closing(sqlite3.connect(database)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM actions WHERE run_id=? AND tool='browser.form.submit'",
                (tasks['jobs'][0]['run_id'],)).fetchone()[0]
            self.assertEqual(count, 0)

    def audit_offline(self, source, database, manifest, execution, admission):
        result = subprocess.run([sys.executable, '-c', '''
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
        report=scheduler.audit_owned_form_candidate_execution(sys.argv[4])
        assert report['available'] is True,report
        assert report['schema_version']=='1.3',report
        assert report['reuse_admission_verified'] is True,report
        assert report['reuse_admission_sha256']==sys.argv[5],report
        assert report['release_admission_verified'] is True,report
        assert report['review_status']=='revoked',report
print('Offline reused execution audit verified after revocation and shutdown')
''', str(source), str(database), manifest, execution, admission], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Offline reused execution audit verified', result.stdout)

    def post(self, client, operation, payload):
        response = client.post(self.prefix + operation, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def selected_execution(self, client, helper, selection, *, page=None):
        if page is None:
            preview = self.post(client, 'execution-preview', selection)
            control = client.get('/api/state').json()['control']
            started = self.post(client, 'execution-start', selection | {
                'preview_sha256': preview['preview_sha256'], 'confirm_sha256': preview['preview_sha256'],
                'lease_id': control['lease_id'], 'generation': control['generation']})
        else:
            from playwright.sync_api import expect

            with page.expect_response(lambda response: response.url.endswith('/release-catalog')) as response:
                page.get_by_test_id('release-catalog-load').click()
            self.assertEqual(response.value.status, 200, response.value.text())
            jobs_before = client.get('/api/tasks').json()['jobs']
            page.get_by_test_id('release-prepare-reuse').click()
            guide = page.get_by_test_id('skill-reuse-guide')
            expect(guide).to_be_visible()
            preview_command = guide.get_by_test_id('reuse-preview-command')
            expect(preview_command).to_contain_text('preview-owned-skill-reuse')
            expect(preview_command).to_contain_text(selection['release_sha256'])
            expect(preview_command).to_contain_text(selection['selection_sha256'])
            expect(guide.get_by_test_id('reuse-start-command')).to_contain_text('PREVIEW_SHA256')
            self.assertEqual(client.get('/api/tasks').json()['jobs'], jobs_before)
            guide.scroll_into_view_if_needed()
            page.screenshot(path='/tmp/aos-reuse-guide-real-en.png')
            page.get_by_role('button', name='Türkçe', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(guide).to_be_visible()
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
            guide.screenshot(path='/tmp/aos-reuse-guide-real-tr.png')
            page.get_by_role('button', name='English', exact=True).click()
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.get_by_test_id('release-use-selected').click()
            expect(page.get_by_test_id('execution-use-release')).to_be_checked()
            expect(page.get_by_test_id('execution-use-release')).to_be_disabled()
            expect(page.get_by_test_id('execution-reuse-source')).to_contain_text(selection['reuse_admission_sha256'])
            page.get_by_test_id('execution-case-key').fill(selection['case_key'])
            page.get_by_test_id('execution-value').fill(selection['development_value'])
            with page.expect_response(lambda response: response.url.endswith('/execution-preview')) as response:
                page.get_by_test_id('execution-preview').click()
            self.assertEqual(response.value.status, 200, response.value.text())
            preview = response.value.json()
            self.assertEqual(preview['schema_version'], '1.3')
            self.assertEqual(preview['reuse_admission_sha256'], selection['reuse_admission_sha256'])
            expect(page.get_by_test_id('execution-start')).to_be_disabled()
            page.get_by_test_id('execution-confirm').check()
            with page.expect_response(lambda response: response.url.endswith('/execution-start')) as response:
                page.get_by_test_id('execution-start').click()
            self.assertEqual(response.value.status, 200, response.value.text())
            started = response.value.json()
        job, approvals = helper.approve_job(client, page=page)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(len(approvals), 6)
        release_helpers.OwnedSkillReleaseManagedTests().wait_idle(client)
        if page is None:
            audit = self.post(client, 'execution-audit', {
                'candidate_execution_sha256': started['candidate_execution_sha256']})
        else:
            with page.expect_response(lambda response: response.url.endswith('/execution-audit')) as response:
                page.get_by_test_id('execution-audit').click()
            self.assertEqual(response.value.status, 200, response.value.text())
            audit = response.value.json()
            expect(page.get_by_test_id('execution-reuse-audit')).to_be_visible()
        self.assertTrue(audit['available'], audit)
        return job, approvals, audit

    def test_selected_skill_runs_in_new_session_without_replaying_old_work(self):
        base = REPO_ROOT / 'data' / ('local-app-test-' + uuid4().hex)
        base.mkdir(mode=0o700)
        helper = execution_helpers.OwnedCandidateExecutionManagedTests()
        release_helper = release_helpers.OwnedSkillReleaseManagedTests()
        completed = False
        try:
            with owned_subprocess_session(base) as first:
                context, candidate, demonstration = helper.create_source_candidate(first.client)
                review = release_helper.evidence_and_review(first.client, helper, context, candidate, 'beta')
                request = {'schema_version': '1.0', 'review_sha256': review, 'parent_release_sha256': None}
                release = self.post(first.client, 'release-preview', request)['release_sha256']
                self.post(first.client, 'release-publish', request | {'confirm_sha256': release})
                request = {'schema_version': '1.0', 'release_sha256': release,
                           'expected_selection_sha256': None, 'operation': 'select'}
                selection = self.post(first.client, 'selection-preview', request)['selection_sha256']
                self.post(first.client, 'selection-commit', request | {'confirm_sha256': selection})
                selected_request = {'schema_version': '1.2', 'candidate_sha256': candidate,
                    'source_run_ref': context['source_run_ref'],
                    'source_invocation_sha256': context['invocation_sha256'],
                    'case_key': 'dev-first-session', 'development_value': 'gamma',
                    'review_sha256': review, 'release_sha256': release, 'selection_sha256': selection}
                first_job, first_approvals, first_audit = self.selected_execution(first.client, helper, selected_request)
                first_control = first.client.get('/api/state').json()['control']
                source_bytes = helper.source_bytes(first.source / 'owned-form')
                database = first.source / 'store.sqlite'
                database_identity = (database.stat().st_dev, database.stat().st_ino)
            previous = read_isolated_state(base)
            self.assertEqual(previous.phase, 'stopped')
            before_counts = helper.database_counts(database)
            old_rows = self.retained_rows(database)
            with patch('socket.socket', side_effect=AssertionError('preview_must_be_offline')):
                material = local_app.prepare_owned_skill_reuse(previous, release, selection, base=base)
            self.assertEqual(material['preview']['release_sha256'], release)
            self.assertEqual(material['preview']['selection_sha256'], selection)
            with owned_subprocess_session(base, previous=previous, material=material) as second:
                self.assertNotEqual(second.state.session, first.state.session)
                self.assertEqual(second.state.owned_skill_source_session, first.state.session)
                self.assertFalse((second.directory / 'store.sqlite').exists())
                self.assertFalse((second.directory / 'owned-form').exists())
                self.assertEqual((database.stat().st_dev, database.stat().st_ino), database_identity)
                self.assertEqual(helper.database_counts(database), before_counts)
                self.assertEqual(self.retained_rows(database), old_rows)
                self.assertEqual(helper.source_bytes(first.source / 'owned-form'), source_bytes)
                with httpx.Client(base_url=second.origin, headers={'Origin': second.origin},
                                  trust_env=False, timeout=10) as stale_client:
                    rejected = stale_client.post('/api/login', json={'token': first.token})
                    self.assertEqual(rejected.status_code, 401, rejected.text)
                with self.assertRaises(ValueError):
                    OwnedLearningWorkspace.acquire(first.source / 'owned-form')
                control = second.client.get('/api/state').json()['control']
                self.assertNotEqual(control['lease_id'], first_control['lease_id'])
                tasks = second.client.get('/api/tasks').json()
                self.assertFalse(tasks['busy'])
                self.assertIsNone(tasks['approval'])
                admission = tasks['owned_form_invocation']['reuse_admission_sha256']
                persisted = json.loads((second.directory / 'owned-skill-reuse-admission.json').read_bytes())
                self.assertEqual(digest(persisted), admission)
                self.assertEqual(persisted['preview_sha256'], material['preview_sha256'])
                self.assertEqual(persisted['manager_session'], second.state.session)
                for changed in ({}, {'schema_version': '1.3', 'reuse_admission_sha256': '0' * 64}):
                    rejected = second.client.post(self.prefix + 'execution-preview', json=selected_request | changed)
                    self.assertEqual(rejected.status_code, 409, rejected.text)
                    self.assertEqual(helper.database_counts(database), before_counts)
                request = selected_request | {'schema_version': '1.3', 'reuse_admission_sha256': admission,
                    'case_key': 'dev-new-session', 'development_value': 'delta'}
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                        'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                    try:
                        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        repeat_warnings = []

                        def console_message(message):
                            if message.type not in {'error', 'warning'}:
                                return
                            if (message.location.get('url') == second.origin + '/api/remote-form/repeats'
                                    and any(code in message.text for code in ('404 (Not Found)', '409 (Conflict)'))):
                                repeat_warnings.append(message.text)
                                return
                            errors.append(message.text + ' ' + str(message.location))

                        page.on('console', console_message)
                        page.goto(second.origin + '/ui/')
                        self.assertEqual(page.url, second.origin + '/ui/')
                        self.assertEqual(page.title(), 'AOS · Control center')
                        page.get_by_label('Local session token').fill(second.token)
                        page.get_by_role('button', name='Sign in', exact=True).click()
                        page.get_by_role('button', name='Tasks', exact=True).click()
                        page.locator('#task-kind').select_option('browser_remote_form')
                        second_job, second_approvals, second_audit = self.selected_execution(
                            second.client, helper, request, page=page)
                        self.assertEqual(errors, [])
                        self.assertEqual(page.locator('vite-error-overlay, nextjs-portal').count(), 0)
                        self.assertGreater(len(page.locator('body').inner_text()), 100)
                        if repeat_warnings:
                            unavailable = second.client.get('/api/remote-form/repeats')
                            self.assertIn(unavailable.status_code, (404, 409))
                            self.assertIn(unavailable.json()['detail'], (
                                'No pinned HTTPS form repeat source', 'HTTPS form repeat source unavailable'))
                        page.screenshot(path='/tmp/aos-owned-reuse-real-en.png', full_page=True)
                        page.get_by_role('button', name='Türkçe', exact=True).click()
                        page.set_viewport_size({'width': 390, 'height': 844})
                        self.assertTrue(page.get_by_test_id('execution-reuse-audit').is_visible())
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                        page.screenshot(path='/tmp/aos-owned-reuse-real-tr-mobile.png', full_page=True)
                    finally:
                        browser.close()
                self.assertNotEqual(first_job['run_id'], second_job['run_id'])
                self.assertTrue(set(first_approvals).isdisjoint(second_approvals))
                self.assertTrue(second_audit['reuse_admission_verified'], second_audit)
                for field in ('recipe_sha256', 'review_sha256', 'release_sha256', 'selection_sha256'):
                    self.assertEqual(second_audit[field], first_audit[field])
                self.assertEqual(second_audit['reuse_admission_sha256'], admission)
                with closing(sqlite3.connect(database)) as connection:
                    sessions = connection.execute('''SELECT tasks.session_id,sessions.runtime_id
                        FROM desktop_tasks tasks JOIN desktop_sessions sessions USING(session_id)
                        WHERE tasks.run_id IN (?,?) ORDER BY tasks.run_id''',
                        (first_job['run_id'], second_job['run_id'])).fetchall()
                    self.assertEqual(len(sessions), 2)
                    self.assertNotEqual(sessions[0][0], sessions[1][0])
                    self.assertNotEqual(sessions[0][1], sessions[1][1])
                self.revoke_before_fill(second.client, request | {
                    'case_key': 'dev-new-session-revoked', 'development_value': 'epsilon'}, database)
            self.assertEqual(read_isolated_state(base).phase, 'stopped')
            self.audit_offline(first.source / 'owned-form', database,
                               first.state.owned_form_manifest_sha256,
                               second_audit['candidate_execution_sha256'], admission)
            completed = True
        finally:
            if completed:
                shutil.rmtree(base)
            else:
                print('Preserved private reuse acceptance evidence:', base, flush=True)

from contextlib import closing, contextmanager
from functools import partial
import httpx
import json
import os
from pathlib import Path
import signal
import socket
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from aos.contracts import REPO_ROOT, digest
from aos import local_app


@contextmanager
def isolated_supervisor(provision_bundle):
    base = REPO_ROOT / 'data' / f'local-app-test-{uuid4().hex}'
    base.mkdir(mode=0o700)
    teardown_verified = False
    preserve_base = False
    try:
        session = 'app-' + uuid4().hex
        session_directory = base / session
        session_directory.mkdir(mode=0o700)
        lock_path = base / 'manager.lock'
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        api_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        api_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        api_listener.bind(('127.0.0.1', 0))
        api_listener.listen(128)
        api_port = api_listener.getsockname()[1]
        origin = f'http://127.0.0.1:{api_port}'
        api_listener_fd = api_listener.detach()
        api_listener_identity = os.fstat(api_listener_fd)

        fixture_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fixture_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        fixture_listener.bind(('127.0.0.1', 0))
        fixture_listener.listen(32)
        fixture_port = fixture_listener.getsockname()[1]
        fixture_listener_fd = fixture_listener.detach()
        fixture_listener_identity = os.fstat(fixture_listener_fd)
        profile_bundle = provision_bundle(session_directory / 'owned-form', fixture_port)

        signal_handlers = {}
        supervisor_errors = []
        original_command = local_app.managed_backend_command

        def managed_command(directory, mode, listen_fd, **options):
            command = original_command(directory, mode, listen_fd, **options)
            command = [argument for argument in command if argument != '--managed-retention']
            command.extend(['--port', str(api_port)])
            return command

        def capture_signal_handler(number, handler):
            signal_handlers[number] = handler

        def run_supervisor():
            try:
                local_app.supervise(
                    session, 'real', lock_fd, api_listener_fd,
                    owned_form_listener_fd=fixture_listener_fd,
                    owned_form_manifest_sha256=profile_bundle['manifest_sha256'])
            except BaseException as failure:
                supervisor_errors.append(failure)

        patches = [
            patch.multiple(local_app, BASE=base, ORIGIN=origin, URL=origin + '/ui/'),
            patch('aos.local_app.managed_backend_command', side_effect=managed_command),
            patch('aos.local_app.signal.signal', side_effect=capture_signal_handler),
        ]
        for active_patch in patches:
            active_patch.start()
        thread = threading.Thread(target=run_supervisor, name='isolated-owned-form-supervisor', daemon=True)
        client = httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=10, trust_env=False)
        teardown_verified = False
        try:
            thread.start()
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if supervisor_errors:
                    raise supervisor_errors[0]
                if thread.ident is not None and not thread.is_alive():
                    state = local_app.read_state()
                    logs = []
                    for name in ('manager.log', 'backend.log'):
                        path = session_directory / name
                        if path.is_file():
                            logs.append(f'{path}: {path.read_text(errors="replace")[-4000:]}')
                    raise AssertionError(
                        f'Isolated supervisor exited before readiness; state={state}; logs={logs}')
                try:
                    response = client.get('/api/session')
                    if response.status_code == 200:
                        state = local_app.read_state()
                        if state is not None and state.session == session and state.phase == 'running':
                            break
                except httpx.HTTPError:
                    pass
                time.sleep(.2)
            else:
                raise AssertionError('Isolated managed backend did not become ready')
            state = local_app.read_state()
            if state is None or state.session != session or state.phase != 'running':
                raise AssertionError('Isolated supervisor did not publish its running state')
            token = local_app.token_value(state)
            client.post('/api/login', json={'token': token}).raise_for_status()
            yield (origin, token, client, session_directory, fixture_port, profile_bundle,
                   signal_handlers)
        finally:
            preserve_base = sys.exc_info()[0] is not None
            handler = signal_handlers.get(signal.SIGTERM)
            if handler is not None and thread.is_alive():
                handler(signal.SIGTERM, None)
            if thread.ident is not None:
                thread.join(timeout=120)
            client.close()
            final_state = local_app.read_state() if not thread.is_alive() else None
            teardown_verified = (not supervisor_errors and final_state is not None
                                 and final_state.session == session and final_state.phase == 'stopped')
            if teardown_verified:
                try:
                    connection = socket.create_connection(('127.0.0.1', fixture_port), timeout=.2)
                except OSError:
                    pass
                else:
                    connection.close()
                    teardown_verified = False
                    preserve_base = True
                    raise AssertionError('Owned HTTPS fixture listener remained reachable after shutdown')
            for descriptor, original_identity in (
                    (api_listener_fd, api_listener_identity),
                    (fixture_listener_fd, fixture_listener_identity),
                    (lock_fd, None)):
                try:
                    current_identity = os.fstat(descriptor)
                except OSError:
                    continue
                if (original_identity is None
                        or (current_identity.st_dev, current_identity.st_ino)
                        == (original_identity.st_dev, original_identity.st_ino)):
                    os.close(descriptor)
            for active_patch in reversed(patches):
                active_patch.stop()
            if thread.is_alive():
                raise AssertionError('Isolated supervisor did not stop cleanly')
            if supervisor_errors:
                raise supervisor_errors[0]
            if not teardown_verified:
                if sys.exc_info()[0] is None:
                    raise AssertionError('Isolated supervisor did not publish a verified stopped state')
    finally:
        if teardown_verified and not preserve_base:
            shutil.rmtree(base, ignore_errors=True)


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_OWNED_FORM_INVOCATION_TESTS') == '1',
                     'Requires explicit owned-form managed real-model acceptance opt-in')
class OwnedFormInvocationManagedTests(unittest.TestCase):
    def test_managed_single_use_form_needs_six_approvals_and_manual_audit(self):
        self._check_managed_single_use_form(recipe=False)

    @unittest.skipUnless(os.environ.get('AOS_OWNED_FORM_RECIPE_TESTS') == '1',
                         'Explicit owned recipe managed real-model acceptance opt-in')
    def test_managed_recipe_steps_audit_and_changed_source(self):
        self._check_managed_single_use_form(recipe=True)

    def _check_managed_single_use_form(self, *, recipe):
        from playwright.sync_api import expect, sync_playwright
        from aos.owned_form_invocation_session import provision_owned_synthetic_form_invocation

        provision = (partial(provision_owned_synthetic_form_invocation, mode='owned_synthetic_form_recipe')
                     if recipe else provision_owned_synthetic_form_invocation)
        with isolated_supervisor(provision) as (
                origin, token, client, session_directory, fixture_port, bundle, signal_handlers
        ), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                start_requests, approval_requests, audit_requests = [], [], []
                try:
                    status = client.get('/api/tasks').json()
                    self.assertTrue(status['available'])
                    self.assertEqual(status['jobs'], [])
                    self.assertEqual(status['owned_form_invocation']['lifecycle'], 'ready')
                    self.assertEqual(status['owned_form_invocation']['invocation_sha256'], bundle['invocation_sha256'])
                    if recipe:
                        self.assertEqual(status['owned_form_invocation']['mode'], 'owned_synthetic_form_recipe')
                        self.assertEqual(status['owned_form_invocation']['recipe_sha256'], bundle['recipe_sha256'])
                        self.assertEqual(client.get('/api/tasks/owned-form-invocation-audit').json(), {
                            'mode': 'owned_synthetic_form_recipe', 'available': False,
                            'status': 'not_ready', 'report': None, 'report_sha256': None})

                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('request', lambda request: start_requests.append(request.post_data_json)
                            if request.method == 'POST' and request.url.endswith('/api/tasks') else None)
                    page.on('request', lambda request: approval_requests.append(request.url)
                            if request.method == 'POST' and '/api/approvals/' in request.url else None)
                    page.on('request', lambda request: audit_requests.append(request.url)
                            if request.url.endswith('/api/tasks/owned-form-invocation-audit') else None)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.locator('#task-kind').select_option('browser_remote_form')
                    expect(page.get_by_test_id('owned-form-invocation')).to_contain_text(
                        'Owned synthetic executable skill' if recipe else 'Owned synthetic fixed-template invocation')
                    expect(page.get_by_test_id('owned-invocation-opt-in')).to_be_visible()
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    self.assertEqual(audit_requests, [])
                    expect(page.get_by_test_id('task-row')).to_have_count(0)

                    page.get_by_test_id('owned-invocation-opt-in').check()
                    start_button = page.get_by_test_id('start-task')
                    start_button.click()
                    first = (('browser.form.open', 'browser.form.fill', 'browser.form.state_before',
                              'browser.form.submit', 'browser.form.receipt', 'browser.form.state_after')
                             if recipe else ('browser.form.open', 'browser.form.state_before', 'browser.form.fill',
                                             'browser.form.submit', 'browser.form.receipt', 'browser.form.state_after'))
                    for tool in first:
                        deadline = time.monotonic() + 120
                        while time.monotonic() < deadline:
                            current = client.get('/api/tasks').json()
                            if current.get('approval') is not None:
                                self.assertEqual(current['approval']['action']['tool'], tool)
                                break
                            current_job = current.get('jobs', [{}])[0]
                            if current_job.get('status') in {'failed', 'cancelled'}:
                                trace = None
                                if current_job.get('run_id'):
                                    trace_response = client.get('/api/runs/' + current_job['run_id'])
                                    trace = trace_response.json() if trace_response.status_code == 200 else None
                                self.fail(f'Owned form failed before {tool}: {current_job}; trace={str(trace)[:2000]}')
                            time.sleep(.2)
                        else:
                            self.fail(f'Timed out waiting for {tool}; current tasks={client.get("/api/tasks").json()}')
                        expect(page.get_by_test_id('approval')).to_contain_text(tool, timeout=15000)
                        if recipe:
                            expect(page.get_by_test_id('owned-recipe-current-step')).to_have_count(1)
                            expect(page.get_by_test_id('owned-recipe-current-step')).to_contain_text('This step awaits approval')
                            expect(page.get_by_test_id('owned-recipe-current-step').locator('..')).to_have_attribute('aria-current', 'step')
                            expect(page.get_by_test_id('owned-recipe-current-step').locator('..')).to_have_attribute(
                                'data-operation', current['approval']['action']['selected_option'])
                        expect(page.get_by_test_id('approve-all')).to_be_disabled()
                        page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=120000)
                    expect(page.get_by_test_id('owned-form-invocation')).to_contain_text('Completed', timeout=30000)
                    expect(page.get_by_test_id('owned-invocation-audit')).to_be_visible()
                    expect(start_button).to_be_disabled()
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_have_count(0)
                    self.assertEqual(len(start_requests), 1)
                    self.assertEqual(set(start_requests[0]), {'kind', 'lease_id', 'generation'})
                    self.assertEqual(start_requests[0]['kind'], 'browser_remote_form')
                    self.assertEqual(len(approval_requests), 6)
                    self.assertEqual(audit_requests, [])
                    self.assertEqual(client.post('/api/tasks', json=start_requests[0]).status_code, 409)

                    with page.expect_response(lambda response: response.url.endswith(
                            '/api/tasks/owned-form-invocation-audit')) as audit_response:
                        page.get_by_test_id('owned-invocation-audit').click()
                    self.assertEqual(audit_response.value.status, 200)
                    self.assertEqual(audit_response.value.json()['status'], 'verified',
                                     audit_response.value.json())
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text(
                        'executable_recipe_executed' if recipe else 'invocation_execution_verified', timeout=30000)
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text(
                        'skill_executed=true' if recipe else 'skill_executed=false')
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text('No site outcome')
                    self.assertEqual(len(audit_requests), 1)
                    self.assertEqual(audit_requests[0], origin + '/api/tasks/owned-form-invocation-audit')

                    audited = client.get('/api/tasks').json()
                    self.assertEqual(audited['owned_form_invocation']['lifecycle'], 'audited')
                    self.assertEqual(audited['owned_form_invocation']['invocation_sha256'], bundle['invocation_sha256'])
                    self.assertTrue((session_directory / 'owned-form' / 'manifest.json').is_file())
                    if not recipe:
                        source_directory = session_directory / 'owned-form'
                        candidate_directory = source_directory / 'site-skill-recipe-candidates'
                        database = session_directory / 'store.sqlite'

                        def trajectory_counts():
                            with closing(sqlite3.connect(database)) as connection:
                                return tuple(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                                             for table in ('runs', 'model_calls', 'actions'))

                        counts_before = trajectory_counts()
                        panel = page.get_by_test_id('owned-skill-candidate')
                        expect(panel).to_be_visible(timeout=15000)
                        self.assertFalse(candidate_directory.exists())
                        with page.expect_response(lambda response: response.url.endswith('/owned-form-candidate/context')) as context_response:
                            page.get_by_test_id('candidate-context').click()
                        self.assertEqual(context_response.value.status, 200)
                        context = context_response.value.json()
                        self.assertTrue(context['available'], context)
                        page.get_by_test_id('candidate-skill-key').fill('managed-demonstration-skill')
                        page.get_by_test_id('candidate-names-confirmed').check()
                        with page.expect_response(lambda response: response.url.endswith('/owned-form-candidate/preview')) as preview_response:
                            page.get_by_test_id('candidate-preview').click()
                        self.assertEqual(preview_response.value.status, 200, preview_response.value.text())
                        preview = preview_response.value.json()
                        self.assertFalse(preview['persisted'])
                        expect(page.get_by_test_id('candidate-result')).to_contain_text('Preview')
                        self.assertFalse(candidate_directory.exists())
                        expect(page.get_by_test_id('candidate-publish')).to_be_disabled()
                        page.get_by_test_id('candidate-publish-confirm').check()
                        with page.expect_response(lambda response: response.url.endswith('/owned-form-candidate/publish')) as publish_response:
                            page.get_by_test_id('candidate-publish').click()
                        self.assertEqual(publish_response.value.status, 200, publish_response.value.text())
                        published = publish_response.value.json()
                        self.assertEqual(published['candidate_sha256'], preview['candidate_sha256'])
                        self.assertTrue(published['persisted'])
                        expect(page.get_by_test_id('candidate-result')).to_contain_text('Unreviewed candidate saved')
                        with page.expect_response(lambda response: response.url.endswith('/owned-form-candidate/inspect')) as inspect_response:
                            page.get_by_test_id('candidate-inspect').click()
                        self.assertEqual(inspect_response.value.status, 200, inspect_response.value.text())
                        expect(page.get_by_test_id('candidate-result')).to_contain_text('revalidated')
                        self.assertEqual(trajectory_counts(), counts_before)
                        panel.scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-owned-candidate-real-en.png')
                        page.close()
                        signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
                        deadline = time.monotonic() + 120
                        while time.monotonic() < deadline:
                            if local_app.read_state().phase == 'stopped':
                                break
                            time.sleep(.2)
                        else:
                            self.fail('Owned supervisor did not stop before offline candidate reinspection')
                        with self.assertRaises(OSError):
                            socket.create_connection(('127.0.0.1', fixture_port), timeout=.2)
                        command = [sys.executable, '-m', 'aos.site_skill_form_recipe_candidate', 'inspect',
                                   '--database', str(database), '--profiles', str(source_directory / 'profiles'),
                                   '--pages', str(source_directory / 'site-knowledge'),
                                   '--owned-source-directory', str(source_directory),
                                   '--owned-source-manifest-sha256', bundle['manifest_sha256'],
                                   '--directory', str(candidate_directory), '--sha256', preview['candidate_sha256']]
                        inspected = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
                        self.assertEqual(inspected.returncode, 0, inspected.stderr)
                        self.assertIn(preview['candidate_sha256'], inspected.stdout)
                        raw_value = (source_directory / 'remote-form-value.txt').read_text()
                        self.assertNotIn(raw_value, inspected.stdout)
                        candidate_path = candidate_directory / (preview['candidate_sha256'] + '.json')
                        candidate = json.loads(candidate_path.read_text())
                        self.assertEqual(digest(candidate), preview['candidate_sha256'])
                        self.assertEqual(candidate['schema_version'], '1.1')
                        self.assertEqual(candidate['source_context'], {
                            'kind': 'owned_fixed_template_v1', 'manifest_sha256': bundle['manifest_sha256'],
                            'invocation_sha256': bundle['invocation_sha256']})
                        with (source_directory / 'remote-form-value.txt').open('ab') as stream:
                            stream.write(b'changed')
                        rejected = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
                        self.assertNotEqual(rejected.returncode, 0)
                        self.assertNotIn(raw_value, rejected.stdout + rejected.stderr)
                    if recipe:
                        expect(page.get_by_test_id('owned-skill-candidate')).to_have_count(0)
                        self.assertFalse(client.get('/api/tasks/owned-form-candidate/context').json()['available'])
                        self.assertEqual(audited['owned_form_invocation']['recipe_sha256'], bundle['recipe_sha256'])
                        operations = page.get_by_test_id('owned-recipe-steps').locator('li').evaluate_all(
                            '(items) => items.map(item => item.dataset.operation)')
                        self.assertEqual(operations, ['open_entry', 'fill_form', 'read_state_before',
                                                     'submit_form', 'read_receipt', 'read_state_after'])
                        report = client.get('/api/tasks/owned-form-invocation-audit').json()
                        self.assertEqual(report['mode'], 'owned_synthetic_form_recipe')
                        self.assertEqual(report['report']['recipe_sha256'], bundle['recipe_sha256'])
                        self.assertTrue(report['report']['executable_recipe_executed'])
                        self.assertFalse(report['report']['skill_validated'])
                        page.get_by_test_id('owned-form-recipe').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-managed-recipe-real-en.png')
                        source = session_directory / 'owned-form' / 'remote-form-skill-recipe.json'
                        with source.open('ab') as stream:
                            stream.write(b' ')
                        rejected = client.get('/api/tasks/owned-form-invocation-audit')
                        self.assertEqual(rejected.status_code, 200)
                        self.assertEqual(rejected.json(), {
                            'mode': 'owned_synthetic_form_recipe', 'available': False,
                            'status': 'unavailable', 'report': None, 'report_sha256': None})
                        expect(page.get_by_test_id('owned-invocation-audit-result')).to_have_count(0, timeout=15000)
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
    'AOS_AUTO_APPROVAL_REAL_TESTS', 'AOS_DESKTOP_TESTS', 'AOS_UI_TESTS')),
    'Requires explicit real Decider/Bonsai, Docker and UI automatic-approval acceptance')
class RealAutomaticApprovalTests(unittest.TestCase):
    def test_three_real_tasks_without_per_action_human_wait(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        root = Path(tempfile.mkdtemp(prefix='auto-approval-real-', dir=REPO_ROOT / 'data'))
        root.chmod(0o700)
        prewarm = os.environ.get('AOS_DECIDER_PREWARM_TESTS') == '1'
        arguments = ('--browser-tasks', '--desktop-browser', '--desktop-vision',
                     '--vision-engine', 'bonsai', '--reuse-decider') + (('--prewarm-decider',) if prewarm else ())
        measurements = []
        with task_server(root, 'decider', arguments) as (origin, token, client, server), sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(
                REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
            errors, approval_requests = [], []
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                page.on('request', lambda request: approval_requests.append(request.url)
                        if request.method == 'POST' and '/api/approvals/' in request.url else None)
                page.goto(origin + '/ui/')
                expect(page).to_have_title('AOS · Control center')
                page.get_by_label('Local session token', exact=True).fill(token)
                page.get_by_role('button', name='Sign in', exact=True).click()
                page.get_by_role('button', name='Tasks', exact=True).click()
                for kind, button, approval_count in (
                    ('hello', 'Start Hello task', 1),
                    ('browser_form', 'Start browser task', 2),
                    ('vision_canvas', 'Start vision task', 1),
                ):
                    page.locator('#task-kind').select_option(kind)
                    readiness_started = time.perf_counter()
                    if prewarm:
                        deadline = readiness_started + 60
                        while client.get('/api/tasks').json().get('decider_preparation') != {'enabled': True, 'state': 'ready'}:
                            self.assertLess(time.perf_counter(), deadline, 'CPU prewarm did not become ready')
                            page.wait_for_timeout(100)
                    readiness_wait = (time.perf_counter() - readiness_started) * 1000
                    checkbox = page.get_by_test_id('approve-all')
                    expect(checkbox).not_to_be_checked()
                    checkbox.check()
                    started = time.perf_counter()
                    with page.expect_response(lambda response: response.request.method == 'POST'
                                              and response.url == origin + '/api/tasks') as admitted:
                        page.get_by_role('button', name=button, exact=True).click()
                    self.assertEqual(admitted.value.status, 200)
                    job_id = admitted.value.json()['job_id']
                    expect(page.get_by_test_id('task-row')).to_have_count(len(measurements) + 1, timeout=10000)
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=120000)
                    ui_elapsed = (time.perf_counter() - started) * 1000
                    status = client.get('/api/tasks').json()
                    release_deadline = time.perf_counter() + 15
                    while status['reserved']:
                        self.assertLess(time.perf_counter(), release_deadline, 'Completed task did not release its reservation')
                        page.wait_for_timeout(50)
                        status = client.get('/api/tasks').json()
                    ready_elapsed = (time.perf_counter() - started) * 1000
                    job = next(item for item in status['jobs'] if item['job_id'] == job_id)
                    self.assertEqual(job['status'], 'succeeded')
                    self.assertEqual(job['real_model'], 1)
                    self.assertIsNone(status['auto_approval'])
                    self.assertFalse(status['reserved'])
                    expect(checkbox).not_to_be_checked()
                    trace = client.get('/api/runs/' + job['run_id']).json()
                    self.assertEqual([item['result'] for item in trace['verifications']], ['passed'] * approval_count)
                    self.assertTrue(all(item['status'] == 'ok' for item in trace['model_calls']))
                    with closing(sqlite3.connect(f'file:{root / "store.sqlite"}?mode=ro', uri=True)) as database:
                        approvals = database.execute('SELECT status FROM desktop_approvals WHERE job_id=?', (job_id,)).fetchall()
                        interventions = database.execute("SELECT actor,payload_json FROM human_interventions WHERE run_id=? AND kind='approve'", (job['run_id'],)).fetchall()
                    self.assertEqual(approvals, [('consumed',)] * approval_count)
                    self.assertEqual([actor for actor, payload in interventions], ['task_scoped_auto_approval'] * approval_count)
                    grants = [json.loads(payload) for actor, payload in interventions]
                    self.assertTrue(all(grant['job_id'] == job_id for grant in grants))
                    self.assertEqual(len({grant['grant_id'] for grant in grants}), 1)
                    self.assertEqual(approval_requests, [])
                    measurements.append({'kind': kind, 'job_id': job_id, 'run_id': job['run_id'],
                                         'cpu_prewarm_ready_wait_ms': round(readiness_wait, 3) if prewarm else None,
                                         'ui_start_to_success_ms': round(ui_elapsed, 3),
                                         'start_to_idle_ms': round(ready_elapsed, 3),
                                         'server_job_elapsed_ms': job['progress']['elapsed_ms'],
                                         'model_calls': [{key: call[key] for key in ('role', 'status', 'latency_ms')}
                                                         for call in trace['model_calls']],
                                         'consumed_approvals': approval_count, 'independent_verification': 'passed'})
                    page.screenshot(path='/tmp/aos-auto-approval-' + kind + '.png', full_page=True)
                self.assertEqual(errors, [])
                print(json.dumps({'evidence': str(root), 'real_models': True, 'cpu_prewarm': prewarm,
                                  'approval_mode': 'explicit_single_task_grant', 'measurements': measurements}), flush=True)
            finally:
                browser.close()

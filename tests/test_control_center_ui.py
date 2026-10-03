from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aos.contracts import REPO_ROOT


class ControlCenterStaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        return super().translate_path(path[3:] if path.startswith('/ui/') else path)


@unittest.skipUnless(os.environ.get('AOS_CONTROL_CENTER_UI_TESTS') == '1',
                     'Requires built UI and isolated CPU Chromium')
class ControlCenterUITests(unittest.TestCase):
    def test_control_center_shell_reads_status_and_navigates_without_effects(self):
        from playwright.sync_api import expect, sync_playwright

        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
            ControlCenterStaticHandler, directory=str(REPO_ROOT / 'ui/dist')))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        sampled_at = datetime.now(timezone.utc).isoformat()
        responses = {
            '/api/session': {'authenticated': True, 'local_auto_login': False},
            '/api/state': {
                'control': {'owner': 'AGENT', 'status': 'running', 'lease_id': 'fixture', 'generation': 1},
                'runtime': {'running': False, 'runtime_id': 'fixture-only', 'image_id': None, 'container_id': None}},
            '/api/overview': {'sampled_at': sampled_at, 'events': [], 'inputs': [],
                              'trajectory': {'available': True, 'runs': [], 'models': [], 'deployments': []}},
            '/api/resources': {'available': False, 'sampled_at': sampled_at},
            '/api/tasks': {'available': True, 'busy': False, 'approval': None, 'kinds': [],
                           'jobs': [], 'real_model': False, 'real_supervisor': False},
            '/api/retention': {'schema_version': '1.0', 'mode': 'managed_remote_metadata_retention_status',
                               'enabled': False, 'state': 'disabled', 'last_attempt_at': None,
                               'session_count': None, 'purged_count': None, 'failed_session_count': None,
                               'failed_consent_count': None, 'metadata_only': True, 'training_ready': False},
            '/api/web-applications': [],
            '/api/web-applications/capabilities': {},
            '/api/capability-checks': {'schema_version': '1.0', 'historical_only': True,
                                     'real_site_acceptance': False, 'approval_driver': 'test harness',
                                     'available': False, 'cases': [
                                         {'case': name, 'status': 'not_run', 'started_at': None,
                                          'counts': None, 'seconds': None}
                                         for name in ['contracts', 'ui', 'transport', 'real_tasks', 'real_mcp',
                                                      'real_takeover', 'real_reuse', 'real_learning']]},
            '/api/scientist/jobs': {'configured': False, 'joint_runtime_admitted': False, 'jobs': []},
        }
        requests = []
        unexpected = []
        errors = []
        console = []
        fail_tasks = False
        fail_resources = False
        malformed_tasks = False
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'),
                    args=['--disable-gpu'])
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: console.append(message.text)
                            if message.type in ('warning', 'error') else None)

                    def read_fixture(route):
                        path = urlsplit(route.request.url).path
                        requests.append((route.request.method, path))
                        if route.request.method != 'GET' or path not in responses:
                            unexpected.append((route.request.method, path))
                            route.fulfill(status=400, json={'detail': 'Unexpected fixture request'})
                        elif fail_resources and path == '/api/resources':
                            route.fulfill(status=503, json={'detail': 'Synthetic optional telemetry failure'})
                        elif malformed_tasks and path == '/api/tasks':
                            route.fulfill(json={'available': True, 'busy': False})
                        elif fail_tasks and path == '/api/tasks':
                            route.fulfill(status=500, json={'detail': 'Explicit synthetic polling failure'})
                        else:
                            route.fulfill(json=responses[path])

                    page.route('**/api/**', read_fixture)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Control center')
                    self.assertEqual(page.url, origin + '/ui/')
                    expect(page.get_by_role('heading', name='Control center', exact=True)).to_be_visible()
                    expect(page.locator('vite-error-overlay')).to_have_count(0)
                    runtime = page.get_by_test_id('development-runtime')
                    expect(runtime).to_contain_text('Fresh observation')
                    expect(runtime).to_contain_text('No active work reported')
                    expect(runtime).to_contain_text('Desktop stopped')
                    journal = page.get_by_test_id('development-journal')
                    expect(journal).to_be_visible()
                    expect(journal).to_contain_text('49')
                    expect(journal).to_contain_text('CPU')
                    expect(page.get_by_test_id('development-history')).not_to_be_visible()
                    page.screenshot(path='/tmp/aos-control-center-shell-desktop.png', full_page=False)
                    page.get_by_role('button', name='Open release checklist', exact=True).click()
                    expect(page.get_by_test_id('development-acceptance')).to_be_visible()
                    expect(journal).not_to_be_visible()
                    page.get_by_role('button', name='Evidence & implementation history', exact=True).click()
                    expect(page.get_by_test_id('development-history')).to_be_visible()
                    page.get_by_role('button', name='Overview', exact=True).click()
                    expect(journal).to_be_visible()
                    initial_reads = requests.count(('GET', '/api/tasks'))
                    page.get_by_role('button', name='Refresh status', exact=True).click()
                    expect(runtime).to_contain_text('Fresh observation')
                    page.wait_for_function('document.querySelector("[data-testid=development-runtime]").getAttribute("aria-busy") === "false"')
                    self.assertGreater(requests.count(('GET', '/api/tasks')), initial_reads)
                    self.assertEqual(console, [])
                    responses['/api/tasks'].update(busy=True, reserved=True, jobs=[{
                        'job_id': 'job-fixture', 'kind': 'hello', 'status': 'running',
                        'progress': {'phase': 'verifying', 'elapsed_ms': 150}}])
                    page.get_by_role('button', name='Refresh status', exact=True).click()
                    expect(runtime).to_contain_text('Work in progress')
                    expect(runtime).not_to_contain_text('Resource reservation')
                    expect(runtime).to_contain_text('verifying')
                    responses['/api/tasks'].update(busy=False, reserved=False, jobs=[])
                    malformed_tasks = True
                    page.get_by_role('button', name='Refresh status', exact=True).click()
                    expect(runtime).to_contain_text('Status unavailable')
                    expect(runtime).not_to_contain_text('No pending approval')
                    page.get_by_role('button', name='Evidence & implementation history', exact=True).click()
                    history = page.get_by_test_id('development-history')
                    expect(history.get_by_test_id('development-history-observation')).to_contain_text('Status unavailable')
                    expect(history.get_by_test_id('development-history-observation')).to_contain_text('not proof of live work')
                    expect(history.get_by_role('heading', name='Last observed task', exact=True)).to_be_visible()
                    expect(history).not_to_contain_text('Live evidence from this session')
                    page.get_by_role('button', name='Overview', exact=True).click()
                    malformed_tasks = False
                    page.get_by_role('button', name='Refresh status', exact=True).click()
                    expect(runtime).to_contain_text('Fresh observation')
                    fail_resources = True
                    with page.expect_response(lambda response: response.url.endswith('/api/resources')
                                              and response.status == 503):
                        page.get_by_role('button', name='Refresh status', exact=True).click()
                    expect(runtime).to_contain_text('No active work reported')
                    page.wait_for_function('document.querySelector("[data-testid=development-runtime]").getAttribute("aria-busy") === "false"')
                    expect(runtime).to_contain_text('Fresh observation')
                    self.assertTrue(all('503' in message and 'Failed to load resource' in message for message in console))
                    console.clear()
                    fail_resources = False
                    fail_tasks = True
                    page.get_by_role('button', name='Refresh status', exact=True).click()
                    expect(runtime).to_contain_text('Status unavailable')
                    expect(runtime).to_contain_text('missing data does not mean idle')
                    expect(runtime).not_to_contain_text('No active work reported')
                    self.assertTrue(all('500' in message and 'Failed to load resource' in message for message in console))
                    console.clear()
                    fail_tasks = False
                    page.get_by_role('button', name='Refresh status', exact=True).click()
                    expect(runtime).to_contain_text('Fresh observation')
                    page.get_by_role('button', name='Open Scientist Lab', exact=True).click()
                    lab = page.get_by_test_id('scientist-lab')
                    expect(lab).to_have_attribute('open', '')
                    expect(lab).to_contain_text('Scientist connection is not configured')
                    expect(lab.get_by_test_id('scientist-proposal-form')).to_have_count(0)
                    page.get_by_role('button', name='Development', exact=True).click()
                    expect(page.get_by_role('heading', name='Control center', exact=True)).to_be_visible()
                    page.get_by_test_id('development-runtime').get_by_role('button', name='Open tasks', exact=True).click()
                    expect(page.get_by_role('heading', name='Tasks', exact=True, level=1)).to_be_visible()
                    page.get_by_role('button', name='Development', exact=True).click()
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_role('heading', name='Kontrol merkezi', exact=True)).to_be_visible()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    expect(journal).to_be_visible()
                    page.screenshot(path='/tmp/aos-control-center-shell-mobile-tr.png', full_page=True)
                    page.get_by_role('button', name='Sürüm kontrol listesi', exact=True).click()
                    snapshot = json.loads((REPO_ROOT / 'docs/release_acceptance.json').read_text())
                    expect(page.get_by_test_id('development-acceptance-date').locator('time')).to_have_attribute(
                        'datetime', snapshot['observed_at'])
                    self.assertEqual(errors, [])
                    self.assertEqual(console, [])
                    self.assertEqual(unexpected, [])
                    self.assertTrue(all(method == 'GET' for method, _ in requests))
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires real Docker and built UI; model results are synthetic fixtures')
class AutoApprovalUITests(unittest.TestCase):
    def test_default_grant_runs_browser_actions_and_can_be_turned_off_for_one_task(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='auto-approval-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks', '--desktop-browser')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, writes, connections = [], [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('warning', 'error') else None)
                    page.on('request', lambda request: writes.append((request.url, request.post_data_json)) if request.method == 'POST' else None)
                    page.on('websocket', lambda connection: connections.append(connection))
                    page.goto(origin + '/ui/')
                    expect(page).to_have_title('AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.locator('.computer-panel canvas')).to_have_count(1)
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    option = page.get_by_role('checkbox', name='Approve all — this task only', exact=True)
                    expect(option).to_be_checked()
                    expect(option).to_be_enabled()
                    page.get_by_label('Task type', exact=True).select_option('hello')
                    expect(option).to_be_checked()
                    page.get_by_label('Task type', exact=True).select_option('browser_form')
                    expect(option).to_be_checked()
                    count = len(writes)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_role('checkbox', name='Tümünü onayla — yalnız bu görev', exact=True)).to_be_checked()
                    page.get_by_role('button', name='English', exact=True).click()
                    expect(option).to_be_checked()
                    self.assertEqual(len(writes), count)
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertEqual(page.evaluate('Object.keys(localStorage)'), ['aos.ui.language'])
                    page.get_by_role('button', name='Start browser task', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=45000)
                    expect(option).to_be_checked()
                    expect(page.get_by_test_id('auto-approval-active')).to_have_count(0)
                    expect(page.get_by_test_id('approval')).to_have_count(0)
                    status = client.get('/api/tasks').json()
                    self.assertIsNone(status['auto_approval'])
                    trace = client.get('/api/runs/' + status['jobs'][0]['run_id']).json()
                    self.assertEqual([action['tool'] for action in trace['actions']], ['browser.fill', 'browser.verify', 'browser.submit', 'browser.verify'])
                    self.assertEqual([verification['result'] for verification in trace['verifications']], ['passed', 'passed'])
                    starts = [body for url, body in writes if url == origin + '/api/tasks']
                    self.assertEqual(len(starts), 1)
                    self.assertIs(starts[0]['approve_all'], True)
                    self.assertFalse(any('/api/approvals/' in url for url, body in writes))
                    self.assertEqual(len(connections), 1)
                    page.screenshot(path='/tmp/aos-auto-approval-succeeded.png', full_page=True)
                    option.uncheck()
                    expect(option).not_to_be_checked()
                    page.get_by_role('button', name='Start browser task', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.fill', timeout=30000)
                    starts = [body for url, body in writes if url == origin + '/api/tasks']
                    self.assertNotIn('approve_all', starts[1])
                    expect(option).to_be_checked()
                    expect(option).to_be_disabled()
                    old_approval = client.get('/api/tasks').json()['approval']['approval_id']
                    page.get_by_role('button', name='Pause', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_have_count(0)
                    expect(option).not_to_be_checked()
                    page.get_by_role('button', name='Resume', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.fill', timeout=30000)
                    self.assertNotEqual(client.get('/api/tasks').json()['approval']['approval_id'], old_approval)
                    self.assertIsNone(client.get('/api/tasks').json()['auto_approval'])
                    page.get_by_role('button', name='Reject', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_have_count(0)
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_response_fixtures_show_bound_grant_and_fail_closed_for_old_backend(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='auto-approval-label-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                status = client.get('/api/tasks').json()
                status.pop('supports_approve_all', None)
                status.pop('auto_approval', None)
                status.pop('decider_preparation', None)
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, writes = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('warning', 'error') else None)
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
                    page.route('**/api/tasks', lambda route: route.fulfill(json=status) if route.request.method == 'GET' else route.continue_())
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    option = page.get_by_role('checkbox', name='Approve all — this task only', exact=True)
                    expect(option).to_be_disabled()
                    expect(option).not_to_be_checked()
                    expect(page.get_by_test_id('auto-approval-option')).to_contain_text('This backend does not support approve all')
                    preparation = page.get_by_test_id('decider-preparation')
                    expect(preparation).to_have_count(0)
                    status['decider_preparation'] = {'enabled': False, 'state': 'inactive'}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(preparation).to_have_count(0)
                    for phase, label in (('preparing', 'Preparing'), ('ready', 'Ready'), ('inactive', 'Inactive')):
                        status['decider_preparation'] = {'enabled': True, 'state': phase}
                        expect(preparation).to_contain_text('System-1 CPU preparation: ' + label, timeout=8000)
                    expect(preparation).to_contain_text('does not mean GPU residency')
                    expect(preparation).to_contain_text('full integrity checks and inference still run')
                    expect(preparation).to_contain_text('expiry does not start or restart tasks')
                    status['decider_preparation'] = {'enabled': True, 'state': 'ready_gpu'}
                    expect(preparation).to_contain_text('System-1 timed GPU residency: Ready', timeout=8000)
                    expect(preparation).to_contain_text('the next task rechecks its pins')
                    expect(preparation).to_contain_text('no task or approval starts automatically')
                    status['decider_preparation'] = {'enabled': True, 'state': 'inactive'}
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    status.update(supports_approve_all=True, auto_approval=None)
                    expect(option).to_be_enabled(timeout=8000)
                    expect(option).to_be_checked()
                    page.reload()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(option).to_be_checked()
                    status.update(busy=True, reserved=True, auto_approval={'job_id': 'synthetic-ui-grant', 'kind': 'hello'})
                    active = page.get_by_test_id('auto-approval-active')
                    expect(active).to_contain_text('Automatic approval active for this task', timeout=8000)
                    expect(active).to_contain_text('synthetic-ui-grant')
                    expect(option).to_be_disabled()
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(active).to_contain_text('Bu görev için otomatik onay etkin')
                    expect(preparation).to_contain_text('System-1 CPU hazırlığı: Etkin değil')
                    expect(preparation).to_contain_text('GPU belleğine yükleme değildir')
                    status['decider_preparation'] = {'enabled': True, 'state': 'ready_gpu'}
                    expect(preparation).to_contain_text('System-1 süreli GPU bekletme: Hazır', timeout=8000)
                    expect(preparation).to_contain_text('sonraki görevde pinler yeniden denetlenir')
                    status['decider_preparation'] = {'enabled': True, 'state': 'inactive'}
                    expect(page.get_by_test_id('auto-approval-option')).to_contain_text('Güvenlik politikası ve bağımsız doğrulama değişmez')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-auto-approval-mobile.png', full_page=True)
                    status.update(busy=False, reserved=True, paused=True, auto_approval=None)
                    expect(active).to_have_count(0, timeout=8000)
                    expect(page.get_by_role('checkbox', name='Tümünü onayla — yalnız bu görev', exact=True)).to_be_disabled()
                    status.pop('decider_preparation')
                    expect(preparation).to_have_count(0, timeout=8000)
                    self.assertEqual(writes, [origin + '/api/login'])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

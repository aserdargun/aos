import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
    'AOS_DESKTOP_TESTS', 'AOS_DESKTOP_MCP_TESTS', 'AOS_UI_TESTS')),
    'Requires a real owned Ubuntu/Chromium/MCP desktop and built UI')
class StagingWorkflowUITests(unittest.TestCase):
    def test_visible_four_step_workflow_requires_four_separate_approvals(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='staging-workflow-ui-', dir=REPO_ROOT / 'data') as directory:
            arguments = ('--browser-tasks', '--desktop-browser', '--desktop-staging-mcp-manifest',
                         str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'))
            with task_server(Path(directory), 'fixture', arguments) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.get_by_test_id('first-use-capability-browser_staging_workflow')).to_contain_text('Enabled')
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    status = client.get('/api/tasks').json()
                    self.assertIn('browser_staging_workflow', status['kinds'])
                    self.assertEqual(status['staging_transport'], 'playwright_mcp')
                    expect(page.get_by_test_id('staging-mcp-transport')).to_contain_text('visible Ubuntu Chromium')
                    page.get_by_label('Task type', exact=True).select_option('browser_staging_workflow')
                    expect(page.get_by_test_id('approve-all')).not_to_be_checked()
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_role('button', name='Uygulama akışını başlat', exact=True)).to_be_enabled()
                    page.get_by_role('button', name='English', exact=True).click()
                    page.get_by_role('button', name='Start application workflow', exact=True).click()
                    for ordinal, tool in enumerate(('browser.staging.open', 'browser.staging.follow',
                                                    'browser.staging.fill', 'browser.staging.submit'), 1):
                        approval = page.get_by_test_id('approval')
                        expect(approval).to_contain_text(tool, timeout=45000)
                        expect(page.get_by_test_id('visible-task-status')).to_contain_text(f'{ordinal} / 4')
                        page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    expect(page.get_by_test_id('visible-task-status')).to_contain_text(
                        'receipt and single POST were independently verified')
                    job = client.get('/api/tasks').json()['jobs'][0]
                    self.assertEqual(job['kind'], 'browser_staging_workflow')
                    trace = client.get('/api/runs/' + job['run_id']).json()
                    self.assertEqual([item['result'] for item in trace['verifications']], ['passed'] * 4)
                    self.assertEqual(sum(item['tool'] == 'browser.staging.submit' and item['status'] == 'ok'
                                         for item in trace['actions']), 1)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

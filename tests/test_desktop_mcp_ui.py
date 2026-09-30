import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(all(os.environ.get(name) == '1' for name in (
    'AOS_DESKTOP_TESTS', 'AOS_DESKTOP_MCP_TESTS', 'AOS_UI_TESTS')),
    'Requires real Ubuntu MCP and built UI; decision model is a synthetic fixture')
class DesktopMCPUITests(unittest.TestCase):
    def test_navigation_only_mcp_keeps_form_on_cdp_and_two_manual_approvals(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='desktop-navigation-mcp-ui-', dir=REPO_ROOT / 'data') as temporary:
            arguments = ('--browser-tasks', '--desktop-browser', '--desktop-navigation-mcp-manifest',
                         str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'))
            with task_server(Path(temporary), 'fixture', arguments) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    status = client.get('/api/tasks').json()
                    self.assertEqual(status['browser_transport'], 'cdp')
                    self.assertEqual(status['navigation_transport'], 'playwright_mcp')
                    self.assertIn('browser_local_navigation', status['kinds'])
                    expect(page.get_by_test_id('navigation-mcp-transport')).to_contain_text('local form uses CDP')
                    expect(page.get_by_test_id('browser-mcp-transport')).to_have_count(0)
                    expect(page.get_by_test_id('task-capability-browser_local_navigation')).to_contain_text('Enabled')
                    page.get_by_label('Task type', exact=True).select_option('browser_local_navigation')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    page.get_by_role('button', name='Start navigation task', exact=True).click()
                    for tool in ('browser.fixture.open', 'browser.fixture.follow'):
                        expect(page.get_by_test_id('approval')).to_contain_text(tool, timeout=45000)
                        page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    navigation = client.get('/api/tasks').json()['jobs'][0]
                    self.assertEqual(navigation['kind'], 'browser_local_navigation')
                    self.assertEqual([entry['result'] for entry in client.get('/api/runs/' + navigation['run_id']).json()['verifications']],
                                     ['passed', 'passed'])
                    page.get_by_label('Task type', exact=True).select_option('browser_form')
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Start browser task', exact=True).click()
                    for tool in ('browser.fill', 'browser.submit'):
                        expect(page.get_by_test_id('approval')).to_contain_text(tool, timeout=45000)
                        page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    form = client.get('/api/tasks').json()['jobs'][0]
                    self.assertEqual(form['kind'], 'browser_form')
                    self.assertEqual([entry['result'] for entry in client.get('/api/runs/' + form['run_id']).json()['verifications']],
                                     ['passed', 'passed'])
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_visible_local_navigation_requires_two_manual_approvals(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='desktop-mcp-navigation-ui-', dir=REPO_ROOT / 'data') as temporary:
            arguments = ('--browser-tasks', '--desktop-browser', '--desktop-mcp-manifest',
                         str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'))
            with task_server(Path(temporary), 'fixture', arguments) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('warning', 'error') else None)
                    page.goto(origin + '/ui/')
                    expect(page).to_have_title('AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('task-capability-browser_local_navigation')).to_contain_text('Enabled')
                    expect(page.get_by_test_id('navigation-capability-note')).to_contain_text('two-page task')
                    page.get_by_label('Task type', exact=True).select_option('browser_local_navigation')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_role('button', name='Gezinme görevini başlat', exact=True)).to_be_enabled()
                    page.get_by_role('button', name='English', exact=True).click()
                    expect(page.get_by_role('button', name='Start navigation task', exact=True)).to_be_enabled()
                    expect(page.get_by_test_id('approve-all')).not_to_be_checked()
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.get_by_test_id('auto-approval-option')).to_contain_text('each transition requires its own approval')
                    page.get_by_role('button', name='Start navigation task', exact=True).click()
                    for tool in ('browser.fixture.open', 'browser.fixture.follow'):
                        expect(page.get_by_test_id('approval')).to_contain_text(tool, timeout=45000)
                        page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    expect(page.get_by_test_id('visible-task-status')).to_contain_text('Details page was independently verified')
                    status = client.get('/api/tasks').json()
                    self.assertEqual(status['jobs'][0]['kind'], 'browser_local_navigation')
                    self.assertIsNone(status['auto_approval'])
                    trace = client.get('/api/runs/' + status['jobs'][0]['run_id']).json()
                    self.assertEqual([record['result'] for record in trace['verifications']], ['passed', 'passed'])
                    page.locator('.computer-panel canvas').screenshot(path='/tmp/aos-ubuntu-mcp-navigation-desktop.png')
                    page.screenshot(path='/tmp/aos-ubuntu-mcp-navigation-console.png', full_page=True)
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_visible_mcp_form_two_approvals_and_same_novnc_canvas(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='desktop-mcp-ui-', dir=REPO_ROOT / 'data') as temporary:
            arguments = ('--browser-tasks', '--desktop-browser', '--desktop-mcp-manifest',
                         str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'))
            with task_server(Path(temporary), 'fixture', arguments) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, connections = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('warning', 'error') else None)
                    page.on('websocket', lambda connection: connections.append(connection))
                    page.goto(origin + '/ui/')
                    expect(page).to_have_title('AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.locator('.computer-panel canvas')).to_have_count(1)
                    expect(page.locator('.computer-panel [role="status"]')).to_contain_text('1280 × 800')
                    initial = page.locator('.computer-panel canvas').evaluate('(canvas) => canvas.toDataURL()')
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('browser-mcp-transport')).to_contain_text('Playwright MCP on Ubuntu Chromium')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_test_id('browser-mcp-transport')).to_contain_text('Ubuntu Chromium üzerinde Playwright MCP')
                    page.get_by_role('button', name='English', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('browser_form')
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Start browser task', exact=True).click()
                    for tool in ('browser.fill', 'browser.submit'):
                        expect(page.get_by_test_id('approval')).to_contain_text(tool, timeout=45000)
                        page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    page.wait_for_function('(before) => document.querySelector(".computer-panel canvas").toDataURL() !== before', arg=initial)
                    status = client.get('/api/tasks').json()
                    self.assertEqual(status['browser_transport'], 'playwright_mcp')
                    self.assertFalse(status['real_model'])
                    trace = client.get('/api/runs/' + status['jobs'][0]['run_id']).json()
                    self.assertEqual([record['result'] for record in trace['verifications']], ['passed', 'passed'])
                    self.assertEqual(len(connections), 1)
                    page.locator('.computer-panel canvas').screenshot(path='/tmp/aos-ubuntu-mcp-desktop.png')
                    page.screenshot(path='/tmp/aos-ubuntu-mcp-console.png', full_page=True)
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

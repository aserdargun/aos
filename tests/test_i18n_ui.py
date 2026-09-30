import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires real Docker and built bilingual UI')
class LanguageUITests(unittest.TestCase):
    def test_english_default_turkish_switch_preserves_approval_and_desktop(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='language-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            with task_server(root, 'fixture', ('--browser-tasks', '--desktop-browser', '--desktop-vision', '--vision-engine', 'fixture')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, connections, writes = [], [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('websocket', lambda connection: connections.append(connection))
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
                    page.goto(origin + '/ui/')
                    expect(page.locator('html')).to_have_attribute('lang', 'en')
                    expect(page).to_have_title('AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.locator('.computer-panel canvas')).to_have_count(1)
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Start browser task', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.fill', timeout=45000)
                    before = client.get('/api/tasks').json()['approval']
                    count = len(writes)
                    page.evaluate("window.languageCanvas = document.querySelector('.remote-screen canvas')")
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.locator('html')).to_have_attribute('lang', 'tr')
                    expect(page.get_by_role('button', name='Onayla', exact=True)).to_be_visible()
                    self.assertEqual(client.get('/api/tasks').json()['approval'], before)
                    self.assertEqual(len(writes), count)
                    self.assertEqual(len(connections), 1)
                    self.assertTrue(page.evaluate("window.languageCanvas === document.querySelector('.remote-screen canvas')"))
                    page.get_by_role('button', name='English', exact=True).click()
                    expect(page.get_by_role('button', name='Approve', exact=True)).to_be_visible()
                    page.screenshot(path='/tmp/aos-language-english-approval.png', full_page=True)
                    self.assertEqual(client.get('/api/tasks').json()['approval'], before)
                    page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.submit', timeout=30000)
                    page.get_by_role('button', name='Approve', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    trace = client.get('/api/runs/' + before['action']['run_id']).json()
                    self.assertEqual([item['result'] for item in trace['verifications']], ['passed', 'passed'])
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    page.reload()
                    expect(page.locator('html')).to_have_attribute('lang', 'tr')
                    expect(page.get_by_role('button', name='Görevler', exact=True)).to_be_visible()
                    self.assertEqual(page.evaluate('Object.keys(localStorage)'), ['aos.ui.language'])
                    self.assertNotIn(token, page.evaluate('JSON.stringify(localStorage)'))
                    page.get_by_role('button', name='English', exact=True).click()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.locator('.computer-panel .panel-heading')).to_contain_text('Live', timeout=15000)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    title = page.locator('.page-heading h1').bounding_box()
                    refresh = page.get_by_role('button', name='Refresh', exact=True).bounding_box()
                    self.assertTrue(title['y'] + title['height'] <= refresh['y'] or title['x'] + title['width'] <= refresh['x'])
                    page.screenshot(path='/tmp/aos-language-english-mobile.png', full_page=True)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_invalid_preference_defaults_to_english_and_storage_failure_still_switches(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='language-fallback-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'unsupported')")
                    page.goto(origin + '/ui/')
                    expect(page.locator('html')).to_have_attribute('lang', 'en')
                    page.evaluate("() => {Storage.prototype.setItem = () => {throw new Error('synthetic storage denial')};}")
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.locator('html')).to_have_attribute('lang', 'tr')
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    page.get_by_role('button', name='English', exact=True).click()
                    expect(page.get_by_label('Local session token', exact=True)).to_be_visible()
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                finally:
                    browser.close()

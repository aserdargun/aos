import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker and built Chromium UI; no inference is performed')
class LocalUILoginUITests(unittest.TestCase):
    def test_local_entry_signout_and_fresh_page_preserve_paused_control(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
        with tempfile.TemporaryDirectory(prefix='local-ui-login-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            with task_server(root, 'fixture', ('--local-ui-auto-login',)) as (
                    origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(browser_path))
                errors, writes = [], []
                try:
                    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                    context.add_init_script("localStorage.setItem('aos.ui.language', 'en')")
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    context.on('request', lambda request: writes.append((request.url, request.post_data_json)) if request.method == 'POST' else None)
                    page.goto(origin + '/ui/')
                    expect(page).to_have_title('AOS · Control center')
                    expect(page.get_by_test_id('development-status')).to_be_visible()
                    expect(page.get_by_test_id('owner')).to_have_text('AGENT')
                    expect(page.locator('input[name="token"]')).to_have_count(0)
                    self.assertEqual(writes, [(origin + '/api/login/local', {})])
                    cookie = next(cookie for cookie in context.cookies() if cookie['name'] == 'aos_session')
                    self.assertTrue(cookie['httpOnly'])
                    self.assertEqual(cookie['sameSite'], 'Strict')
                    self.assertNotIn('aos_session', page.evaluate('document.cookie'))
                    self.assertNotIn(token, page.url)
                    self.assertNotIn(token, page.evaluate('JSON.stringify(localStorage)'))
                    page.screenshot(path='/tmp/aos-local-ui-entry-en.png', full_page=False)
                    page.get_by_role('button', name='Sign out', exact=True).click()
                    expect(page.get_by_role('button', name='Open local session', exact=True)).to_be_visible()
                    expect(page.locator('input[name="token"]')).to_have_count(0)
                    page.wait_for_timeout(500)
                    self.assertEqual(writes, [(origin + '/api/login/local', {}), (origin + '/api/logout', {})])
                    client.post('/api/login', json={'token': token}).raise_for_status()
                    self.assertEqual(client.get('/api/state').json()['control']['owner'], 'PAUSED')
                    page.get_by_role('button', name='Open local session', exact=True).click()
                    expect(page.get_by_test_id('development-status')).to_be_visible()
                    expect(page.get_by_test_id('owner')).to_have_text('PAUSED')
                    page.get_by_role('button', name='Sign out', exact=True).click()
                    expect(page.get_by_role('button', name='Open local session', exact=True)).to_be_visible()
                    reopened = context.new_page()
                    reopened.goto(origin + '/ui/')
                    expect(reopened.get_by_test_id('development-status')).to_be_visible()
                    expect(reopened.get_by_test_id('owner')).to_have_text('PAUSED')
                    reopened.get_by_role('button', name='Türkçe', exact=True).click()
                    reopened.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(reopened.evaluate('document.documentElement.scrollWidth'), 390)
                    reopened.screenshot(path='/tmp/aos-local-ui-entry-tr-mobile.png', full_page=False)
                    self.assertEqual([url for url, body in writes], [origin + '/api/login/local', origin + '/api/logout',
                        origin + '/api/login/local', origin + '/api/logout', origin + '/api/login/local'])
                    self.assertTrue(all(body == {} for url, body in writes))
                    client.post('/api/login', json={'token': token}).raise_for_status()
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    with sqlite3.connect('file:' + str(root / 'store.sqlite') + '?mode=ro', uri=True) as connection:
                        for table in ('model_calls', 'actions'):
                            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                finally:
                    browser.close()

    def test_failed_local_entry_keeps_explicit_retry_without_token_fallback(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
        with tempfile.TemporaryDirectory(prefix='local-ui-login-error-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--local-ui-auto-login',)) as (
                    origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(browser_path))
                try:
                    page = browser.new_page()
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'en')")
                    attempts = []
                    page.route('**/api/login/local', lambda route: (attempts.append(route.request.post_data_json),
                        route.fulfill(status=503, json={'detail': 'Local session unavailable'})))
                    page.goto(origin + '/ui/')
                    retry = page.get_by_role('button', name='Open local session', exact=True)
                    expect(retry).to_be_visible()
                    expect(page.get_by_role('alert')).to_contain_text('Request rejected (503)')
                    expect(page.locator('input[name="token"]')).to_have_count(0)
                    page.wait_for_timeout(500)
                    self.assertEqual(attempts, [{}])
                    page.unroute('**/api/login/local')
                    retry.click()
                    expect(page.get_by_test_id('development-status')).to_be_visible()
                    expect(page.get_by_test_id('owner')).to_have_text('AGENT')
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

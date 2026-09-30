import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import httpx

from aos.contracts import REPO_ROOT
from aos.desktop import DOCKER


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires an isolated Docker desktop and built UI')
class DesktopPointerUITests(unittest.TestCase):
    def test_owned_x11_pointer_is_shown_on_live_desktop(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='desktop-pointer-ui-', dir=REPO_ROOT / 'data') as temporary:
            with task_server(Path(temporary), 'fixture') as (origin, token, client, _server), sync_playwright() as playwright:
                self.assertEqual(httpx.get(origin + '/api/desktop/pointer').status_code, 401)
                state = client.get('/api/state').json()
                self.assertTrue(state['runtime']['pointer_tracking'])
                container_id = state['runtime']['container_id']
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.get_by_test_id('desktop-pointer-status')).to_contain_text('Live pointer position:')
                    subprocess.run([*DOCKER, 'exec', container_id, '/usr/bin/xdotool',
                                    'mousemove', '--sync', '240', '360'], check=True, timeout=10,
                                   stdout=subprocess.DEVNULL)
                    expect(page.get_by_test_id('desktop-pointer-status')).to_contain_text(
                        '240, 360 / 1280 × 800', timeout=10000)
                    expect(page.get_by_test_id('desktop-pointer-marker')).to_be_visible()
                    first_marker = page.get_by_test_id('desktop-pointer-marker').bounding_box()
                    subprocess.run([*DOCKER, 'exec', container_id, '/usr/bin/xdotool',
                                    'mousemove', '--sync', '900', '100'], check=True, timeout=10,
                                   stdout=subprocess.DEVNULL)
                    expect(page.get_by_test_id('desktop-pointer-status')).to_contain_text(
                        '900, 100 / 1280 × 800', timeout=10000)
                    moved_marker = page.get_by_test_id('desktop-pointer-marker').bounding_box()
                    self.assertGreater(moved_marker['x'], first_marker['x'])
                    self.assertLess(moved_marker['y'], first_marker['y'])
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_test_id('desktop-pointer-status')).to_contain_text(
                        'Canlı imleç konumu: 900, 100')
                    self.assertEqual(client.get('/api/desktop/pointer').json()['available'], True)
                finally:
                    browser.close()

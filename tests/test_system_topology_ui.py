from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from aos.contracts import REPO_ROOT


class TopologyAssetHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        return super().translate_path(path[3:] if path.startswith('/ui/') else path)

    def log_message(self, *arguments):
        pass


@unittest.skipUnless(os.environ.get('AOS_TOPOLOGY_UI_TESTS') == '1',
                     'Opt-in built UI and synthetic API Chromium navigation')
class SystemTopologyUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        if not (REPO_ROOT / 'ui/dist/index.html').is_file():
            raise AssertionError('Build the isolated UI before topology acceptance')
        cls.evidence = Path(tempfile.mkdtemp(prefix='topology-ui-', dir=REPO_ROOT / 'data'))
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0),
            partial(TopologyAssetHandler, directory=str(REPO_ROOT / 'ui/dist')))
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        cls.addClassCleanup(cls.close_server)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        executable = os.environ.get('AOS_TEST_CHROMIUM_PATH', str(REPO_ROOT /
            'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, args=['--disable-gpu'])
        cls.addClassCleanup(cls.browser.close)
        print('Browser plugin not available; isolated built main UI, synthetic APIs, GPU-disabled Chromium:', cls.evidence)

    @classmethod
    def close_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.addCleanup(self.page.close)
        self.errors = []
        self.writes = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('console', lambda message: self.errors.append(message.text)
            if message.type in ('error', 'warning') else None)
        self.page.route('**/api/**', self.endpoint)
        self.page.route('**/favicon.ico', lambda route: route.fulfill(status=204))

    def endpoint(self, route):
        if route.request.method != 'GET':
            self.writes.append(route.request.url)
            route.fulfill(status=403, json={'error': 'Synthetic topology test denies effects'})
            return
        payloads = {
            '/api/session': {'authenticated': True},
            '/api/state': {'control': {'owner': 'PAUSED', 'status': 'running',
                'lease_id': 'synthetic-lease', 'generation': 1},
                'runtime': {'running': False, 'runtime_id': 'synthetic-topology-runtime',
                            'image_id': None, 'container_id': None}},
            '/api/tasks': {'available': False, 'busy': False, 'approval': None, 'jobs': []},
            '/api/overview': {'sampled_at': '2026-10-03T00:00:00Z', 'events': [], 'inputs': [],
                              'trajectory': {'available': False}},
            '/api/resources': {'available': False, 'sampled_at': '2026-10-03T00:00:00Z'},
            '/api/web-applications': [],
            '/api/scientist/jobs': {'configured': False, 'jobs': []},
        }
        route.fulfill(json=payloads.get(urlsplit(route.request.url).path, {}))

    def open_panel(self):
        from playwright.sync_api import expect

        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/ui/')
        expect(self.page.get_by_role('button', name='Development', exact=True)).to_have_attribute('aria-current', 'page')
        self.page.get_by_role('button', name='System & topology', exact=True).click()
        expect(self.page.get_by_test_id('system-topology')).to_be_visible()

    def check_read_only_health(self):
        self.assertEqual(self.writes, [])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.page.locator('vite-error-overlay').count(), 0)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))

    def test_english_main_navigation_architecture_and_scientist_link_are_read_only(self):
        from playwright.sync_api import expect

        self.open_panel()
        expect(self.page).to_have_title('AOS · Control center')
        expect(self.page.locator('.topology-flow > li')).to_have_count(7)
        for text in ('not live telemetry', 'System 1 · Decider', 'System 2 · Bonsai',
                     'Playwright MCP', 'single GPU allocation authority',
                     'simultaneous residency is not required', 'General learning quality remains unproven'):
            expect(self.page.get_by_test_id('system-topology')).to_contain_text(text)
        self.page.screenshot(path=str(self.evidence / 'en-desktop.png'), full_page=True)
        self.check_read_only_health()
        self.page.get_by_role('button', name='Open Scientist', exact=True).click()
        expect(self.page.get_by_test_id('scientist-lab')).to_be_visible()
        self.page.get_by_role('button', name='System & topology', exact=True).click()
        self.page.get_by_role('button', name='View development evidence', exact=True).click()
        expect(self.page.get_by_role('button', name='Development', exact=True)).to_have_attribute('aria-current', 'page')
        self.check_read_only_health()

    def test_turkish_mobile_diagram_and_limits_are_visible(self):
        from playwright.sync_api import expect

        self.open_panel()
        self.page.get_by_role('button', name='Türkçe', exact=True).click()
        self.page.set_viewport_size({'width': 390, 'height': 844})
        expect(self.page).to_have_title('AOS · Kontrol merkezi')
        expect(self.page.get_by_role('heading', name='Sistem nasıl çalışır?', exact=True)).to_be_visible()
        expect(self.page.locator('.topology-flow > li')).to_have_count(7)
        expect(self.page.get_by_test_id('system-topology')).to_contain_text('canlı telemetri değildir')
        expect(self.page.get_by_test_id('system-topology')).to_contain_text('Ortak GPU entegrasyonu henüz kabul edilmedi')
        expect(self.page.get_by_test_id('system-topology')).to_contain_text('her eylem için S2 çağrısı gerekmez')
        self.page.screenshot(path=str(self.evidence / 'tr-mobile.png'), full_page=True)
        self.check_read_only_health()

import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from aos.contracts import REPO_ROOT
from aos.desktop import DOCKER


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Opt in with AOS_DESKTOP_TESTS=1')
class DesktopConsoleIntegrationTests(unittest.TestCase):
    def test_real_authenticated_rfb_ownership_and_graceful_shutdown(self):
        import httpx
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        from websockets.sync.client import connect

        with tempfile.TemporaryDirectory(prefix='console-test-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            if os.environ.get('AOS_UI_TESTS') == '1':
                import asyncio
                from aos.computer import WorkspaceRuntime
                from aos.contracts import Settings
                from aos.decision import FixtureDecisionEngine
                from aos.operator import Operator
                from aos.storage import TrajectoryStore

                self.assertTrue((REPO_ROOT / 'ui/dist/index.html').is_file(), 'Build UI before AOS_UI_TESTS=1')
                settings = Settings(workspace=root / 'fixture-workspace', database=root / 'trajectory.sqlite')
                runtime = WorkspaceRuntime(settings.workspace)
                store = TrajectoryStore(settings.database)
                try:
                    runtime.start()
                    fixture_run = asyncio.run(Operator(settings, store, runtime, FixtureDecisionEngine()).hello())['run_id']
                finally:
                    runtime.stop()
                    store.close()
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            address = f'ws://127.0.0.1:{port}/websockify'
            key = None
            container = None
            with (root / 'server.log').open('w') as output:
                server = subprocess.Popen([sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'), '--port', str(port),
                                           '--workspace', str(root / 'workspace'), '--database', str(root / 'store.sqlite'),
                                           '--trajectory-database', str(root / 'trajectory.sqlite')],
                                          stdout=output, stderr=output)
                try:
                    with httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=15) as client:
                        for attempt in range(120):
                            self.assertIsNone(server.poll(), (root / 'server.log').read_text())
                            try:
                                if client.get('/api/session').status_code == 200:
                                    break
                            except httpx.ConnectError:
                                pass
                            time.sleep(.25)
                        else:
                            self.fail('Owned console did not start')
                        key = Path(re.search(r'Local token file \(0600\): (.+)', (root / 'server.log').read_text()).group(1))
                        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(client.get('/api/retention').status_code, 401)
                        self.assertEqual(client.post('/api/login', json={'token': key.read_text()}).status_code, 200)
                        self.assertEqual(client.get('/api/retention').json()['state'], 'disabled')
                        cookie = 'aos_session=' + client.cookies['aos_session']
                        for headers, source in [({}, origin), ({'Cookie': cookie}, 'http://attacker.invalid')]:
                            with self.assertRaises(InvalidStatus) as caught:
                                with connect(address, origin=source, additional_headers=headers):
                                    pass
                            self.assertEqual(caught.exception.response.status_code, 403)
                        state = client.get('/api/state').json()
                        container = state['runtime']['container_id']
                        control = state['control']
                        queued = client.post('/api/input', json={'lease_id': control['lease_id'], 'generation': control['generation']}).json()['input_id']
                        self.assertEqual(client.post('/api/input/' + queued + '/execute', json={}).status_code, 200)

                        def note():
                            result = subprocess.run([*DOCKER, 'exec', '-i', container, '/usr/bin/python3', '/opt/aos/tools.py'],
                                                    input=b'{"tool":"read_note","arguments":{}}\n', capture_output=True, check=True, timeout=5)
                            return json.loads(result.stdout)['text']

                        def handshake(connection):
                            pending = bytearray()

                            def read(size):
                                while len(pending) < size:
                                    pending.extend(connection.recv(timeout=5))
                                value = bytes(pending[:size])
                                del pending[:size]
                                return value

                            self.assertEqual(read(12), b'RFB 003.008\n')
                            connection.send(b'RFB 003.008\n')
                            self.assertIn(1, read(read(1)[0]))
                            connection.send(b'\x01')
                            self.assertEqual(read(4), bytes(4))
                            connection.send(b'\x01')
                            initial = read(24)
                            self.assertEqual(struct.unpack('!HH', initial[:4]), (1280, 800))
                            read(struct.unpack('!I', initial[20:24])[0])

                        def key_event(connection, keysym):
                            connection.send(struct.pack('!BBHI', 4, 1, 0, keysym) + struct.pack('!BBHI', 4, 0, 0, keysym))

                        with connect(address, origin=origin, additional_headers={'Cookie': cookie}) as connection:
                            handshake(connection)
                            key_event(connection, 0xff57)
                            key_event(connection, ord('X'))
                            time.sleep(.3)
                            self.assertEqual(note(), 'AOS desktop input')
                        self.assertEqual(client.post('/api/control', json={'command': 'take-control'}).status_code, 200)
                        with connect(address, origin=origin, additional_headers={'Cookie': cookie}) as connection:
                            handshake(connection)
                            key_event(connection, 0xff57)
                            key_event(connection, ord('X'))
                            time.sleep(.3)
                            self.assertEqual(note(), 'AOS desktop inputX')
                            self.assertEqual(client.post('/api/control', json={'command': 'return-control'}).status_code, 200)
                            with self.assertRaises(ConnectionClosed):
                                connection.recv(timeout=3)
                        with connect(address, origin=origin, additional_headers={'Cookie': cookie}) as connection:
                            handshake(connection)
                            connection.send(b'x' * 65537)
                            with self.assertRaises(ConnectionClosed):
                                connection.recv(timeout=3)
                        if os.environ.get('AOS_BROWSER_TESTS') == '1':
                            self.check_rendered_console(origin, key.read_text())
                        if os.environ.get('AOS_UI_TESTS') == '1':
                            self.assertEqual(client.post('/api/login', json={'token': key.read_text()}).status_code, 200)
                            self.assertEqual(client.post('/api/control', json={'command': 'pause'}).status_code, 200)
                            self.check_react_console(origin, key.read_text(), fixture_run)
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                        self.fail('Console did not stop gracefully')
            if key:
                self.assertFalse(key.exists(), 'Graceful shutdown must delete its token')
            if container:
                remaining = subprocess.check_output([*DOCKER, 'ps', '-a', '--filter', 'id=' + container, '--format', '{{.ID}}'], timeout=5)
                self.assertEqual(remaining, b'', 'Graceful shutdown must remove its owned container')

    def check_react_console(self, origin, token, run_id):
        from playwright.sync_api import expect, sync_playwright

        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                page.goto(origin + '/ui/')
                self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                page.get_by_label('Yerel oturum anahtarı').fill(token)
                page.get_by_role('button', name='Giriş yap', exact=True).click()
                expect(page.get_by_test_id('owner')).to_have_text('PAUSED')
                page.get_by_role('button', name='Devam et', exact=True).click()
                expect(page.get_by_test_id('owner')).to_have_text('AGENT')
                expect(page.locator('.remote-screen canvas')).to_have_attribute('width', '1280')
                expect(page.locator('.computer-panel').get_by_role('status')).to_have_text('Canlı · 1280 × 800')
                for attempt in range(50):
                    colors = page.locator('.remote-screen canvas').evaluate('canvas => { const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data; const colors = new Set(); for (let index = 0; index < pixels.length; index += 400) colors.add([pixels[index], pixels[index + 1], pixels[index + 2]].join(",")); return colors.size; }')
                    if colors > 8:
                        break
                    page.wait_for_timeout(200)
                self.assertGreater(colors, 8, 'React noVNC must render actual desktop pixels')
                previous_canvas = page.locator('.remote-screen canvas').element_handle()
                page.get_by_role('button', name='Yenile', exact=True).click()
                expect(page.locator('.remote-screen canvas')).to_have_attribute('width', '1280')
                self.assertFalse(previous_canvas.evaluate('element => element.isConnected'))
                expect(page.locator('.computer-panel').get_by_role('status')).to_have_text('Canlı · 1280 × 800')
                page.get_by_role('button', name='Sentetik giriş testi', exact=True).click()
                expect(page.locator('.event-list')).to_contain_text('type_note')
                expect(page.locator('.event-list')).to_contain_text('ok')
                for button, owner in [('Kontrolü al', 'HUMAN'), ('Ajana geri ver', 'AGENT'), ('Duraklat', 'PAUSED'), ('Devam et', 'AGENT')]:
                    page.get_by_role('button', name=button, exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text(owner)
                page.get_by_role('button', name='Kaynaklar', exact=True).click()
                expect(page.locator('.metrics')).to_contain_text('3.0 GiB')
                expect(page.locator('.metrics')).to_contain_text('512')
                page.get_by_role('button', name='Çalışmalar', exact=True).click()
                page.get_by_role('button', name=run_id, exact=True).click()
                expect(page.get_by_test_id('trace')).to_contain_text('independent_read_equals')
                expect(page.get_by_test_id('trace')).to_contain_text('passed')
                page.get_by_role('button', name='Modeller', exact=True).click()
                expect(page.locator('.content')).to_contain_text('Henüz model/deployment kaydı yok.')
                page.set_viewport_size({'width': 390, 'height': 844})
                self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                page.get_by_role('button', name='Bilgisayar', exact=True).click()
                expect(page.locator('.remote-screen canvas')).to_have_attribute('width', '1280')
                page.get_by_role('button', name='Durdur', exact=True).click()
                expect(page.locator('.computer-panel').get_by_role('status')).to_have_text('Masaüstü durduruldu')
                page.get_by_role('button', name='Yeniden başlat', exact=True).click()
                expect(page.get_by_test_id('owner')).to_have_text('PAUSED')
                page.get_by_role('button', name='Çıkış', exact=True).click()
                expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                expect(page.locator('.workspace')).to_have_count(0)
                self.assertEqual(page.evaluate('() => Object.keys(localStorage)'), ['aos.ui.language'])
                self.assertEqual(page.evaluate("() => localStorage.getItem('aos.ui.language')"), 'tr')
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def check_rendered_console(self, origin, token):
        from playwright.sync_api import expect, sync_playwright

        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                page.goto(origin)
                self.assertEqual(page.title(), 'AOS · İzole bilgisayar')
                expect(page.locator('#login')).to_be_visible()
                page.locator('[name=token]').fill(token)
                page.get_by_role('button', name='Giriş yap', exact=True).click()
                expect(page.locator('#status')).to_have_text('AGENT · running')
                expect(page.locator('#screen canvas')).to_have_attribute('width', '1280')
                for attempt in range(30):
                    colors = page.locator('#screen canvas').evaluate('canvas => { const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data; const colors = new Set(); for (let index = 0; index < pixels.length; index += 400) colors.add([pixels[index], pixels[index + 1], pixels[index + 2]].join(",")); return colors.size; }')
                    if colors > 8:
                        break
                    page.wait_for_timeout(100)
                self.assertGreater(colors, 8, 'noVNC must render desktop pixels, not only allocate a blank canvas')
                with page.expect_response(lambda response: response.request.method == 'POST'
                                          and re.fullmatch(re.escape(origin) + r'/api/input/[^/]+/execute', response.url)) as executed:
                    page.get_by_role('button', name='Sentetik giriş testi', exact=True).click()
                self.assertEqual(executed.value.status, 200)
                expect(page.locator('#status')).to_have_text('AGENT · running')
                expect(page.locator('#error')).to_have_text('')
                for button, expected in [('Kontrolü al', 'HUMAN · running'),
                                         ('Ajana geri ver', 'AGENT · running'), ('Duraklat', 'PAUSED · paused'), ('Devam et', 'AGENT · running')]:
                    page.get_by_role('button', name=button, exact=True).click()
                    expect(page.locator('#status')).to_have_text(expected)
                    expect(page.locator('#error')).to_have_text('')
                page.set_viewport_size({'width': 390, 'height': 844})
                self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                page.get_by_role('button', name='Çıkış', exact=True).click()
                expect(page.locator('#login')).to_be_visible()
                self.assertEqual(errors, [])
            finally:
                browser.close()

import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


def wait_for_desktop_frame(page):
    page.wait_for_function('''() => {
        const source = document.querySelector('.remote-screen canvas');
        if (!source || !source.width || !source.height) return false;
        const captured = document.createElement('canvas');
        captured.width = source.width;
        captured.height = source.height;
        const context = captured.getContext('2d', {willReadFrequently: true});
        context.drawImage(source, 0, 0);
        return new Set(context.getImageData(0, 0, captured.width, captured.height).data).size > 8;
    }''')


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1', 'Requires real Docker, pinned browser and built UI')
class VisibleTaskUITests(unittest.TestCase):
    def test_visible_vision_shows_one_save_approval_and_independent_result(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='visible-vision-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture', ('--browser-tasks', '--desktop-browser', '--vision-engine', 'fixture', '--desktop-vision')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, connections, writes = [], [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('websocket', lambda connection: connections.append(connection))
                    page.on('request', lambda request: writes.append(request.post_data_json) if request.method == 'POST' and request.url == origin + '/api/tasks' else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    wait_for_desktop_frame(page)
                    page.get_by_text('Kontrol ve güvenlik sınırları', exact=True).click()
                    expect(page.get_by_test_id('first-use')).to_contain_text('Browser formu ve görsel SAVE')
                    page.evaluate("() => { window.testDesktopCanvas = document.querySelector('.remote-screen canvas'); }")
                    page.get_by_role('button', name='İlk göreve git', exact=True).click()
                    page.get_by_label('Görev türü', exact=True).select_option('vision_canvas')
                    panel = page.get_by_test_id('visible-task-status')
                    expect(panel).to_contain_text('Görsel SAVE görevini bu bilgisayarda izleyin')
                    expect(panel).to_contain_text('aynı canlı izole masaüstünde')
                    expect(panel).not_to_contain_text('headless')
                    expect(page.get_by_text('Ayrı ağsız, headless Bubblewrap/Chromium runtime.', exact=False)).to_have_count(0)
                    expect(page.get_by_text('Görsel gözlemci: Sentetik fixture; görüntü modeli çalışmaz', exact=True)).to_be_visible()
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Vision görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('vision.click', timeout=45000)
                    expect(panel).to_contain_text('1 / 1 · SAVE hedefi seçildi')
                    self.assertTrue(page.evaluate("() => window.testDesktopCanvas === document.querySelector('.remote-screen canvas')"))
                    self.assertEqual(len(connections), 1)
                    expect(page.locator('.computer-panel')).to_have_count(1)
                    pending_frame = page.locator('.remote-screen canvas').evaluate('(canvas) => canvas.toDataURL()')
                    page.screenshot(path='/tmp/aos-visible-vision-before-save.png', full_page=True)
                    page.screenshot(path='/tmp/aos-visible-vision-before-save-viewport.png')
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    expect(panel).to_contain_text('Görsel SAVE görevi tamamlandı; kaydedilen sonuç bağımsız doğrulandı')
                    expect(page.get_by_test_id('approval')).to_have_count(0)
                    page.wait_for_function('(previous) => document.querySelector(".remote-screen canvas").toDataURL() !== previous', arg=pending_frame)
                    page.screenshot(path='/tmp/aos-visible-vision-saved.png', full_page=True)
                    page.screenshot(path='/tmp/aos-visible-vision-saved-viewport.png')
                    tasks = client.get('/api/tasks').json()
                    self.assertEqual(tasks['vision_display'], 'desktop')
                    self.assertFalse(tasks['real_model'])
                    self.assertFalse(tasks['real_supervisor'])
                    self.assertEqual(len(tasks['jobs']), 1)
                    trace = client.get('/api/runs/' + tasks['jobs'][0]['run_id']).json()
                    self.assertEqual([record['tool'] for record in trace['actions'] if record['tool'] == 'vision.click'], ['vision.click'])
                    self.assertTrue(any(record['result'] == 'passed' and record['method'] == 'independent_canvas_equals' for record in trace['verifications']))
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-visible-vision-mobile.png', full_page=True)
                    page.screenshot(path='/tmp/aos-visible-vision-mobile-viewport.png')
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Vision görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('vision.click', timeout=45000)
                    page.get_by_role('button', name='Reddet', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('cancelled')
                    expect(panel).to_contain_text('Görev tamamlanmadı')
                    self.assertEqual(len(writes), 2)
                    for request in writes:
                        self.assertEqual(set(request), {'kind', 'lease_id', 'generation'})
                        self.assertEqual(request['kind'], 'vision_canvas')
                    self.assertEqual(len(connections), 1)
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    expect(page.locator('.computer-panel')).to_have_count(0)
                    self.assertTrue(all(connection.is_closed() for connection in connections))
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                except BaseException:
                    page.screenshot(path='/tmp/aos-visible-vision-ui-failure.png', full_page=True)
                    Path('/tmp/aos-visible-vision-ui-server.log').write_text((root / 'server.log').read_text())
                    Path('/tmp/aos-visible-vision-ui-status.json').write_text(json.dumps(client.get('/api/tasks').json(), indent=2))
                    raise
                finally:
                    browser.close()

    def test_visible_form_keeps_one_desktop_and_shows_each_approval_and_result(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='visible-task-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture', ('--browser-tasks', '--desktop-browser', '--vision-engine', 'fixture')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, connections, writes = [], [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('websocket', lambda connection: connections.append(connection))
                    page.on('request', lambda request: writes.append(request.post_data_json) if request.method == 'POST' and request.url == origin + '/api/tasks' else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    wait_for_desktop_frame(page)
                    page.evaluate("() => { window.testDesktopCanvas = document.querySelector('.remote-screen canvas'); }")
                    page.get_by_role('button', name='İlk göreve git', exact=True).click()
                    expect(page.get_by_label('Görev türü', exact=True)).to_have_value('browser_form')
                    expect(page.locator('.computer-panel')).to_have_count(1)
                    self.assertTrue(page.evaluate("() => window.testDesktopCanvas === document.querySelector('.remote-screen canvas')"))
                    self.assertEqual(len(connections), 1)
                    panel = page.get_by_test_id('visible-task-status')
                    expect(panel).to_contain_text('canlı izole masaüstünde')
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Browser görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.fill', timeout=45000)
                    expect(panel).to_contain_text('1 / 2')
                    expect(page.get_by_role('button', name='Onayla', exact=True)).to_be_visible()
                    empty_frame = page.locator('.remote-screen canvas').evaluate('(canvas) => canvas.toDataURL()')
                    page.screenshot(path='/tmp/aos-visible-task-before-fill.png', full_page=True)
                    page.screenshot(path='/tmp/aos-visible-task-before-fill-viewport.png')
                    self.assertEqual(set(writes[0]), {'kind', 'lease_id', 'generation'})
                    self.assertEqual(writes[0]['kind'], 'browser_form')
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.submit', timeout=30000)
                    expect(panel).to_contain_text('2 / 2')
                    page.wait_for_function('(previous) => document.querySelector(".remote-screen canvas").toDataURL() !== previous', arg=empty_frame)
                    filled_frame = page.locator('.remote-screen canvas').evaluate('(canvas) => canvas.toDataURL()')
                    page.screenshot(path='/tmp/aos-visible-task-filled.png', full_page=True)
                    page.screenshot(path='/tmp/aos-visible-task-filled-viewport.png')
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    self.assertTrue(page.evaluate("() => window.testDesktopCanvas === document.querySelector('.remote-screen canvas')"))
                    self.assertEqual(len(connections), 1)
                    expect(panel).to_contain_text('2 / 2')
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded', timeout=30000)
                    expect(panel).to_contain_text('kaydedilen içerik bağımsız doğrulandı')
                    page.wait_for_function('(previous) => document.querySelector(".remote-screen canvas").toDataURL() !== previous', arg=filled_frame)
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-visible-task-saved.png', full_page=True)
                    page.screenshot(path='/tmp/aos-visible-task-saved-viewport.png')
                    tasks = client.get('/api/tasks').json()
                    self.assertEqual(tasks['browser_display'], 'desktop')
                    self.assertEqual(tasks['jobs'][0]['status'], 'succeeded')
                    self.assertFalse(tasks['real_model'])
                    trace = client.get('/api/runs/' + tasks['jobs'][0]['run_id']).json()
                    self.assertTrue(any(record['result'] == 'passed' and record['method'] == 'independent_dom_equals' for record in trace['verifications']))
                    page.get_by_label('Görev türü', exact=True).select_option('vision_canvas')
                    expect(page.get_by_text('Ayrı ağsız, headless Bubblewrap/Chromium runtime.', exact=False)).to_be_visible()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    expect(page.locator('.computer-panel')).to_have_count(1)
                    page.screenshot(path='/tmp/aos-visible-task-mobile.png', full_page=True)
                    page.screenshot(path='/tmp/aos-visible-task-mobile-viewport.png')
                    page.get_by_role('button', name='Kontrolü al', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('HUMAN')
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    self.assertTrue(connections[0].is_closed())
                    expect(page.get_by_role('button', name='Vision görevi başlat', exact=True)).to_be_disabled()
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    expect(page.locator('.computer-panel')).to_have_count(0)
                    self.assertTrue(all(connection.is_closed() for connection in connections))
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(errors, [])
                except BaseException:
                    page.screenshot(path='/tmp/aos-visible-task-failure.png', full_page=True)
                    Path('/tmp/aos-visible-task-ui-server.log').write_text((root / 'server.log').read_text())
                    Path('/tmp/aos-visible-task-ui-status.json').write_text(json.dumps(client.get('/api/tasks').json(), indent=2))
                    raise
                finally:
                    browser.close()

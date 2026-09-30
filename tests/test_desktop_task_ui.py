from contextlib import contextmanager
import os
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

from aos.contracts import HELLO_CONTENT, REPO_ROOT
from aos.desktop import DOCKER


@contextmanager
def task_server(root, engine, extra_arguments=(), port=None):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port or 0))
        port = listener.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    key = None
    container = None
    with (root / 'server.log').open('w') as output:
        server = subprocess.Popen([sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'), '--port', str(port),
                                   '--workspace', str(root / 'workspace'), '--database', str(root / 'store.sqlite'),
                                   '--engine', engine, *extra_arguments], stdout=output, stderr=output)
        try:
            with httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=20) as client:
                for attempt in range(120):
                    assert server.poll() is None, (root / 'server.log').read_text()
                    try:
                        if client.get('/api/session').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.25)
                else:
                    raise AssertionError('Task console startup timed out')
                key = Path(re.search(r'Local token file \(0600\): (.+)', (root / 'server.log').read_text()).group(1))
                token = key.read_text()
                client.post('/api/login', json={'token': token}).raise_for_status()
                container = client.get('/api/state').json()['runtime']['container_id']
                yield origin, token, client, server
        finally:
            server.terminate()
            server.wait(timeout=30)
            if key:
                assert not key.exists(), 'Token was not removed'
            if container:
                assert not subprocess.check_output([*DOCKER, 'ps', '-a', '--filter', 'id=' + container, '--format', '{{.ID}}'], timeout=5)


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1', 'Requires real Docker and built UI')
class DesktopTaskUITests(unittest.TestCase):
    def test_sequential_tasks_require_four_approvals_and_stop_on_rejection(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory(prefix='sequence-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture', ('--browser-tasks', '--vision-engine', 'fixture')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_label('Görev akışı', exact=True).select_option('2')
                    page.get_by_role('button', name='Sıralı akışı başlat', exact=True).click()
                    for tool in ('filesystem.write', 'browser.fill', 'browser.submit', 'vision.click'):
                        expect(page.get_by_test_id('approval')).to_contain_text(tool, timeout=30000)
                        expect(page.get_by_role('button', name='Sıralı akışı başlat', exact=True)).to_be_disabled()
                        expect(page.get_by_role('button', name='Hello görevi başlat', exact=True)).to_be_disabled()
                        page.get_by_role('button', name='Onayla', exact=True).click()
                    status = page.get_by_test_id('sequence-status')
                    expect(status).to_contain_text('succeeded')
                    expect(status).to_contain_text('3 / 3')
                    expect(page.get_by_test_id('task-row')).to_have_count(3)
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                    page.evaluate('() => window.scrollTo(0, 0)')
                    page.screenshot(path='/tmp/aos-sequence-desktop.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-sequence-mobile.png', full_page=True)
                    page.get_by_role('button', name='Sıralı akışı başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.read')
                    page.get_by_role('button', name='Reddet', exact=True).click()
                    expect(status).to_contain_text('failed')
                    expect(status).to_contain_text('0 / 3')
                    expect(page.get_by_test_id('task-row')).to_have_count(4)
                    self.assertEqual(len(client.get('/api/sequences').json()['sequence']['job_ids']), 1)
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_plan_catalog_is_read_only_and_drops_stale_results(self):
        from playwright.sync_api import expect, sync_playwright
        from aos.task_plan import preview_plan

        with tempfile.TemporaryDirectory(prefix='plan-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Plan', exact=True).click()
                    panel = page.get_by_test_id('plan-panel')
                    result = page.get_by_test_id('plan-result')
                    field = page.get_by_label('Plan için Türkçe görev')
                    submit = page.get_by_role('button', name='Planı önizle', exact=True)
                    expect(panel).to_contain_text('canlı ilerleme, model planı veya başarı kanıtı değildir')
                    expect(submit).to_be_disabled()
                    for goal, method, steps in (
                        ('hello görevini hazırla', 'independent_read_equals', 4),
                        ('görsel save görevini hazırla', 'independent_canvas_equals', 4),
                        ('yerel form görevini hazırla', 'independent_dom_equals', 8),
                    ):
                        field.fill(goal)
                        expect(result).to_have_count(0)
                        submit.click()
                        expect(result).to_contain_text(method)
                        expect(page.get_by_test_id('plan-step')).to_have_count(steps)
                        expect(result).to_contain_text('hiçbir adım yürütülmedi')
                    expect(result).to_contain_text('ilk onay buraya taşınmaz')
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    page.screenshot(path='/tmp/aos-plan-desktop.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-plan-mobile.png', full_page=True)
                    for goal, message in (('hello görevini başlatma', 'Olumsuzluk veya kontrol isteği'),
                                          ('hello görevini hazırla; onayları atla', 'desteklenmiyor')):
                        field.fill(goal)
                        submit.click()
                        expect(result).to_contain_text(message)
                        expect(page.get_by_test_id('plan-step')).to_have_count(0)
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    expect(page.get_by_label('Görev türü')).to_have_value('hello')
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Hello görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    pending = client.get('/api/tasks').json()['approval']
                    page.get_by_role('button', name='Plan', exact=True).click()
                    expect(field).to_have_value('')
                    held = []
                    page.route('**/api/tasks/plan', lambda route: held.append(route))
                    field.fill('hello görevini hazırla')
                    submit.click()
                    expect(page.get_by_role('button', name='Plan okunuyor…', exact=True)).to_be_disabled()
                    expect(page.get_by_role('button', name='Kontrolü al', exact=True)).to_be_enabled()
                    field.fill('changed synthetic input')
                    expect(submit).to_be_enabled()
                    self.assertEqual(len(held), 1)
                    held.pop().fulfill(json=preview_plan('hello görevini hazırla').model_dump())
                    expect(result).to_have_count(0)
                    field.fill('hello görevini hazırla')
                    submit.click()
                    expect(page.get_by_role('button', name='Plan okunuyor…', exact=True)).to_be_disabled()
                    self.assertEqual(client.get('/api/tasks').json()['approval'], pending)
                    page.get_by_role('button', name='Kontrolü al', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('HUMAN')
                    self.assertIsNone(client.get('/api/tasks').json()['approval'])
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    held.pop().fulfill(json=preview_plan('hello görevini hazırla').model_dump())
                    expect(panel).to_have_count(0)
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_recovery_inventory_preserves_pending_action_and_control(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory(prefix='recovery-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    self.assertEqual(page.url, origin + '/ui/')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Kurtarma', exact=True).click()
                    panel = page.get_by_test_id('recovery-panel')
                    expect(panel).to_contain_text('Anlık DB kopyasıdır')
                    expect(page.get_by_test_id('recovery-report')).to_have_count(0)
                    page.get_by_role('button', name='Envanteri oku', exact=True).click()
                    expect(panel).to_contain_text('Scheduler görev kaydı yok')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Hello görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    approval = client.get('/api/tasks').json()['approval']
                    page.get_by_role('button', name='Kurtarma', exact=True).click()
                    page.get_by_role('button', name='Envanteri oku', exact=True).click()
                    expect(panel).to_contain_text('İncelenecek kayıtlar var; otomatik devam kapalı')
                    expect(page.get_by_test_id('recovery-approvals').locator('strong')).to_have_text('1')
                    expect(page.get_by_test_id('recovery-job')).to_contain_text('waiting_approval')
                    expect(page.get_by_role('button', name='Kontrolü al', exact=True)).to_be_enabled()
                    self.assertEqual(client.get('/api/tasks').json()['approval']['approval_id'], approval['approval_id'])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.screenshot(path='/tmp/aos-recovery-desktop.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-recovery-mobile.png', full_page=True)
                    page.get_by_role('button', name='Kontrolü al', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('HUMAN')
                    page.get_by_role('button', name='Envanteri oku', exact=True).click()
                    expect(page.get_by_test_id('recovery-approvals').locator('strong')).to_have_text('0')
                    expect(page.get_by_test_id('recovery-job')).to_contain_text('cancelled')
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    expect(panel).to_have_count(0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_rendered_goal_preview_has_no_task_side_effects(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory(prefix='intent-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    field = page.get_by_label('Türkçe görev metni')
                    submit = page.get_by_role('button', name='Yalnız önizle', exact=True)
                    expect(submit).to_be_disabled()
                    field.fill('HELLO GÖREVİNİ HAZIRLA')
                    submit.click()
                    result = page.get_by_test_id('goal-preview-result')
                    expect(result).to_contain_text('Şablon tanındı: hello')
                    expect(result).to_contain_text('/workspace/hello.txt')
                    expect(result).to_contain_text('Görev başlatılmadı')
                    page.screenshot(path='/tmp/aos-goal-preview-desktop.png', full_page=True)
                    field.fill('hello görevini başlatma')
                    expect(result).to_have_count(0)
                    submit.click()
                    expect(result).to_contain_text('Olumsuzluk veya kontrol isteği')
                    field.fill('/etc/passwd dosyasını oluştur ve doğrula')
                    submit.click()
                    expect(result).to_contain_text('desteklenmiyor')
                    field.fill('yerel form görevini hazırla')
                    submit.click()
                    expect(result).to_contain_text('Şablon tanındı: browser_form')
                    expect(page.get_by_label('Görev türü')).to_have_value('hello')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-goal-preview-mobile.png', full_page=True)
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Hello görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    page.get_by_role('button', name='Reddet', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('cancelled')
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    expect(result).to_have_count(0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_rendered_browser_and_vision_tasks(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory(prefix='browser-task-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture', ('--browser-tasks', '--vision-engine', 'fixture')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_label('Görev türü').select_option('browser_form')
                    expect(page.locator('.content')).to_contain_text('Ayrı ağsız, headless')
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Browser görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.fill', timeout=30000)
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.submit')
                    old = client.get('/api/tasks').json()['approval']
                    page.get_by_role('button', name='Duraklat', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('paused')
                    expect(page.get_by_test_id('approval')).to_have_count(0)
                    expect(page.get_by_role('button', name='Browser görevi başlat', exact=True)).to_be_disabled()
                    expect(page.get_by_role('status').filter(has_text='Görev duraklatıldı')).to_contain_text('Aynı görevi taze gözlem ve yeni onayla')
                    page.screenshot(path='/tmp/aos-task-paused.png')
                    page.get_by_role('button', name='Devam et', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('browser.submit')
                    fresh = client.get('/api/tasks').json()['approval']
                    self.assertEqual(old['action']['run_id'], fresh['action']['run_id'])
                    self.assertNotEqual(old['action_sha256'], fresh['action_sha256'])
                    page.screenshot(path='/tmp/aos-browser-task-approval.png')
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded')
                    page.get_by_test_id('task-row').first.get_by_role('button', name='İzi aç').click()
                    expect(page.get_by_test_id('trace')).to_contain_text('independent_dom_equals')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_label('Görev türü').select_option('vision_canvas')
                    expect(page.locator('.content')).to_contain_text('Sentetik fixture; görüntü modeli çalışmaz')
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Vision görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('vision.click', timeout=30000)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-vision-task-mobile.png', full_page=True)
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded')
                    page.get_by_test_id('task-row').first.get_by_role('button', name='İzi aç').click()
                    expect(page.get_by_test_id('trace')).to_contain_text('independent_canvas_equals')
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_rendered_approval_rejection_takeover_and_trace(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory(prefix='task-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors = []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    expect(page.locator('.content')).to_contain_text('SENTETİK TEST MOTORU')
                    start = page.get_by_role('button', name='Hello görevi başlat', exact=True)
                    page.get_by_test_id('approve-all').uncheck()
                    start.click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.get_by_role('button', name='Reddet', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('cancelled')
                    page.get_by_test_id('approve-all').uncheck()
                    start.click()
                    expect(page.get_by_test_id('approval')).to_be_visible()
                    page.get_by_role('button', name='Kontrolü al', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('HUMAN')
                    expect(page.get_by_test_id('approval')).to_have_count(0)
                    expect(start).to_be_disabled()
                    page.get_by_role('button', name='Ajana geri ver', exact=True).click()
                    expect(page.get_by_test_id('owner')).to_have_text('AGENT')
                    page.get_by_test_id('approve-all').uncheck()
                    start.click()
                    expect(page.get_by_test_id('approval')).to_be_visible()
                    page.screenshot(path='/tmp/aos-task-approval-desktop.png')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-task-approval-mobile.png', full_page=True)
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded')
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                    page.get_by_test_id('task-row').first.get_by_role('button', name='İzi aç').click()
                    expect(page.get_by_test_id('trace')).to_contain_text('independent_read_equals')
                    expect(page.get_by_test_id('trace')).to_contain_text('passed')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_test_id('approve-all').uncheck()
                    start.click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.read')
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    client.post('/api/login', json={'token': token}).raise_for_status()
                    status = client.get('/api/tasks').json()
                    self.assertFalse(status['busy'])
                    self.assertEqual(status['jobs'][0]['status'], 'cancelled')
                    self.assertEqual(errors, [])
                finally:
                    browser.close()


@unittest.skipUnless(os.environ.get('AOS_REAL_TASK_TESTS') == '1', 'Explicit opt-in for real pinned Decider inference')
class RealDesktopTaskTests(unittest.TestCase):
    def test_real_decider_gpu_idle_reuses_resident_worker_and_expires(self):
        with tempfile.TemporaryDirectory(prefix='task-gpu-idle-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            flags = ('--reuse-decider', '--prewarm-decider', '--gpu-idle-seconds', '5')
            with task_server(root, 'decider', flags) as (origin, token, client, server):
                def resident_worker():
                    children = Path(f'/proc/{server.pid}/task/{server.pid}/children').read_text().split()
                    workers = []
                    for child in children:
                        try:
                            if b'services/decider/worker.py' in Path(f'/proc/{child}/cmdline').read_bytes():
                                workers.append(child)
                        except FileNotFoundError:
                            pass
                    self.assertEqual(len(workers), 1)
                    return workers[0]

                def run_hello():
                    control = client.get('/api/state').json()['control']
                    response = client.post('/api/tasks', json={
                        'kind': 'hello', 'approve_all': True,
                        'lease_id': control['lease_id'], 'generation': control['generation']})
                    response.raise_for_status()
                    job_id = response.json()['job_id']
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        status = client.get('/api/tasks').json()
                        job = next((item for item in status['jobs'] if item['job_id'] == job_id), None)
                        if (job is not None and job['status'] in ('succeeded', 'failed', 'cancelled')
                                and status['decider_preparation']['state'] == 'ready_gpu'):
                            self.assertEqual(job['status'], 'succeeded')
                            trace = client.get('/api/runs/' + job['run_id']).json()
                            self.assertEqual([call['status'] for call in trace['model_calls']], ['ok'])
                            self.assertEqual([item['result'] for item in trace['verifications']], ['passed'])
                            return trace
                        time.sleep(.1)
                    self.fail('Real task did not complete with an idle GPU model')

                first = run_hello()
                worker_pid = resident_worker()
                usage = subprocess.check_output([
                    '/usr/bin/nvidia-smi', '--query-compute-apps=pid,used_memory',
                    '--format=csv,noheader,nounits'], text=True, timeout=5)
                memory = [int(fields[1]) for line in usage.splitlines()
                          if len(fields := [field.strip() for field in line.split(',')]) == 2
                          and fields[0] == worker_pid and fields[1].isdigit()]
                self.assertTrue(memory and memory[0] >= 128, 'Idle worker must hold a real CUDA allocation')
                second = run_hello()
                self.assertEqual(resident_worker(), worker_pid)
                self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    if status['decider_preparation']['state'] == 'inactive' and not Path(f'/proc/{worker_pid}').exists():
                        break
                    time.sleep(.1)
                else:
                    self.fail('GPU idle deadline did not reap the resident worker')
                print(json.dumps({'gpu_idle_real': 'passed', 'model_latency_ms': [
                    first['model_calls'][0]['latency_ms'], second['model_calls'][0]['latency_ms']]}), flush=True)

    def test_real_gpu_idle_is_released_before_bonsai_vision(self):
        with tempfile.TemporaryDirectory(prefix='task-gpu-vision-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            flags = ('--browser-tasks', '--vision-engine', 'bonsai', '--reuse-decider',
                     '--prewarm-decider', '--gpu-idle-seconds', '30')
            with task_server(root, 'decider', flags) as (origin, token, client, server):
                def start(kind, approve_all=False):
                    control = client.get('/api/state').json()['control']
                    response = client.post('/api/tasks', json={
                        'kind': kind, 'approve_all': approve_all,
                        'lease_id': control['lease_id'], 'generation': control['generation']})
                    response.raise_for_status()
                    return response.json()['job_id']

                def wait_for(predicate):
                    deadline = time.monotonic() + 180
                    while time.monotonic() < deadline:
                        status = client.get('/api/tasks').json()
                        if predicate(status):
                            return status
                        time.sleep(.1)
                    self.fail('Real GPU/Vision transition timed out')

                hello_id = start('hello', approve_all=True)
                hello = wait_for(lambda status: status['decider_preparation']['state'] == 'ready_gpu'
                                 and any(job['job_id'] == hello_id and job['status'] == 'succeeded'
                                         for job in status['jobs']))
                children = Path(f'/proc/{server.pid}/task/{server.pid}/children').read_text().split()
                resident = [child for child in children
                            if b'services/decider/worker.py' in Path(f'/proc/{child}/cmdline').read_bytes()]
                self.assertEqual(len(resident), 1)
                vision_id = start('vision_canvas')
                pending = wait_for(lambda status: status['approval'] is not None
                                   or any(job['job_id'] == vision_id and job['status'] in ('failed', 'cancelled')
                                          for job in status['jobs']))
                approval = pending['approval']
                self.assertIsNotNone(approval, pending)
                self.assertFalse(Path(f'/proc/{resident[0]}').exists(),
                                 'Previous CUDA worker must be reaped before Vision approval')
                self.assertEqual(approval['action']['tool'], 'vision.click')
                client.post('/api/approvals/' + approval['approval_id'], json={
                    'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                settled = wait_for(lambda status: any(
                    job['job_id'] == vision_id and job['status'] in ('succeeded', 'failed', 'cancelled')
                    for job in status['jobs']))
                job = next(job for job in settled['jobs'] if job['job_id'] == vision_id)
                self.assertEqual(job['status'], 'succeeded')
                trace = client.get('/api/runs/' + job['run_id']).json()
                self.assertEqual([call['role'] for call in trace['model_calls']], ['system2', 'system1'])
                self.assertEqual([call['status'] for call in trace['model_calls']], ['ok', 'ok'])
                self.assertEqual([item['result'] for item in trace['verifications']], ['passed'])
                self.assertNotEqual(hello['jobs'][0]['job_id'], vision_id)

    def test_real_decider_takeover_then_fresh_approved_task(self):
        root = Path(tempfile.mkdtemp(prefix='task-real-', dir=REPO_ROOT / 'data'))
        with task_server(root, 'decider') as (origin, token, client, server):
            def start():
                state = client.get('/api/state').json()['control']
                response = client.post('/api/tasks', json={'kind': 'hello', 'lease_id': state['lease_id'], 'generation': state['generation']})
                response.raise_for_status()
                return response.json()['job_id']

            def wait_for(predicate, timeout=120):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    if predicate(status):
                        return status
                    time.sleep(.1)
                self.fail('Real task timed out')

            start()
            worker_pid = None
            for attempt in range(100):
                children = Path(f'/proc/{server.pid}/task/{server.pid}/children').read_text().split()
                for child in children:
                    try:
                        if b'services/decider/worker.py' in Path(f'/proc/{child}/cmdline').read_bytes():
                            worker_pid = child
                    except FileNotFoundError:
                        pass
                if worker_pid:
                    break
                time.sleep(.05)
            self.assertIsNotNone(worker_pid, 'Real native worker must be running before takeover')
            gpu_memory_mib = 0
            for attempt in range(150):
                usage = subprocess.check_output(['/usr/bin/nvidia-smi', '--query-compute-apps=pid,used_memory', '--format=csv,noheader,nounits'], text=True, timeout=5)
                for line in usage.splitlines():
                    fields = [field.strip() for field in line.split(',')]
                    if len(fields) == 2 and fields[0] == worker_pid and fields[1].isdigit():
                        gpu_memory_mib = int(fields[1])
                if gpu_memory_mib >= 128:
                    break
                time.sleep(.1)
            self.assertGreaterEqual(gpu_memory_mib, 128, 'Cancellation probe requires a real CUDA-allocated worker')
            client.post('/api/control', json={'command': 'take-control'}).raise_for_status()
            cancelled = wait_for(lambda status: not status['busy'])['jobs'][0]
            self.assertEqual(cancelled['status'], 'cancelled')
            self.assertFalse(Path(f'/proc/{worker_pid}').exists(), 'Takeover must reap its native worker')
            cancelled_trace = client.get('/api/runs/' + cancelled['run_id']).json()
            self.assertEqual([call['status'] for call in cancelled_trace['model_calls']], ['cancelled'])
            self.assertEqual(cancelled_trace['actions'], [])
            self.assertFalse((root / 'workspace/hello.txt').exists())
            self.assertEqual(client.get('/api/state').json()['control']['owner'], 'HUMAN')
            client.post('/api/control', json={'command': 'return-control'}).raise_for_status()
            start()
            status = wait_for(lambda status: status['approval'] is not None or not status['busy'])
            self.assertTrue(status['real_model'])
            approval = status['approval']
            self.assertIsNotNone(approval, status)
            self.assertFalse((root / 'workspace/hello.txt').exists())
            client.post('/api/approvals/' + approval['approval_id'], json={'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
            completed = wait_for(lambda status: not status['busy'])['jobs'][0]
            self.assertEqual(completed['status'], 'succeeded')
            self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
            trace = client.get('/api/runs/' + completed['run_id']).json()
            self.assertEqual([call['role'] for call in trace['model_calls']], ['system1'])
            self.assertEqual([call['status'] for call in trace['model_calls']], ['ok'])
            self.assertEqual([item['result'] for item in trace['verifications']], ['passed'])
            print(json.dumps({'evidence_directory': str(root), 'cancelled_run': cancelled['run_id'],
                              'cancelled_worker_gpu_mib': gpu_memory_mib, 'approved_trace': trace}), flush=True)


@unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1', 'Explicit opt-in for real Decider and Bonsai scheduled tasks')
class RealBrowserTaskTests(unittest.TestCase):
    def test_real_decider_bonsai_three_task_sequence(self):
        root = Path(tempfile.mkdtemp(prefix='sequence-real-', dir=REPO_ROOT / 'data'))
        with task_server(root, 'decider', ('--browser-tasks', '--vision-engine', 'bonsai')) as (origin, token, client, server):
            state = client.get('/api/state').json()['control']
            response = client.post('/api/sequences', json={'plan': {'kinds': ['hello', 'browser_form', 'vision_canvas']},
                                                          'lease_id': state['lease_id'], 'generation': state['generation']})
            response.raise_for_status()
            seen, tools = set(), []
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                tasks = client.get('/api/tasks').json()
                approval = tasks['approval']
                if approval and approval['approval_id'] not in seen:
                    seen.add(approval['approval_id'])
                    tools.append(approval['action']['tool'])
                    client.post('/api/approvals/' + approval['approval_id'], json={
                        'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                progress = client.get('/api/sequences').json()
                if not progress['reserved']:
                    break
                time.sleep(.1)
            else:
                self.fail('Real sequence exceeded acceptance budget')
            report = progress['sequence']
            self.assertEqual((report['status'], report['completed']), ('succeeded', 3), report)
            self.assertEqual(tools, ['filesystem.write', 'browser.fill', 'browser.submit', 'vision.click'])
            tasks = client.get('/api/tasks').json()
            self.assertTrue(all(job['real_model'] == 1 and job['status'] == 'succeeded' for job in tasks['jobs']))
            traces = [client.get('/api/runs/' + job['run_id']).json() for job in reversed(tasks['jobs'])]
            self.assertEqual([[item['role'] for item in trace['model_calls']] for trace in traces],
                             [['system1'], ['system1', 'system1'], ['system2', 'system1']])
            self.assertTrue(all(item['status'] == 'ok' for trace in traces for item in trace['model_calls']))
            self.assertEqual([len(trace['verifications']) for trace in traces], [1, 2, 1])
            self.assertTrue(all(item['result'] == 'passed' for trace in traces for item in trace['verifications']))
            self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
            print(json.dumps({'evidence_directory': str(root), 'sequence': report, 'traces': traces}), flush=True)

    def test_real_browser_vision_and_bonsai_takeover(self):
        root = Path(tempfile.mkdtemp(prefix='browser-task-real-', dir=REPO_ROOT / 'data'))
        with task_server(root, 'decider', ('--browser-tasks', '--vision-engine', 'bonsai')) as (origin, token, client, server):
            def start(kind):
                state = client.get('/api/state').json()['control']
                client.post('/api/tasks', json={'kind': kind, 'lease_id': state['lease_id'], 'generation': state['generation']}).raise_for_status()

            def approve_to_completion(pause_tool=None):
                seen = set()
                paused = False
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    approval = status['approval']
                    if approval and approval['approval_id'] not in seen:
                        if approval['action']['tool'] == pause_tool and not paused:
                            client.post('/api/control', json={'command': 'pause'}).raise_for_status()
                            waiting = client.get('/api/tasks').json()
                            self.assertTrue(waiting['paused'])
                            self.assertFalse(waiting['busy'])
                            self.assertEqual(client.post('/api/approvals/' + approval['approval_id'], json={'action_sha256': approval['action_sha256'], 'accept': True}).status_code, 409)
                            client.post('/api/control', json={'command': 'resume'}).raise_for_status()
                            paused = True
                            continue
                        seen.add(approval['approval_id'])
                        client.post('/api/approvals/' + approval['approval_id'], json={'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                    if not status['busy']:
                        self.assertEqual(status['jobs'][0]['status'], 'succeeded', status)
                        return client.get('/api/runs/' + status['jobs'][0]['run_id']).json(), len(seen)
                    time.sleep(.1)
                self.fail('Real scheduled task exceeded acceptance budget')

            start('browser_form')
            browser_trace, approvals = approve_to_completion('browser.submit')
            self.assertEqual(approvals, 2)
            self.assertEqual([item['role'] for item in browser_trace['model_calls']], ['system1', 'system1', 'system1'])
            self.assertEqual([item['result'] for item in browser_trace['verifications']], ['passed', 'passed'])
            start('vision_canvas')
            bonsai_pid = None
            gpu_memory_mib = 0
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                for child in Path(f'/proc/{server.pid}/task/{server.pid}/children').read_text().split():
                    try:
                        command = Path(f'/proc/{child}/cmdline').read_bytes()
                    except FileNotFoundError:
                        continue
                    if b'--alias\x00bonsai-' in command:
                        bonsai_pid = child
                if bonsai_pid:
                    usage = subprocess.check_output(['/usr/bin/nvidia-smi', '--query-compute-apps=pid,used_memory', '--format=csv,noheader,nounits'], text=True, timeout=5)
                    for line in usage.splitlines():
                        fields = [field.strip() for field in line.split(',')]
                        if len(fields) == 2 and fields[0] == bonsai_pid and fields[1].isdigit():
                            gpu_memory_mib = int(fields[1])
                if gpu_memory_mib >= 128:
                    break
                time.sleep(.1)
            self.assertGreaterEqual(gpu_memory_mib, 128, 'Bonsai must allocate CUDA memory before takeover')
            client.post('/api/control', json={'command': 'take-control'}).raise_for_status()
            status = client.get('/api/tasks').json()
            self.assertFalse(status['busy'])
            self.assertEqual(status['jobs'][0]['status'], 'cancelled')
            self.assertFalse(Path(f'/proc/{bonsai_pid}').exists())
            cancelled_trace = client.get('/api/runs/' + status['jobs'][0]['run_id']).json()
            self.assertEqual([(item['role'], item['status']) for item in cancelled_trace['model_calls']], [('system2', 'cancelled')])
            self.assertEqual(cancelled_trace['actions'], [])
            client.post('/api/control', json={'command': 'return-control'}).raise_for_status()
            start('vision_canvas')
            vision_trace, approvals = approve_to_completion('vision.click')
            self.assertEqual(approvals, 1)
            self.assertEqual(sorted(item['role'] for item in vision_trace['model_calls']), ['system1', 'system1', 'system2', 'system2'])
            self.assertTrue(all(item['status'] == 'ok' for item in vision_trace['model_calls']))
            self.assertEqual([item['result'] for item in vision_trace['verifications']], ['passed'])
            print(json.dumps({'evidence_directory': str(root), 'browser_trace': browser_trace,
                              'cancelled_bonsai_gpu_mib': gpu_memory_mib, 'cancelled_trace': cancelled_trace,
                              'vision_trace': vision_trace}), flush=True)

import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1', 'Requires real Docker and built UI')
class FirstUseUITests(unittest.TestCase):
    def test_workflow_guidance_preserves_control_and_exposes_pending_approval(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='workflow-ui-', dir=REPO_ROOT / 'data') as temporary:
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
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.get_by_test_id('pilot-engine')).to_contain_text('SENTETİK')
                    notice = page.get_by_test_id('workflow-notice')
                    page.get_by_role('button', name='Duraklat', exact=True).click()
                    expect(notice).to_contain_text('üstteki Devam et')
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    start = page.get_by_role('button', name='Hello görevi başlat', exact=True)
                    expect(start).to_be_disabled()
                    self.assertEqual(client.get('/api/state').json()['control']['owner'], 'PAUSED')
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    page.screenshot(path='/tmp/aos-workflow-paused.png', full_page=True)
                    page.get_by_role('button', name='Devam et', exact=True).click()
                    expect(start).to_be_enabled()
                    page.get_by_test_id('approve-all').uncheck()
                    start.click()
                    expect(notice).to_contain_text('Görev eylem onayınızı bekliyor')
                    approval = client.get('/api/tasks').json()['approval']
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(notice).to_contain_text('Görev eylem onayınızı bekliyor')
                    page.get_by_role('button', name='Onayı göster', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    self.assertTrue(page.evaluate('''() => {
                        const approval = document.querySelector('[data-testid="approval"]');
                        const form = document.querySelector('.goal-preview');
                        return Boolean(approval.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING);
                    }'''))
                    self.assertEqual(client.get('/api/tasks').json()['approval'], approval)
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-workflow-approval-mobile.png', full_page=True)
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded')
                    expect(notice).to_have_count(0)
                    page.get_by_role('button', name='Kontrolü al', exact=True).click()
                    expect(notice).to_contain_text('üstteki Ajana geri ver')
                    expect(start).to_be_disabled()
                    page.get_by_role('button', name='Ajana geri ver', exact=True).click()
                    expect(start).to_be_enabled()
                    page.get_by_test_id('approve-all').uncheck()
                    start.click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.read')
                    page.get_by_role('button', name='Reddet', exact=True).click()
                    expect(notice).to_contain_text('Son görev iptal edildi')
                    page.get_by_role('button', name='Durdur', exact=True).click()
                    expect(notice).to_contain_text('Önce üstteki Yeniden başlat')
                    page.get_by_role('button', name='Yeniden başlat', exact=True).click()
                    expect(notice).to_contain_text('üstteki Devam et')
                    expect(start).to_be_disabled()
                    self.assertEqual(client.get('/api/state').json()['control']['owner'], 'PAUSED')
                    self.assertEqual(len(client.get('/api/tasks').json()['jobs']), 2)
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(notice).to_have_count(0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_first_use_explains_scope_without_starting_tasks_and_labels_engine_states(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='first-use-ui-', dir=REPO_ROOT / 'data') as temporary:
            with task_server(Path(temporary), 'fixture', ('--browser-tasks', '--vision-engine', 'fixture')) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, writes = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
                    page.goto(origin + '/ui/')
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertEqual(page.title(), 'AOS · Kontrol merkezi')
                    panel = page.get_by_test_id('first-use')
                    expect(panel).to_have_count(0)
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(panel).to_contain_text('İlk deneme · v0.1 yerel pilot')
                    expect(page.get_by_test_id('pilot-engine')).to_have_text('SENTETİK TEST MOTORU · gerçek model sonucu değildir')
                    expect(panel.locator('li')).to_have_count(4)
                    expect(panel.get_by_test_id('first-use-capability-browser_local_navigation')).to_contain_text('Bu oturumda kapalı')
                    expect(panel.get_by_test_id('first-use-capability-vision_canvas')).to_contain_text('sentetik gözlemci')
                    expect(panel).to_contain_text('her eylemi okuyup ayrı onaylayın')
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-first-use-desktop.png', full_page=True)
                    page.get_by_role('button', name='İlk göreve git', exact=True).click()
                    expect(page.get_by_role('heading', name='Onaylı sınırlı görevler')).to_be_visible()
                    expect(page.get_by_test_id('task-capability-browser_local_navigation')).to_contain_text('Bu oturumda kapalı')
                    expect(page.get_by_test_id('navigation-capability-note')).to_contain_text('bu sunucu oturumunda sunulmuyor')
                    expect(page.get_by_label('Görev türü').locator('option[value="browser_local_navigation"]')).to_have_count(0)
                    expect(panel).to_have_count(0)
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    page.get_by_role('button', name='Plan oluşturmaya git', exact=True).click()
                    expect(page.get_by_test_id('plan-panel')).to_be_visible()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    page.get_by_text('Kontrol ve güvenlik sınırları', exact=True).click()
                    expect(panel).to_contain_text('çökme sonrası otomatik devam yoktur')
                    expect(panel).to_contain_text('noVNC masaüstünde görünmez')
                    expect(panel).to_contain_text('backend’i kapatmaz')
                    expect(panel).to_contain_text('.venv/bin/python -m aos.local_app stop')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-first-use-mobile.png', full_page=True)
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertEqual(writes, [origin + '/api/login'])
                    synthetic_status = client.get('/api/tasks').json()
                    page.route('**/api/tasks', lambda route: route.fulfill(json=synthetic_status))
                    synthetic_status.update(available=False, real_model=True)
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(page.get_by_test_id('pilot-engine')).to_have_text('Görev motoru kapalı; görev başlatılamaz.')
                    expect(panel.locator('li').nth(0)).to_contain_text('Bu oturumda kapalı')
                    synthetic_status.update(available=True, real_model=True, real_supervisor=True)
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(page.get_by_test_id('pilot-engine')).to_have_text('Gerçek yerel Decider · sabit deployment')
                    expect(panel.get_by_test_id('first-use-capability-vision_canvas')).to_contain_text('gerçek Bonsai gözlemci')
                    synthetic_status.update(kinds=['hello'])
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(panel.get_by_test_id('first-use-capability-browser_local_navigation')).to_contain_text('Bu oturumda kapalı')
                    expect(panel.get_by_test_id('first-use-capability-vision_canvas')).to_contain_text('Bu oturumda kapalı')
                    page.unroute('**/api/tasks')
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(page.get_by_label('Yerel oturum anahtarı')).to_be_visible()
                    expect(panel).to_have_count(0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

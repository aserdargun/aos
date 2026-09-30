import os
from pathlib import Path
import re
import tempfile
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1', 'Requires real Docker and built UI')
class TaskProgressUITests(unittest.TestCase):
    def test_registry_response_fixtures_distinguish_global_enablement_from_session_configuration(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='registry-label-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                overview = client.get('/api/overview').json()
                overview['trajectory'] = {'available': True, 'runs': [], 'models': [
                    {'model_id': 'synthetic-bonsai-base', 'backend': 'prism_llama_cpp_cuda', 'revision': 'synthetic-revision', 'enabled': 0},
                    {'model_id': 'synthetic-decider', 'backend': 'pytorch_cuda', 'revision': 'synthetic-revision', 'enabled': 0}
                ], 'deployments': [{'deployment_id': 'synthetic-experimental', 'model_id': 'synthetic-decider', 'status': 'EXPERIMENTAL'}]}
                tasks = client.get('/api/tasks').json()
                tasks.update(available=True, real_model=True, real_supervisor=True, kinds=['hello', 'browser_form', 'vision_canvas'])
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, mutations = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: mutations.append(request.url) if request.method == 'POST' else None)
                    page.route('**/api/overview', lambda route: route.fulfill(json=overview))
                    page.route('**/api/tasks', lambda route: route.fulfill(json=tasks))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Modeller', exact=True).click()
                    panel = page.get_by_test_id('model-registry')
                    configured = page.get_by_test_id('model-session-configuration')
                    expect(panel.get_by_text('Registry: genel kullanım için etkin değil', exact=False)).to_have_count(2)
                    expect(panel).to_contain_text('EXPERIMENTAL pilot, açık oturum ayarıyla')
                    expect(panel).to_contain_text('genel kullanıma etkinleştirilmemiş bir kaydı kullanabilir')
                    expect(configured).to_contain_text('Yerel Decider ile gerçek çıkarım seçili')
                    expect(configured).to_contain_text('Yerel Bonsai ile gerçek çıkarım seçili')
                    expect(configured).to_contain_text('Bunlar yapılandırma seçimleridir')
                    expect(configured).to_contain_text('bellekte yüklü veya servisinin sağlıklı olduğunu kanıtlamaz')
                    expect(panel.get_by_text('disabled', exact=True)).to_have_count(0)
                    page.screenshot(path='/tmp/aos-registry-configuration-fixture.png', full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    page.screenshot(path='/tmp/aos-registry-configuration-fixture-mobile.png', full_page=True)
                    overview['trajectory']['models'][0]['enabled'] = 1
                    tasks.update(real_model=False, real_supervisor=False)
                    expect(panel.get_by_text('Registry: etkin', exact=False)).to_have_count(1, timeout=8000)
                    expect(configured).to_contain_text('Sentetik test motoru seçili')
                    expect(configured).to_contain_text('Sentetik gözlemci seçili')
                    tasks.update(available=False, kinds=['hello'])
                    expect(configured).to_contain_text('Bu oturumda görev motoru kapalı', timeout=8000)
                    expect(configured).to_contain_text('Bu oturumda görsel görev açık değil')
                    self.assertEqual(mutations, [origin + '/api/login'])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(panel).to_have_count(0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_actual_hello_progress_preserves_pause_and_fresh_approval(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='task-progress-ui-', dir=REPO_ROOT / 'data') as temporary:
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
                    page.get_by_role('button', name='İlk göreve git', exact=True).click()
                    panel = page.get_by_test_id('task-progress')
                    expect(panel).to_have_count(0)
                    page.get_by_test_id('approve-all').uncheck()
                    page.get_by_role('button', name='Hello görevi başlat', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    expect(page.get_by_test_id('task-progress-current')).to_have_text('Eylem onayı bekleniyor')
                    tasks = client.get('/api/tasks').json()
                    previous_approval = tasks['approval']['approval_id']
                    progress = tasks['jobs'][0]['progress']
                    self.assertIsNotNone(progress['phase'])
                    self.assertEqual(progress['model_call_totals'], {
                        'system1': {'ok': 0, 'total': 0},
                        'system2': {'ok': 0, 'total': 0}})
                    expect(page.get_by_test_id('task-progress-phase')).to_contain_text(progress['phase'])
                    expect(page.get_by_test_id('task-progress-elapsed')).to_have_text(re.compile(r'[\d.,]+ sn'))
                    expect(panel).to_contain_text('Süre onay beklemeyi ve duraklatmayı içerir')
                    expect(page.get_by_test_id('task-model-call')).to_have_count(len(progress['model_calls']))
                    expect(panel).not_to_contain_text('yerel Decider')
                    expect(panel).not_to_contain_text('yerel Bonsai')
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    page.screenshot(path='/tmp/aos-task-progress-hello-approval.png', full_page=True)
                    page.get_by_role('button', name='Duraklat', exact=True).click()
                    expect(page.get_by_test_id('task-progress-current')).to_have_text('Duraklatıldı')
                    expect(page.get_by_test_id('approval')).to_have_count(0)
                    page.get_by_role('button', name='Devam et', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('filesystem.write')
                    expect(page.get_by_test_id('task-progress-current')).to_have_text('Eylem onayı bekleniyor')
                    self.assertNotEqual(client.get('/api/tasks').json()['approval']['approval_id'], previous_approval)
                    page.get_by_role('button', name='Onayla', exact=True).click()
                    expect(page.get_by_test_id('task-row').first).to_contain_text('succeeded')
                    expect(page.get_by_test_id('task-progress-current')).to_have_text('Tamamlandı')
                    expect(page.get_by_test_id('task-progress-phase')).to_contain_text('SUCCEEDED')
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    panel.evaluate("element => window.scrollBy(0, element.getBoundingClientRect().top - document.querySelector('.controlbar').getBoundingClientRect().height - 16)")
                    page.screenshot(path='/tmp/aos-task-progress-hello-mobile.png', full_page=True)
                    page.screenshot(path='/tmp/aos-task-progress-hello-mobile-viewport.png')
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(panel).to_have_count(0)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

    def test_response_fixtures_label_brief_phases_without_claiming_live_inference(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='task-progress-phases-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            with task_server(root, 'fixture', ('--browser-tasks', '--desktop-browser', '--vision-engine', 'fixture', '--desktop-vision')) as (origin, token, client, server), sync_playwright() as playwright:
                synthetic = client.get('/api/tasks').json()
                progress = {'phase': 'OBSERVE', 'state_version': 4, 'elapsed_ms': 12500.0, 'model_calls': [
                    {'role': 'system2', 'status': 'ok', 'latency_ms': 2300.0},
                    {'role': 'system1', 'status': 'timeout', 'latency_ms': 1200.0},
                    {'role': 'system1', 'status': 'cancelled', 'latency_ms': 100.0},
                    {'role': 'system2', 'status': 'error', 'latency_ms': 0.0}
                ]}
                job = {'job_id': 'synthetic-progress-rendering', 'run_id': None, 'kind': 'vision_canvas', 'status': 'running', 'real_model': 0, 'runtime_id': None, 'progress': progress}
                synthetic.update(busy=True, reserved=True, jobs=[job])
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                errors, connections, mutations = [], [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'tr')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('websocket', lambda connection: connections.append(connection))
                    page.on('request', lambda request: mutations.append(request.url) if request.method == 'POST' else None)
                    page.route('**/api/tasks', lambda route: route.fulfill(json=synthetic))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Yerel oturum anahtarı').fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    page.get_by_role('button', name='Bilgisayar', exact=True).click()
                    expect(page.locator('.computer-panel [role="status"]')).to_have_text('Canlı · 1280 × 800')
                    page.evaluate("() => { window.testProgressCanvas = document.querySelector('.remote-screen canvas'); }")
                    page.get_by_role('button', name='İlk göreve git', exact=True).click()
                    panel = page.get_by_test_id('task-progress')
                    current = page.get_by_test_id('task-progress-current')
                    expect(current).to_have_text('Gözlem alınıyor')
                    expect(page.get_by_test_id('task-progress-phase')).to_contain_text('OBSERVE · state 4')
                    expect(page.get_by_test_id('task-progress-elapsed')).to_have_text('12,5 sn')
                    expect(page.get_by_test_id('task-model-call')).to_have_count(4)
                    expect(page.get_by_test_id('task-model-call').nth(0)).to_have_text('System-2 · sentetik fixtureTamamlandı · 2.300,0 ms')
                    expect(page.get_by_test_id('task-model-call').nth(1)).to_contain_text('Zaman aşımı · 1.200,0 ms')
                    expect(page.get_by_test_id('task-model-call').nth(2)).to_contain_text('İptal · 100,0 ms')
                    expect(page.get_by_test_id('task-model-call').nth(3)).to_contain_text('Hata · 0,0 ms')
                    expect(panel).not_to_contain_text('yerel Decider')
                    expect(panel).not_to_contain_text('yerel Bonsai')
                    for phase, label in (
                        ('SUPERVISOR', 'Supervisor değerlendirmesi'), ('DECIDE', 'Karar seçiliyor'),
                        ('POLICY', 'Güvenlik kontrolü'), ('EXECUTE', 'Eylem uygulanıyor'),
                        ('VERIFY', 'Sonuç doğrulanıyor'), ('WAITING_HUMAN', 'İnsan yanıtı bekleniyor'),
                        ('REPLAN', 'Yeniden planlanıyor'), ('CREATED', 'Görev hazırlanıyor')
                    ):
                        progress['phase'] = phase
                        expect(current).to_have_text(label, timeout=8000)
                        expect(page.get_by_test_id('task-progress-phase')).to_contain_text(phase)
                    progress['phase'] = 'DECIDE'
                    for status, label in (
                        ('queued', 'Hazırlanıyor'), ('waiting_approval', 'Eylem onayı bekleniyor'),
                        ('paused', 'Duraklatıldı'), ('succeeded', 'Tamamlandı'),
                        ('failed', 'Başarısız'), ('cancelled', 'İptal edildi')
                    ):
                        job['status'] = status
                        expect(current).to_have_text(label, timeout=8000)
                        expect(page.get_by_test_id('task-progress-phase')).to_contain_text('DECIDE')
                    job['status'] = 'failed'
                    progress.update(phase='FAILED', failure_code='INVALID_OUTPUT')
                    expect(page.get_by_test_id('task-progress-failure')).to_contain_text(
                        'Doğrulanmış hata kodu: INVALID_OUTPUT · Model veya worker yanıtı geçersiz')
                    page.get_by_role('button', name='English', exact=True).click()
                    expect(page.get_by_test_id('task-progress-failure')).to_contain_text(
                        'Verified failure code: INVALID_OUTPUT · Model or worker response was invalid')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    job['status'] = 'running'
                    progress.pop('failure_code')
                    progress.update(phase='SUPERVISOR', model_calls=[{'role': 'system1', 'status': 'ok', 'latency_ms': float(position)} for position in range(10)])
                    expect(current).to_have_text('Supervisor değerlendirmesi', timeout=8000)
                    expect(page.get_by_test_id('task-model-call')).to_have_count(10)
                    panel.scroll_into_view_if_needed()
                    page.screenshot(path='/tmp/aos-task-progress-fixture-phases.png', full_page=True)
                    page.screenshot(path='/tmp/aos-task-progress-fixture-phases-viewport.png')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    panel.evaluate("element => window.scrollBy(0, element.getBoundingClientRect().top - document.querySelector('.controlbar').getBoundingClientRect().height - 16)")
                    page.screenshot(path='/tmp/aos-task-progress-fixture-mobile.png', full_page=True)
                    page.screenshot(path='/tmp/aos-task-progress-fixture-mobile-viewport.png')
                    progress.update(phase=None, state_version=None, elapsed_ms=None, model_calls=[])
                    expect(current).to_have_text('Çalışıyor · evre bilgisi yok', timeout=8000)
                    expect(page.get_by_test_id('task-progress-elapsed')).to_have_text('Ölçülmedi')
                    expect(page.get_by_test_id('task-model-empty')).to_be_visible()
                    del job['progress']
                    expect(panel).to_contain_text('Bu backend görev ilerlemesi ve süre ölçümü sağlamıyor', timeout=8000)
                    expect(page.get_by_test_id('task-progress-phase')).to_have_text('Evre kaydı yok')
                    self.assertTrue(page.evaluate("() => window.testProgressCanvas === document.querySelector('.remote-screen canvas')"))
                    self.assertEqual(len(connections), 1)
                    self.assertEqual(mutations, [origin + '/api/login'])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    page.unroute('**/api/tasks')
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    expect(panel).to_have_count(0)
                    self.assertTrue(all(connection.is_closed() for connection in connections))
                    self.assertEqual(errors, [])
                finally:
                    browser.close()

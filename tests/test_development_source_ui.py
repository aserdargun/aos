from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DEVELOPMENT_SOURCE_UI_TESTS') == '1',
                     'Opt-in isolated Chromium source delivery panel')
class DevelopmentSourceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='development-source-ui-', dir=REPO_ROOT / 'data'))
        (cls.root / 'index.html').write_text(
            '<html><head><title>AOS control center fixture</title></head><body><div id="root"></div><script type="module" src="/entry.tsx"></script></body></html>')
        (cls.root / 'entry.tsx').write_text('''import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Development} from '../../ui/src/Development';
import {setLanguage, useLanguage} from '../../ui/src/i18n';
import {api} from '../../ui/src/api';
import {OwnedSkillReuseGuide} from '../../ui/src/OwnedSkillReuseGuide';
import '../../ui/src/style.css';
function Harness() {useLanguage(); const navigations:string[] = [];
  const [mode, setMode] = useState('loading');
  const [refreshes, setRefreshes] = useState(0);
  const status = mode === 'loading' ? null : {available: true, busy: mode === 'approval', reserved: mode === 'approval',
    manager_session: 'app-' + 'f'.repeat(32), jobs: [{job_id: 'job-fixture', kind: 'hello',
      status: mode === 'approval' ? 'waiting_approval' : 'succeeded',
      progress: {phase: mode === 'approval' ? 'waiting_approval' : 'completed'}}],
    approval: mode === 'approval' ? {action: {tool: 'file.write'}} : null};
  return <><button onClick={() => setLanguage('tr')}>TR</button><button onClick={() => setLanguage('en')}>EN</button>
    <div data-testid="runtime-fixture-controls">{['fresh', 'stale', 'error', 'approval'].map(value =>
      <button key={value} onClick={() => setMode(value)}>{value}</button>)}<output data-testid="refresh-count">{refreshes}</output></div>
    <button data-testid="api-error-probe" onClick={async () => {
      const output = document.getElementById('api-error-output')!;
      output.textContent = '';
      try {await api('/api/tasks', {kind: 'hello'}); output.textContent = 'accepted';}
      catch (error) {output.textContent = error instanceof Error ? error.message : 'unexpected';}
    }}>Check task admission</button><output id="api-error-output"/>
    <Development tasks={status as any} overview={null} retention={null} formRepeats={null} webApplications={null}
      imageDraftCapability="checking" staticQueryCapability="checking"
      snapshot={mode === 'loading' ? null : {control: {owner: 'AGENT', status: 'running'}, runtime: {running: true}} as any}
      runtimeObservedAt={mode === 'loading' ? null : mode === 'stale' ? Date.now() - 60000 : Date.now()}
      runtimeError={mode === 'error'} onRefresh={() => {setRefreshes(value => value + 1); setMode('fresh');}}
      onNavigate={tab => {navigations.push(tab); (window as any).navigations = [...navigations];}} />
    <div data-testid="named-project-guide"><OwnedSkillReuseGuide
      release={{release_sha256: 'a'.repeat(64), selection_sha256: 'b'.repeat(64)} as any}
      managerScope={{project: 'learning-demo', port: 18766}}/></div>
    <div data-testid="invalid-project-guide"><OwnedSkillReuseGuide
      release={{release_sha256: 'a'.repeat(64), selection_sha256: 'b'.repeat(64)} as any}
      managerScope={{project: '../default', port: 8765}}/></div>
    {location.search.includes('restart') ? <section data-testid="named-restart-view"><Development
      tasks={{available: false, jobs: [], busy: false, approval: null, restart_quiesced: true,
        manager_scope: {project: 'learning-demo', port: 18766},
        manager_session: location.search.includes('missing') ? undefined : 'app-' + 'c'.repeat(32)} as any}
      overview={null} retention={null} formRepeats={null} webApplications={null}
      imageDraftCapability="checking" staticQueryCapability="checking" onNavigate={() => {}}/></section> : null}
  </>;}
createRoot(document.getElementById('root')!).render(<Harness/>);
''')
        configuration = {'root': str(cls.root), 'resolve': {'alias': [
            {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
            {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]},
            'build': {'outDir': str(cls.root / 'build')}}
        (cls.root / 'vite.config.mjs').write_text('export default ' + json.dumps(configuration) + ';')
        result = subprocess.run(['node', str(REPO_ROOT / 'ui/node_modules/vite/bin/vite.js'),
                                 'build', '--config', str(cls.root / 'vite.config.mjs')],
                                cwd=REPO_ROOT / 'ui', capture_output=True, text=True, timeout=60)
        (cls.root / 'build.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise AssertionError('Isolated Development panel build failed')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
            SimpleHTTPRequestHandler, directory=str(cls.root / 'build')))
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(executable_path=os.environ.get('AOS_TEST_CHROMIUM_PATH', str(
                REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell')),
                args=['--disable-gpu'])
        except BaseException:
            cls.playwright.stop()
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(3)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def test_source_delivery_and_open_gates_render_en_tr_without_effects(self):
        from playwright.sync_api import expect

        page = self.browser.new_page(viewport={'width': 1280, 'height': 900})
        self.addCleanup(page.close)
        errors = []
        api_requests = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def api_stub(route):
            api_requests.append((route.request.method, route.request.url))
            self.assertEqual(route.request.method, 'GET')
            self.assertTrue(route.request.url.endswith('/api/capability-checks'))
            route.fulfill(status=503, json={'detail': 'synthetic fixture unavailable'})

        page.route('**/api/**', api_stub)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        self.assertEqual(page.title(), 'AOS · Control center')
        expect(page.get_by_role('heading', name='Control center', exact=True)).to_be_visible()
        history = page.get_by_test_id('development-history')
        expect(history).not_to_be_visible()
        expect(page.get_by_test_id('development-journal')).to_be_visible()
        expect(page.get_by_test_id('development-runtime-status')).to_have_text('Waiting for the current session')
        page.get_by_test_id('development-runtime').get_by_role('button', name='Open tasks', exact=True).click()
        self.assertEqual(page.evaluate('window.navigations'), ['Görevler'])
        focus = page.get_by_test_id('development-current-focus')
        expect(focus).to_contain_text('Where are we now?')
        expect(focus).to_contain_text('Current native integration and user delivery still need acceptance')
        expect(focus).to_contain_text('Dated manual record — not live agent health')
        expect(focus.get_by_test_id('development-checkpoint-next')).to_contain_text('reviewed candidate')
        page.get_by_role('button', name='Evidence & implementation history', exact=True).click()
        expect(page.get_by_test_id('capability-evidence-state')).to_contain_text(
            'The test evidence source is unavailable; success is not assumed.')
        source = page.get_by_test_id('development-source-delivery')
        for text in ('Archived source development · 1 October 2026',
                     'Implemented in source and CPU/mock verified. This historical record does not prove the running session',
                     'Free-goal chain:', 'Knowledge usage report:', 'Setup:',
                     'Scientist single startup connection:',
                     'durable before-send audit is not GPU authority.',
                     'Scientist source verification:',
                     'CPU tests do not prove model execution or GPU handoff',
                     'Remaining integration: the shared Scientist capability/principal/cancel/drain contract',
                     'real GPU acceptance run only by the Scientist session',
                     'general two-application task acceptance also remains open.'):
            expect(source).to_contain_text(text)
        acceptance = page.get_by_test_id('development-acceptance')
        expect(acceptance).to_contain_text('not live runtime status, execution authority or an overall completion percentage')
        expect(page.get_by_test_id('development-acceptance-scientist')).to_contain_text(
            'The first real file→experiment→report workflow passed.')
        snapshot = json.loads((REPO_ROOT / 'docs/release_acceptance.json').read_text())
        page.get_by_role('button', name='Release checklist', exact=True).click()
        expect(page.get_by_test_id('development-acceptance-date').locator('time')).to_have_attribute('datetime', snapshot['observed_at'])
        for stage in snapshot['stages']:
            card = page.get_by_test_id('development-acceptance-' + stage['id'])
            expect(card).to_be_visible()
            card.locator('summary').click()
            for test_id in ('acceptance-source', 'acceptance-verification', 'acceptance-delivery', 'acceptance-next'):
                expect(card.get_by_test_id(test_id)).to_be_visible()
            for evidence in stage['evidence']:
                expect(card.get_by_test_id('acceptance-evidence')).to_contain_text(evidence)
        for test_id in ('development-release-acceptance', 'development-completed-percent', 'development-open-percent'):
            expect(page.get_by_test_id(test_id)).to_have_count(0)
        expect(page.get_by_test_id('development-swapp-deferred')).to_contain_text('not complete')
        for gate in (
            'W1 · Target URL, authorized test account, permitted tasks and data rights',
            'W1 · Network, origin, redirect and tenant boundaries for the real site',
            'W1 · Explicit execution grant, real task and independent outcome verification',
            'W1 · Authorized live S1/S2 event collection, redaction and retention',
            'W2 · Observed site knowledge, change detection and in-task retrieval',
            'W3 · Parameterized real skill tests, review and explicit activation',
            'W4 · Separately reviewed S1/S2 datasets and held-out evaluation',
            'W5 · S1 training, evaluation, promotion and rollback; S2 compatibility',
            'W6 · Repeated end-to-end success and speed acceptance on one application'):
            expect(page.locator('.development-gates')).to_contain_text(gate)
        expect(page.locator('.development-gates')).to_have_count(1)

        expect(page.get_by_test_id('development-scientist-report-history')).to_contain_text('Scientist report history:')
        expect(page.get_by_test_id('development-scientist-report-history')).to_contain_text('The live session was not updated.')
        expect(page.get_by_test_id('development-isolated-learning-project')).to_contain_text('Isolated learning project:')
        expect(page.get_by_test_id('development-isolated-learning-project')).to_contain_text('not native model, learning, or GPU acceptance')
        expect(page.get_by_test_id('development-public-cpu-applications')).to_contain_text('six individual approvals, one POST')
        expect(page.get_by_test_id('development-public-cpu-applications')).to_contain_text('not native S1/S2')
        expect(page.get_by_test_id('development-skill-record-recovery')).to_contain_text('CLI or Tasks/API')
        expect(page.get_by_test_id('development-skill-record-recovery')).to_contain_text('not automatically retried')
        page.get_by_role('button', name='TR', exact=True).click()
        expect(focus).to_contain_text('Şu an neredeyiz?')
        expect(focus).to_contain_text('Tarihli manuel kayıt; canlı ajan sağlığı değildir')
        expect(focus.get_by_test_id('development-checkpoint-next')).to_contain_text('tek incelenmiş adayda')
        translated_gates = (
            'W1 · Hedef URL, yetkili test hesabı, izinli görevler ve veri hakları',
            'W1 · Gerçek site için ağ, origin, yönlendirme ve tenant sınırları',
            'W1 · Açık yürütme izni, gerçek görev ve bağımsız sonuç doğrulaması',
            'W1 · İzinli canlı S1/S2 olay toplama, redaction ve retention',
            'W2 · Gözlenen site bilgisi, değişim algılama ve görev içi retrieval',
            'W3 · Parametreli gerçek skill testleri, review ve açık aktivasyon',
            'W4 · Ayrı incelenmiş S1/S2 veri setleri ve held-out değerlendirme',
            'W5 · S1 eğitim, değerlendirme, promotion ve rollback; S2 uyumluluk',
            'W6 · Tek uygulamada tekrarlı uçtan uca başarı ve hız kabulü')
        expect(source).to_contain_text('Bu tarihsel kayıt çalışan oturumun build kimliğini veya sürüm kabulünü kanıtlamaz.')
        expect(source).to_contain_text('Scientist tek başlangıç bağlantısı:')
        expect(source).to_contain_text('Scientist kaynak doğrulaması:')
        expect(page.get_by_test_id('development-scientist-report-history')).to_contain_text('Scientist rapor geçmişi:')
        expect(page.get_by_test_id('development-scientist-report-history')).to_contain_text('Canlı oturum güncellenmedi.')
        expect(page.get_by_test_id('development-isolated-learning-project')).to_contain_text('İzole öğrenme projesi:')
        expect(source).to_contain_text('CPU testleri model veya GPU devrini kanıtlamaz')
        expect(acceptance).to_contain_text('canlı runtime durumu, yürütme izni veya toplam tamamlanma yüzdesi değildir')
        expect(page.get_by_test_id('development-acceptance-swapp')).to_contain_text('son aşama')
        page.set_viewport_size({'width': 390, 'height': 844})
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        for gate in translated_gates:
            expect(page.locator('.development-gates')).to_contain_text(gate)
        page.get_by_role('button', name='Kanıt ve uygulama geçmişi', exact=True).click()
        source.get_by_role('button', name='Görevleri aç', exact=True).click()
        self.assertEqual(page.evaluate('window.navigations'), ['Görevler'])
        self.assertEqual(len(api_requests), 1)
        self.assertEqual(errors, [])

    def test_runtime_freshness_approval_navigation_and_read_only_refresh(self):
        from playwright.sync_api import expect

        page = self.browser.new_page(viewport={'width': 1440, 'height': 1100})
        self.addCleanup(page.close)
        errors = []
        requests = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        page.route('**/api/**', lambda route: (requests.append(route.request.method), route.fulfill(json={'available': False, 'items': []})))
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        runtime = page.get_by_test_id('development-runtime')
        expect(runtime).to_contain_text('Unavailable')
        expect(runtime).not_to_contain_text('No active work reported')
        page.get_by_test_id('development-status').screenshot(path='/tmp/aos-control-center-desktop.png')
        page.get_by_role('button', name='fresh', exact=True).click()
        expect(runtime).to_contain_text('Fresh observation')
        expect(runtime).to_contain_text('Last record · not an active task:')
        page.get_by_role('button', name='approval', exact=True).click()
        expect(runtime).to_contain_text('Work in progress')
        expect(runtime).to_contain_text('Awaiting approval')
        expect(runtime).not_to_contain_text('Last record · not an active task:')
        page.get_by_role('button', name='stale', exact=True).click()
        expect(runtime).to_contain_text('Stale observation')
        expect(runtime).to_contain_text('missing data does not mean idle')
        expect(runtime).not_to_contain_text('No active work reported')
        expect(runtime).not_to_contain_text('No pending approval')
        expect(runtime).not_to_contain_text('Desktop running')
        page.get_by_role('button', name='error', exact=True).click()
        expect(runtime).to_contain_text('Status unavailable')
        expect(runtime).not_to_contain_text('No active work reported')
        page.get_by_role('button', name='Refresh status', exact=True).click()
        expect(runtime).to_contain_text('Fresh observation')
        expect(page.get_by_test_id('refresh-count')).to_have_text('1')
        page.get_by_role('button', name='Open Scientist Lab', exact=True).click()
        self.assertEqual(page.evaluate('window.navigations'), ['Scientist'])
        page.get_by_role('button', name='Release checklist', exact=True).click()
        candidate = page.get_by_test_id('development-acceptance-candidate')
        candidate.locator('summary').focus()
        page.keyboard.press('Enter')
        expect(candidate.get_by_test_id('acceptance-next')).to_be_visible()
        page.keyboard.press('Enter')
        expect(candidate.get_by_test_id('acceptance-next')).not_to_be_visible()
        page.get_by_role('button', name='TR', exact=True).click()
        expect(page.get_by_role('heading', name='Kontrol merkezi', exact=True)).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        page.get_by_test_id('development-status').screenshot(path='/tmp/aos-control-center-mobile-tr.png')
        self.assertTrue(all(method == 'GET' for method in requests))
        self.assertEqual(errors, [])

    def test_task_admission_error_is_localized_without_detail_leak_or_retry(self):
        from playwright.sync_api import expect

        page = self.browser.new_page()
        self.addCleanup(page.close)
        submitted = []
        response = {'status': 409, 'body': json.dumps({
            'code': 'reconciliation_required', 'detail': 'PRIVATE-ERROR-MARKER'})}

        def respond(route):
            if route.request.method == 'POST':
                self.assertTrue(route.request.url.endswith('/api/tasks'))
                submitted.append(route.request.post_data_json)
                route.fulfill(status=response['status'], body=response['body'], content_type='application/json')
            else:
                route.fulfill(status=503, json={})

        page.route('**/api/**', respond)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        output = page.locator('#api-error-output')
        for language, message in [('EN', 'The task was not retried automatically.'),
                                  ('TR', 'Görev otomatik tekrarlanmadı.')]:
            page.get_by_role('button', name=language, exact=True).click()
            page.get_by_test_id('api-error-probe').click()
            expect(output).to_contain_text(message)
            expect(output).not_to_contain_text('PRIVATE-ERROR-MARKER')
        self.assertEqual(len(submitted), 2)
        page.get_by_role('button', name='EN', exact=True).click()
        for status, body in [(409, json.dumps({'code': 'unknown', 'detail': 'PRIVATE-ERROR-MARKER'})),
                             (409, '<html>PRIVATE-ERROR-MARKER</html>'),
                             (500, json.dumps({'code': 'reconciliation_required', 'detail': 'PRIVATE-ERROR-MARKER'}))]:
            response.update(status=status, body=body)
            page.get_by_test_id('api-error-probe').click()
            expect(output).to_contain_text(f'Request rejected ({status})')
            expect(output).not_to_contain_text('PRIVATE-ERROR-MARKER')
        self.assertEqual(submitted, [{'kind': 'hello'}] * 5)

    def test_named_project_reuse_commands_preserve_scope_without_effects(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        self.addCleanup(page.close)
        effects = []
        page.route('**/api/**', lambda route: (effects.append(route.request.method),
                                              route.fulfill(json={'available': False, 'items': []})))
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        guide = page.get_by_test_id('named-project-guide')
        for operation, test_id in (('preview-owned-skill-reuse', 'reuse-preview-command'),
                                   ('start', 'reuse-start-command')):
            expect(guide.get_by_test_id(test_id)).to_contain_text(
                f'./scripts/aos-v1 {operation} --project learning-demo --project-port 18766')
        expect(guide).to_contain_text('./scripts/aos-v1 token --project learning-demo --project-port 18766')
        expect(page.get_by_test_id('invalid-project-guide').get_by_test_id('skill-reuse-guide')).to_have_count(0)
        page.get_by_role('button', name='TR', exact=True).click()
        expect(guide.get_by_test_id('reuse-start-command')).to_contain_text('--project learning-demo --project-port 18766')
        self.assertTrue(all(method == 'GET' for method in effects))

    def test_restart_release_guidance_requires_named_scope_and_exact_session(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        self.addCleanup(page.close)
        effects = []
        page.route('**/api/**', lambda route: (effects.append(route.request.method),
                                              route.fulfill(json={'available': False, 'items': []})))
        origin = f'http://127.0.0.1:{self.server.server_port}/'
        page.goto(origin + '?restart')
        page.get_by_test_id('named-restart-view').get_by_role('button', name='Evidence & implementation history', exact=True).click()
        notice = page.get_by_test_id('named-restart-view').get_by_test_id('development-restart-quiesced')
        command = './scripts/aos-v1 release-restart --project learning-demo --project-port 18766 --expected-session app-' + 'c' * 32
        expect(notice.locator('code')).to_have_text(command)
        page.get_by_role('button', name='TR', exact=True).click()
        expect(notice.locator('code')).to_have_text(command)
        page.goto(origin + '?restart-missing')
        page.get_by_test_id('named-restart-view').get_by_role('button', name='Kanıt ve uygulama geçmişi', exact=True).click()
        notice = page.get_by_test_id('named-restart-view').get_by_test_id('development-restart-quiesced')
        expect(notice.locator('code')).to_have_count(0)
        self.assertTrue(all(method == 'GET' for method in effects))

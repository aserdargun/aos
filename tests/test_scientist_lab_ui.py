import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_SCIENTIST_UI_TESTS') == '1', 'Opt-in synthetic Chromium UI acceptance')
class ScientistLabUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='scientist-ui-qa-', dir=REPO_ROOT / 'data'))
        (cls.root / 'index.html').write_text('<html><head><title>Scientist panel synthetic QA</title></head><body><div id="root"></div><script type="module" src="/entry.tsx"></script></body></html>')
        (cls.root / 'entry.tsx').write_text('''import React from 'react';
import {createRoot} from 'react-dom/client';
import {ScientistLab} from '../../ui/src/ScientistLab';
import {setLanguage, useLanguage} from '../../ui/src/i18n';
import '../../ui/src/style.css';
function Harness() { useLanguage(); return <><button onClick={() => setLanguage('tr')}>TR</button><button onClick={() => setLanguage('en')}>EN</button><ScientistLab/></>; }
createRoot(document.getElementById('root')!).render(<Harness/>);
''')
        config = {'root': str(cls.root), 'resolve': {'alias': [
            {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
            {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]},
            'build': {'outDir': str(cls.root / 'build')}}
        (cls.root / 'vite.config.mjs').write_text('export default ' + json.dumps(config) + ';\n')
        built = subprocess.run(['pnpm', 'exec', 'vite', 'build', '--config', str(cls.root / 'vite.config.mjs')],
                               cwd=REPO_ROOT / 'ui', capture_output=True, text=True, timeout=60)
        (cls.root / 'build.log').write_text(built.stdout + built.stderr)
        if built.returncode:
            raise AssertionError('Synthetic panel build failed: ' + built.stderr)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(cls.root / 'build')))
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(args=['--disable-gpu'], executable_path=str(
                REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
        except BaseException:
            cls.playwright.stop()
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(3)
            raise
        print('Browser plugin not available; existing Python Playwright, owned synthetic static panel. Evidence:', cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    @staticmethod
    def enter_requested_budget(page, *, track='anomaly', experiments='1', wall_seconds='30', model_tokens='100'):
        page.get_by_label('Requested experiment track', exact=True).select_option(track)
        page.get_by_label('Requested experiment count', exact=True).fill(experiments)
        page.get_by_label('Requested duration (seconds)', exact=True).fill(wall_seconds)
        page.get_by_label('Requested model token limit', exact=True).fill(model_tokens)

    def test_requested_budget_is_explicit_bounded_previewed_and_not_host_caps(self):
        from playwright.sync_api import expect
        page = self.browser.new_page(viewport={'width': 1280, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type in {'error', 'warning'} else None)
        inventory = {'configured': True, 'joint_runtime_admitted': False, 'jobs': [],
                     'allowed_suites': ['synthetic.allowed.v1', 'synthetic.second.v1'],
                     'program_version': 'synthetic.v1'}
        posts = []

        def endpoint(route):
            if route.request.method == 'POST':
                posts.append((route.request.url.rsplit('/', 1)[-1], route.request.post_data_json))
                route.fulfill(json={'state': 'pending'})
            else:
                route.fulfill(json=inventory)

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            expect(page).to_have_title('AOS · Control center')
            page.get_by_text('Scientist experiments', exact=True).click()
            create = page.get_by_role('button', name='Create experiment proposal', exact=True)
            expect(create).to_be_disabled()
            expect(page.get_by_label('Requested experiment count', exact=True)).to_have_value('')
            expect(page.get_by_label('Requested duration (seconds)', exact=True)).to_have_value('')
            expect(page.get_by_label('Requested model token limit', exact=True)).to_have_value('')
            expect(page.get_by_test_id('scientist-proposal-preview')).to_have_count(0)
            expect(page.get_by_text('The server reports allowed suites; it does not report track permissions or effective budget caps. The requested scope is checked separately by the server.')).to_be_visible()
            self.enter_requested_budget(page, track='mode', experiments='2', wall_seconds='900', model_tokens='12000')
            page.get_by_label('Allowed experiment suite', exact=True).select_option('synthetic.second.v1')
            expected = {'suite': 'synthetic.second.v1', 'track': 'mode', 'program_version': 'synthetic.v1',
                        'budget': {'experiments': 2, 'wall_seconds': 900, 'model_tokens': 12000}}
            preview = page.get_by_test_id('scientist-proposal-preview')
            expect(preview).to_be_visible()
            self.assertEqual(json.loads(preview.locator('pre').inner_text()), expected)
            self.assertEqual(posts, [])
            for field, invalid_values, restored in [
                    ('Requested experiment count', ['0', '36', '1.5', '1e1', ''], '2'),
                    ('Requested duration (seconds)', ['0', '14401', '-1', ''], '900'),
                    ('Requested model token limit', ['-1', '350001', '0.5', ''], '12000')]:
                for invalid in invalid_values:
                    page.get_by_label(field, exact=True).fill(invalid)
                    expect(create).to_be_disabled()
                    expect(preview).to_have_count(0)
                page.get_by_label(field, exact=True).fill(restored)
                expect(create).to_be_enabled()
            page.screenshot(path='/tmp/aos-scientist-request-controls-20261003-en.png', full_page=True)
            create.click()
            expect(create).to_be_enabled()
            self.assertEqual(posts, [('propose', expected)])
            self.enter_requested_budget(page, experiments='35', wall_seconds='14400', model_tokens='0')
            expect(create).to_be_enabled()
            self.assertEqual(json.loads(preview.locator('pre').inner_text())['budget'],
                             {'experiments': 35, 'wall_seconds': 14400, 'model_tokens': 0})
            page.get_by_role('button', name='TR', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(page.get_by_label('İstenen deney sayısı', exact=True)).to_have_value('35')
            expect(page.get_by_text('İstenen önerinin önizlemesi', exact=True)).to_be_visible()
            expect(page.get_by_text('Bütçeyi açıkça girin. Öneri çalıştırma izni vermez; exact kapsam için ayrı insan onayı gerekir.')).to_be_visible()
            page.screenshot(path='/tmp/aos-scientist-request-controls-20261003-tr.png', full_page=True)
            self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
            self.assertEqual(posts, [('propose', expected)])
            self.assertEqual(errors, [])
            self.assertEqual(page.locator('vite-error-overlay').count(), 0)
        finally:
            page.close()

    def test_requested_proposal_rejects_missing_changed_or_invalid_host_metadata(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        inventory = {'configured': True, 'joint_runtime_admitted': False, 'jobs': [],
                     'allowed_suites': ['synthetic.allowed.v1', 'synthetic.second.v1'],
                     'program_version': 'synthetic.v1'}
        posts = []
        failed = {'value': False}

        def endpoint(route):
            if route.request.method == 'POST':
                posts.append(route.request.post_data_json)
                route.fulfill(json={})
            elif failed['value']:
                route.fulfill(status=503, json={'detail': 'Synthetic unavailable inventory'})
            else:
                route.fulfill(json=inventory)

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            page.get_by_text('Scientist experiments', exact=True).click()
            self.enter_requested_budget(page)
            page.get_by_label('Allowed experiment suite', exact=True).select_option('synthetic.second.v1')
            create = page.get_by_role('button', name='Create experiment proposal', exact=True)
            expect(create).to_be_enabled()
            inventory['allowed_suites'] = ['synthetic.allowed.v1']
            page.get_by_role('button', name='Refresh', exact=True).click()
            expect(create).to_be_disabled()
            page.get_by_label('Allowed experiment suite', exact=True).select_option('synthetic.allowed.v1')
            expect(create).to_be_enabled()
            for invalid in [None, '', 'x' * 65, ['synthetic.v1']]:
                inventory['program_version'] = invalid
                page.get_by_role('button', name='Refresh', exact=True).click()
                expect(create).to_be_disabled()
            inventory['program_version'] = 'synthetic.v1'
            for invalid in [None, [], ['bad suite'], ['synthetic.allowed.v1', 'synthetic.allowed.v1']]:
                inventory['allowed_suites'] = invalid
                page.get_by_role('button', name='Refresh', exact=True).click()
                expect(create).to_be_disabled()
            inventory['allowed_suites'] = ['synthetic.allowed.v1']
            page.get_by_role('button', name='Refresh', exact=True).click()
            expect(create).to_be_enabled()
            failed['value'] = True
            page.get_by_role('button', name='Refresh', exact=True).click()
            expect(page.get_by_text('Inventory refresh failed; displayed information may be stale.', exact=False)).to_be_visible()
            expect(create).to_be_disabled()
            self.assertEqual(posts, [])
        finally:
            page.close()

    def test_resolution_history_is_not_gpu_authority_and_missing_count_is_not_zero(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        calls = []
        inference = {'configured': False, 'admission_blocked': True, 'unresolved_count': 1,
            'resolved_count': 2, 'other_session_count': 0, 'local_cleanup_pending': False,
            'truncated': False, 'intents': [{'request_id': 'a' * 32, 'state': 'pending',
                'profile_id': 'aos.decider.turn.v1', 'deployment_digest': 'b' * 64,
                'request_sha256': 'c' * 64, 'owner': 'AGENT', 'generation': 0,
                'worker_unit': None, 'resolution_recorded': True}]}

        def endpoint(route):
            calls.append(route.request.method)
            route.fulfill(json={'configured': False, 'joint_runtime_admitted': False,
                                'jobs': [], 'inference': inference})

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            page.get_by_text('Scientist experiments', exact=True).click()
            expect(page.get_by_text('Historical resolution record count: 2', exact=True)).to_be_visible()
            page.get_by_text('aos.decider.turn.v1 · pending', exact=True).click()
            expect(page.get_by_text('This request has a resolution record; historical evidence is not new GPU authority.')).to_be_visible()
            expect(page.get_by_text('New task admission is blocked. Trusted GPU release verification is required; intents cannot be reset.')).to_be_visible()
            page.get_by_role('button', name='TR', exact=True).click()
            expect(page.get_by_text('Geçmiş çözüm kaydı sayısı: 2', exact=True)).to_be_visible()
            expect(page.get_by_text('Bu istek için çözüm kaydı var; geçmiş kayıt yeni GPU yetkisi değildir.')).to_be_visible()
            inference.pop('resolved_count')
            page.get_by_role('button', name='Yenile', exact=True).click()
            expect(page.get_by_text('Geçmiş çözüm kaydı sayısı:', exact=False)).to_have_count(0)
            self.assertTrue(calls and all(method == 'GET' for method in calls))
        finally:
            page.close()

    def test_explicit_saved_report_history_english_turkish(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        calls = []
        run_id = 'run-' + 'a' * 32
        remote_id = '11111111-1111-4111-8111-111111111111'
        report = {'run_id': remote_id, 'report_sha256': 'b' * 64,
                  'report': {'run_id': remote_id, 'status': 'completed', 'synthetic_result': 'PRIVATE_REPORT_BODY'}}
        metadata = {'readback_id': 'lab-readback-' + 'c' * 32, 'report_sha256': 'b' * 64,
                    'record_sha256': 'd' * 64, 'created_at': '2026-10-01T00:00:00Z', 'status': 'completed'}
        history = {'supported': True, 'available': True, 'items': [], 'truncated': False}
        inventory = {'configured': True, 'joint_runtime_admitted': False,
                     'allowed_suites': ['synthetic.allowed.v1'], 'program_version': 'synthetic.v1',
                     'jobs': [{'run_id': run_id, 'lab_run_id': remote_id, 'actions': [], 'readbacks': history}]}

        def endpoint(route):
            operation = route.request.url.rsplit('/', 1)[-1]
            if route.request.method == 'GET':
                route.fulfill(json=inventory)
                return
            body = route.request.post_data_json
            calls.append((operation, body))
            if operation == 'report':
                route.fulfill(json=report)
            elif operation == 'save_report':
                history['items'] = [metadata]
                route.fulfill(json=metadata)
            elif operation == 'read_saved_report':
                route.fulfill(json=dict(metadata, record={'report': report}))
            else:
                route.fulfill(status=400, json={'detail': 'Unexpected synthetic operation'})

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            page.get_by_text('Scientist experiments', exact=True).click()
            expect(page.get_by_text('Saved verified reports', exact=True)).to_be_visible()
            expect(page.get_by_text('PRIVATE_REPORT_BODY', exact=False)).to_have_count(0)
            self.assertEqual(calls, [])
            page.get_by_role('button', name='Independently verify report', exact=True).click()
            expect(page.get_by_test_id('scientist-report-retention')).to_be_visible()
            self.assertEqual([operation for operation, body in calls], ['report'])
            page.get_by_role('button', name='Save this exact report to private history', exact=True).click()
            expect(page.get_by_role('button', name='Read saved report', exact=True)).to_be_visible()
            self.assertEqual(calls[-1], ('save_report', {'run_id': run_id, 'expected_report_sha256': 'b' * 64}))
            page.get_by_role('button', name='TR', exact=True).click()
            expect(page.get_by_text('Kaydedilmiş doğrulanmış raporlar', exact=True)).to_be_visible()
            page.get_by_role('button', name='Kaydedilmiş raporu oku', exact=True).click()
            expect(page.get_by_test_id('scientist-historical-report')).to_be_visible()
            expect(page.get_by_test_id('scientist-report-retention')).to_have_count(0)
            self.assertEqual([operation for operation, body in calls], ['report', 'save_report', 'read_saved_report'])
            history.update(available=False, items=[])
            page.get_by_role('button', name='Yenile', exact=True).click()
            expect(page.get_by_text('Rapor geçmişi okunamıyor; boş veya güvenilir olduğu varsayılmaz.')).to_be_visible()
        finally:
            page.close()

    def test_control_counts_remain_metadata_and_missing_history_is_not_zero(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        calls = []
        control = {'available': True, 'supported': True, 'pending_count': 2,
            'current_session_pending_count': 1, 'other_session_pending_count': 1, 'metadata_only': True}

        def endpoint(route):
            calls.append(route.request.method)
            route.fulfill(json={'configured': False, 'joint_runtime_admitted': False, 'jobs': [],
                'inference': {'configured': False, 'admission_blocked': True, 'unresolved_count': 1,
                    'other_session_count': 0, 'local_cleanup_pending': False, 'truncated': False,
                    'intents': [], 'evidence_controls': dict(control)}})

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            page.get_by_text('Scientist experiments', exact=True).click()
            expect(page.get_by_text('Control requests without a recorded response: 2', exact=True)).to_be_visible()
            expect(page.get_by_text('Control journal metadata is informational only; a response record does not prove GPU release or resolve a task.')).to_be_visible()
            control.update(pending_count=0, current_session_pending_count=0, other_session_pending_count=0)
            page.get_by_role('button', name='Refresh', exact=True).click()
            expect(page.get_by_text('Control requests without a recorded response: 0', exact=True)).to_be_visible()
            expect(page.get_by_text('New task admission is blocked. Trusted GPU release verification is required; intents cannot be reset.')).to_be_visible()
            page.get_by_role('button', name='TR', exact=True).click()
            expect(page.get_by_text('Yanıtı kaydedilmemiş kontrol istekleri: 0', exact=True)).to_be_visible()
            control.update(supported=False, pending_count=None)
            page.get_by_role('button', name='Yenile', exact=True).click()
            expect(page.get_by_text('Bu veritabanında kontrol günlüğü yok; otomatik yükseltme yapılmaz.')).to_be_visible()
            expect(page.get_by_text('Yanıtı kaydedilmemiş kontrol istekleri: 0', exact=True)).to_have_count(0)
            control.update(available=False)
            page.get_by_role('button', name='Yenile', exact=True).click()
            expect(page.get_by_text('Kontrol günlüğü okunamıyor; sıfır bekleyen işlem varsayılmaz.')).to_be_visible()
            self.assertTrue(calls and all(method == 'GET' for method in calls))
        finally:
            page.close()

    def test_disabled_panel_english_turkish_desktop_mobile_no_side_effects(self):
        from playwright.sync_api import expect
        page = self.browser.new_page(viewport={'width': 1280, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type in {'error', 'warning'} else None)
        calls = []
        identity = {'available': True, 'present': False, 'record_sha256': None, 'binding_sha256': None}

        def endpoint(route):
            calls.append(route.request.method)
            route.fulfill(json={'configured': False, 'joint_runtime_admitted': False, 'jobs': [],
                'inference': {'configured': False, 'admission_blocked': True, 'unresolved_count': 2,
                    'unresolved_lab_effect_count': 1,
                    'other_session_count': 1, 'local_cleanup_pending': True, 'truncated': False,
                    'intents': [{'request_id': 'a' * 32, 'state': 'receipt_recorded',
                        'profile_id': 'aos.decider.turn.v1', 'deployment_digest': 'b' * 64,
                        'request_sha256': 'c' * 64, 'owner': 'AGENT', 'generation': 3,
                        'worker_unit': 'swapp-aos-gpu-turn-' + 'a' * 32 + '.service',
                        'admission_identity': dict(identity)}]}})

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            expect(page).to_have_title('AOS · Control center')
            page.get_by_text('Scientist experiments', exact=True).click()
            expect(page.get_by_text('Scientist connection is not configured; real experiment starts are disabled.')).to_be_visible()
            expect(page.get_by_text('New task admission is blocked. Trusted GPU release verification is required; intents cannot be reset.')).to_be_visible()
            expect(page.get_by_text('Unresolved Lab effects: 1', exact=True)).to_be_visible()
            page.get_by_text('aos.decider.turn.v1 · receipt_recorded', exact=True).click()
            expect(page.get_by_text('b' * 64, exact=True)).to_be_visible()
            expect(page.get_by_text('No admission identity is recorded for this intent; it is not automatically adopted.')).to_be_visible()
            expect(page.get_by_text('Admission identity history is informational only; it is not new authority or GPU verification.')).to_be_visible()
            page.screenshot(path=str(self.root / 'disabled-desktop.png'))
            page.get_by_role('button', name='TR', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(page.get_by_text('Scientist bağlantısı yapılandırılmadı; gerçek deney başlatma kapalı.')).to_be_visible()
            expect(page.get_by_text('Yeni görev kabulü kapalı. Güvenilir GPU bırakımı doğrulaması gerekir; intent sıfırlanamaz.')).to_be_visible()
            expect(page.get_by_text('Belirsiz Lab işlemleri: 1', exact=True)).to_be_visible()
            expect(page.get_by_text('Bu intent için kayıtlı kabul kimliği yok; otomatik benimseme yapılmaz.')).to_be_visible()
            identity.update(available=True, present=True, record_sha256='e' * 64, binding_sha256='f' * 64)
            page.get_by_role('button', name='Yenile', exact=True).click()
            expect(page.get_by_text('e' * 64, exact=True)).to_be_visible()
            expect(page.get_by_text('f' * 64, exact=True)).to_be_visible()
            expect(page.get_by_text('Kabul kimliği geçmişi yalnız bilgi amaçlıdır; yeni yetki veya GPU doğrulaması değildir.')).to_be_visible()
            identity.update(available=False, present=False)
            page.get_by_role('button', name='Yenile', exact=True).click()
            expect(page.get_by_text('Kabul kimliği geçmişi kullanılamıyor; güvenli kabul varsayılmaz.')).to_be_visible()
            expect(page.get_by_text('e' * 64, exact=True)).to_have_count(0)
            expect(page.get_by_text('f' * 64, exact=True)).to_have_count(0)
            page.get_by_role('button', name='EN', exact=True).click()
            expect(page.get_by_text('Admission identity history is unavailable; safe admission is not assumed.')).to_be_visible()
            identity.update(available=True, present=True, record_sha256=['e' * 64])
            page.get_by_role('button', name='Refresh', exact=True).click()
            expect(page.get_by_text('Admission identity history is unavailable; safe admission is not assumed.')).to_be_visible()
            expect(page.get_by_text('e' * 64, exact=True)).to_have_count(0)
            page.screenshot(path=str(self.root / 'disabled-mobile-tr.png'))
            self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
            self.assertEqual(errors, [])
            self.assertEqual(page.locator('vite-error-overlay').count(), 0)
            self.assertTrue(all(method == 'GET' for method in calls))
        finally:
            page.close()

    def test_exact_approval_then_uncertain_intent_hides_execute_and_never_retries(self):
        from playwright.sync_api import expect
        page = self.browser.new_page(viewport={'width': 1280, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        console_messages = []
        page.on('console', lambda message: console_messages.append(message.text) if message.type in {'error', 'warning'} else None)
        approval = {'action_id': 'action-' + 'a' * 32, 'state': 'pending', 'envelope_sha256': 'b' * 64,
            'expires_at': 4102444800, 'envelope': {'action': {'tool': 'lab.start'},
            'task': {'request': {'suite': 'synthetic.allowed.v1', 'budget': {'experiments': 1, 'wall_seconds': 30, 'model_tokens': 100}}}}}
        inventory = {'configured': True, 'joint_runtime_admitted': False,
            'allowed_suites': ['synthetic.allowed.v1'], 'program_version': 'synthetic.v1', 'jobs': []}
        posts = []

        def endpoint(route):
            operation = route.request.url.rsplit('/', 1)[-1]
            if route.request.method == 'GET':
                route.fulfill(json=inventory)
            else:
                posts.append((operation, route.request.post_data_json))
                if operation == 'propose':
                    inventory['jobs'] = [{'run_id': 'run-' + 'c' * 32, 'lab_run_id': None, 'actions': [approval]}]
                elif operation == 'approve':
                    approval['state'] = 'approved'
                elif operation == 'execute':
                    approval['state'] = 'intent'
                    route.fulfill(status=409, json={'detail': 'Synthetic uncertain outcome'})
                    return
                route.fulfill(json=approval)

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            page.get_by_text('Scientist experiments', exact=True).click()
            self.enter_requested_budget(page)
            page.get_by_role('button', name='Create experiment proposal', exact=True).click()
            expect(page.get_by_role('button', name='Approve this exact operation', exact=True)).to_be_visible()
            page.get_by_text('Exact operation scope', exact=True).click()
            expect(page.locator('details details pre')).to_contain_text('wall_seconds')
            page.get_by_role('button', name='Approve this exact operation', exact=True).click()
            expect(page.get_by_role('button', name='Execute approved operation', exact=True)).to_be_visible()
            self.assertEqual([operation for operation, _ in posts], ['propose', 'approve'])
            self.assertEqual(posts[1][1]['envelope_sha256'], approval['envelope_sha256'])
            page.get_by_role('button', name='Execute approved operation', exact=True).click()
            expect(page.get_by_text('Outcome uncertain; automatic retry is disabled.', exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='Execute approved operation', exact=True)).to_have_count(0)
            expect(page.locator('[data-testid="scientist-lab"] > pre')).to_have_count(0)
            page.get_by_role('button', name='Refresh', exact=True).click()
            self.assertEqual([operation for operation, _ in posts], ['propose', 'approve', 'execute'])
            page.screenshot(path=str(self.root / 'uncertain-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(self.root / 'uncertain-mobile.png'))
            self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
            rejected = dict(approval, action_id='action-' + 'd' * 32, state='rejected', rejection_reason='stale_controller')
            inventory['jobs'].append({'run_id': 'run-' + 'e' * 32, 'lab_run_id': None, 'actions': [rejected]})
            page.get_by_role('button', name='Refresh', exact=True).click()
            expect(page.get_by_text('The old approval was cancelled because control changed.', exact=True)).to_be_visible()
            expect(page.get_by_role('button', name='Execute approved operation', exact=True)).to_have_count(0)
            rejected['rejection_reason'] = 'approval_expired'
            page.get_by_role('button', name='Refresh', exact=True).click()
            expect(page.get_by_text('The expired approval was cancelled; fresh approval is required.', exact=True)).to_be_visible()
            page.get_by_role('button', name='TR', exact=True).click()
            expect(page.get_by_text('Süresi dolan onay iptal edildi; yeni onay gerekir.', exact=True)).to_be_visible()
            page.screenshot(path=str(self.root / 'rejected-mobile-tr.png'))
            self.assertEqual(errors, [])
            self.assertEqual(page.locator('vite-error-overlay').count(), 0)
            self.assertTrue(all('409' in message and 'Failed to load resource' in message for message in console_messages))
            (self.root / 'console-health.json').write_text(json.dumps({
                'page_errors': errors, 'expected_rejected_effect_console_messages': console_messages,
                'framework_overlay_count': page.locator('vite-error-overlay').count()}))
        finally:
            page.close()

    def test_expanded_inventory_polling_is_readonly_single_flight_stale_and_canceled_on_close(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        page.clock.install()
        calls = []
        held = []
        mode = {'value': 'success'}

        def endpoint(route):
            calls.append((route.request.method, route.request.url.rsplit('/', 1)[-1]))
            if mode['value'] == 'hold':
                held.append(route)
            elif mode['value'] == 'failure':
                route.fulfill(status=503, json={'detail': 'Synthetic inventory unavailable'})
            else:
                route.fulfill(json={'configured': False, 'joint_runtime_admitted': False, 'jobs': []})

        page.route('**/api/scientist/**', endpoint)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            expect(page.locator('[data-testid="scientist-inventory-refresh"] time')).to_have_count(1)
            initial = len(calls)
            page.clock.fast_forward(15000)
            self.assertEqual(len(calls), initial)
            page.get_by_text('Scientist experiments', exact=True).click()
            expect(page.get_by_text('Inventory refreshes read-only every 5 seconds while this panel is open.')).to_be_visible()
            page.wait_for_function('document.querySelector("[data-testid=scientist-lab]").open')
            expect(page.locator('[data-testid="scientist-inventory-refresh"] time')).to_be_visible()
            previous_time = page.locator('[data-testid="scientist-inventory-refresh"] time').get_attribute('datetime')
            mode['value'] = 'failure'
            page.clock.fast_forward(5000)
            expect(page.get_by_text('Inventory refresh failed; displayed information may be stale.', exact=False)).to_be_visible()
            self.assertEqual(page.locator('[data-testid="scientist-inventory-refresh"] time').get_attribute('datetime'), previous_time)
            page.get_by_role('button', name='TR', exact=True).click()
            expect(page.get_by_text('Envanter yenilenemedi; gösterilen bilgiler güncel olmayabilir.', exact=False)).to_be_visible()
            mode['value'] = 'success'
            page.clock.fast_forward(5000)
            expect(page.get_by_text('Envanter yenilenemedi; gösterilen bilgiler güncel olmayabilir.', exact=False)).to_have_count(0)
            mode['value'] = 'hold'
            page.clock.fast_forward(5000)
            page.wait_for_timeout(50)
            self.assertEqual(len(held), 1)
            held_count = len(calls)
            page.clock.fast_forward(15000)
            page.wait_for_timeout(50)
            self.assertEqual(len(calls), held_count)
            canceled = []
            page.on('requestfailed', lambda request: canceled.append(request.url))
            page.get_by_text('Scientist deneyleri', exact=True).click()
            page.wait_for_function('!document.querySelector("[data-testid=scientist-lab]").open')
            page.wait_for_timeout(50)
            self.assertTrue(any(url.endswith('/api/scientist/jobs') for url in canceled))
            held.pop().abort()
            page.clock.fast_forward(15000)
            self.assertEqual(len(calls), held_count)
            self.assertTrue(all(method == 'GET' and operation == 'jobs' for method, operation in calls))
        finally:
            page.close()

    def test_explicit_effect_cancels_inventory_and_suspends_polling_until_completion(self):
        from playwright.sync_api import expect
        page = self.browser.new_page()
        page.add_init_script('''
window.syntheticFetchEvents = [];
const originalFetch = window.fetch.bind(window);
window.fetch = (url, options) => {
  if (String(url).startsWith('/api/scientist/')) {
    if (options?.method === 'POST') window.syntheticFetchEvents.push('post');
    else options?.signal?.addEventListener('abort', () => window.syntheticFetchEvents.push('cancel'), {once: true});
  }
  return originalFetch(url, options);
};
''')
        page.clock.install()
        inventory = {'configured': True, 'joint_runtime_admitted': False, 'jobs': [],
                     'allowed_suites': ['synthetic.allowed.v1'], 'program_version': 'synthetic.v1'}
        calls = []
        held_reads = []
        held_posts = []
        events = []
        hold_reads = {'value': False}

        def endpoint(route):
            operation = route.request.url.rsplit('/', 1)[-1]
            calls.append((route.request.method, operation))
            if route.request.method == 'POST':
                events.append('post')
                held_posts.append(route)
            elif hold_reads['value']:
                held_reads.append(route)
            else:
                route.fulfill(json=inventory)

        page.route('**/api/scientist/**', endpoint)
        page.on('requestfailed', lambda request: events.append('cancel') if request.url.endswith('/api/scientist/jobs') else None)
        try:
            page.goto(f'http://127.0.0.1:{self.server.server_port}/')
            page.get_by_text('Scientist experiments', exact=True).click()
            self.enter_requested_budget(page)
            expect(page.get_by_role('button', name='Create experiment proposal', exact=True)).to_be_enabled()
            page.wait_for_timeout(50)
            hold_reads['value'] = True
            page.clock.fast_forward(5000)
            page.wait_for_timeout(50)
            self.assertEqual(len(held_reads), 1)
            page.get_by_role('button', name='Create experiment proposal', exact=True).click()
            page.wait_for_timeout(50)
            self.assertCountEqual(events, ['cancel', 'post'])
            self.assertEqual(page.evaluate('window.syntheticFetchEvents.slice(-2)'), ['cancel', 'post'])
            self.assertEqual(len(held_posts), 1)
            during_effect = len(calls)
            page.clock.fast_forward(15000)
            page.wait_for_timeout(50)
            self.assertEqual(len(calls), during_effect)
            expect(page.get_by_role('button', name='Create experiment proposal', exact=True)).to_be_disabled()
            hold_reads['value'] = False
            held_reads.pop().abort()
            held_posts.pop().fulfill(json={'synthetic_acknowledgment_only': True})
            expect(page.get_by_role('button', name='Create experiment proposal', exact=True)).to_be_enabled()
            self.assertEqual([operation for method, operation in calls if method == 'POST'], ['propose'])
            self.assertEqual(len(calls), during_effect + 1)
        finally:
            for route in held_reads + held_posts:
                route.abort()
            page.close()

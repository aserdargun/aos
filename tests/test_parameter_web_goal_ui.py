import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.web_goal_execution_binding import build_web_goal_execution_binding
from test_web_goal_execution_binding import source_fixture


@unittest.skipUnless(os.environ.get('AOS_WEB_GOAL_UI_TESTS') == '1', 'Opt-in synthetic Chromium panel')
class ParameterWebGoalUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='parameter-web-goal-ui-', dir=REPO_ROOT / 'data')
        cls.root = Path(cls.temporary.name)
        sources = source_fixture(cls.root / 'source', 'synthetic-crm-note', 'contact_name', 'Ada', '<b>literal</b>')
        cls.binding = build_web_goal_execution_binding(**sources).model_dump(mode='json')
        cls.checksum = digest(cls.binding)
        cls.review = {'available': True, 'status': 'review_required', 'binding_sha256': cls.checksum,
            'confirm_sha256': cls.binding['confirm_sha256'], 'field_count': 2, 'unresolved_intent': False,
            'independently_verified': False, 'training_ready': False, 'gpu_release_verified': False}
        (cls.root / 'index.html').write_text('<html><body><div id="root"></div><script type="module" src="/entry.tsx"></script></body></html>')
        (cls.root / 'entry.tsx').write_text('''import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ParameterWebGoal} from '../../ui/src/ParameterWebGoal';
import {setLanguage} from '../../ui/src/i18n';
const initial = ''' + json.dumps(cls.review) + ''';
function Harness() {const [control,setControl] = useState({owner:'AGENT',status:'running',lease_id:'synthetic-lease',generation:2});
const [status,setStatus] = useState<any>(initial); return <><button onClick={()=>setLanguage('en')}>EN</button>
<button onClick={()=>setLanguage('tr')}>TR</button>
<button onClick={()=>setControl({...control,owner:'HUMAN',generation:3})}>TAKEOVER</button>
<button onClick={()=>setStatus((window as any).journal)}>JOURNAL</button>
<ParameterWebGoal snapshot={{control,runtime:{runtime_id:'synthetic-runtime',running:true}} as any}
status={status} disabled={control.owner !== 'AGENT'}/></>;}
createRoot(document.getElementById('root')!).render(<Harness/>);''')
        configuration = {'root': str(cls.root), 'resolve': {'alias': [
            {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
            {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]},
            'build': {'outDir': str(cls.root / 'build')}}
        (cls.root / 'vite.config.mjs').write_text('export default ' + json.dumps(configuration) + ';')
        result = subprocess.run(['pnpm', 'exec', 'vite', 'build', '--config', str(cls.root / 'vite.config.mjs')],
            cwd=REPO_ROOT / 'ui', capture_output=True, text=True, timeout=60)
        if result.returncode:
            cls.temporary.cleanup()
            raise AssertionError(result.stdout + result.stderr)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(cls.root / 'build')))
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.temporary.cleanup()

    def page(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        page.get_by_role('button', name='EN', exact=True).click()
        return page

    def preview_response(self):
        return {'schema_version': '1.0', 'binding': self.binding, 'binding_canonical': canonical(self.binding),
            'binding_sha256': self.checksum, 'execution_authorized': False, 'training_ready': False, 'gpu_release_verified': False}

    def journal_response(self):
        return {'schema_version': '1.0', 'kind': 'web_goal_execution_journal_status', 'intent_sha256': 'a' * 64,
            'binding_sha256': self.checksum, 'confirmation_sha256': 'b' * 64, 'authority_sha256': 'c' * 64,
            'source_sha256': 'd' * 64, 'run_binding_sha256': 'e' * 64, 'run_ref': 'f' * 64,
            'receipt_sha256': None, 'status': 'awaiting_independent_verification', 'reserved': True,
            'confirmation_consumed': True, 'task_terminal_verified': False, 'record_outcome_verified': False,
            'replay_authorized': False, 'site_outcome_verified': False, 'training_ready': False, 'gpu_release_verified': False}

    def test_exact_preview_literal_map_and_separate_confirmation_no_auto_start(self):
        from playwright.sync_api import expect
        page = self.page()
        calls = []
        page.route('**/api/tasks/parameter-web-goal/**', lambda route: (calls.append(route.request.post_data_json), route.fulfill(json=self.preview_response())))
        self.assertEqual(calls, [])
        page.get_by_role('button', name='Preview parameters', exact=True).click()
        expect(page.get_by_text('<b>literal</b>', exact=True)).to_be_visible()
        self.assertEqual(page.locator('dd b').count(), 0)
        start = page.get_by_role('button', name='Start reviewed task', exact=True)
        expect(start).to_be_disabled()
        page.get_by_role('checkbox').check()
        expect(start).to_be_enabled()
        self.assertEqual(calls, [{'schema_version': '1.0', 'lease_id': 'synthetic-lease', 'generation': 2}])
        page.get_by_role('button', name='TR', exact=True).click()
        expect(page.get_by_role('button', name='İncelenen görevi başlat', exact=True)).to_be_visible()

    def test_start_failure_is_sticky_no_double_click_replay_and_human_report(self):
        from playwright.sync_api import expect
        page = self.page()
        calls = []
        def endpoint(route):
            calls.append(route.request.url.rsplit('/', 1)[1])
            if calls[-1] == 'preview':
                route.fulfill(json=self.preview_response())
            elif calls[-1] == 'start':
                self.assertTrue(route.request.post_data_json['human_confirmation'])
                self.assertEqual(route.request.post_data_json['confirm_sha256'], self.binding['confirm_sha256'])
                route.fulfill(status=409, json={'detail': 'Synthetic lost start acknowledgement'})
            else:
                self.assertEqual(route.request.post_data_json, {'schema_version': '1.0', 'intent_sha256': 'a' * 64})
                route.fulfill(json=self.journal_response())
        page.route('**/api/tasks/parameter-web-goal/**', endpoint)
        page.get_by_role('button', name='Preview parameters', exact=True).click()
        page.get_by_role('checkbox').check()
        page.get_by_role('button', name='Start reviewed task', exact=True).evaluate('(button)=>{button.click();button.click();}')
        expect(page.get_by_text('Start outcome uncertain. Do not restart; read the journal report.', exact=True)).to_be_visible()
        self.assertEqual(calls.count('start'), 1)
        expect(page.get_by_role('button', name='Start reviewed task', exact=True)).to_be_disabled()
        page.evaluate('(journal)=>window.journal=journal', self.journal_response())
        page.get_by_role('button', name='JOURNAL', exact=True).click()
        page.get_by_role('button', name='TAKEOVER', exact=True).click()
        page.get_by_role('button', name='Read journal report', exact=True).click()
        expect(page.get_by_text('Independent verification pending; confirmation consumed, no replay.', exact=True)).to_be_visible()
        self.assertEqual(calls, ['preview', 'start', 'report'])

    def test_preview_hash_mismatch_and_takeover_pending_response_are_rejected(self):
        from playwright.sync_api import expect
        page = self.page()
        response = self.preview_response() | {'binding_canonical': canonical(self.binding) + ' '}
        page.route('**/api/tasks/parameter-web-goal/preview', lambda route: route.fulfill(json=response))
        page.get_by_role('button', name='Preview parameters', exact=True).click()
        expect(page.get_by_text('Server response does not match the reviewed contract.', exact=True)).to_be_visible()
        self.assertEqual(page.get_by_role('checkbox').count(), 0)
        page.unroute('**/api/tasks/parameter-web-goal/preview')
        pending = []
        page.route('**/api/tasks/parameter-web-goal/preview', lambda route: pending.append(route))
        page.get_by_role('button', name='Preview parameters', exact=True).click()
        page.wait_for_timeout(30)
        page.get_by_role('button', name='TAKEOVER', exact=True).click()
        pending[0].fulfill(json=self.preview_response())
        page.wait_for_timeout(50)
        self.assertEqual(page.get_by_role('checkbox').count(), 0)

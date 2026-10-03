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


@unittest.skipUnless(os.environ.get('AOS_PARAMETER_PROJECT_UI_TESTS') == '1', 'Opt-in isolated Chromium fixture')
class OwnedParameterProjectUITests(unittest.TestCase):
    def test_read_only_english_turkish_and_fail_closed_receipt_states(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory(prefix='parameter-project-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<div id="root"></div><script type="module" src="/entry.tsx"></script>')
            (root / 'entry.tsx').write_text('''import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {OwnedParameterProject,ownedParameterProjectCanStart} from '../../ui/src/OwnedParameterProject';
import {setLanguage} from '../../ui/src/i18n';
function Harness(){const [status,setStatus]=useState<any>(null);
(window as any).update=setStatus;return <><button onClick={()=>setLanguage('en')}>EN</button>
<button onClick={()=>setLanguage('tr')}>TR</button><span data-testid="can-start">{String(ownedParameterProjectCanStart(status))}</span><OwnedParameterProject status={status}/></>}
createRoot(document.getElementById('root')!).render(<Harness/>);''')
            configuration = {'root': str(root), 'resolve': {'alias': [
                {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
                {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]},
                'build': {'outDir': str(root / 'build')}}
            (root / 'vite.config.mjs').write_text('export default ' + json.dumps(configuration))
            subprocess.run(['pnpm', 'exec', 'vite', 'build', '--config', str(root / 'vite.config.mjs')],
                           cwd=REPO_ROOT / 'ui', capture_output=True, check=True, timeout=60)
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(
                SimpleHTTPRequestHandler, directory=str(root / 'build')))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                        'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                    try:
                        page = browser.new_page()
                        requests = []
                        page.on('request', lambda request: requests.append(request.url) if '/api/' in request.url else None)
                        page.goto(f'http://127.0.0.1:{server.server_port}/')
                        page.get_by_role('button', name='EN', exact=True).click()
                        expect(page.get_by_role('alert')).to_have_text('Project evidence is unavailable.')
                        expect(page.get_by_test_id('can-start')).to_have_text('false')
                        status = {'schema_version': '1.0', 'kind': 'owned_parameter_project_manual_bootstrap_status',
                                  'entries': [], 'reserved': False}
                        status.update({name: False for name in ('released_skill_verified', 'native_model_verified',
                            'site_outcome_verified', 'activation_authorized', 'training_ready',
                            'gpu_release_verified', 'replay_authorized')})
                        page.evaluate('value=>window.update(value)', status)
                        expect(page.get_by_text('Source prepared; no task execution yet.', exact=True)).to_be_visible()
                        expect(page.get_by_test_id('can-start')).to_have_text('true')
                        entry = {'intent_sha256': 'a' * 64, 'job_id': 'job-' + 'b' * 32,
                                 'manifest_sha256': 'c' * 64, 'status': 'unresolved', 'receipt_sha256': None}
                        status.update(entries=[entry], reserved=True)
                        page.evaluate('value=>window.update(value)', status)
                        expect(page.get_by_text('Uncertain outcome: replay is blocked.', exact=True)).to_be_visible()
                        expect(page.get_by_test_id('can-start')).to_have_text('false')
                        entry.update(status='accepted_verified', receipt_sha256='d' * 64)
                        status['reserved'] = False
                        page.evaluate('value=>window.update(value)', status)
                        expect(page.get_by_text('Finite task and whole-record readback verified.', exact=True)).to_be_visible()
                        expect(page.get_by_test_id('can-start')).to_have_text('false')
                        page.get_by_role('button', name='TR', exact=True).click()
                        expect(page.get_by_text('Sonlu görev ve bütün alanların readback sonucu doğrulandı.', exact=True)).to_be_visible()
                        status['gpu_release_verified'] = True
                        page.evaluate('value=>window.update(value)', status)
                        expect(page.get_by_role('alert')).to_have_text('Proje kanıtı kullanılamıyor.')
                        self.assertEqual(requests, [])
                        self.assertEqual(page.get_by_test_id('owned-parameter-project').get_by_role('button').count(), 0)
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

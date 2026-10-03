import copy
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from aos.contracts import REPO_ROOT, canonical, digest


@unittest.skipUnless(os.environ.get('AOS_PARAMETER_SKILL_UI_TESTS') == '1', 'Opt-in isolated Chromium fixture')
class OwnedParameterSkillUITests(unittest.TestCase):
    def test_exact_review_lost_ack_human_read_and_malformed_evidence(self):
        from playwright.sync_api import expect, sync_playwright
        from aos.owned_parameter_skill_candidate import OwnedParameterSkillCandidateSession
        from test_owned_parameter_skill_candidate import create_accepted_bootstrap

        flags = {key: False for key in ('native_model_verified', 'activation_authorized', 'training_ready', 'gpu_release_verified')}
        bootstrap = create_accepted_bootstrap(self)
        session = OwnedParameterSkillCandidateSession(bootstrap.directory, bootstrap.manifest_sha256,
            bootstrap.database, bootstrap.journal_directory, bootstrap.candidate_directory)
        candidate, candidate_sha256 = session.preview(bootstrap.intent_sha256)
        envelope = {'schema_version': '1.0', 'candidate': candidate, 'candidate_canonical': canonical(candidate),
                    'candidate_sha256': digest(candidate), 'status': 'awaiting_manual_review', **flags}
        snapshot = {'runtime': {'runtime_id': 'fixture-runtime', 'running': True},
                    'control': {'owner': 'AGENT', 'status': 'running', 'lease_id': 'fixture-lease', 'generation': 2}}
        calls = []
        mutation = {'invalid': None}

        def handle(route):
            action = route.request.url.rsplit('/', 1)[-1]
            if action == 'state':
                route.fulfill(json=snapshot)
                return
            body = route.request.post_data_json
            calls.append((action, body))
            if action == 'publish':
                self.assertEqual(body, {'schema_version': '1.0', 'intent_sha256': bootstrap.intent_sha256,
                    'confirm_candidate_sha256': candidate_sha256, 'human_confirmation': 'PUBLISH_MANUAL_CANDIDATE',
                    'lease_id': 'fixture-lease', 'generation': 2})
                route.abort()
                return
            response = copy.deepcopy(envelope)
            if mutation['invalid'] == 'hash':
                response['candidate_sha256'] = 'b' * 64
            elif mutation['invalid'] == 'flag':
                response['training_ready'] = 0
            elif mutation['invalid'] == 'canonical':
                response['candidate_canonical'] = json.dumps(response['candidate'], sort_keys=True)
                response['candidate_sha256'] = hashlib.sha256(response['candidate_canonical'].encode()).hexdigest()
            elif mutation['invalid'] == 'native':
                response['candidate']['native_model_verified'] = True
                response['candidate_canonical'] = canonical(response['candidate'])
                response['candidate_sha256'] = digest(response['candidate'])
            elif mutation['invalid'] == 'scope':
                response['candidate']['scope']['account_role'] = 'foreign'
                response['candidate_canonical'] = canonical(response['candidate'])
                response['candidate_sha256'] = digest(response['candidate'])
            route.fulfill(json=response)

        with tempfile.TemporaryDirectory(prefix='parameter-skill-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<div id="root"></div><script type="module" src="/entry.tsx"></script>')
            (root / 'entry.tsx').write_text('''import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {OwnedParameterSkill} from '../../ui/src/OwnedParameterSkill';
import {setLanguage} from '../../ui/src/i18n';
function Harness(){const [snapshot,setSnapshot]=useState<any>(SNAPSHOT);
(window as any).update=setSnapshot;return <><button onClick={()=>setLanguage('en')}>EN</button>
<button onClick={()=>setLanguage('tr')}>TR</button><OwnedParameterSkill intentSha256={INTENT} snapshot={snapshot} disabled={snapshot.control.owner!=='AGENT'}/></>}
createRoot(document.getElementById('root')!).render(<Harness/>);'''.replace('SNAPSHOT', json.dumps(snapshot)).replace('INTENT', json.dumps(bootstrap.intent_sha256)))
            configuration = {'root': str(root), 'resolve': {'alias': [
                {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
                {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]},
                'build': {'outDir': str(root / 'build')}}
            (root / 'vite.config.mjs').write_text('export default ' + json.dumps(configuration))
            subprocess.run(['pnpm', 'exec', 'vite', 'build', '--config', str(root / 'vite.config.mjs')],
                           cwd=REPO_ROOT / 'ui', capture_output=True, check=True, timeout=60)
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(root / 'build')))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                        'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                    try:
                        page = browser.new_page()
                        page.route('**/api/**', handle)
                        page.goto(f'http://127.0.0.1:{server.server_port}/')
                        page.get_by_role('button', name='EN', exact=True).click()
                        expect(page.get_by_role('heading', name='Manual bootstrap skill candidate')).to_be_visible()
                        self.assertEqual(calls, [])
                        for invalid in ('hash', 'flag', 'scope', 'canonical', 'native'):
                            mutation['invalid'] = invalid
                            page.get_by_test_id('parameter-skill-preview').click()
                            expect(page.get_by_test_id('parameter-skill-message')).to_have_text('Candidate response or source could not be verified.')
                            expect(page.get_by_test_id('parameter-skill-confirm')).to_have_count(0)
                        mutation['invalid'] = None
                        page.get_by_test_id('parameter-skill-preview').click()
                        expect(page.get_by_test_id('parameter-skill-confirm')).not_to_be_checked()
                        expect(page.get_by_test_id('parameter-skill-publish')).to_be_disabled()
                        page.get_by_role('button', name='TR', exact=True).click()
                        expect(page.get_by_role('heading', name='Manuel bootstrap skill adayı')).to_be_visible()
                        page.get_by_test_id('parameter-skill-confirm').check()
                        page.get_by_test_id('parameter-skill-publish').click()
                        expect(page.get_by_test_id('parameter-skill-uncertain')).to_be_visible()
                        expect(page.get_by_test_id('parameter-skill-preview')).to_be_disabled()
                        snapshot['control'].update(owner='HUMAN', generation=3, lease_id='human-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        expect(page.get_by_test_id('parameter-skill-uncertain')).to_be_visible()
                        expect(page.get_by_test_id('parameter-skill-read')).to_be_enabled()
                        page.get_by_test_id('parameter-skill-read').click()
                        expect(page.get_by_test_id('parameter-skill-result')).to_be_visible()
                        expect(page.get_by_test_id('parameter-skill-uncertain')).to_have_count(0)
                        expect(page.get_by_test_id('parameter-skill-preview')).to_be_disabled()
                        self.assertEqual([action for action, _ in calls].count('publish'), 1)
                        self.assertEqual(calls[-1], ('read', {'schema_version': '1.0', 'candidate_sha256': digest(candidate)}))
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

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


@unittest.skipUnless(os.environ.get('AOS_PARAMETER_SKILL_REVIEW_UI_TESTS') == '1', 'Opt-in isolated Chromium fixture')
class OwnedParameterSkillReviewUITests(unittest.TestCase):
    def test_actual_manual_review_revocation_exact_hashes_and_uncertain_human_read(self):
        from playwright.sync_api import expect, sync_playwright
        from aos.owned_parameter_skill_candidate import OwnedParameterSkillCandidateSession
        from aos.owned_parameter_skill_review import OwnedParameterSkillReviewSession
        from test_owned_parameter_skill_candidate import create_accepted_bootstrap

        bootstrap = create_accepted_bootstrap(self)
        candidates = OwnedParameterSkillCandidateSession(bootstrap.directory, bootstrap.manifest_sha256,
            bootstrap.database, bootstrap.journal_directory, bootstrap.candidate_directory)
        _, candidate_hash = candidates.preview(bootstrap.intent_sha256)
        candidates.publish(bootstrap.intent_sha256, candidate_hash, 'PUBLISH_MANUAL_CANDIDATE')
        reviews = OwnedParameterSkillReviewSession(candidates, bootstrap.root / 'manual-skill-reviews')
        _, review_hash = reviews.preview(candidate_hash)
        snapshot = {'runtime': {'runtime_id': 'fixture-runtime', 'running': True},
                    'control': {'owner': 'AGENT', 'status': 'running', 'lease_id': 'fixture-lease', 'generation': 2}}
        calls = []
        mutation = {'invalid': None, 'lose_accept': False, 'lose_revoke': True, 'lose_recover': False, 'stale_state': False}
        flags = {key: False for key in ('native_model_verified', 'activation_authorized', 'execution_authorized', 'training_ready', 'gpu_release_verified')}

        def handle(route):
            action = route.request.url.rsplit('/', 1)[-1]
            if action == 'state':
                state = copy.deepcopy(snapshot)
                if mutation['stale_state']:
                    state['control']['generation'] += 1
                route.fulfill(json=state)
                return
            body = route.request.post_data_json
            calls.append((action, body))
            if action == 'preview':
                record, checksum = reviews.preview(body['candidate_sha256'])
                kind = 'review'
            elif action == 'accept':
                self.assertEqual(body, {'schema_version': '1.0', 'candidate_sha256': candidate_hash,
                    'confirm_review_sha256': review_hash, 'human_confirmation': 'ACCEPT_MANUAL_REVIEW',
                    'lease_id': snapshot['control']['lease_id'], 'generation': snapshot['control']['generation']})
                record, checksum = reviews.accept(body['candidate_sha256'], body['confirm_review_sha256'], body['human_confirmation'])
                kind = 'review'
                if mutation['lose_accept']:
                    route.abort()
                    return
            elif action == 'read':
                try:
                    record, checksum = reviews.read(body['review_sha256'])
                except ValueError:
                    route.fulfill(status=409, json={'error': 'review_record_missing_or_changed'})
                    return
                kind = 'status'
            elif action == 'revoke-preview':
                record, checksum = reviews.revocation_preview(body['review_sha256'])
                kind = 'revocation'
            elif action == 'revoke':
                preview, checksum = reviews.revocation_preview(review_hash)
                self.assertEqual(body, {'schema_version': '1.0', 'review_sha256': review_hash,
                    'confirm_revocation_sha256': checksum, 'human_confirmation': 'REVOKE_MANUAL_REVIEW',
                    'lease_id': snapshot['control']['lease_id'], 'generation': snapshot['control']['generation']})
                record, checksum = reviews.revoke(body['review_sha256'], body['confirm_revocation_sha256'], body['human_confirmation'])
                kind = 'revocation'
                if mutation['lose_revoke']:
                    route.abort()
                    return
            elif action == 'recovery-preview':
                try:
                    record, checksum = reviews.recovery_preview(body['record_sha256'])
                except ValueError:
                    route.fulfill(status=409, json={'error': 'recovery_unavailable'})
                    return
                kind = 'recovery'
            elif action == 'recover':
                _, checksum = reviews.recovery_preview(body['record_sha256'])
                self.assertEqual(body, {'schema_version': '1.0', 'record_sha256': body['record_sha256'],
                    'confirm_recovery_sha256': checksum, 'human_confirmation': 'RESTORE_ANCHORED_REVIEW_RECORD',
                    'lease_id': snapshot['control']['lease_id'], 'generation': snapshot['control']['generation']})
                record, checksum = reviews.recover(body['record_sha256'], body['confirm_recovery_sha256'], body['human_confirmation'])
                kind = 'recovery'
                if mutation['lose_recover']:
                    route.abort()
                    return
            else:
                self.fail(action)
            response = {'schema_version': '1.0', kind: record, kind + '_canonical': canonical(record), kind + '_sha256': checksum, **flags}
            invalid = mutation['invalid']
            if invalid == 'hash':
                response[kind + '_sha256'] = 'b' * 64
            elif invalid == 'canonical':
                response[kind + '_canonical'] = json.dumps(record, sort_keys=True)
                response[kind + '_sha256'] = hashlib.sha256(response[kind + '_canonical'].encode()).hexdigest()
            elif invalid in ('native', 'scope', 'flag', 'kind', 'record'):
                if invalid == 'scope':
                    record['scope']['account_role'] = 'foreign'
                elif invalid == 'flag':
                    record['activation_authorized'] = 0
                elif invalid == 'kind':
                    record['kind'] = 'released_skill_review'
                elif invalid == 'record':
                    record['record']['reviewed'] = not record['record']['reviewed']
                else:
                    record['native_model_verified'] = True
                response[kind + '_canonical'] = canonical(record)
                response[kind + '_sha256'] = digest(record)
            route.fulfill(json=response)

        with tempfile.TemporaryDirectory(prefix='parameter-skill-review-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<div id="root"></div><script type="module" src="/entry.tsx"></script>')
            (root / 'entry.tsx').write_text('''import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {OwnedParameterSkillReview} from '../../ui/src/OwnedParameterSkillReview';
import {setLanguage} from '../../ui/src/i18n';
function Harness(){const [snapshot,setSnapshot]=useState<any>(SNAPSHOT);
(window as any).update=setSnapshot;return <><button onClick={()=>setLanguage('en')}>EN</button>
<button onClick={()=>setLanguage('tr')}>TR</button><OwnedParameterSkillReview snapshot={snapshot} disabled={snapshot.control.owner!=='AGENT'}/></>}
createRoot(document.getElementById('root')!).render(<Harness/>);'''.replace('SNAPSHOT', json.dumps(snapshot)))
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
                        expect(page.get_by_role('heading', name='Human review of manual candidate')).to_be_visible()
                        self.assertEqual(calls, [])
                        page.get_by_test_id('manual-review-candidate-hash').fill(candidate_hash)
                        for invalid in ('hash', 'canonical', 'native', 'scope', 'flag', 'kind'):
                            mutation['invalid'] = invalid
                            page.get_by_test_id('manual-review-preview').click()
                            expect(page.get_by_test_id('manual-review-message')).to_have_text('Review response could not be verified.')
                            expect(page.get_by_test_id('manual-review-confirm')).to_have_count(0)
                        mutation['invalid'] = None
                        page.get_by_test_id('manual-review-preview').click()
                        expect(page.get_by_test_id('manual-review-confirm')).not_to_be_checked()
                        expect(page.get_by_test_id('manual-review-accept')).to_be_disabled()
                        expect(page.get_by_test_id('manual-review-revoke-preview')).to_be_disabled()
                        mutation['stale_state'] = True
                        page.get_by_test_id('manual-review-confirm').check()
                        page.get_by_test_id('manual-review-accept').click()
                        expect(page.get_by_test_id('manual-review-message')).to_have_text('Review response could not be verified.')
                        self.assertEqual([action for action, _ in calls].count('accept'), 0)
                        mutation['stale_state'] = False
                        page.get_by_test_id('manual-review-preview').click()
                        page.get_by_test_id('manual-review-confirm').check()
                        page.get_by_test_id('manual-review-accept').click()
                        expect(page.get_by_test_id('manual-review-message')).to_have_text('Manual review acceptance verified.')
                        expect(page.get_by_test_id('manual-review-revoke-preview')).to_be_disabled()
                        page.get_by_test_id('manual-review-read').click()
                        expect(page.get_by_test_id('manual-review-revoke-preview')).to_be_enabled()
                        page.get_by_test_id('manual-review-revoke-preview').click()
                        expect(page.get_by_test_id('manual-review-revoke-confirm')).not_to_be_checked()
                        page.get_by_role('button', name='TR', exact=True).click()
                        expect(page.get_by_role('heading', name='Manuel aday için insan incelemesi')).to_be_visible()
                        page.get_by_test_id('manual-review-revoke-confirm').check()
                        page.get_by_test_id('manual-review-revoke').click()
                        expect(page.get_by_test_id('manual-review-uncertain')).to_be_visible()
                        snapshot['control'].update(owner='HUMAN', generation=3, lease_id='human-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        expect(page.get_by_test_id('manual-review-uncertain')).to_be_visible()
                        expect(page.get_by_test_id('manual-review-read')).to_be_enabled()
                        page.get_by_test_id('manual-review-read').click()
                        expect(page.get_by_test_id('manual-review-uncertain')).to_have_count(0)
                        expect(page.get_by_test_id('manual-review-result')).to_contain_text('"status":"revoked"')
                        expect(page.get_by_test_id('manual-review-revoke-preview')).to_be_disabled()
                        self.assertEqual([action for action, _ in calls].count('accept'), 1)
                        self.assertEqual([action for action, _ in calls].count('revoke'), 1)
                        revocation_path, = (bootstrap.root / 'manual-skill-reviews').glob('*.revoke.json')
                        revoked_record_hash = revocation_path.name.split('.')[0]
                        revocation_path.unlink()
                        reviews = OwnedParameterSkillReviewSession(candidates, bootstrap.root / 'manual-skill-reviews')
                        page.get_by_test_id('manual-review-read').click()
                        expect(page.get_by_test_id('manual-review-result')).to_have_count(0)
                        page.get_by_test_id('manual-review-record-hash').fill('b' * 64)
                        page.get_by_test_id('manual-review-recovery-preview').click()
                        expect(page.get_by_test_id('manual-review-recovery-confirm')).to_have_count(0)
                        page.get_by_test_id('manual-review-record-hash').fill(revoked_record_hash)
                        for invalid in ('hash', 'canonical', 'native', 'scope', 'flag', 'kind', 'record'):
                            mutation['invalid'] = invalid
                            page.get_by_test_id('manual-review-recovery-preview').click()
                            expect(page.get_by_test_id('manual-review-message')).to_have_text('İnceleme yanıtı doğrulanamadı.')
                            expect(page.get_by_test_id('manual-review-recovery-confirm')).to_have_count(0)
                        mutation['invalid'] = None
                        page.get_by_test_id('manual-review-recovery-preview').click()
                        expect(page.get_by_test_id('manual-review-recovery-confirm')).not_to_be_checked()
                        expect(page.get_by_test_id('manual-review-recovery-confirm')).to_be_disabled()
                        self.assertFalse(revocation_path.exists())
                        snapshot['control'].update(owner='AGENT', generation=4, lease_id='recovery-agent-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        page.get_by_test_id('manual-review-recovery-preview').click()
                        page.get_by_test_id('manual-review-recovery-confirm').check()
                        mutation['stale_state'] = True
                        page.get_by_test_id('manual-review-recover').click()
                        expect(page.get_by_test_id('manual-review-message')).to_have_text('İnceleme yanıtı doğrulanamadı.')
                        self.assertEqual([action for action, _ in calls].count('recover'), 0)
                        mutation['stale_state'] = False
                        page.get_by_test_id('manual-review-recovery-preview').click()
                        page.get_by_test_id('manual-review-recovery-confirm').check()
                        page.get_by_test_id('manual-review-recover').click()
                        expect(page.get_by_test_id('manual-review-message')).to_have_text('Özgün kayıt geri yüklendi; kabul veya iptal durumunu exact inceleme hash’iyle yeniden okuyun.')
                        self.assertTrue(revocation_path.exists())
                        page.get_by_test_id('manual-review-read').click()
                        expect(page.get_by_test_id('manual-review-result')).to_contain_text('"status":"revoked"')
                        reviews = OwnedParameterSkillReviewSession(candidates, bootstrap.root / 'second-manual-reviews')
                        _, review_hash = reviews.preview(candidate_hash)
                        mutation.update(lose_accept=True, lose_revoke=False)
                        snapshot['control'].update(owner='AGENT', generation=2, lease_id='fixture-lease')
                        page.reload()
                        page.get_by_role('button', name='EN', exact=True).click()
                        page.get_by_test_id('manual-review-candidate-hash').fill(candidate_hash)
                        page.get_by_test_id('manual-review-preview').click()
                        page.get_by_test_id('manual-review-confirm').check()
                        page.get_by_test_id('manual-review-accept').click()
                        expect(page.get_by_test_id('manual-review-uncertain')).to_be_visible()
                        snapshot['control'].update(owner='HUMAN', generation=3, lease_id='human-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        expect(page.get_by_test_id('manual-review-uncertain')).to_be_visible()
                        page.get_by_test_id('manual-review-read').click()
                        expect(page.get_by_test_id('manual-review-uncertain')).to_have_count(0)
                        expect(page.get_by_test_id('manual-review-result')).to_contain_text('"status":"accepted"')
                        snapshot['control'].update(owner='AGENT', generation=4, lease_id='fresh-agent-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        page.get_by_test_id('manual-review-preview').click()
                        page.get_by_test_id('manual-review-confirm').check()
                        expect(page.get_by_test_id('manual-review-accept')).to_be_disabled()
                        self.assertEqual([action for action, _ in calls].count('accept'), 2)
                        acceptance_path = bootstrap.root / 'second-manual-reviews' / (review_hash + '.accept.json')
                        acceptance_path.unlink()
                        reviews = OwnedParameterSkillReviewSession(candidates, bootstrap.root / 'second-manual-reviews')
                        page.get_by_test_id('manual-review-read').click()
                        expect(page.get_by_test_id('manual-review-result')).to_have_count(0)
                        page.get_by_test_id('manual-review-record-hash').fill(review_hash)
                        page.get_by_test_id('manual-review-recovery-preview').click()
                        page.get_by_test_id('manual-review-recovery-confirm').check()
                        mutation['lose_recover'] = True
                        page.get_by_test_id('manual-review-recover').click()
                        expect(page.get_by_test_id('manual-review-uncertain')).to_be_visible()
                        page.get_by_test_id('manual-review-recovery-preview').click()
                        expect(page.get_by_test_id('manual-review-recovery-confirm')).to_have_count(0)
                        snapshot['control'].update(owner='HUMAN', generation=5, lease_id='recovery-human-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        expect(page.get_by_test_id('manual-review-uncertain')).to_be_visible()
                        page.get_by_test_id('manual-review-read').click()
                        expect(page.get_by_test_id('manual-review-uncertain')).to_have_count(0)
                        expect(page.get_by_test_id('manual-review-result')).to_contain_text('"status":"accepted"')
                        self.assertEqual([action for action, _ in calls].count('recover'), 2)
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

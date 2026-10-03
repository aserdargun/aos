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


@unittest.skipUnless(os.environ.get('AOS_PARAMETER_SKILL_RELEASE_UI_TESTS') == '1', 'Opt-in isolated Chromium fixture')
class OwnedParameterSkillReleaseUITests(unittest.TestCase):
    def test_pinned_source_release_select_historical_inventory_and_own_source_rollback(self):
        from playwright.sync_api import expect, sync_playwright
        from test_owned_parameter_skill_release import create_shared_reviewed_bootstraps

        first, second = create_shared_reviewed_bootstraps(self)
        session = first.releases
        _, first_release_hash = session.preview(first.review_sha256, None)
        snapshot = {'runtime': {'runtime_id': 'fixture-runtime', 'running': True},
                    'control': {'owner': 'AGENT', 'status': 'running', 'lease_id': 'fixture-lease', 'generation': 2}}
        flags = {key: False for key in ('native_model_verified', 'execution_authorized', 'activation_authorized', 'training_ready', 'gpu_release_verified')}
        calls = []
        mutation = {'invalid': None, 'lose_release': True, 'lose_rollback': True,
                    'lose_recovery': False, 'stale_state': False}
        effects_before = {table: digest([dict(row) for row in first.source.store.connection.execute(
            'SELECT * FROM ' + table + ' ORDER BY rowid')]) for table in ('actions', 'model_calls', 'desktop_tasks', 'trajectory_labels')}

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
            try:
                if action == 'inventory':
                    response = {'schema_version': '1.0', 'inventory': session.inventory(), **flags}
                    if mutation['invalid'] == 'inventory':
                        response['inventory']['source_current_verified'] = True
                    route.fulfill(json=response)
                    return
                if action == 'preview':
                    record, checksum = session.preview(body['review_sha256'], body['expected_parent_release_sha256'])
                    kind = 'release'
                elif action == 'release':
                    self.assertEqual(body, {'schema_version': '1.0', 'review_sha256': first.review_sha256,
                        'expected_parent_release_sha256': None, 'confirm_release_sha256': first_release_hash,
                        'human_confirmation': 'RELEASE_MANUAL_SKILL', 'lease_id': snapshot['control']['lease_id'],
                        'generation': snapshot['control']['generation']})
                    record, checksum = session.release(body['review_sha256'], body['expected_parent_release_sha256'],
                        body['confirm_release_sha256'], body['human_confirmation'])
                    kind = 'release'
                    if mutation['lose_release']:
                        route.abort()
                        return
                elif action == 'read':
                    record, checksum = session.read(body['release_sha256'])
                    kind = 'release'
                elif action == 'recovery-preview':
                    record, checksum = session.recovery_preview(body['record_sha256'])
                    kind = 'recovery'
                elif action == 'recover':
                    preview, checksum = session.recovery_preview(body['record_sha256'])
                    self.assertEqual(body, {'schema_version': '1.0', 'record_sha256': preview['record_sha256'],
                        'confirm_recovery_sha256': checksum, 'human_confirmation': 'RESTORE_ANCHORED_RELEASE_RECORD',
                        'lease_id': snapshot['control']['lease_id'], 'generation': snapshot['control']['generation']})
                    record, checksum = session.recover(body['record_sha256'], body['confirm_recovery_sha256'],
                                                      body['human_confirmation'])
                    kind = 'recovery'
                    if mutation['lose_recovery']:
                        route.abort()
                        return
                elif action in ('select-preview', 'rollback-preview'):
                    record, checksum = session.selection_preview(body['release_sha256'], body['expected_selection_sha256'],
                        'rollback' if action.startswith('rollback') else 'select')
                    kind = 'selection'
                elif action in ('select', 'rollback'):
                    operation = action
                    preview, checksum = session.selection_preview(body['release_sha256'], body['expected_selection_sha256'], operation)
                    self.assertEqual(body, {'schema_version': '1.0', 'release_sha256': first_release_hash,
                        'expected_selection_sha256': body['expected_selection_sha256'], 'confirm_selection_sha256': checksum,
                        'human_confirmation': 'SELECT_MANUAL_SKILL' if operation == 'select' else 'ROLLBACK_MANUAL_SKILL',
                        'lease_id': snapshot['control']['lease_id'], 'generation': snapshot['control']['generation']})
                    record, checksum = session.select(body['release_sha256'], body['expected_selection_sha256'], operation,
                        body['confirm_selection_sha256'], body['human_confirmation'])
                    kind = 'selection'
                    if action == 'rollback' and mutation['lose_rollback']:
                        route.abort()
                        return
                else:
                    self.fail(action)
            except ValueError:
                route.fulfill(status=409, json={'error': 'release_source_or_head_unavailable'})
                return
            response = {'schema_version': '1.0', kind: record, kind + '_canonical': canonical(record), kind + '_sha256': checksum, **flags}
            invalid = mutation['invalid']
            if invalid == 'hash':
                response[kind + '_sha256'] = 'b' * 64
            elif invalid == 'canonical':
                response[kind + '_canonical'] = json.dumps(record, sort_keys=True)
                response[kind + '_sha256'] = hashlib.sha256(response[kind + '_canonical'].encode()).hexdigest()
            elif invalid == 'outerflag':
                response['execution_authorized'] = True
            elif invalid == 'record-digest':
                record['record_sha256'] = 'f' * 64
                response[kind + '_canonical'] = canonical(record)
                response[kind + '_sha256'] = digest(record)
            elif invalid in ('native', 'scope', 'flag', 'kind', 'review', 'parent', 'selection-head'):
                if invalid == 'scope':
                    record['scope']['account_role'] = 'foreign'
                elif invalid == 'flag':
                    record['activation_authorized'] = 0
                elif invalid == 'kind':
                    record['purpose'] = 'runtime_activated'
                elif invalid == 'review':
                    record['review_sha256'] = 'c' * 64
                elif invalid == 'parent':
                    record['parent_release_sha256'] = 'c' * 64
                elif invalid == 'selection-head':
                    record['previous_selection_sha256'] = 'c' * 64
                else:
                    record['native_model_verified'] = True
                response[kind + '_canonical'] = canonical(record)
                response[kind + '_sha256'] = digest(record)
            route.fulfill(json=response)

        with tempfile.TemporaryDirectory(prefix='parameter-skill-release-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<div id="root"></div><script type="module" src="/entry.tsx"></script>')
            (root / 'entry.tsx').write_text('''import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {OwnedParameterSkillRelease} from '../../ui/src/OwnedParameterSkillRelease';
import {setLanguage} from '../../ui/src/i18n';
import '../../ui/src/style.css';
function Harness(){const [snapshot,setSnapshot]=useState<any>(SNAPSHOT);
(window as any).update=setSnapshot;return <><button onClick={()=>setLanguage('en')}>EN</button>
<button onClick={()=>setLanguage('tr')}>TR</button><OwnedParameterSkillRelease snapshot={snapshot} disabled={snapshot.control.owner!=='AGENT'}/></>}
createRoot(document.getElementById('root')!).render(<Harness/>);'''.replace('SNAPSHOT', json.dumps(snapshot)))
            configuration = {'root': str(root), 'resolve': {'alias': [
                {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
                {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]},
                'build': {'outDir': str(root / 'build')}}
            (root / 'vite.config.mjs').write_text('export default ' + json.dumps(configuration))
            subprocess.run(['/usr/bin/node', str(REPO_ROOT / 'ui/node_modules/vite/bin/vite.js'),
                            'build', '--config', str(root / 'vite.config.mjs')],
                           cwd=REPO_ROOT / 'ui', capture_output=True, check=True, timeout=60)
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(root / 'build')))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                        'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'),
                        args=['--disable-gpu'])
                    try:
                        page = browser.new_page()
                        page_errors = []
                        page.on('pageerror', lambda error: page_errors.append(str(error)))
                        page.route('**/api/**', handle)
                        page.goto(f'http://127.0.0.1:{server.server_port}/')
                        page.get_by_role('button', name='EN', exact=True).click()
                        expect(page.get_by_role('heading', name='Manual skill development release')).to_be_visible()
                        self.assertEqual(calls, [])
                        page.get_by_test_id('manual-release-review').fill(first.review_sha256)
                        for invalid in ('hash', 'canonical', 'native', 'scope', 'flag', 'kind', 'review', 'parent'):
                            mutation['invalid'] = invalid
                            page.get_by_test_id('manual-release-preview').click()
                            expect(page.get_by_test_id('manual-release-message')).to_have_text('Release response or current source could not be verified.')
                            expect(page.get_by_test_id('manual-release-confirm')).to_have_count(0)
                        mutation['invalid'] = None
                        page.get_by_test_id('manual-release-review').fill(second.review_sha256)
                        page.get_by_test_id('manual-release-preview').click()
                        expect(page.get_by_test_id('manual-release-confirm')).to_have_count(0)
                        page.get_by_test_id('manual-release-review').fill(first.review_sha256)
                        page.get_by_test_id('manual-release-parent').fill('b' * 64)
                        page.get_by_test_id('manual-release-preview').click()
                        expect(page.get_by_test_id('manual-release-confirm')).to_have_count(0)
                        page.get_by_test_id('manual-release-parent').fill('')
                        page.get_by_test_id('manual-release-preview').click()
                        expect(page.get_by_test_id('manual-release-confirm')).not_to_be_checked()
                        expect(page.get_by_test_id('manual-release-write')).to_be_disabled()
                        page.get_by_test_id('manual-release-confirm').check()
                        mutation['stale_state'] = True
                        page.get_by_test_id('manual-release-write').click()
                        expect(page.get_by_test_id('manual-release-message')).to_have_text('Release response or current source could not be verified.')
                        self.assertEqual([action for action, _ in calls].count('release'), 0)
                        mutation['stale_state'] = False
                        page.get_by_test_id('manual-release-preview').click()
                        page.get_by_test_id('manual-release-confirm').check()
                        page.get_by_test_id('manual-release-write').click()
                        expect(page.get_by_test_id('manual-release-uncertain')).to_be_visible()
                        snapshot['control'].update(owner='HUMAN', generation=3, lease_id='human-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        page.get_by_test_id('manual-release-read').click()
                        expect(page.get_by_test_id('manual-release-uncertain')).to_have_count(0)
                        expect(page.get_by_test_id('manual-release-result')).to_contain_text('"purpose":"development_release"')
                        snapshot['control'].update(owner='AGENT', generation=4, lease_id='fresh-agent-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        page.get_by_test_id('manual-selection-target').fill(first_release_hash)
                        page.get_by_test_id('manual-select-preview').click()
                        expect(page.get_by_test_id('manual-selection-confirm')).not_to_be_checked()
                        page.get_by_test_id('manual-selection-confirm').check()
                        page.get_by_test_id('manual-selection-write').click()
                        expect(page.get_by_test_id('manual-release-message')).to_have_text('Manual metadata storage verified; runtime not activated.')
                        first_selection_hash = session.inventory()['families'][0]['selection_sha256']
                        for stage, checksum in (('release', first_release_hash), ('selection', first_selection_hash)):
                            path = session.store.directory / (checksum + '.' + stage + '.json')
                            original = path.read_bytes()
                            path.unlink()
                            page.get_by_test_id('manual-recovery-record').fill(checksum)
                            for invalid in ('hash', 'canonical', 'native', 'scope', 'flag', 'kind', 'record-digest', 'outerflag'):
                                mutation['invalid'] = invalid
                                page.get_by_test_id('manual-recovery-preview').click()
                                expect(page.get_by_test_id('manual-recovery-confirm')).to_have_count(0)
                                expect(page.get_by_test_id('manual-release-message')).to_have_text('Recovery response or current source could not be verified.')
                                self.assertFalse(path.exists())
                            mutation['invalid'] = None
                            page.get_by_test_id('manual-recovery-preview').click()
                            expect(page.get_by_test_id('manual-recovery-confirm')).not_to_be_checked()
                            expect(page.get_by_test_id('manual-recovery-proposal')).to_contain_text('"record_stage":"' + stage + '"')
                            expect(page.get_by_test_id('manual-recovery-write')).to_be_disabled()
                            if stage == 'release':
                                page.get_by_test_id('manual-recovery-confirm').check()
                                mutation['stale_state'] = True
                                page.get_by_test_id('manual-recovery-write').click()
                                expect(page.get_by_test_id('manual-release-message')).to_have_text('Recovery response or current source could not be verified.')
                                self.assertEqual([action for action, _ in calls].count('recover'), 0)
                                self.assertFalse(path.exists())
                                mutation['stale_state'] = False
                                page.get_by_test_id('manual-recovery-preview').click()
                            page.get_by_test_id('manual-recovery-confirm').check()
                            mutation['lose_recovery'] = stage == 'selection'
                            page.get_by_test_id('manual-recovery-write').click()
                            if stage == 'selection':
                                expect(page.get_by_test_id('manual-release-uncertain')).to_be_visible()
                                expect(page.get_by_test_id('manual-recovery-result')).to_have_count(0)
                                page.get_by_test_id('manual-release-inventory').click()
                                expect(page.get_by_test_id('manual-release-uncertain')).to_have_count(0)
                            else:
                                expect(page.get_by_test_id('manual-recovery-result')).to_contain_text('"record_sha256":"' + checksum + '"')
                            self.assertEqual(path.read_bytes(), original)
                            page.get_by_role('button', name='TR', exact=True).click()
                            expect(page.get_by_test_id('manual-recovery-record')).to_be_visible()
                            page.get_by_role('button', name='EN', exact=True).click()
                        mutation['lose_recovery'] = False
                        _, second_release_hash = second.releases.preview(second.review_sha256, first_release_hash)
                        second.releases.release(second.review_sha256, first_release_hash, second_release_hash, 'RELEASE_MANUAL_SKILL')
                        _, second_selection_hash = second.releases.selection_preview(second_release_hash, first_selection_hash, 'select')
                        second.releases.select(second_release_hash, first_selection_hash, 'select', second_selection_hash, 'SELECT_MANUAL_SKILL')
                        mutation['invalid'] = 'inventory'
                        page.get_by_test_id('manual-release-inventory').click()
                        expect(page.get_by_test_id('manual-release-history')).to_have_count(0)
                        mutation['invalid'] = None
                        page.get_by_test_id('manual-release-inventory').click()
                        expect(page.get_by_test_id('manual-release-history')).to_contain_text('"source_current_verified":false')
                        expect(page.get_by_text('Inventory is historical metadata; it does not prove current source or reuse authority.', exact=True)).to_be_visible()
                        page.get_by_test_id('manual-release-read-hash').fill(second_release_hash)
                        page.get_by_test_id('manual-release-read').click()
                        expect(page.get_by_test_id('manual-release-result')).to_have_count(0)
                        page.get_by_test_id('manual-selection-target').fill(second_release_hash)
                        page.get_by_test_id('manual-selection-head').fill(second_selection_hash)
                        page.get_by_test_id('manual-select-preview').click()
                        expect(page.get_by_test_id('manual-selection-proposal')).to_have_count(0)
                        page.get_by_test_id('manual-selection-target').fill(first_release_hash)
                        mutation['invalid'] = 'selection-head'
                        page.get_by_test_id('manual-rollback-preview').click()
                        expect(page.get_by_test_id('manual-selection-proposal')).to_have_count(0)
                        mutation['invalid'] = None
                        page.get_by_test_id('manual-rollback-preview').click()
                        expect(page.get_by_test_id('manual-selection-confirm')).not_to_be_checked()
                        page.get_by_role('button', name='TR', exact=True).click()
                        expect(page.get_by_role('heading', name='Manuel skill geliştirme sürümü')).to_be_visible()
                        page.get_by_test_id('manual-selection-confirm').check()
                        page.get_by_test_id('manual-selection-write').click()
                        expect(page.get_by_test_id('manual-release-uncertain')).to_be_visible()
                        snapshot['control'].update(owner='HUMAN', generation=5, lease_id='rollback-human-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        page.get_by_test_id('manual-release-inventory').click()
                        expect(page.get_by_test_id('manual-release-uncertain')).to_have_count(0)
                        expect(page.get_by_test_id('manual-release-history')).to_contain_text('"operation":"rollback"')
                        snapshot['control'].update(owner='AGENT', generation=6, lease_id='rollback-agent-lease')
                        page.evaluate('value=>window.update(value)', snapshot)
                        page.get_by_test_id('manual-rollback-preview').click()
                        page.get_by_test_id('manual-selection-confirm').check()
                        expect(page.get_by_test_id('manual-selection-write')).to_be_disabled()
                        _, revocation_hash = first.reviews.revocation_preview(first.review_sha256)
                        first.reviews.revoke(first.review_sha256, revocation_hash, 'REVOKE_MANUAL_REVIEW')
                        page.get_by_test_id('manual-release-read-hash').fill(first_release_hash)
                        page.get_by_test_id('manual-release-read').click()
                        expect(page.get_by_test_id('manual-release-result')).to_have_count(0)
                        page.get_by_test_id('manual-release-inventory').click()
                        expect(page.get_by_test_id('manual-release-history')).to_contain_text('"source_current_verified":false')
                        page.get_by_test_id('manual-select-preview').click()
                        expect(page.get_by_test_id('manual-selection-proposal')).to_have_count(0)
                        self.assertEqual([action for action, _ in calls].count('release'), 1)
                        self.assertEqual([action for action, _ in calls].count('select'), 1)
                        self.assertEqual([action for action, _ in calls].count('rollback'), 1)
                        self.assertEqual([action for action, _ in calls].count('recover'), 2)
                        revoked_path = session.store.directory / (first_release_hash + '.release.json')
                        revoked_path.unlink()
                        page.get_by_test_id('manual-recovery-record').fill(first_release_hash)
                        page.get_by_test_id('manual-recovery-preview').click()
                        expect(page.get_by_test_id('manual-recovery-confirm')).to_have_count(0)
                        self.assertFalse(revoked_path.exists())
                        page.set_viewport_size({'width': 390, 'height': 844})
                        expect(page.get_by_test_id('manual-recovery-record')).to_be_visible()
                        page.get_by_test_id('manual-recovery-record').scroll_into_view_if_needed()
                        screenshot = Path(tempfile.gettempdir()) / ('aos-release-recovery-' + root.name + '.png')
                        page.screenshot(path=str(screenshot))
                        print('GPU-disabled synthetic release recovery UI screenshot: ' + str(screenshot), flush=True)
                        self.assertEqual(page_errors, [])
                        effects_after = {table: digest([dict(row) for row in first.source.store.connection.execute(
                            'SELECT * FROM ' + table + ' ORDER BY rowid')]) for table in effects_before}
                        self.assertEqual(effects_after, effects_before)
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

import asyncio
import copy
from contextlib import contextmanager
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


@unittest.skipUnless(os.environ.get('AOS_PARAMETER_SKILL_REUSE_UI_TESTS') == '1', 'Opt-in isolated Chromium CPU fixture')
class OwnedParameterSkillReuseUITests(unittest.TestCase):
    def test_actual_compiled_source_exact_start_fence_and_durable_lost_ack_human_read(self):
        from playwright.sync_api import expect, sync_playwright
        from test_owned_parameter_skill_reuse_execution import OwnedParameterSkillReuseExecutionTests

        fixture = OwnedParameterSkillReuseExecutionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()

        def fail_after_intent(_bundle):
            raise ValueError('Synthetic failure after durable intent before child task')

        service = fixture.service(prepare=fail_after_intent)
        preview = fixture.preview(service)
        snapshot = {'runtime': {'runtime_id': fixture.control['runtime_id'], 'running': True},
                    'control': {key: fixture.control[key] for key in ('owner', 'status', 'lease_id', 'generation')}}
        calls = []
        mutation = {'invalid': None, 'stale_state': False}
        effects_before = {table: digest([dict(row) for row in fixture.manager.store.connection.execute(
            'SELECT * FROM ' + table + ' ORDER BY rowid')]) for table in ('actions', 'model_calls', 'desktop_tasks', 'trajectory_labels')}

        def route_api(route):
            operation = route.request.url.rsplit('/', 1)[-1]
            if operation == 'state':
                state = copy.deepcopy(snapshot)
                if mutation['stale_state']:
                    state['control']['generation'] += 1
                route.fulfill(json=state)
                return
            body = route.request.post_data_json
            calls.append((operation, body))
            if operation == 'preview':
                response = copy.deepcopy(preview)
                if mutation['invalid'] == 'hash':
                    response['binding_sha256'] = 'b' * 64
                elif mutation['invalid'] == 'flag':
                    response['execution_authorized'] = 0
                elif mutation['invalid'] == 'canonical':
                    response['binding_canonical'] = json.dumps(response['binding'], sort_keys=True)
                    response['binding_sha256'] = hashlib.sha256(response['binding_canonical'].encode()).hexdigest()
                elif mutation['invalid'] == 'source':
                    response['admission']['source']['release_sha256'] = 'b' * 64
                    response['admission_canonical'] = canonical(response['admission'])
                    response['admission_sha256'] = digest(response['admission'])
                route.fulfill(json=response)
            elif operation == 'start':
                self.assertEqual(body, {'schema_version': '1.0', 'release_sha256': fixture.release_sha,
                    'selection_sha256': fixture.selection_sha, 'parameters': fixture.parameters,
                    'lease_id': fixture.control['lease_id'], 'generation': fixture.control['generation'],
                    'confirm_sha256': preview['confirm_sha256'], 'human_confirmation': True})
                with self.assertRaisesRegex(ValueError, 'Synthetic failure'):
                    service.start(preview['admission'], confirm_sha256=body['confirm_sha256'], human_confirmation=True,
                        lease_id=body['lease_id'], generation=body['generation'])
                route.abort()
            elif operation == 'status':
                self.assertEqual(body, {'schema_version': '1.0'})
                route.fulfill(json={**service.status(), 'transition_blocked': service.transition_blocked, 'transition_in_progress': service.transitioning})
            else:
                self.fail(operation)

        with tempfile.TemporaryDirectory(prefix='manual-reuse-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<div id="root"></div><script type="module" src="/entry.tsx"></script>')
            (root / 'entry.tsx').write_text('''import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {OwnedParameterSkillReuse} from '../../ui/src/OwnedParameterSkillReuse';
import {setLanguage} from '../../ui/src/i18n';
function Harness(){const [snapshot,setSnapshot]=useState<any>(SNAPSHOT);(window as any).update=setSnapshot;
return <><button onClick={()=>setLanguage('en')}>EN</button><button onClick={()=>setLanguage('tr')}>TR</button>
<OwnedParameterSkillReuse snapshot={snapshot} disabled={snapshot.control.owner!=='AGENT'}/></>}
createRoot(document.getElementById('root')!).render(<Harness/>);'''.replace('SNAPSHOT', json.dumps(snapshot)))
            configuration = {'root': str(root), 'resolve': {'alias': [
                {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
                {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]}, 'build': {'outDir': str(root / 'build')}}
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
                        page.route('**/api/**', route_api)
                        page.goto(f'http://127.0.0.1:{server.server_port}/')
                        page.get_by_role('button', name='EN', exact=True).click()
                        expect(page.get_by_role('heading', name='Separate task using a selected manual skill')).to_be_visible()
                        self.assertEqual(calls, [])
                        page.get_by_test_id('manual-reuse-release').fill(fixture.release_sha)
                        page.get_by_test_id('manual-reuse-selection').fill(fixture.selection_sha)
                        page.get_by_test_id('manual-reuse-parameters').fill(json.dumps(fixture.parameters))
                        for invalid in ('hash', 'flag', 'canonical', 'source'):
                            mutation['invalid'] = invalid
                            page.get_by_test_id('manual-reuse-preview').click()
                            expect(page.get_by_test_id('manual-reuse-message')).to_have_text('Reuse response, source or control could not be verified.')
                            expect(page.get_by_test_id('manual-reuse-confirm')).to_have_count(0)
                        mutation['invalid'] = None
                        page.get_by_test_id('manual-reuse-preview').click()
                        expect(page.get_by_test_id('manual-reuse-confirm')).not_to_be_checked()
                        expect(page.get_by_test_id('manual-reuse-start')).to_be_disabled()
                        page.get_by_test_id('manual-reuse-confirm').check()
                        mutation['stale_state'] = True
                        page.get_by_test_id('manual-reuse-start').click()
                        expect(page.get_by_test_id('manual-reuse-message')).to_have_text('Reuse response, source or control could not be verified.')
                        self.assertEqual([operation for operation, _ in calls].count('start'), 0)
                        mutation['stale_state'] = False
                        page.get_by_test_id('manual-reuse-preview').click()
                        page.get_by_role('button', name='TR', exact=True).click()
                        expect(page.get_by_role('heading', name='Seçilmiş manuel skill ile ayrı görev')).to_be_visible()
                        page.get_by_test_id('manual-reuse-confirm').check()
                        page.get_by_test_id('manual-reuse-start').click()
                        expect(page.get_by_test_id('manual-reuse-uncertain')).to_be_visible()
                        snapshot['control'].update(owner='HUMAN', generation=snapshot['control']['generation'] + 1)
                        page.evaluate('value=>window.update(value)', snapshot)
                        expect(page.get_by_test_id('manual-reuse-uncertain')).to_be_visible()
                        page.get_by_test_id('manual-reuse-status').click()
                        expect(page.get_by_test_id('manual-reuse-result')).to_contain_text('"status":"uncertain_before_run_binding"')
                        expect(page.get_by_test_id('manual-reuse-preview')).to_be_disabled()
                        self.assertEqual([operation for operation, _ in calls].count('start'), 1)
                        self.assertIsNone(service.child_manager)
                        effects_after = {table: digest([dict(row) for row in fixture.manager.store.connection.execute(
                            'SELECT * FROM ' + table + ' ORDER BY rowid')]) for table in effects_before}
                        self.assertEqual(effects_after, effects_before)
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    @contextmanager
    def isolated_page(self, snapshot, route_api):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory(prefix='manual-reuse-next-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            (root / 'index.html').write_text('<div id="root"></div><script type="module" src="/entry.tsx"></script>')
            (root / 'entry.tsx').write_text('''import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {OwnedParameterSkillReuse} from '../../ui/src/OwnedParameterSkillReuse';
import {setLanguage} from '../../ui/src/i18n';
function Harness(){const [snapshot,setSnapshot]=useState<any>(SNAPSHOT);(window as any).update=setSnapshot;
return <><button onClick={()=>setLanguage('en')}>EN</button><button onClick={()=>setLanguage('tr')}>TR</button>
<OwnedParameterSkillReuse snapshot={snapshot} disabled={snapshot.control.owner!=='AGENT'}/></>}
createRoot(document.getElementById('root')!).render(<Harness/>);'''.replace('SNAPSHOT', json.dumps(snapshot)))
            configuration = {'root': str(root), 'resolve': {'alias': [
                {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
                {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]}, 'build': {'outDir': str(root / 'build')}}
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
                        page.route('**/api/**', route_api)
                        page.goto(f'http://127.0.0.1:{server.server_port}/')
                        yield page
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_actual_separate_next_task_exact_predecessor_lost_ack_and_historical_read(self):
        from playwright.sync_api import expect
        from test_owned_parameter_skill_reuse_next_execution import OwnedParameterSkillReuseNextExecutionTests

        fixture = OwnedParameterSkillReuseNextExecutionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        service = fixture.service()
        original_rows = fixture.original_rows()
        first, _child, first_approvals = asyncio.run(fixture.execute(service, fixture.preview(service)))
        predecessor = fixture.predecessor(service)
        prior_status = service.status()
        snapshot = {'runtime': {'runtime_id': fixture.control['runtime_id'], 'running': True},
                    'control': {key: fixture.control[key] for key in ('owner', 'status', 'lease_id', 'generation')}}
        next_parameters = {'record-id': 'Synthetic third record', 'note-text': 'Third approved note'}
        calls = []
        mutation = {'invalid_pair': True, 'stale_state': False}
        second_approvals = []
        execution_tasks = []

        def route_api(route):
            operation = route.request.url.rsplit('/', 1)[-1]
            if operation == 'state':
                state = copy.deepcopy(snapshot)
                if mutation['stale_state']:
                    state['control']['generation'] += 1
                route.fulfill(json=state)
                return
            body = route.request.post_data_json
            calls.append((operation, body))
            if operation in ('status', 'read'):
                value = service.status() if operation == 'status' else service.read(body['intent_sha256'])
                route.fulfill(json={**value, 'transition_blocked': service.transition_blocked, 'transition_in_progress': service.transitioning})
                return
            self.assertEqual(body['previous_intent_sha256'], predecessor['previous_intent_sha256'])
            self.assertEqual(body['previous_receipt_sha256'], predecessor['previous_receipt_sha256'])
            value = service.next_preview(fixture.release_sha, fixture.selection_sha, next_parameters,
                **predecessor, lease_id=fixture.control['lease_id'], generation=fixture.control['generation'])
            if operation == 'next-preview':
                if mutation['invalid_pair']:
                    value['predecessor']['receipt_sha256'] = 'b' * 64
                route.fulfill(json=value)
            elif operation == 'next-start':
                self.assertEqual(body, {'schema_version': '1.0', 'release_sha256': fixture.release_sha,
                    'selection_sha256': fixture.selection_sha, 'parameters': next_parameters, **predecessor,
                    'lease_id': fixture.control['lease_id'], 'generation': fixture.control['generation'],
                    'confirm_sha256': value['confirm_sha256'], 'human_confirmation': True})
                execution_tasks.append(asyncio.get_running_loop().create_task(fixture.execute(service, value, predecessor)))
                route.abort()
            else:
                self.fail(operation)

        with self.isolated_page(snapshot, route_api) as page:
            page.get_by_role('button', name='EN', exact=True).click()
            self.assertEqual(calls, [])
            page.get_by_test_id('manual-reuse-release').fill(fixture.release_sha)
            page.get_by_test_id('manual-reuse-selection').fill(fixture.selection_sha)
            page.get_by_test_id('manual-reuse-history-hash').fill(first['intent_sha256'])
            page.get_by_test_id('manual-reuse-history-read').click()
            expect(page.get_by_test_id('manual-reuse-history')).to_contain_text('"status":"accepted_verified"')
            expect(page.get_by_test_id('manual-reuse-next-preview')).to_have_count(0)
            page.get_by_test_id('manual-reuse-status').click()
            expect(page.get_by_test_id('manual-reuse-predecessor')).to_contain_text(prior_status['receipt_sha256'])
            page.get_by_test_id('manual-reuse-parameters').fill(json.dumps(next_parameters))
            expect(page.get_by_test_id('manual-reuse-predecessor')).to_contain_text(prior_status['receipt_sha256'])
            page.get_by_test_id('manual-reuse-next-preview').click()
            expect(page.get_by_test_id('manual-reuse-message')).to_have_text('Reuse response, source or control could not be verified.')
            expect(page.get_by_test_id('manual-reuse-confirm')).to_have_count(0)
            mutation['invalid_pair'] = False
            mutation['stale_state'] = True
            before = len(calls)
            page.get_by_test_id('manual-reuse-next-preview').click()
            expect(page.get_by_test_id('manual-reuse-message')).to_have_text('Reuse response, source or control could not be verified.')
            self.assertEqual(len(calls), before)
            mutation['stale_state'] = False
            page.get_by_test_id('manual-reuse-next-preview').click()
            expect(page.get_by_test_id('manual-reuse-confirm')).not_to_be_checked()
            expect(page.get_by_test_id('manual-reuse-start')).to_be_disabled()
            page.get_by_role('button', name='TR', exact=True).click()
            page.get_by_test_id('manual-reuse-confirm').check()
            page.get_by_test_id('manual-reuse-start').click()
            expect(page.get_by_test_id('manual-reuse-uncertain')).to_be_visible()
            expect(page.get_by_test_id('manual-reuse-predecessor')).to_contain_text(prior_status['receipt_sha256'])
            for _attempt in range(600):
                if execution_tasks and execution_tasks[0].done():
                    break
                page.wait_for_timeout(25)
            self.assertTrue(execution_tasks[0].done())
            _started, _child, approvals = execution_tasks[0].result()
            second_approvals.extend(approvals)
            snapshot['control'].update(owner='HUMAN', generation=snapshot['control']['generation'] + 1)
            page.evaluate('value=>window.update(value)', snapshot)
            page.get_by_test_id('manual-reuse-status').click()
            expect(page.get_by_test_id('manual-reuse-uncertain')).to_have_count(0)
            expect(page.get_by_test_id('manual-reuse-predecessor')).to_contain_text(service.status()['receipt_sha256'])
            expect(page.get_by_test_id('manual-reuse-completed-history')).to_contain_text(prior_status['receipt_sha256'])
            page.get_by_test_id('manual-reuse-history-hash').fill(first['intent_sha256'])
            page.get_by_test_id('manual-reuse-history-read').click()
            expect(page.get_by_test_id('manual-reuse-history')).to_contain_text(prior_status['receipt_sha256'])
            self.assertEqual([operation for operation, _ in calls].count('next-start'), 1)
        self.assertEqual(len(first_approvals), 6)
        self.assertEqual(len(second_approvals), 6)
        self.assertTrue(set(first_approvals).isdisjoint(second_approvals))
        self.assertEqual(fixture.original_rows(), original_rows)
        self.assertNotEqual(service.status()['intent_sha256'], first['intent_sha256'])

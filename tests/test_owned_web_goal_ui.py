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


@unittest.skipUnless(os.environ.get('AOS_WEB_GOAL_UI_TESTS') == '1', 'Opt-in synthetic Chromium panel')
class OwnedWebGoalUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='owned-web-goal-ui-', dir=REPO_ROOT / 'data'))
        (cls.root / 'index.html').write_text('<html><body><div id="root"></div><script type="module" src="/entry.tsx"></script></body></html>')
        (cls.root / 'entry.tsx').write_text('''import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {OwnedWebGoalCatalog} from '../../ui/src/OwnedWebGoalCatalog';
import {setLanguage, useLanguage} from '../../ui/src/i18n';
import '../../ui/src/style.css';
const source = {mode:'owned_synthetic_form_invocation', lifecycle:'audited', profile_sha256:'b'.repeat(64),
  skill_sha256:'a'.repeat(64), invocation_sha256:'a'.repeat(64), run_ref:'c'.repeat(64), reuse_admission_sha256:'d'.repeat(64)};
const snapshot = {control:{owner:'AGENT',status:'running',lease_id:'synthetic-lease',generation:2},
  runtime:{runtime_id:'synthetic-runtime',running:true,image_id:null,container_id:null}};
function Harness() {useLanguage(); const [control, setControl] = useState(snapshot.control);
  const [planning, setPlanning] = useState<any>(undefined);
  return <><button onClick={() => setLanguage('tr')}>TR</button>
  <button onClick={() => setLanguage('en')}>EN</button>
  <button onClick={() => setPlanning({available:true, status:'bound', execution_review_pending:true,
    bundle_sha256:'7'.repeat(64), execution_authorized:false})}>RECOVERY</button>
  <button onClick={() => setPlanning({available:true, status:'consumed', job_id:'job-synthetic',
    bundle_sha256:'7'.repeat(64), execution_authorized:false})}>CONSUMED</button>
  <button onClick={() => setPlanning({available:true, status:'idle', knowledge_available:true,
    execution_authorized:false})}>KNOWLEDGE</button>
  <button onClick={() => setPlanning({available:true, status:'ready', knowledge_available:true,
    bundle_sha256:(window as any).syntheticReportBundle, execution_authorized:false})}>REPORT</button>
  <button onClick={() => setControl({...control, owner:'HUMAN', generation:3})}>TAKEOVER</button>
  <OwnedWebGoalCatalog source={source} snapshot={{...snapshot, control}} disabled={Boolean(planning?.execution_review_pending)} planning={planning}/></>;}
createRoot(document.getElementById('root')!).render(<Harness/>);
''')
        configuration = {'root': str(cls.root), 'resolve': {'alias': [
            {'find': 'react-dom/client', 'replacement': str(REPO_ROOT / 'ui/node_modules/react-dom/client.js')},
            {'find': 'react', 'replacement': str(REPO_ROOT / 'ui/node_modules/react')}]},
            'build': {'outDir': str(cls.root / 'build')}}
        (cls.root / 'vite.config.mjs').write_text('export default ' + json.dumps(configuration) + ';')
        result = subprocess.run(['pnpm', 'exec', 'vite', 'build', '--config', str(cls.root / 'vite.config.mjs')],
                                cwd=REPO_ROOT / 'ui', capture_output=True, text=True, timeout=60)
        (cls.root / 'build.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise AssertionError('Synthetic UI build failed')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(
            SimpleHTTPRequestHandler, directory=str(cls.root / 'build')))
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(executable_path=str(
                REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
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

    def test_manual_preview_languages_mobile_and_no_start_or_model_calls(self):
        from playwright.sync_api import expect

        page = self.browser.new_page(viewport={'width': 1280, 'height': 800})
        self.addCleanup(page.close)
        errors, effects = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        state = {'generation': 2, 'invalid_catalog': False}
        skill = {'skill_ref': 'save-message', 'description': 'Synthetic save-message recipe',
                 'recipe_sha256': 'e' * 64, 'release_sha256': 'f' * 64, 'selection_sha256': 'a' * 64,
                 'parameters': {'message': {'description': 'Exact text', 'min_chars': 1, 'max_chars': 128}}}
        catalog = {'schema_version': '1.0', 'synthetic': True, 'profile_sha256': 'b' * 64,
                   'application_key': 'synthetic-notes', 'tenant_key': 'synthetic-tenant', 'account_role': 'tester', 'execution_authorized': False,
                   'activation_authorized': False, 'training_ready': False,
                   'scope_authorization_verified': False, 'skills': [skill]}

        def endpoint(route):
            path = route.request.url.split('/api/', 1)[1]
            if path == 'state':
                self.assertEqual(route.request.method, 'GET')
                route.fulfill(json={'control': {'owner': 'AGENT', 'status': 'running',
                    'lease_id': 'synthetic-lease', 'generation': state['generation']},
                    'runtime': {'runtime_id': 'synthetic-runtime', 'running': True}})
                return
            effects.append(path)
            self.assertEqual(route.request.method, 'POST')
            if path == 'tasks/owned-web-goal/catalog':
                route.fulfill(json={'catalog': catalog | {'execution_authorized': state['invalid_catalog']},
                                    'catalog_sha256': '9' * 64})
            elif path == 'tasks/owned-web-goal/preview':
                body = route.request.post_data_json
                self.assertEqual(body['proposal']['parameters'], {'message': 'literal.42'})
                self.assertIs(body['proposal']['execution_authorized'], False)
                self.assertEqual(body['confirm_catalog_sha256'], '9' * 64)
                route.fulfill(json={'schema_version': '1.3', 'available': True, 'status': 'preview',
                    'preview_sha256': '8' * 64, 'profile_sha256': 'b' * 64, 'source_run_ref': 'c' * 64,
                    'reuse_admission_sha256': 'd' * 64, **{key: skill[key] for key in (
                        'recipe_sha256', 'release_sha256', 'selection_sha256')},
                    'steps': [{'step_key': operation, 'operation': operation} for operation in (
                        'open_entry', 'read_state_before', 'fill_form', 'submit_form', 'read_receipt', 'read_state_after')]})
            else:
                raise AssertionError('Unexpected API dispatch: ' + path)

        page.route('**/api/**', endpoint)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        expect(page.get_by_test_id('web-goal-load')).to_be_enabled()
        self.assertEqual(effects, [])
        page.get_by_test_id('web-goal-load').click()
        expect(page.get_by_test_id('web-goal-preview')).to_be_disabled()
        page.get_by_test_id('web-goal-message').fill('unsupported ç')
        expect(page.get_by_test_id('web-goal-preview')).to_be_disabled()
        page.get_by_test_id('web-goal-message').fill('literal.42')
        page.get_by_test_id('web-goal-preview').click()
        expect(page.get_by_test_id('web-goal-preview-result')).to_be_visible()
        page.get_by_role('button', name='TR', exact=True).click()
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(page.get_by_text('Desteklenen web skill', exact=True)).to_be_visible()
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        page.screenshot(path=str(self.root / 'manual-preview-mobile-tr.png'), full_page=True)
        state['generation'] = 3
        page.get_by_test_id('web-goal-preview').click()
        expect(page.get_by_role('alert')).to_be_visible()
        expect(page.get_by_test_id('web-goal-preview-result')).to_have_count(0)
        self.assertEqual(effects, ['tasks/owned-web-goal/catalog', 'tasks/owned-web-goal/preview'])
        state['generation'] = 2
        state['invalid_catalog'] = True
        page.get_by_test_id('web-goal-load').click()
        expect(page.get_by_role('alert')).to_be_visible()
        expect(page.get_by_test_id('web-goal-message')).to_have_count(0)
        self.assertEqual(effects, ['tasks/owned-web-goal/catalog', 'tasks/owned-web-goal/preview',
                                   'tasks/owned-web-goal/catalog'])
        self.assertEqual(errors, [])

    def test_saved_knowledge_report_is_readonly_under_human_and_legacy_use_is_unverified(self):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from aos.contracts import canonical, digest
        from test_web_goal_knowledge_service import WebGoalKnowledgeServiceTests
        from test_web_goal_planner import response_for
        from playwright.sync_api import expect

        async def fixture_report():
            fixture = WebGoalKnowledgeServiceTests('test_actual_reviewed_store_context_flows_into_private_model_request')
            try:
                await fixture.asyncSetUp()
                fixture.planner.identity.update(real_model=True, kind='bonsai_native_web_goal_planner',
                    deployment_id='bonsai-' + digest(fixture.planner.pins))
                review = fixture.preview()

                async def native(instance, goal, evidence, *, request_context):
                    instance.before_model_call()
                    instance.last_request = instance.request_body(goal, evidence, request_context=request_context)
                    instance.last_response = response_for(fixture.case, fixture.catalog)
                    return instance.parse_response(canonical(instance.last_response), evidence)

                with patch('aos.web_goal_planner.BonsaiSupervisor.plan', native):
                    fixture.planning.begin(fixture.case['goal'], fixture.authority['lease_id'], 2,
                        confirm_catalog_sha256=review['catalog_sha256'], inference_consent=True,
                        knowledge_review=review, confirm_knowledge_sha256=review['confirm_sha256'], storage_consent=True)
                    await fixture.planning.task
                checksum = fixture.planning.status()['bundle_sha256']
                (fixture.planning.directory / (checksum + '.knowledge-ack.json')).unlink()
                fixture.authority['generation'] = 3
                return fixture.service.report(bundle_sha256=checksum)
            finally:
                fixture.temporary.cleanup()

        with ThreadPoolExecutor(max_workers=1) as worker:
            report = worker.submit(lambda: asyncio.run(fixture_report())).result(timeout=10)
        self.assertTrue(report['real_model'])
        self.assertFalse(report['actual_model_use_verified'])
        self.assertFalse(report['current_authority_valid'])
        page = self.browser.new_page(viewport={'width': 390, 'height': 844})
        self.addCleanup(page.close)
        effects = []

        def endpoint(route):
            path = route.request.url.split('/api/', 1)[1]
            effects.append(path)
            self.assertEqual(path, 'tasks/owned-web-goal/report-knowledge')
            self.assertEqual(route.request.post_data_json, {'schema_version': '1.0', 'bundle_sha256': report['bundle_sha256']})
            route.fulfill(json=report)

        page.route('**/api/**', endpoint)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        page.evaluate('checksum => {window.syntheticReportBundle = checksum;}', report['bundle_sha256'])
        page.get_by_role('button', name='REPORT', exact=True).click()
        page.get_by_role('button', name='TAKEOVER', exact=True).click()
        expect(page.get_by_test_id('web-goal-load')).to_be_disabled()
        expect(page.get_by_test_id('web-goal-read-knowledge-report')).to_be_enabled()
        self.assertEqual(effects, [])
        page.get_by_test_id('web-goal-read-knowledge-report').evaluate('button => {button.click(); button.click();}')
        expect(page.get_by_test_id('web-goal-knowledge-report')).to_be_visible()
        expect(page.get_by_text('No matching durable model acknowledgement. A saved proposal alone does not prove document use by the model.', exact=True)).to_be_visible()
        expect(page.get_by_text('Unavailable; this is not a running-task ownership failure.', exact=True)).to_be_visible()
        self.assertEqual(effects, ['tasks/owned-web-goal/report-knowledge'])
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        report['training_ready'] = True
        page.get_by_test_id('web-goal-read-knowledge-report').click()
        expect(page.get_by_test_id('web-goal-knowledge-report')).to_have_count(0)
        expect(page.get_by_text('Saved document-binding evidence could not be verified. No model or task was retried.', exact=True)).to_be_visible()
        self.assertEqual(effects, ['tasks/owned-web-goal/report-knowledge', 'tasks/owned-web-goal/report-knowledge'])

    def test_recovery_is_single_dispatch_and_discards_late_takeover_response(self):
        from playwright.sync_api import expect

        page = self.browser.new_page()
        self.addCleanup(page.close)
        pending, effects = [], []

        def endpoint(route):
            path = route.request.url.split('/api/', 1)[1]
            if path == 'state':
                route.fulfill(json={'control': {'owner': 'AGENT', 'status': 'running',
                    'lease_id': 'synthetic-lease', 'generation': 2},
                    'runtime': {'runtime_id': 'synthetic-runtime'}})
            elif path == 'tasks/owned-web-goal/recover-start':
                effects.append(path)
                self.assertEqual(route.request.post_data_json['confirm_job_id'], 'job-synthetic')
                pending.append(route)
            else:
                raise AssertionError('Unexpected API dispatch: ' + path)

        page.route('**/api/**', endpoint)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        page.get_by_role('button', name='RECOVERY', exact=True).click()
        expect(page.get_by_test_id('web-goal-load')).to_be_disabled()
        page.get_by_test_id('web-goal-recovery-job').fill('job-synthetic')
        expect(page.get_by_test_id('web-goal-recover-start')).to_be_disabled()
        page.get_by_test_id('web-goal-recovery-confirm').check()
        with page.expect_request('**/api/tasks/owned-web-goal/recover-start'):
            page.get_by_test_id('web-goal-recover-start').evaluate(
                "button => {button.click(); button.click();}")
        expect(page.get_by_test_id('web-goal-recover-start')).to_be_disabled()
        expect(page.get_by_text('Reading back the existing task. No task is being restarted…', exact=True)).to_be_visible()
        self.assertEqual(effects, ['tasks/owned-web-goal/recover-start'])
        page.get_by_role('button', name='TAKEOVER', exact=True).click()
        expect(page.get_by_test_id('web-goal-recover-start')).to_be_disabled()
        pending[0].fulfill(json={'schema_version': '1.0', 'job_id': 'job-synthetic',
            'proposal_bundle_sha256': '7' * 64, 'acknowledgement_recovered': True,
            'execution_authorized': False, 'independently_verified': False, 'gpu_release_verified': False})
        page.wait_for_load_state('networkidle')
        expect(page.get_by_test_id('web-goal-read-result')).to_have_count(0)
        self.assertEqual(effects, ['tasks/owned-web-goal/recover-start'])

    def test_readonly_result_discards_late_takeover_response(self):
        from playwright.sync_api import expect

        page = self.browser.new_page()
        self.addCleanup(page.close)
        pending = []

        def endpoint(route):
            self.assertTrue(route.request.url.endswith('/api/tasks/owned-web-goal/report'))
            self.assertEqual(route.request.post_data_json,
                             {'schema_version': '1.0', 'bundle_sha256': '7' * 64})
            pending.append(route)

        page.route('**/api/**', endpoint)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        page.get_by_role('button', name='CONSUMED', exact=True).click()
        page.get_by_test_id('web-goal-read-result').evaluate('button => {button.click(); button.click();}')
        expect(page.get_by_test_id('web-goal-read-result')).to_be_disabled()
        self.assertEqual(len(pending), 1)
        page.get_by_role('button', name='TAKEOVER', exact=True).click()
        pending[0].fulfill(json={'schema_version': '1.0', 'proposal_bundle_sha256': '7' * 64,
            'job_id': 'job-synthetic', 'job_status': 'succeeded', 'status': 'verified',
            'independently_verified': True, 'receipt_sha256': '6' * 64, 'report_sha256': '5' * 64,
            'verification_scope': 'owned_synthetic_recipe', 'execution_authorized': False,
            'training_ready': False, 'gpu_release_verified': False})
        page.wait_for_load_state('networkidle')
        expect(page.get_by_test_id('web-goal-result')).to_have_count(0)
        expect(page.get_by_test_id('web-goal-read-result')).to_be_enabled()

    def test_optional_document_context_requires_review_and_two_separate_consents(self):
        from datetime import datetime, timedelta, timezone
        from playwright.sync_api import expect
        from aos.contracts import digest
        from aos.knowledge import text_sha256
        from aos.owned_skill_knowledge import build_planning_knowledge_context

        page = self.browser.new_page(viewport={'width': 390, 'height': 844})
        self.addCleanup(page.close)
        effects, reviews = [], []
        scope = {'application_id': 'synthetic-notes', 'tenant_id': 'synthetic-tenant', 'account_role': 'tester'}
        skill = {'skill_ref': 'save-message', 'description': 'Synthetic note recipe',
            'recipe_sha256': 'e' * 64, 'release_sha256': 'f' * 64, 'selection_sha256': 'a' * 64,
            'parameters': {'message': {'description': 'Exact text', 'min_chars': 1, 'max_chars': 128}}}
        catalog = {'schema_version': '1.0', 'synthetic': True, 'profile_sha256': 'b' * 64,
            'application_key': scope['application_id'], 'tenant_key': scope['tenant_id'], 'account_role': scope['account_role'],
            'execution_authorized': False, 'activation_authorized': False, 'training_ready': False,
            'scope_authorization_verified': False, 'skills': [skill]}

        def endpoint(route):
            path = route.request.url.split('/api/', 1)[1]
            if path == 'state':
                route.fulfill(json={'control': {'owner': 'AGENT', 'status': 'running', 'lease_id': 'synthetic-lease', 'generation': 2},
                    'runtime': {'runtime_id': 'synthetic-runtime'}})
                return
            effects.append(path)
            body = route.request.post_data_json
            if path == 'tasks/owned-web-goal/catalog':
                route.fulfill(json={'catalog': catalog, 'catalog_sha256': digest(catalog)})
            elif path == 'tasks/owned-web-goal/preview-knowledge':
                self.assertEqual(body['scope'], scope)
                self.assertEqual((body['top_k'], body['context_chars']), (4, 2048))
                excerpt = 'café synthetic note'
                hit = {'document_sha256': '1' * 64, 'content_sha256': '2' * 64, 'review_sha256': '3' * 64,
                    'source_id': 'synthetic-doc', 'title': 'Synthetic café note', 'revision': 1, 'chunk_index': 0,
                    'chunk_sha256': text_sha256(excerpt), 'start': 0, 'end': len(excerpt), 'text': excerpt, 'score': 1, 'synthetic': True}
                retrieval = {'schema_version': '1.0', 'kind': 'knowledge_search', 'scope': scope,
                    'query_sha256': text_sha256(body['query']), 'method': 'deterministic_lexical', 'top_k': 4, 'context_chars': 2048,
                    'used_context_chars': len(excerpt), 'hits': [hit], 'untrusted': True, 'execution_authorized': False,
                    'training_ready': False, 'gold': False}
                authority = {key: '4' * 64 for key in ('source_manifest_sha256', 'source_fingerprint_sha256',
                    'family_sha256', 'review_sha256', 'candidate_sha256')}
                authority.update({'manager_session': 'synthetic-manager', 'desktop_session_id': 'synthetic-session',
                    'runtime_id': 'synthetic-runtime', 'lease_id': 'synthetic-lease', 'generation': 2,
                    'reuse_admission_sha256': 'd' * 64, 'source_run_ref': 'c' * 64, 'source_invocation_sha256': 'a' * 64,
                    **{key: skill[key] for key in ('release_sha256', 'selection_sha256', 'recipe_sha256')}})
                context = build_planning_knowledge_context(retrieval, body['query'])
                review = {'schema_version': '1.0', 'kind': 'web_goal_knowledge_review', 'goal': body['goal'],
                    'authority': authority, 'catalog_sha256': digest(catalog), 'deployment_sha256': '5' * 64,
                    'query': body['query'], 'retrieval': retrieval,
                    'workspace_identity': {'version': 'descriptor-workspace-v1', 'path_sha256': '6' * 64, 'device': 1, 'inode': 2, 'owner_uid': 1000},
                    'store_identity': {'device': 1, 'inode': 3, 'owner': 1000, 'mode': 448},
                    'payload': {'context_version': 'aos-owned-skill-knowledge-v1', 'context_text': context,
                        'context_sha256': text_sha256(context), 'untrusted': True},
                    'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
                review['confirm_sha256'] = digest(review)
                reviews.append(review)
                route.fulfill(json={'schema_version': '1.0', 'review': review, 'execution_authorized': False, 'training_ready': False})
            elif path == 'tasks/owned-web-goal/propose-knowledge':
                self.assertEqual(body['review'], reviews[-1])
                self.assertEqual(body['confirm_knowledge_sha256'], reviews[-1]['confirm_sha256'])
                self.assertIs(body['inference_consent'], True)
                self.assertIs(body['storage_consent'], True)
                route.fulfill(json={'available': True, 'status': 'pending', 'execution_authorized': False,
                    'planning_id': 'planning-' + '7' * 32})
            else:
                raise AssertionError('Unexpected API dispatch: ' + path)

        page.route('**/api/**', endpoint)
        page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        page.get_by_role('button', name='KNOWLEDGE', exact=True).click()
        page.get_by_test_id('web-goal-load').click()
        page.get_by_test_id('web-goal-free-text').fill('Save message gamma')
        page.get_by_test_id('web-goal-knowledge-query').fill('café')
        page.get_by_test_id('web-goal-knowledge-preview').click()
        expect(page.get_by_test_id('web-goal-knowledge-review')).to_be_visible()
        expect(page.get_by_text('café synthetic note', exact=True)).to_be_visible()
        expect(page.get_by_test_id('web-goal-knowledge-generate')).to_be_disabled()
        page.get_by_test_id('web-goal-document-inference-consent').check()
        expect(page.get_by_test_id('web-goal-knowledge-generate')).to_be_disabled()
        page.get_by_test_id('web-goal-document-storage-consent').check()
        expect(page.get_by_test_id('web-goal-knowledge-generate')).to_be_enabled()
        page.get_by_test_id('web-goal-knowledge-query').fill('café changed')
        expect(page.get_by_test_id('web-goal-knowledge-review')).to_have_count(0)
        page.get_by_test_id('web-goal-knowledge-preview').click()
        expect(page.get_by_test_id('web-goal-document-inference-consent')).not_to_be_checked()
        expect(page.get_by_test_id('web-goal-document-storage-consent')).not_to_be_checked()
        page.get_by_test_id('web-goal-document-inference-consent').check()
        page.get_by_test_id('web-goal-document-storage-consent').check()
        page.get_by_test_id('web-goal-knowledge-generate').click()
        expect(page.get_by_test_id('web-goal-planning-progress')).to_be_visible()
        self.assertEqual(effects, ['tasks/owned-web-goal/catalog', 'tasks/owned-web-goal/preview-knowledge',
            'tasks/owned-web-goal/preview-knowledge', 'tasks/owned-web-goal/propose-knowledge'])
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))

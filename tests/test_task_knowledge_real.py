from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from aos.contracts import HELLO_CONTENT, Option, Prediction, REPO_ROOT, canonical, digest


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_TASK_KNOWLEDGE_REAL_TESTS') == '1',
                     'Requires explicit isolated idle pinned Decider/Docker/Chromium-MCP; synthetic documents only')
class TaskKnowledgeRealTests(unittest.TestCase):
    def test_reviewed_context_reaches_real_hello_and_two_browser_decisions(self):
        from test_desktop_task_ui import task_server
        from playwright.sync_api import expect, sync_playwright

        root = Path(tempfile.mkdtemp(prefix='task-knowledge-real-', dir=REPO_ROOT / 'data'))
        root.chmod(0o700)
        deployment = 'decider-' + digest(json.loads((REPO_ROOT / 'models/decider-manifest.json').read_text()))
        scope = {'application_id': 'synthetic-evaluation', 'tenant_id': 'synthetic', 'account_role': 'reader'}
        flags = ('--knowledge-root', str(root / 'knowledge'), '--browser-tasks', '--desktop-browser',
                 '--desktop-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'), '--reuse-decider')
        began = time.perf_counter()
        results = []
        with task_server(root, 'decider', flags) as (origin, token, client, server):
            def post(path, value):
                response = client.post(path, json=value)
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            def authority():
                control = client.get('/api/state').json()['control']
                return {key: control[key] for key in ('lease_id', 'generation')}

            preview = post('/api/knowledge/publish-preview', {'schema_version': '1.0', 'scope': scope,
                'source_id': 'manual', 'title': 'Synthetic task manual',
                'text': 'Synthetic hello and browser manual: inspect the fresh observation, choose only an authorized option, and verify exact saved content.',
                'previous_sha256': None, 'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                'rights_attested': True, 'storage_consent': True, 'synthetic': True})
            document = post('/api/knowledge/publish', {'schema_version': '1.0', 'preview': preview,
                'confirm_sha256': preview['preview_sha256'], **authority()})
            review = post('/api/knowledge/review-preview', {'schema_version': '1.0', 'scope': scope,
                'document_sha256': document['document_sha256'], 'decision': 'accept'})
            post('/api/knowledge/review', {'schema_version': '1.0', 'preview': review,
                'confirm_sha256': review['preview_sha256'], **authority()})
            for kind, expected_calls in (('hello', 1), ('browser_form', 2)):
                preview = post('/api/tasks/knowledge/preview', {'schema_version': '1.0', 'scope': scope,
                    'query': 'hello browser manual', 'top_k': 4, 'context_chars': 2048, 'task_kind': kind, **authority()})
                self.assertEqual(preview['target']['system1_deployment_id'], deployment)
                started = post('/api/tasks/knowledge/start', {'schema_version': '1.0', 'preview': preview,
                    'confirm_sha256': preview['confirm_sha256'], 'consent': True, **authority()})
                approved = set()
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    self.assertIsNone(status['auto_approval'])
                    job = next(item for item in status['jobs'] if item['job_id'] == started['job_id'])
                    pending = status['approval']
                    if pending is not None and pending['approval_id'] not in approved:
                        self.assertEqual(pending['job_id'], started['job_id'])
                        if kind == 'hello':
                            self.assertFalse((root / 'workspace/hello.txt').exists())
                        approved.add(pending['approval_id'])
                        post('/api/approvals/' + pending['approval_id'], {'action_sha256': pending['action_sha256'], 'accept': True})
                    if job['status'] in {'succeeded', 'failed', 'cancelled'} and not status['reserved']:
                        break
                    time.sleep(.05)
                else:
                    self.fail('Task did not settle within the isolated acceptance window')
                self.assertEqual(job['status'], 'succeeded', job)
                self.assertEqual(len(approved), expected_calls)
                report = post('/api/tasks/knowledge/report', {'schema_version': '1.0', 'job_id': job['job_id']})
                for field in ('historical_binding_verified', 'context_binding_verified', 'model_request_verified',
                              'knowledge_applied', 'current_source_valid', 'current_authority_valid'):
                    self.assertTrue(report[field], (field, report))
                for field in ('scope_authorization_verified', 'causality_verified', 'gold', 'training_ready', 'execution_authorized'):
                    self.assertFalse(report[field])
                self.assertEqual(report['successful_model_call_count'], expected_calls)
                self.assertEqual(report['prepared_context_count'], expected_calls)
                self.assertEqual(report['dispatched_context_count'], expected_calls)
                self.assertEqual(len(report['model_proofs']), expected_calls)
                dispatches = {proof['call_id']: proof for proof in report['dispatch_proofs']}
                for proof in report['model_proofs']:
                    self.assertEqual(proof['deployment_id'], deployment)
                    self.assertEqual(proof['response_sha256'], digest(proof['response']))
                    self.assertEqual(proof['response_canonical'], canonical(proof['response']))
                    self.assertEqual(proof['actual_request_sha256'], dispatches[proof['call_id']]['actual_request_sha256'])
                    self.assertEqual(proof['snapshot_id'], dispatches[proof['call_id']]['snapshot_id'])
                    prediction = Prediction.model_validate(proof['response'])
                    prediction.validate_options([Option.model_validate(option) for option in proof['options']])
                self.assertEqual(report['outcome']['status'], 'verified', report['outcome'])
                for proof in report['dispatch_proofs']:
                    request = proof['actual_request']
                    self.assertEqual(digest(request), proof['request_sha256'])
                    self.assertEqual(proof['actual_request_sha256'], proof['request_sha256'])
                    self.assertIn(preview['context_text'], request['state'])
                if kind == 'hello':
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                descriptor = os.open(root / ('audit-report-' + kind + '.json'),
                                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    output.write(canonical(report))
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(REPO_ROOT
                        / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                    try:
                        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(origin + '/ui/')
                        expect(page).to_have_title('AOS · Control center')
                        page.get_by_label('Local session token', exact=True).fill(token)
                        page.get_by_role('button', name='Sign in', exact=True).click()
                        page.get_by_role('button', name='Tasks', exact=True).click()
                        page.get_by_test_id('task-knowledge').locator('summary').click()
                        page.get_by_test_id('task-knowledge-report-job').select_option(job['job_id'])
                        page.get_by_test_id('task-knowledge-report-refresh').click()
                        expect(page.get_by_test_id('task-knowledge-report-applied')).to_contain_text(
                            'Document context applied to an actual model call')
                        expect(page.get_by_test_id('task-knowledge-report-counts')).to_contain_text(
                            f'{expected_calls} / {expected_calls} / {expected_calls}')
                        self.assertEqual(errors, [])
                        page.screenshot(path='/tmp/aos-task-knowledge-real-report-' + kind + '.png', full_page=True)
                    finally:
                        browser.close()
                results.append({'task_kind': kind, 'job_id': job['job_id'], 'run_id': job['run_id'],
                    'intent_sha256': started['intent_sha256'], 'successful_model_call_count': expected_calls,
                    'manual_approval_count': len(approved), 'independent_outcome': report['outcome'],
                    'authenticated_real_report_ui_verified': True, 'real_model': True})
        summary = {'schema_version': '1.0', 'synthetic': True, 'deployment_id': deployment, 'cases': results,
            'elapsed_seconds': time.perf_counter() - began, 'scope_authorization_verified': False,
            'causality_verified': False, 'gold': False, 'training_ready': False, 'real_site_acceptance': False}
        descriptor = os.open(root / 'acceptance.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(canonical(summary))
        print(json.dumps({'private_acceptance': str(root), 'real_decider': True, 'cases_passed': len(results)}))

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aos.contracts import REPO_ROOT, digest


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker and built Chromium UI; report payloads are synthetic fixtures')
class TaskKnowledgeReportUITests(unittest.TestCase):
    def test_historical_fixture_report_and_tampered_model_claims(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        fixture = json.loads((REPO_ROOT / 'examples/task_knowledge.json').read_text())['responses']['report']
        self.assertEqual(fixture['model_proofs'], [])
        payload = [deepcopy(fixture)]
        browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
        with tempfile.TemporaryDirectory(prefix='task-knowledge-report-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--knowledge-root', str(Path(directory) / 'knowledge'))) as (
                    origin, token, client, server), sync_playwright() as playwright:
                fixture['intent']['preview']['target']['parent_runtime_id'] = client.get('/api/state').json()['runtime']['runtime_id']
                preview = fixture['intent']['preview']
                preview['confirm_sha256'] = digest({key: value for key, value in preview.items() if key != 'confirm_sha256'})
                fixture['intent_sha256'] = digest(fixture['intent'])
                fixture['admission'].update(intent_sha256=fixture['intent_sha256'], preview_sha256=preview['confirm_sha256'], target=deepcopy(preview['target']))
                fixture['admission_sha256'] = digest(fixture['admission'])
                fixture['context_proofs'][0].update(intent_sha256=fixture['intent_sha256'], admission_sha256=fixture['admission_sha256'])
                payload[0] = deepcopy(fixture)
                browser = playwright.chromium.launch(executable_path=str(browser_path))
                errors, writes = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'en')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)

                    def tasks_route(route):
                        response = route.fetch()
                        body = response.json()
                        body['jobs'].append({'job_id': fixture['job_id'], 'run_id': fixture['context_proofs'][0]['run_id'],
                                             'kind': 'hello', 'status': 'succeeded', 'real_model': 0, 'runtime_id': None})
                        route.fulfill(response=response, json=body)

                    page.route('**/api/tasks', tasks_route)
                    page.route('**/api/tasks/knowledge/report', lambda route: route.fulfill(json=payload[0]))
                    page.goto(origin + '/ui/')
                    expect(page).to_have_title('AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    panel = page.get_by_test_id('task-knowledge')
                    panel.locator('summary').click()
                    page.get_by_test_id('task-knowledge-report-job').select_option(fixture['job_id'])
                    refresh = page.get_by_test_id('task-knowledge-report-refresh')
                    refresh.click()
                    report = page.get_by_test_id('task-knowledge-report')
                    expect(report).to_be_visible()
                    expect(report).to_contain_text('Server-audited historical binding: true')
                    expect(report).to_contain_text('Independent task outcome: verified')
                    expect(page.get_by_test_id('task-knowledge-report-applied')).to_contain_text('fixture preparation is not model use')
                    expect(page.get_by_test_id('task-knowledge-report-counts')).to_contain_text('1 / 0 / 0')
                    expect(page.get_by_test_id('task-knowledge-report-citation')).to_contain_text('Synthetic manual')
                    page.get_by_test_id('task-knowledge-query').fill('different query')
                    expect(page.get_by_test_id('task-knowledge-report-job')).to_have_value(fixture['job_id'])
                    page.screenshot(path='/tmp/aos-task-knowledge-report-en.png', full_page=True)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(report).to_contain_text('Sunucuda denetlenmiş tarihsel bağ')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    page.screenshot(path='/tmp/aos-task-knowledge-report-tr-mobile.png', full_page=True)
                    for mutation in ('flags', 'count', 'context', 'intent', 'options', 'applied', 'foreign-runtime'):
                        payload[0] = deepcopy(fixture)
                        if mutation == 'flags':
                            payload[0]['training_ready'] = True
                        elif mutation == 'count':
                            payload[0]['prepared_context_count'] += 1
                        elif mutation == 'context':
                            payload[0]['intent']['preview']['context_text'] += 'changed'
                        elif mutation == 'intent':
                            payload[0]['intent_sha256'] = '0' * 64
                        elif mutation == 'options':
                            payload[0]['context_proofs'][0]['options'][0]['label'] = 'tampered'
                        elif mutation == 'applied':
                            payload[0].update(knowledge_applied=True, model_request_verified=True, successful_model_call_count=1)
                        else:
                            foreign_preview = payload[0]['intent']['preview']
                            foreign_preview['target']['parent_runtime_id'] = 'foreign-runtime'
                            foreign_preview['confirm_sha256'] = digest({key: value for key, value in foreign_preview.items() if key != 'confirm_sha256'})
                            payload[0]['intent_sha256'] = digest(payload[0]['intent'])
                            payload[0]['admission'].update(intent_sha256=payload[0]['intent_sha256'], preview_sha256=foreign_preview['confirm_sha256'], target=deepcopy(foreign_preview['target']))
                            payload[0]['admission_sha256'] = digest(payload[0]['admission'])
                            payload[0]['context_proofs'][0].update(intent_sha256=payload[0]['intent_sha256'], admission_sha256=payload[0]['admission_sha256'])
                        refresh.click()
                        expect(page.get_by_test_id('task-knowledge-report-error')).to_be_visible()
                        expect(report).to_have_count(0)
                    applied = deepcopy(fixture)
                    preview = applied['intent']['preview']
                    preview['target']['system1_deployment_id'] = 'decider-synthetic-audit'
                    preview['confirm_sha256'] = digest({key: value for key, value in preview.items() if key != 'confirm_sha256'})
                    applied['intent_sha256'] = digest(applied['intent'])
                    applied['admission'].update(intent_sha256=applied['intent_sha256'], preview_sha256=preview['confirm_sha256'], target=deepcopy(preview['target']))
                    applied['admission_sha256'] = digest(applied['admission'])
                    marker = applied['context_proofs'][0]
                    request = {'state': 'Synthetic goal\nObservation: ' + preview['context_text'],
                               'question': 'What is the next action?', 'options': deepcopy(marker['options'])}
                    marker.update(intent_sha256=applied['intent_sha256'], admission_sha256=applied['admission_sha256'],
                                  call_id='call-synthetic-audit', request_sha256=digest(request))
                    applied['dispatch_proofs'] = [dict(marker, actual_request_sha256=digest(request), actual_request=request)]
                    response = {'selected_option': marker['options'][0]['id'], 'probabilities': {
                        option['id']: probability for option, probability in zip(marker['options'], [0.9999899, 1e-5, 1e-7])}}
                    response_canonical = json.dumps(response, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                    applied['model_proofs'] = [dict(marker, deployment_id='decider-synthetic-audit',
                        decision_id='decision-synthetic-audit', actual_request_sha256=digest(request),
                        response_sha256=digest(response), response_canonical=response_canonical, response=response)]
                    applied.update(model_request_verified=True, knowledge_applied=True, dispatched_context_count=1, successful_model_call_count=1)
                    payload[0] = deepcopy(applied)
                    refresh.click()
                    expect(report).to_be_visible()
                    expect(page.get_by_test_id('task-knowledge-report-applied')).to_contain_text('gerçek model çağrısına uygulandı')
                    for mutation in ('response', 'deployment', 'request', 'model-count', 'duplicate-decision'):
                        payload[0] = deepcopy(applied)
                        if mutation == 'response':
                            payload[0]['model_proofs'][0]['response_sha256'] = '0' * 64
                        elif mutation == 'deployment':
                            payload[0]['model_proofs'][0]['deployment_id'] = 'decider-other'
                        elif mutation == 'request':
                            payload[0]['dispatch_proofs'][0]['actual_request']['state'] += 'tampered'
                        elif mutation == 'model-count':
                            payload[0]['model_proofs'] = []
                        else:
                            for field in ('context_proofs', 'dispatch_proofs', 'model_proofs'):
                                repeated = deepcopy(payload[0][field][0])
                                repeated.update(call_id='call-synthetic-second', snapshot_id='snapshot-synthetic-second',
                                                step_id='step-synthetic-second', state_version=repeated['state_version'] + 1)
                                payload[0][field].append(repeated)
                            payload[0].update(prepared_context_count=2, dispatched_context_count=2, successful_model_call_count=2)
                        refresh.click()
                        expect(page.get_by_test_id('task-knowledge-report-error')).to_be_visible()
                        expect(report).to_have_count(0)
                    payload[0] = deepcopy(fixture)
                    payload[0].update(current_source_valid=False, current_authority_valid=False)
                    refresh.click()
                    expect(report).to_be_visible()
                    expect(report).to_contain_text('Güncel kaynak geçerli: false')
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertTrue(all(url in (origin + '/api/login', origin + '/api/tasks/knowledge/report') for url in writes))
                    self.assertEqual(writes.count(origin + '/api/login'), 1)
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    with sqlite3.connect('file:' + str(Path(directory) / 'store.sqlite') + '?mode=ro', uri=True) as connection:
                        self.assertEqual(connection.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

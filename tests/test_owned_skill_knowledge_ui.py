from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.owned_skill_knowledge import FLAGS
from aos.owned_skill_knowledge_transport import OwnedSkillKnowledgeCanonicalStartRequest, preview_transport, report_transport


def report_wire(report):
    intent = report['bundle']['knowledge']['intent']
    preview = intent['preview']
    return {'schema_version': '1.1', 'report': report, 'preview_canonical': canonical(preview),
        'preview_body_canonical': canonical({key: value for key, value in preview.items() if key != 'confirm_sha256'}),
        'model_pins_canonical': canonical(preview['model_pins']), 'intent_canonical': canonical(intent),
        'model_request_canonical': canonical(report['bundle']['model_request'])}


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker and built Chromium UI; all S2 endpoint payloads are synthetic fixtures')
class OwnedSkillKnowledgeUITests(unittest.TestCase):
    def test_separate_s2_consent_and_readonly_fixture_proof(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        example = json.loads((REPO_ROOT / 'examples/owned_skill_knowledge.json').read_text())
        browser_path = REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
        with tempfile.TemporaryDirectory(prefix='owned-skill-knowledge-ui-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            with task_server(root, 'fixture', ('--knowledge-root', str(root / 'knowledge'))) as (
                    origin, token, client, server), sync_playwright() as playwright:
                snapshot = client.get('/api/state').json()
                preview = deepcopy(example['responses']['preview'])
                preview['authority'].update(runtime_id=snapshot['runtime']['runtime_id'], lease_id=snapshot['control']['lease_id'], generation=snapshot['control']['generation'])
                preview['expires_at'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                preview['model_pins']['temperature'] = 0.0
                preview['deployment']['pins'] = deepcopy(preview['model_pins'])
                preview['deployment'].update(real_model=False, kind='fixture_owned_skill_planner', deployment_id='fixture-' + digest(preview['model_pins']))
                preview['confirm_sha256'] = digest({key: value for key, value in preview.items() if key != 'confirm_sha256'})
                intent = {'schema_version': '1.0', 'preview': deepcopy(preview), 'inference_consent': True, 'storage_consent': True}
                bundle = deepcopy(json.loads((REPO_ROOT / 'examples/owned_skill_planning_bundle.json').read_text())['bundle'])
                bundle.update(schema_version='1.2', planning_id='planning-' + '1' * 32, authority=deepcopy(preview['authority']),
                    evidence=deepcopy(preview['evidence']), deployment=deepcopy(preview['deployment']), model_pins=deepcopy(preview['model_pins']), real_model=False)
                bundle['request'] = {'schema_version': '1.0', 'goal': preview['goal'], 'case_key': preview['case_key'],
                                     'lease_id': preview['authority']['lease_id'], 'generation': preview['authority']['generation']}
                bundle['model_request'] = deepcopy(example['model_request'])
                bundle['model_request']['model'] = preview['deployment']['deployment_id']
                bundle['model_request']['temperature'] = 0.0
                bundle['metrics'].update(latency_ms=1e-7, pin_verify_ms=-0.0, request_build_ms=1e-5)
                bundle['model_response'].update(case_key=preview['case_key'], parameter_value=preview['evidence'][0]['requested_value'], steps=deepcopy(preview['evidence'][0]['ordered_steps']))
                bundle['knowledge'] = {'intent_sha256': digest(intent), 'intent': intent, 'dispatch': {
                    'schema_version': '1.0', 'planning_id': bundle['planning_id'], 'intent_sha256': digest(intent),
                    'preview_sha256': preview['confirm_sha256'], 'context_sha256': preview['context_sha256'],
                    'request_sha256': digest(bundle['model_request']), 'deployment_id': preview['deployment']['deployment_id']}}
                checksum = digest(bundle)
                report = {**FLAGS, 'schema_version': '1.0', 'kind': 'owned_skill_knowledge_report', 'planning_bundle_sha256': checksum,
                    'bundle': bundle, 'bundle_canonical': canonical(bundle), 'historical_binding_verified': True,
                    'current_source_valid': True, 'current_authority_valid': True, 'dispatch_recorded': True,
                    'model_request_verified': False, 'knowledge_applied': False, 'real_model': False,
                    'downstream_verified': False, 'semantic_relevance_verified': False}
                report_payload = [report_transport(report)]
                preview_payload = [preview_transport(preview)]
                status = {'available': True, 'status': 'idle', 'execution_authorized': False, 'knowledge_available': True}
                browser = playwright.chromium.launch(executable_path=str(browser_path))
                errors, writes = [], []
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("localStorage.setItem('aos.ui.language', 'en')")
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type in ('error', 'warning') else None)
                    page.on('request', lambda request: writes.append((request.url, request.post_data_json)) if request.method == 'POST' else None)

                    def tasks_route(route):
                        response = route.fetch()
                        body = response.json()
                        body['kinds'].append('browser_remote_form')
                        body['owned_form_invocation'] = {'mode': 'owned_synthetic_form_invocation', 'lifecycle': 'ready',
                            'profile_sha256': '1' * 64, 'skill_sha256': '2' * 64, 'invocation_sha256': '3' * 64,
                            'reuse_admission_sha256': preview['authority']['reuse_admission_sha256']}
                        body['owned_skill_planning'] = deepcopy(status)
                        route.fulfill(response=response, json=body)

                    def start_route(route):
                        request = route.request.post_data_json
                        self.assertEqual(request['schema_version'], '1.1')
                        self.assertNotIn('preview', request)
                        self.assertEqual(request['preview_canonical'], canonical(preview))
                        normalized = OwnedSkillKnowledgeCanonicalStartRequest.model_validate(request).normalized_start()
                        self.assertEqual(normalized['preview'], preview)
                        self.assertIsInstance(normalized['preview']['model_pins']['temperature'], float)
                        self.assertIs(request['inference_consent'], True)
                        self.assertIs(request['storage_consent'], True)
                        self.assertNotIn('collect_learning', request)
                        status.update(status='ready', planning_id=bundle['planning_id'], bundle_sha256=checksum, real_model=False, model_called=True)
                        route.fulfill(status=202, json={**status, 'status': 'pending', 'bundle_sha256': None})

                    page.route('**/api/tasks', tasks_route)
                    page.route('**/api/tasks/owned-skill-knowledge/preview', lambda route: route.fulfill(json=preview_payload[0]))
                    page.route('**/api/tasks/owned-skill-knowledge/start', start_route)
                    page.route('**/api/tasks/owned-skill-knowledge/report', lambda route: route.fulfill(json=report_payload[0]))
                    page.goto(origin + '/ui/')
                    expect(page).to_have_title('AOS · Control center')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_form')
                    panel = page.get_by_test_id('planning-knowledge')
                    expect(panel).to_be_visible()
                    panel.locator('summary').first.click()
                    for field, value in [('application', preview['scope']['application_id']), ('tenant', preview['scope']['tenant_id']),
                                         ('role', preview['scope']['account_role']), ('query', preview['query'])]:
                        page.get_by_test_id('planning-knowledge-' + field).fill(value)
                    prepare = page.get_by_test_id('planning-knowledge-preview')
                    prepare.click()
                    expect(page.get_by_test_id('planning-knowledge-context')).to_have_text(preview['context_text'])
                    start = page.get_by_test_id('planning-knowledge-start')
                    expect(start).to_be_disabled()
                    page.get_by_test_id('planning-knowledge-confirm').fill(preview['confirm_sha256'])
                    page.get_by_test_id('planning-knowledge-inference').check()
                    expect(start).to_be_disabled()
                    page.get_by_test_id('planning-knowledge-storage').check()
                    expect(start).to_be_enabled()
                    page.get_by_test_id('planning-knowledge-query').fill('changed')
                    expect(start).to_have_count(0)
                    page.get_by_test_id('planning-knowledge-query').fill(preview['query'])
                    page.get_by_test_id('episode-opt-in').check()
                    prepare.click()
                    expect(page.get_by_test_id('planning-knowledge-context')).to_be_visible()
                    page.get_by_test_id('planning-knowledge-confirm').fill(preview['confirm_sha256'])
                    page.get_by_test_id('planning-knowledge-inference').check()
                    page.get_by_test_id('planning-knowledge-storage').check()
                    expect(start).to_be_disabled()
                    page.get_by_test_id('episode-opt-in').uncheck()
                    expect(start).to_have_count(0)
                    prepare.click()
                    expect(page.get_by_test_id('planning-knowledge-context')).to_be_visible()
                    page.get_by_test_id('planning-knowledge-confirm').fill(preview['confirm_sha256'])
                    page.get_by_test_id('planning-knowledge-inference').check()
                    page.get_by_test_id('planning-knowledge-storage').check()
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    start.click()
                    expect(page.get_by_test_id('planning-knowledge-report-hash')).to_have_value(checksum)
                    expect(page.get_by_test_id('planning-bind')).to_be_enabled()
                    refresh = page.get_by_test_id('planning-knowledge-report-refresh')
                    refresh.click()
                    result = page.get_by_test_id('planning-knowledge-report')
                    expect(result).to_be_visible()
                    expect(page.get_by_test_id('planning-knowledge-applied')).to_have_text('Fixture preparation; actual model use is not verified')
                    expect(page.get_by_test_id('planning-knowledge-citation')).to_contain_text('Synthetic planning manual')
                    panel.screenshot(path='/tmp/aos-owned-skill-knowledge-en.png')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('S2 planlama için isteğe bağlı incelenmiş belge bağlamı')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    panel.screenshot(path='/tmp/aos-owned-skill-knowledge-tr-mobile.png')
                    for mutation in ('flag', 'applied', 'canonical', 'request', 'dispatch', 'preview_canonical', 'preview_body_canonical',
                                     'model_pins_canonical', 'intent_canonical', 'model_request_canonical'):
                        report_payload[0] = report_transport(report)
                        corrupted = report_payload[0]['report']
                        if mutation == 'flag':
                            corrupted['training_ready'] = True
                        elif mutation == 'applied':
                            corrupted['knowledge_applied'] = True
                        elif mutation == 'canonical':
                            corrupted['bundle_canonical'] += ' '
                        elif mutation == 'request':
                            corrupted['bundle']['model_request']['messages'][1]['content'] += 'changed'
                        elif mutation == 'dispatch':
                            corrupted['bundle']['knowledge']['dispatch']['context_sha256'] = '0' * 64
                        else:
                            report_payload[0][mutation] = report_payload[0][mutation].replace('"temperature":0.0', '"temperature":0')
                        refresh.click()
                        expect(page.get_by_test_id('planning-knowledge-error')).to_be_visible()
                        expect(result).to_have_count(0)
                    report_payload[0] = report_transport(report)
                    report_payload[0]['report'].update(current_source_valid=False, current_authority_valid=False)
                    refresh.click()
                    expect(result).to_be_visible()
                    expect(result).to_contain_text('Güncel kaynak / yetki geçerli: false / false')
                    for mutation in ('context-join', 'response-authority', 'foreign-runtime'):
                        broken_report = deepcopy(report)
                        broken_bundle = broken_report['bundle']
                        if mutation == 'context-join':
                            broken_bundle['knowledge']['dispatch']['context_sha256'] = '0' * 64
                        elif mutation == 'response-authority':
                            broken_bundle['model_response']['activation_authorized'] = True
                        else:
                            broken_preview = broken_bundle['knowledge']['intent']['preview']
                            broken_preview['authority']['runtime_id'] = 'foreign-runtime'
                            broken_preview['confirm_sha256'] = digest({key: value for key, value in broken_preview.items() if key != 'confirm_sha256'})
                            broken_bundle['knowledge']['intent_sha256'] = digest(broken_bundle['knowledge']['intent'])
                        broken_checksum = digest(broken_bundle)
                        broken_report.update(planning_bundle_sha256=broken_checksum, bundle_canonical=canonical(broken_bundle))
                        report_payload[0] = report_wire(broken_report)
                        page.get_by_test_id('planning-knowledge-report-hash').fill(broken_checksum)
                        refresh.click()
                        expect(page.get_by_test_id('planning-knowledge-error')).to_be_visible()
                        expect(result).to_have_count(0)
                    report_payload[0] = report_transport(report)
                    report_payload[0]['report'].update(current_source_valid=False, current_authority_valid=False)
                    page.get_by_test_id('planning-knowledge-report-hash').fill(checksum)
                    for field in ('preview_canonical', 'preview_body_canonical', 'model_pins_canonical'):
                        preview_payload[0] = preview_transport(preview)
                        preview_payload[0][field] = preview_payload[0][field].replace('"temperature":0.0', '"temperature":0')
                        prepare.click()
                        expect(page.get_by_test_id('planning-knowledge-error')).to_be_visible()
                        expect(page.get_by_test_id('planning-knowledge-context')).to_have_count(0)
                    client.post('/api/control', json={'command': 'pause'}).raise_for_status()
                    expect(page.get_by_test_id('owner')).to_have_text('PAUSED')
                    expect(refresh).to_be_enabled()
                    refresh.click()
                    expect(result).to_be_visible()
                    self.assertEqual(errors, [])
                    self.assertEqual(page.locator('vite-error-overlay').count(), 0)
                    self.assertEqual(len([url for url, body in writes if url.endswith('/owned-skill-knowledge/start')]), 1)
                    self.assertTrue(all(url in (origin + '/api/login', origin + '/api/tasks/owned-skill-knowledge/preview',
                        origin + '/api/tasks/owned-skill-knowledge/start', origin + '/api/tasks/owned-skill-knowledge/report') for url, body in writes))
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    with sqlite3.connect('file:' + str(root / 'store.sqlite') + '?mode=ro', uri=True) as connection:
                        for table in ('model_calls', 'actions'):
                            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

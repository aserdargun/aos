import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker desktop and built UI')
class DevelopmentUITests(unittest.TestCase):
    def test_default_development_reports_idle_decider_state_without_gpu_claims(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='development-preparation-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    status = client.get('/api/tasks').json()
                    writes = []
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
                    page.route('**/api/tasks', lambda route: route.fulfill(json=status))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    panel = page.get_by_test_id('development-system1-preparation')
                    expect(page.get_by_test_id('development-work-order')).to_contain_text('SWAPP intranet/tunnel access and real-site acceptance are deferred to the final stage')
                    expect(page.get_by_test_id('development-work-order')).to_contain_text('do not block other work and are not counted as complete')
                    expect(panel).to_contain_text('Idle Decider preparation is unavailable')
                    expect(panel).to_contain_text('does not establish GPU use during a task')
                    for state, label in (('preparing', 'CPU model is preparing'),
                                         ('ready', 'CPU model is ready'),
                                         ('ready_gpu', 'GPU worker is idle for a bounded time'),
                                         ('inactive', 'No idle model is ready')):
                        status['decider_preparation'] = {'enabled': True, 'state': state}
                        page.get_by_role('button', name='Refresh', exact=True).click()
                        expect(panel.get_by_test_id('development-system1-preparation-state')).to_have_text(label)
                        if state == 'ready_gpu':
                            expect(panel).to_contain_text('does not measure VRAM, the next inference, or task success')
                    status['decider_preparation'] = {'enabled': True, 'state': 'ready_gpu'}
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(panel).to_contain_text('GPU worker süreli beklemede')
                    expect(page.get_by_test_id('development-work-order')).to_contain_text('SWAPP intranet/tünel bağlantısı ve gerçek-site kabulü son aşamaya ertelendi')
                    expect(page.get_by_test_id('development-release-acceptance')).to_contain_text('0/6 · 0%')
                    expect(panel).to_contain_text('bu kart VRAM miktarını, sonraki çıkarımı veya görev başarısını ölçmez')
                    self.assertEqual(writes, [origin + '/api/login'])
                finally:
                    browser.close()

    def test_form_metadata_panel_requires_plain_plan_and_first_approval(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='form-metadata-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    status = client.get('/api/tasks').json()
                    status['supports_remote_learning_metadata'] = True
                    status['kinds'].append('browser_remote_form')
                    status['remote_form'] = {
                        'plan_sha256': 'a' * 64, 'entry_url': 'https://fixture.invalid/entry',
                        'submit_url': 'https://fixture.invalid/submit',
                        'receipt_url': 'https://fixture.invalid/receipt',
                        'field_name': 'message', 'body_sha256': 'b' * 64,
                        'mode': 'synthetic_one_post'}
                    status['jobs'] = [{
                        'job_id': 'job-' + 'c' * 32, 'run_id': 'run-' + 'd' * 32,
                        'kind': 'browser_remote_form', 'status': 'waiting_approval',
                        'real_model': 0, 'runtime_id': 'browser-' + 'e' * 32,
                        'remote_learning_metadata': {'enabled': False, 'state': 'disabled'}}]
                    status['approval'] = {
                        'approval_id': 'approval-' + 'f' * 32, 'action_sha256': '1' * 64,
                        'expires_at': 9999999999,
                        'action': {'tool': 'browser.form.open', 'arguments': {'stage': 0}}}
                    page = browser.new_page()
                    page.route('**/api/tasks', lambda route: route.fulfill(json=status))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    panel = page.get_by_test_id('remote-learning-metadata')
                    expect(panel).to_contain_text('HTTPS form metadata recording')
                    panel.get_by_label('S1 decision metadata').check()
                    panel.get_by_label(
                        'As the local operator, I attest that I have the right to collect metadata for this run').check()
                    expect(panel.get_by_role('button', name='Preview metadata consent')).to_be_enabled()
                    status['approval']['action']['tool'] = 'browser.form.fill'
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_role('button', name='Preview metadata consent')).to_be_disabled()
                    status['remote_form']['state_plan_sha256'] = '2' * 64
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel).to_contain_text('All six stages need separate approvals')
                    expect(panel.get_by_role('button', name='Preview metadata consent')).to_be_disabled()
                    status['approval']['action']['tool'] = 'browser.form.open'
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_role('button', name='Preview metadata consent')).to_be_enabled()
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('Altı aşama ayrı onay ister')
                    status['remote_form'].pop('state_plan_sha256')
                    status['remote_form']['cookie_sha256'] = '3' * 64
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(panel).to_have_count(0)
                finally:
                    browser.close()

    def test_json_live_metadata_requires_review_pin_and_fresh_match(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='json-live-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    status = client.get('/api/tasks').json()
                    status['kinds'].append('browser_remote_static_assets')
                    status['remote_static_assets'] = {
                        'plan_sha256': 'a' * 64, 'asset_count': 1,
                        'assets': ['https://fixture.invalid/assets/app.js'],
                        'data_resources': ['https://fixture.invalid/api/summary'],
                        'mode': 'one_shot_readonly_data_bundle'}
                    status['jobs'] = [{
                        'job_id': 'job-' + 'b' * 32, 'run_id': 'run-live',
                        'kind': 'browser_remote_static_assets', 'status': 'succeeded',
                        'real_model': 0, 'runtime_id': 'browser-' + 'c' * 32}]
                    page.route('**/api/tasks', lambda route: route.fulfill(json=status))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    expect(page.get_by_test_id('development-status')).to_be_visible()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('remote-json-review-scope')).to_have_count(0)
                    expect(page.get_by_test_id('remote-json-live-knowledge')).to_have_count(0)

                    status['remote_static_assets']['review_sha256'] = 'd' * 64
                    status['jobs'][0]['json_knowledge'] = {
                        'status': 'matched', 'page_key': 'summary', 'draft_revision': 1}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    scope = page.get_by_test_id('remote-json-review-scope')
                    live = page.get_by_test_id('remote-json-live-knowledge')
                    expect(scope).to_contain_text('d' * 64)
                    expect(live).to_contain_text('summary')
                    expect(live).to_contain_text('Fresh readback fingerprint matched.')
                    expect(live).not_to_contain_text('fixture.invalid')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(live).to_contain_text('Taze readback parmak izi eşleşti.')

                    status['jobs'][0]['json_knowledge'] = {
                        'status': 'stale', 'page_key': None, 'draft_revision': None}
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(live).to_contain_text('Eski: JSON sayfa anahtarı raporlanmaz.')
                    expect(live).not_to_contain_text('summary')
                    status['remote_static_assets']['review_sha256'] = None
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(scope).to_have_count(0)
                    expect(live).to_have_count(0)
                finally:
                    browser.close()

    def test_json_metadata_review_requires_exact_confirmation(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='json-review-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    status = client.get('/api/tasks').json()
                    status['kinds'].append('browser_remote_static_assets')
                    status['remote_static_assets'] = {
                        'plan_sha256': 'a' * 64, 'asset_count': 1,
                        'assets': ['https://fixture.invalid/assets/app.js'],
                        'data_resources': ['https://fixture.invalid/api/summary'],
                        'mode': 'one_shot_readonly_data_bundle'}
                    status['jobs'] = [{
                        'job_id': 'job-' + str(index) * 32,
                        'run_id': 'run-' + label, 'kind': 'browser_remote_static_assets',
                        'status': 'succeeded', 'real_model': 0,
                        'runtime_id': 'browser-' + str(index) * 32}
                        for index, label in ((4, 'd'), (3, 'c'), (2, 'b'))]
                    candidate = json.loads((REPO_ROOT / 'examples/remote_readonly_data_knowledge_ui_preview.json').read_text())['preview']
                    seed = json.loads((REPO_ROOT / 'examples/remote_readonly_data_page_draft_seed.json').read_text())['report']
                    registration = json.loads((REPO_ROOT / 'examples/remote_readonly_data_page_draft_registration.json').read_text())['report']
                    fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_knowledge_review.json').read_text())
                    receipt = {**fixture['receipt'], 'candidate_sha256': candidate['candidate_sha256']}
                    recheck = fixture['recheck']
                    requests = []

                    def endpoint(route):
                        requests.append((route.request.url.rsplit('/', 1)[-1], route.request.post_data_json))
                        name = requests[-1][0]
                        route.fulfill(json=(seed if name == 'json-page-draft-seed'
                                            else registration if name == 'json-page-draft-register'
                                            else candidate if name == 'json-knowledge-preview'
                                            else receipt if name == 'json-knowledge-review'
                                            else recheck))

                    page.route('**/api/tasks', lambda route: route.fulfill(json=status))
                    page.route('**/api/tasks/json-knowledge-*', endpoint)
                    page.route('**/api/tasks/json-page-draft-*', endpoint)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    expect(page.get_by_test_id('development-status')).to_be_visible()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    panel = page.get_by_test_id('remote-json-knowledge')
                    expect(panel.get_by_role('heading', name='JSON page metadata review')).to_be_visible()
                    panel.get_by_label('Later JSON run').select_option('run-c')
                    panel.get_by_label('Previous JSON run').select_option('run-b')
                    panel.get_by_label('JSON page key').fill('summary')
                    panel.get_by_label('Page draft SHA-256').fill('f' * 64)
                    panel.get_by_role('button', name='Create private JSON page draft').click()
                    expect(panel.get_by_test_id('remote-json-page-seed')).to_contain_text(seed['draft_sha256'])
                    expect(panel.get_by_label('Page draft SHA-256')).to_have_value('')
                    register_button = panel.get_by_role('button', name='Register JSON page draft')
                    expect(register_button).to_be_disabled()
                    panel.get_by_label('Confirm JSON draft SHA-256').fill(seed['draft_sha256'])
                    register_button.click()
                    expect(panel.get_by_test_id('remote-json-page-registration')).to_contain_text(
                        registration['knowledge_sha256'])
                    expect(panel.get_by_label('Page draft SHA-256')).to_have_value(
                        registration['knowledge_sha256'])
                    panel.get_by_role('button', name='Preview JSON metadata candidate').click()
                    expect(panel.get_by_test_id('remote-json-knowledge-candidate')).to_contain_text('summary')
                    review_button = panel.get_by_role('button', name='Record JSON metadata review')
                    expect(review_button).to_be_disabled()
                    panel.get_by_label('Confirm JSON candidate SHA-256').fill(candidate['candidate_sha256'])
                    expect(review_button).to_be_disabled()
                    panel.get_by_label('I acknowledge this is metadata review only').check()
                    expect(review_button).to_be_enabled()
                    review_button.click()
                    expect(panel.get_by_test_id('remote-json-knowledge-review')).to_contain_text(receipt['review_sha256'])
                    expect(panel.get_by_label('JSON review SHA-256')).to_have_value(receipt['review_sha256'])
                    panel.get_by_role('button', name='Recheck fresh JSON metadata').click()
                    expect(panel.get_by_test_id('remote-json-knowledge-recheck')).to_contain_text('summary')
                    self.assertEqual([name for name, _payload in requests], [
                        'json-page-draft-seed', 'json-page-draft-register',
                        'json-knowledge-preview', 'json-knowledge-review',
                        'json-knowledge-recheck'])
                    self.assertEqual(requests[1][1]['confirm_sha256'],
                                     seed['draft_sha256'])
                    self.assertEqual(requests[3][1]['confirm_candidate_sha256'],
                                     candidate['candidate_sha256'])
                    self.assertTrue(requests[3][1]['acknowledge_metadata_only'])
                    self.assertEqual(requests[4][1]['current_run_id'], 'run-d')
                    page.get_by_role('button', name='Türkçe').click()
                    expect(panel.get_by_role('heading', name='JSON sayfa metadata incelemesi')).to_be_visible()
                finally:
                    browser.close()

    def test_static_bundle_scope_and_manual_approval_are_visible(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='static-bundle-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    status = client.get('/api/tasks').json()
                    status['kinds'].append('browser_remote_static_assets')
                    status['remote_static_assets'] = {
                        'plan_sha256': 'a' * 64, 'asset_count': 2,
                        'assets': ['https://example.invalid/assets/app.js',
                                   'https://example.invalid/assets/site.css'],
                        'mode': 'one_shot_static_bundle'}
                    status['jobs'] = [{'job_id': 'job-' + 'b' * 32, 'run_id': 'run-' + 'c' * 32,
                                       'kind': 'browser_remote_static_assets',
                                       'status': 'waiting_approval', 'real_model': 0,
                                       'runtime_id': 'browser-' + 'd' * 32,
                                       'remote_learning_metadata': {'enabled': False,
                                                                    'state': 'disabled'}}]
                    status['supports_remote_learning_metadata'] = True
                    status['approval'] = {
                        'approval_id': 'approval-' + 'e' * 32, 'action_sha256': 'f' * 64,
                        'expires_at': 9999999999,
                        'action': {'tool': 'browser.static.open',
                                   'arguments': {'entry_url': 'https://example.invalid/entry',
                                                 'plan_sha256': 'a' * 64},
                                   'runtime_id': 'browser-' + 'd' * 32,
                                   'owner_lease_id': 'lease-' + 'e' * 32,
                                   'state_version': 4,
                                   'expected_effect': 'Only the confirmed HTTPS entry and static assets'}}
                    page = browser.new_page()
                    page.route('**/api/tasks', lambda route: route.fulfill(json=status))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_static_assets')
                    expect(page.get_by_test_id('remote-static-assets-scope')).to_contain_text('https://example.invalid/assets/app.js')
                    expect(page.get_by_test_id('remote-static-assets-scope')).to_contain_text('https://example.invalid/assets/site.css')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.get_by_test_id('approval')).to_contain_text('This single approval covers the exact entry')
                    metadata = page.get_by_test_id('remote-learning-metadata')
                    expect(metadata).to_contain_text('Static-task metadata recording')
                    expect(metadata).to_contain_text('Readback is not a gold label')
                    preview = metadata.get_by_role('button', name='Preview metadata consent')
                    expect(preview).to_be_disabled()
                    metadata.get_by_label('S1 decision metadata').check()
                    metadata.get_by_label('As the local operator, I attest that I have the right to collect metadata for this run').check()
                    expect(preview).to_be_enabled()
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_test_id('remote-static-assets-capability-note')).to_contain_text('tek manuel onay')
                    expect(page.get_by_test_id('approval')).to_contain_text('Bu tek onay exact giriş')
                    expect(metadata).to_contain_text('Statik görev metadata kaydı')
                    expect(metadata).to_contain_text('Okuma sonucu gold değildir')
                finally:
                    browser.close()

    def test_remote_learning_attach_is_explicit_and_run_bound(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='remote-learning-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    task_status = client.get('/api/tasks').json()
                    task_status.update(
                        supports_remote_learning_metadata=True,
                        kinds=[*task_status['kinds'], 'browser_remote_routes'],
                        remote_routes={'plan_sha256': 'b' * 64, 'route_count': 2,
                                       'mode': 'ordered_read_only'},
                        jobs=[{'job_id': 'job-' + 'a' * 32, 'run_id': 'run-' + 'c' * 32,
                               'kind': 'browser_remote_routes', 'status': 'waiting_approval',
                               'real_model': 1, 'runtime_id': 'browser-' + 'd' * 32,
                               'remote_learning_metadata': {'enabled': False, 'state': 'disabled'}}],
                        approval={'approval_id': 'approval-' + 'e' * 32,
                                  'action_sha256': 'f' * 64, 'expires_at': 9999999999,
                                  'action': {'tool': 'browser.remote.route',
                                             'arguments': {'route_index': 0}}})
                    writes = []
                    consent_writes = []
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    page.route('**/api/tasks', lambda route: route.fulfill(json=task_status))
                    def consent_response(route):
                        request = json.loads(route.request.post_data or '{}')
                        consent_writes.append(request)
                        route.fulfill(json={'consent_sha256': 'a' * 64,
                                            'registered': 'confirm_sha256' in request,
                                            'run_ref': 'b' * 64, 'roles': request['roles'],
                                            'expires_at': request['expires_at'],
                                            'metadata_only': True,
                                            'external_rights_verified': False,
                                            'training_authorized': False})
                    page.route('**/api/tasks/remote-learning/consent', consent_response)
                    page.route('**/api/tasks/remote-learning', lambda route: (
                        writes.append(json.loads(route.request.post_data or '{}')),
                        route.fulfill(json={'enabled': True, 'state': 'collecting'})))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    panel = page.get_by_test_id('remote-learning-metadata')
                    expect(panel).to_be_visible()
                    expect(panel).to_contain_text('run-' + 'c' * 32)
                    button = panel.get_by_role('button', name='Attach metadata recording to this run')
                    expect(button).to_be_disabled()
                    preview = panel.get_by_role('button', name='Preview metadata consent')
                    expect(preview).to_be_disabled()
                    panel.get_by_label('S1 decision metadata').check()
                    panel.get_by_label('As the local operator, I attest that I have the right to collect metadata for this run').check()
                    expect(preview).to_be_enabled()
                    preview.click()
                    expect(panel.get_by_test_id('remote-consent-preview')).to_contain_text('a' * 64)
                    confirm = panel.get_by_role('button', name='Register exact consent')
                    expect(confirm).to_be_disabled()
                    panel.get_by_label('Confirm consent SHA-256').fill('0' * 64)
                    expect(confirm).to_be_disabled()
                    panel.get_by_label('Confirm consent SHA-256').fill('a' * 64)
                    expect(confirm).to_be_enabled()
                    confirm.click()
                    expect(panel).to_contain_text('Private consent recorded')
                    expect(panel.get_by_label('Local metadata consent SHA-256')).to_have_value('a' * 64)
                    self.assertEqual(len(consent_writes), 2)
                    self.assertEqual(consent_writes[0]['roles'], ['system1'])
                    self.assertRegex(consent_writes[0]['expires_at'],
                                     r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$')
                    self.assertNotIn('confirm_sha256', consent_writes[0])
                    self.assertEqual(consent_writes[1]['confirm_sha256'], 'a' * 64)
                    self.assertEqual(writes, [])
                    expect(button).to_be_enabled()
                    button.click()
                    self.assertEqual(writes, [{'job_id': 'job-' + 'a' * 32,
                                               'consent_sha256': 'a' * 64}])
                    task_status['jobs'][0]['remote_learning_metadata'] = {
                        'enabled': True, 'state': 'collecting',
                        'entries_by_role': {'system1': 1, 'system2': 0}}
                    page.reload()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('remote-learning-metadata')).to_contain_text('S1 events: 1')
                finally:
                    browser.close()

    def test_remote_entry_capability_is_explicit_and_never_auto_approved(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='remote-entry-capability-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    task_status = client.get('/api/tasks').json()
                    original_jobs = task_status['jobs']
                    task_status.update(kinds=[*task_status['kinds'], 'browser_remote_entry',
                                              'browser_remote_routes', 'browser_remote_form'],
                                       browser_display='desktop', supports_approve_all=True,
                                       supports_learning_metadata=True,
                                       remote_entry={'profile_sha256': 'a' * 64,
                                                     'entry_url': 'https://www.synthetic.invalid/entry',
                                                     'task_key': 'synthetic-entry', 'mode': 'one_shot_read_only'},
                                       remote_routes={'plan_sha256': 'b' * 64,
                                                      'route_count': 2, 'review_sha256': 'e' * 64,
                                                      'mode': 'ordered_read_only'},
                                       remote_form={'plan_sha256': 'c' * 64,
                                                    'entry_url': 'https://app.example.invalid/entry',
                                                    'submit_url': 'https://app.example.invalid/submit',
                                                    'receipt_url': 'https://app.example.invalid/receipt',
                                                    'field_name': 'message',
                                                    'body_sha256': 'd' * 64,
                                                    'mode': 'synthetic_one_post'})
                    task_status['jobs'] = [{
                        'job_id': 'synthetic-route-job', 'run_id': 'synthetic-route-run',
                        'kind': 'browser_remote_routes', 'status': 'succeeded',
                        'real_model': 0, 'runtime_id': None,
                        'route_knowledge': {'status': 'matched', 'route_index': 0,
                                            'stale_reason': None}}]
                    writes = []
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
                    page.route('**/api/tasks', lambda route: route.fulfill(json=task_status))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.get_by_test_id('first-use-capability-browser_remote_entry')).to_contain_text('Enabled')
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_entry')
                    expect(page.get_by_test_id('remote-entry-scope')).to_contain_text('https://www.synthetic.invalid/entry')
                    expect(page.get_by_test_id('remote-entry-capability-note')).to_contain_text('Approve all is disabled')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.locator('[data-testid="learning-metadata-option"] input')).to_be_disabled()
                    expect(page.get_by_role('button', name='Start entry trial', exact=True)).to_be_enabled()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_routes')
                    expect(page.get_by_test_id('remote-routes-scope')).to_contain_text('2')
                    expect(page.get_by_test_id('remote-route-review-scope')).to_contain_text('Historical route metadata review pin:')
                    expect(page.get_by_test_id('remote-route-review-scope')).to_contain_text('e' * 64)
                    expect(page.get_by_test_id('remote-route-review-scope')).to_contain_text('every mapped outgoing target')
                    expect(page.get_by_test_id('remote-route-review-scope')).to_contain_text('approvals remain unchanged')
                    expect(page.get_by_test_id('remote-route-live-knowledge')).to_contain_text('Source fingerprint matched')
                    expect(page.get_by_test_id('remote-routes-capability-note')).to_contain_text('Approve all is disabled')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.locator('[data-testid="learning-metadata-option"] input')).to_be_disabled()
                    expect(page.get_by_role('button', name='Start route reading', exact=True)).to_be_enabled()
                    page.get_by_label('Task type', exact=True).select_option('browser_remote_form')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('c' * 64)
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('https://app.example.invalid/submit')
                    expect(page.get_by_test_id('remote-form-capability-note')).to_contain_text('Approve all is disabled')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.locator('[data-testid="learning-metadata-option"] input')).to_be_disabled()
                    expect(page.get_by_role('button', name='Start HTTPS form task', exact=True)).to_be_enabled()
                    task_status['approval'] = json.loads((REPO_ROOT / 'examples/desktop_remote_form_approval.json').read_text())['approval']
                    page.reload()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('https://app.example.invalid/submit')
                    expect(page.get_by_test_id('approval')).to_contain_text('293bc662b1766b12804b47200978b34c4b6a9e5728258123bbe29b64e364678a')
                    expect(page.get_by_test_id('approval')).to_contain_text('private value is not shown')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_test_id('approval')).to_contain_text('özel değer gösterilmez')
                    page.get_by_label('Görev türü', exact=True).select_option('browser_remote_form')
                    expect(page.get_by_role('button', name='HTTPS form görevini başlat', exact=True)).to_be_enabled()
                    page.get_by_label('Görev türü', exact=True).select_option('browser_remote_routes')
                    expect(page.get_by_test_id('remote-route-review-scope')).to_contain_text('Geçmiş rota metadata inceleme pini:')
                    expect(page.get_by_test_id('remote-route-review-scope')).to_contain_text('tüm eşlenmiş giden hedefler')
                    expect(page.get_by_test_id('remote-route-review-scope')).to_contain_text('onaylar değişmez')
                    task_status['jobs'][0]['route_knowledge'] = {
                        'status': 'stale', 'route_index': 1,
                        'stale_reason': 'target_fingerprint_changed'}
                    page.reload()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_label('Görev türü', exact=True).select_option('browser_remote_routes')
                    expect(page.get_by_test_id('remote-route-live-knowledge')).to_contain_text('Giden hedef parmak izi değişti.')
                    task_status['remote_routes']['review_sha256'] = None
                    page.reload()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_label('Görev türü', exact=True).select_option('browser_remote_routes')
                    expect(page.get_by_test_id('remote-route-review-scope')).to_have_count(0)
                    expect(page.get_by_test_id('remote-route-live-knowledge')).to_have_count(0)
                    expect(page.get_by_role('button', name='Rota okumasını başlat', exact=True)).to_be_enabled()
                    task_status['jobs'] = original_jobs
                    task_status['remote_form'].update(mode='public_explicit_one_post',
                                                      entry_url='https://app.example.com/entry',
                                                      submit_url='https://app.example.com/submit',
                                                      receipt_url='https://app.example.com/receipt')
                    page.reload()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_label('Görev türü', exact=True).select_option('browser_remote_form')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('https://app.example.com/submit')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('Açık public plan izni')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    task_status['remote_form'].update(field_name=None,
                                                      field_names=['subject', 'message'])
                    page.reload()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('Alanlar: subject, message')
                    page.get_by_role('button', name='English', exact=True).click()
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('Fields: subject, message')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    task_status['remote_form'].update(
                        state_plan_sha256='e' * 64,
                        state_url='https://app.example.com/state',
                        cookie_sha256='9' * 64)
                    task_status['approval']['action'].update(
                        tool='browser.form.state_after', selected_option='read_state_after',
                        arguments={'profile_sha256': 'a' * 64,
                                   'binding_sha256': 'b' * 64,
                                   'form_plan_sha256': 'c' * 64,
                                   'state_plan_sha256': 'e' * 64,
                                   'phase': 'after', 'url': 'https://app.example.com/state',
                                   'request_sha256': 'f' * 64})
                    page.reload()
                    page.get_by_role('button', name='Görevler', exact=True).click()
                    page.get_by_label('Görev türü', exact=True).select_option('browser_remote_form')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('Altı ayrı onay')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('https://app.example.com/state')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('Özel Cookie SHA-256:')
                    expect(page.get_by_test_id('remote-form-scope')).to_contain_text('9' * 64)
                    expect(page.get_by_test_id('approval')).to_contain_text('host HTTPS durum GET’i')
                    expect(page.get_by_test_id('visible-task-status')).to_contain_text('6 / 6')
                    self.assertEqual(writes, [origin + '/api/login'])
                finally:
                    browser.close()

    def test_optin_staging_workflow_is_visible_but_requires_manual_approvals(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='staging-capability-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    task_status = client.get('/api/tasks').json()
                    task_status.update(kinds=[*task_status['kinds'], 'browser_staging_workflow'],
                                       staging_transport='playwright_mcp', browser_display='desktop')
                    writes = []
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
                    page.route('**/api/tasks', lambda route: route.fulfill(json=task_status))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Computer', exact=True).click()
                    expect(page.get_by_test_id('first-use-capability-browser_staging_workflow')).to_contain_text('Enabled')
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('staging-mcp-transport')).to_contain_text('synthetic App')
                    page.get_by_label('Task type').select_option('browser_staging_workflow')
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.get_by_test_id('staging-capability-note')).to_contain_text('Approve all is disabled')
                    expect(page.get_by_role('button', name='Start application workflow')).to_be_enabled()
                    self.assertEqual(writes, [origin + '/api/login'])
                finally:
                    browser.close()

    def test_live_counts_stay_separate_from_static_release_gates(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='development-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture') as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    context = browser.new_context(viewport={'width': 1280, 'height': 900})
                    page = context.new_page()
                    task_status = client.get('/api/tasks').json()
                    overview = client.get('/api/overview').json()
                    retention_status = json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())['retention_status']
                    repeat_report = json.loads((REPO_ROOT / 'examples/remote_form_repeat.json').read_text())['report']
                    web_applications = []
                    writes = []
                    repeat_requests = []
                    page.on('request', lambda request: writes.append(request.url) if request.method == 'POST' else None)
                    page.on('request', lambda request: repeat_requests.append(request.url)
                            if '/api/remote-form/repeats' in request.url else None)
                    page.route('**/api/tasks', lambda route: route.fulfill(json=task_status))
                    page.route('**/api/overview', lambda route: route.fulfill(json=overview))
                    page.route('**/api/retention', lambda route: route.fulfill(json=retention_status))
                    page.route('**/api/remote-form/repeats', lambda route: route.fulfill(json=repeat_report))
                    page.route('**/api/web-applications', lambda route: route.fulfill(json=web_applications))
                    page.route('**/api/web-applications/capabilities',
                               lambda route: route.fulfill(status=404))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    panel = page.get_by_test_id('development-status')
                    expect(panel).to_be_visible()
                    expect(panel.get_by_test_id('development-backend-capability')).to_contain_text(
                        'does not confirm image-plan capability')
                    expect(panel.get_by_test_id('development-static-query-capability')).to_contain_text(
                        'does not confirm exact static asset query drafts')
                    expect(page.get_by_role('button', name='Development', exact=True)).to_have_attribute('aria-current', 'page')
                    self.assertEqual(page.locator('.sidebar nav button').first.inner_text(), 'Development')
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(panel).to_have_count(0)
                    page.reload()
                    expect(panel).to_be_visible()
                    expect(page.get_by_role('button', name='Development', exact=True)).to_have_attribute('aria-current', 'page')
                    reopened = context.new_page()
                    reopened.goto(origin + '/')
                    self.assertEqual(reopened.url, origin + '/ui/')
                    expect(reopened.get_by_test_id('development-status')).to_be_visible()
                    expect(reopened.get_by_test_id('development-backend-capability')).to_contain_text(
                        'offers PNG/JPEG/WebP/GIF drafting')
                    expect(reopened.get_by_test_id('development-static-query-capability')).to_contain_text(
                        'supports exact static asset query drafts')
                    expect(reopened.get_by_role('button', name='Development', exact=True)).to_have_attribute('aria-current', 'page')
                    reopened.close()
                    page.unroute('**/api/web-applications/capabilities')
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-backend-capability')).to_contain_text(
                        'offers PNG/JPEG/WebP/GIF drafting')
                    expect(panel.get_by_test_id('development-static-query-capability')).to_contain_text(
                        'supports exact static asset query drafts')
                    expect(panel.get_by_test_id('development-release-acceptance')).to_contain_text('0/6 · 0%')
                    for test_id in ('development-release-acceptance', 'development-completed-percent', 'development-open-percent'):
                        bounds = panel.get_by_test_id(test_id).bounding_box()
                        self.assertIsNotNone(bounds)
                        self.assertGreaterEqual(bounds['y'], 0)
                        self.assertLess(bounds['y'] + bounds['height'], 900)
                    onboarding = panel.get_by_test_id('development-onboarding')
                    expect(onboarding).to_contain_text('No target profile draft is registered.')
                    expect(onboarding).to_contain_text('No remote task scope is pinned in this session.')
                    expect(onboarding.get_by_test_id('development-next-setup-step')).to_contain_text(
                        'First register a profile draft with the authorized target URL')
                    task_status['remote_entry'] = {'profile_sha256': 'a' * 64,
                                                   'entry_url': 'https://synthetic.invalid/entry',
                                                   'task_key': 'synthetic-task', 'mode': 'one_shot_read_only'}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(onboarding.get_by_test_id('development-next-setup-step')).to_contain_text(
                        'A remote task is pinned but profile inventory is empty')
                    task_status['remote_entry'] = None
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-form-repeats')).to_contain_text('No HTTPS form plan is pinned')
                    self.assertEqual(repeat_requests, [])
                    expect(panel.get_by_test_id('development-completed-percent')).to_contain_text('56/65 · 86.2%')
                    expect(panel.get_by_test_id('development-checklist-date')).to_contain_text('30 September 2026')
                    expect(panel.get_by_test_id('development-open-percent')).to_contain_text('9/65 · 13.8%')
                    expect(panel).to_contain_text('Real S2 skill proposals with reviewed document context')
                    expect(panel).to_contain_text('Reviewed document context in real S1 task decisions')
                    expect(panel).to_contain_text('Real Bonsai source-quotation answers from reviewed documents')
                    expect(panel).to_contain_text('Private document publication, human review and scoped search')
                    expect(panel.get_by_test_id('development-release-acceptance')).to_contain_text('0/6 · 0%')
                    expect(panel).to_contain_text('Same-case paired base and adapter evaluation')
                    expect(panel).to_contain_text('On one frozen synthetic development input, separately started base and adapter arms')
                    expect(panel).to_contain_text('Real S1 execution with a per-task adapter')
                    expect(panel).to_contain_text('Authorized experimental S1 adapter from a real episode')
                    expect(panel).to_contain_text('Episode conversion and real S1 tokenizer preparation')
                    expect(panel).to_contain_text('Consented live S1/S2 content candidates and local export')
                    expect(panel).to_contain_text('Goal-to-execution with Bonsai and Decider')
                    expect(panel).to_contain_text('Selected skill reuse in a new session')
                    expect(panel).to_contain_text('Reviewed skill versions and explicit development rollback')
                    expect(panel).to_contain_text('Evidence-bound candidate review and revocation')
                    expect(panel).to_contain_text('Saved candidate execution with new inputs')
                    expect(panel).to_contain_text('Dataset-bound S1 model-gradient smoke')
                    expect(panel).to_contain_text('Synthetic S1 adapter candidate and independent reload')
                    expect(panel).to_contain_text('published fixture has 1/0/0, plus a private 1/1/1 worker probe')
                    expect(panel).to_contain_text('Managed restart admission lock')
                    expect(panel).to_contain_text('Deadline-bound HTTPS form approval')
                    expect(panel).to_contain_text('Required email field in HTTPS forms')
                    expect(panel.get_by_test_id('development-restart-quiesced')).to_have_count(0)
                    task_status['restart_quiesced'] = True
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-restart-quiesced')).to_contain_text('release-restart')
                    task_status['restart_quiesced'] = False
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-restart-quiesced')).to_have_count(0)
                    expect(panel).to_contain_text('Submitted value–state marker binding')
                    expect(panel).to_contain_text('State readback binding for a synthetic skill case')
                    expect(panel).to_contain_text('Read-only source audit for state-bound HTTPS form')
                    expect(panel).to_contain_text('Separate consent-bound state-form metadata stream')
                    expect(panel).to_contain_text('Repeat measurement for basic and state-bound forms')
                    expect(panel.get_by_test_id('development-open-percent')).to_contain_text('9/65 · 13.8%')
                    expect(panel).to_contain_text('Separate HTTPS form state plan from console')
                    expect(panel).to_contain_text('a third separate confirmation')
                    expect(panel).to_contain_text('Read-only source audit for HTTPS form task')
                    expect(panel).to_contain_text('Consent-bound HTTPS form metadata stream')
                    expect(panel).to_contain_text('Skill drafts for both roles in a pinned profile')
                    expect(panel).to_contain_text('Synthetic skill case-input binding')
                    expect(panel).to_contain_text('Approved form transport binding for a synthetic skill case')
                    expect(panel).to_contain_text('the exact body of an existing HTTPS form plan')
                    expect(panel).to_contain_text('Explicit host HTTPS entry preflight')
                    expect(panel).to_contain_text('Approved one-shot HTTPS entry task')
                    expect(panel).to_contain_text('Pinned static JS/CSS/images browser worker')
                    expect(panel).to_contain_text('Approved exact JSON GET browser task')
                    expect(panel).to_contain_text('Read-only model source audit for JSON task')
                    expect(panel).to_contain_text('JSON change and page draft candidate')
                    expect(panel).to_contain_text('Private JSON page draft from console')
                    expect(panel).to_contain_text('JSON metadata review and fresh-run recheck')
                    expect(panel).to_contain_text('Consent-bound JSON-task metadata stream')
                    expect(panel).to_contain_text('Static JS/CSS/images plan from console')
                    expect(panel).to_contain_text('one manual approval')
                    expect(panel).to_contain_text('Ordered HTTPS route approvals')
                    expect(panel).to_contain_text('Managed HTTPS form opt-in')
                    expect(panel).to_contain_text('exact 2–8 route plan')
                    expect(panel).to_contain_text('Remote route / page draft candidate')
                    expect(panel).to_contain_text('In-task symbolic route context · synthetic')
                    expect(panel).to_contain_text('if the target changes, the later choice receives no hint')
                    expect(panel).to_contain_text('Run-bound remote metadata stream')
                    expect(panel).to_contain_text('Explicit static-task metadata stream')
                    expect(panel).to_contain_text('the scheduler deduplicates real pinned S1/S2 metadata')
                    expect(panel).to_contain_text('registered in two steps on Tasks or via CLI')
                    expect(panel).to_contain_text('a private outbox per consent')
                    expect(panel).to_contain_text('revoke the exact consent')
                    expect(panel).to_contain_text('at startup and hourly')
                    expect(panel).to_contain_text('verified-action S1 / downstream-linked scene S2 candidates')
                    expect(panel).to_contain_text('the consent-bound metadata stream has its own card')
                    expect(panel.get_by_test_id('development-live-evidence')).to_contain_text('0/0')
                    model_evidence = panel.get_by_test_id('development-model-evidence')
                    expect(model_evidence).to_contain_text('S1 calls · successful/total')
                    expect(model_evidence).to_contain_text('No remote metadata consent is attached')
                    expect(panel.get_by_test_id('development-s1-calls')).to_contain_text('—')
                    expect(panel.get_by_test_id('development-s2-calls')).to_contain_text('—')
                    expect(panel.get_by_test_id('development-s1-latency')).to_contain_text('—')
                    expect(panel.get_by_test_id('development-s2-latency')).to_contain_text('—')
                    task_status['jobs'] = [{
                        'job_id': 'synthetic-failed-job', 'run_id': 'synthetic-failed-run',
                        'kind': 'browser_form', 'status': 'failed', 'real_model': 0,
                        'runtime_id': 'synthetic-browser',
                        'progress': {'phase': 'FAILED', 'state_version': 3, 'elapsed_ms': 2000,
                                     'failure_code': 'MODEL_FAILURE', 'model_calls': []}}]
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-task-failure')).to_contain_text(
                        'Verified failure code: MODEL_FAILURE')
                    task_status['jobs'] = []
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-task-failure')).to_have_count(0)
                    task_status['remote_form'] = {
                        'plan_sha256': 'a' * 64, 'entry_url': 'https://synthetic.invalid/entry',
                        'submit_url': 'https://synthetic.invalid/submit',
                        'receipt_url': 'https://synthetic.invalid/receipt',
                        'field_name': 'message', 'body_sha256': 'b' * 64,
                        'mode': 'synthetic_one_post'}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    repeat = panel.get_by_test_id('development-form-repeats')
                    expect(repeat).to_contain_text('Transport readback verified / bound attempts')
                    self.assertGreaterEqual(len(repeat_requests), 1)
                    expect(panel.get_by_test_id('development-form-repeat-outcomes')).to_contain_text('1/2')
                    expect(panel.get_by_test_id('development-form-repeat-elapsed')).to_contain_text('1,000 / 1,200 ms')
                    expect(panel.get_by_test_id('development-form-repeat-approval')).to_contain_text('300 ms · n=1')
                    expect(panel.get_by_test_id('development-form-repeat-excluding-approval')).to_contain_text('700 / 1,200 ms')
                    expect(panel.get_by_test_id('development-form-repeat-first')).to_contain_text('200 / 200 ms · n=1')
                    expect(panel.get_by_test_id('development-form-stage-browser-form-submit')).to_contain_text('Form POST · p50/p95')
                    expect(panel.get_by_test_id('development-form-stage-browser-form-submit')).to_contain_text('200 / 200 ms · n=1')
                    expect(repeat).to_contain_text('not pure browser time')
                    expect(repeat).not_to_contain_text(repeat_report['profile_sha256'])
                    repeat_report.update(json.loads((REPO_ROOT / 'examples/remote_form_state_repeat.json').read_text())['report'])
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(repeat).to_contain_text('exact form and state plans')
                    expect(repeat).to_contain_text('Six-approval state and four-approval basic plans are measured separately')
                    expect(panel.get_by_test_id('development-form-repeat-approval')).to_contain_text('300 ms · n=2')
                    expect(panel.get_by_test_id('development-form-repeat-excluding-approval')).to_contain_text('850 / 1,050 ms')
                    expect(panel.get_by_test_id('development-form-stage-browser-form-state-before')).to_contain_text('Before-state GET')
                    expect(panel.get_by_test_id('development-form-stage-browser-form-state-after')).to_contain_text('After-state GET')
                    expect(repeat).not_to_contain_text(repeat_report['state_plan_sha256'])
                    repeat_report['post_approval_stage_ms'] = {}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(repeat).to_contain_text('measurement is unavailable')
                    expect(panel.get_by_test_id('development-form-stage-timing')).to_have_count(0)
                    repeat_report.clear()
                    repeat_report.update(json.loads((REPO_ROOT / 'examples/remote_form_repeat.json').read_text())['report'])
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Last sweep completed.')
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Purged outboxes: 1')
                    retention_status.update(json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())['stale_retention_status'])
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Last sweep is stale')
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Purged outboxes: —')
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Failures: —')
                    retention_status.update(json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())['retention_status'])
                    form_pin = task_status['remote_form']
                    task_status['remote_form'] = None
                    web_applications.append(json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['report'])
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(onboarding.get_by_test_id('development-next-setup-step')).to_contain_text(
                        'Prepare the exact task and plan from the registered profile')
                    task_status['remote_form'] = form_pin
                    task_status['remote_entry'] = {'profile_sha256': 'a' * 64,
                                                   'entry_url': 'https://synthetic.invalid/entry',
                                                   'task_key': 'synthetic-task', 'mode': 'one_shot_read_only'}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(onboarding).to_contain_text('Registered profile drafts: 1')
                    expect(onboarding).to_contain_text('Remote task scope is pinned in this session')
                    expect(onboarding.get_by_test_id('development-next-setup-step')).to_contain_text(
                        'Review the pinned scope in Tasks')
                    expect(onboarding.get_by_role('button', name='Open tasks', exact=True)).to_be_visible()
                    task_status['remote_entry'] = None
                    task_status['remote_static_assets'] = {
                        'plan_sha256': 'a' * 64, 'asset_count': 1,
                        'assets': ['https://synthetic.invalid/assets/app.js'],
                        'mode': 'one_shot_static_bundle'}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(onboarding).to_contain_text('Remote task scope is pinned in this session')
                    self.assertEqual(writes, [origin + '/api/login'])

                    task_status.update(kinds=['hello', 'browser_form', 'browser_local_navigation', 'vision_canvas'],
                                       jobs=[{'job_id': 'synthetic-job', 'run_id': 'synthetic-run', 'kind': 'hello',
                                              'status': 'succeeded', 'real_model': 0, 'runtime_id': None,
                                              'progress': {'phase': 'SUCCEEDED', 'state_version': 2,
                                                           'elapsed_ms': 10, 'model_calls': []}}])
                    overview['trajectory'] = {'available': True, 'runs': [
                        {'run_id': 'synthetic-run', 'status': 'succeeded', 'started_at': '2026-09-23T00:00:00Z',
                         'model_calls': 0, 'passed': 1, 'failed': 0},
                        {'run_id': 'failed-run', 'status': 'failed', 'started_at': '2026-09-23T00:00:01Z',
                         'model_calls': 0, 'passed': 0, 'failed': 1}]}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    live = panel.get_by_test_id('development-live-evidence')
                    expect(live).to_contain_text('Offered fixed task types4')
                    expect(live).to_contain_text('1/1')
                    expect(live).to_contain_text('1/2')
                    expect(panel.get_by_test_id('development-current-job')).to_contain_text('hello · succeeded')
                    expect(panel.get_by_test_id('development-task-progress')).to_contain_text('Persisted phase:')
                    expect(panel.get_by_test_id('development-task-phase')).to_have_text('SUCCEEDED')
                    expect(panel.get_by_test_id('development-task-elapsed')).to_have_text('10 ms')
                    task_status['jobs'][0]['status'] = 'waiting_approval'
                    task_status['jobs'][0]['progress']['phase'] = 'EXECUTE'
                    task_status['jobs'][0]['progress']['elapsed_ms'] = 12345
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-task-progress')).to_contain_text('waiting for action approval')
                    expect(panel.get_by_test_id('development-task-phase')).to_have_text('EXECUTE')
                    expect(panel.get_by_test_id('development-task-elapsed')).to_have_text('12.3 s')
                    task_status['jobs'][0]['status'] = 'succeeded'
                    task_status['jobs'][0]['progress']['phase'] = 'SUCCEEDED'
                    expect(panel.get_by_test_id('development-s1-calls')).to_contain_text('0/0')
                    expect(panel.get_by_test_id('development-s2-calls')).to_contain_text('0/0')
                    expect(panel.get_by_test_id('development-remote-metadata')).to_contain_text('—')
                    task_status['jobs'][0]['progress']['phase'] = None
                    task_status['jobs'][0]['progress']['model_call_totals'] = {
                        'system1': {'ok': 12, 'total': 20},
                        'system2': {'ok': 3, 'total': 4}}
                    task_status['jobs'][0]['remote_learning_metadata'] = {
                        'enabled': True, 'state': 'collecting', 'entries_by_role': None}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-s1-calls')).to_contain_text('—')
                    expect(panel.get_by_test_id('development-remote-metadata')).to_contain_text('—')
                    task_status['jobs'][0]['progress']['phase'] = 'SUCCEEDED'
                    task_status['jobs'][0]['progress'].pop('model_call_totals')
                    task_status['jobs'][0]['progress']['model_calls'] = [
                        {'role': 'system1', 'status': 'ok', 'latency_ms': 10},
                        {'role': 'system1', 'status': 'error', 'latency_ms': 20},
                        {'role': 'system2', 'status': 'ok', 'latency_ms': 30}]
                    task_status['jobs'][0]['remote_learning_metadata'] = {
                        'enabled': True, 'state': 'synced',
                        'entries_by_role': {'system1': 2, 'system2': 0}}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-s1-calls')).to_contain_text('1/2')
                    expect(panel.get_by_test_id('development-s2-calls')).to_contain_text('1/1')
                    expect(panel.get_by_test_id('development-remote-metadata')).to_contain_text('2/0')
                    expect(model_evidence).to_contain_text('not a dataset or gold labels')
                    expect(panel).to_contain_text('This backend does not provide total call counts')
                    task_status['jobs'][0]['progress']['model_call_totals'] = {
                        'system1': {'ok': 12, 'total': 20},
                        'system2': {'ok': 3, 'total': 4}}
                    task_status['jobs'][0]['progress']['model_call_latency'] = {
                        'system1': {'samples': 20, 'p50_ms': 62, 'p95_ms': 5400},
                        'system2': {'samples': 4, 'p50_ms': 300, 'p95_ms': 700}}
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-s1-calls')).to_contain_text('12/20')
                    expect(panel.get_by_test_id('development-s2-calls')).to_contain_text('3/4')
                    expect(panel.get_by_test_id('development-s1-latency')).to_contain_text('62 / 5,400 ms · n=20')
                    expect(panel.get_by_test_id('development-s2-latency')).to_contain_text('300 / 700 ms · n=4')
                    expect(model_evidence).to_contain_text('All completed calls bound to this run')
                    task_status['jobs'][0]['progress']['model_call_latency']['system2']['p95_ms'] = None
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(panel.get_by_test_id('development-s2-latency')).to_contain_text('—')
                    expect(panel.get_by_test_id('development-release-acceptance')).to_contain_text('0/6 · 0%')
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(panel).to_contain_text('Bu oturumdan canlı kanıt')
                    expect(panel).to_contain_text('Görev başına ayrı adapter ile gerçek S1 yürütmesi')
                    expect(panel.get_by_test_id('development-completed-percent')).to_contain_text('56/65 · 86,2%')
                    expect(panel.get_by_test_id('development-open-percent')).to_contain_text('9/65 · 13,8%')
                    expect(panel).to_contain_text('İncelenmiş belge bağlamıyla gerçek S2 skill önerisi')
                    expect(panel).to_contain_text('İncelenmiş belgeyi gerçek S1 görev kararında kullanma')
                    expect(panel.get_by_test_id('development-release-acceptance')).to_contain_text('0/6 · 0%')
                    expect(panel).to_contain_text('Aynı vakada eşli temel ve adapter değerlendirmesi')
                    expect(panel).to_contain_text('Bu tek çift held-out, kalite veya hız üstünlüğü')
                    expect(panel.get_by_test_id('development-backend-capability')).to_contain_text(
                        'Bu backend PNG/JPEG/WebP/GIF taslaklarını sunuyor')
                    expect(panel.get_by_test_id('development-task-progress')).to_contain_text('Son görevin canlı durumu')
                    expect(panel).to_contain_text('Konsoldan ayrı HTTPS form durum planı')
                    expect(panel).to_contain_text('HTTPS form onayında son kullanma sınırı')
                    expect(panel).to_contain_text('HTTPS formda gerekli e-posta alanı')
                    expect(panel).to_contain_text('mevcut fixture 1/0/0, ayrıca özel işçi probu 1/1/1')
                    expect(panel).to_contain_text('Sentetik skill vakasında durum readback bağı')
                    expect(panel).to_contain_text('Son görevde S1/S2 kanıtı')
                    expect(panel).to_contain_text('Son görevde model gecikmesi')
                    expect(panel.get_by_test_id('development-s1-latency')).to_contain_text('62 / 5.400 ms · n=20')
                    expect(repeat).to_contain_text('Aynı HTTPS form planında tekrar ölçümü')
                    expect(panel.get_by_test_id('development-form-repeat-elapsed')).to_contain_text('1.000 / 1.200 ms')
                    expect(panel.get_by_test_id('development-form-repeat-excluding-approval')).to_contain_text('700 / 1.200 ms')
                    expect(panel.get_by_test_id('development-form-stage-browser-form-submit')).to_contain_text('Form POST · p50/p95')
                    expect(model_evidence).to_contain_text('Yalnız içeriksiz outbox adayları')
                    expect(panel).to_contain_text('Açık host HTTPS giriş ön kontrolü')
                    expect(panel).to_contain_text('Onaylı tek HTTPS giriş görevi')
                    expect(panel).to_contain_text('Pinli statik JS/CSS/görsel tarayıcı işçisi')
                    expect(panel).to_contain_text('Onaylı exact JSON GET tarayıcı görevi')
                    expect(panel).to_contain_text('JSON görevinde salt okunur model kaynak denetimi')
                    expect(panel).to_contain_text('JSON görevinde izinli metadata akışı')
                    expect(panel).to_contain_text('Konsoldan statik JS/CSS/görsel planı')
                    expect(panel).to_contain_text('tek manuel onaydan sonra')
                    expect(panel).to_contain_text('Managed HTTPS form opt-in')
                    expect(panel).to_contain_text('Uzak rota / sayfa taslağı adayı')
                    expect(panel).to_contain_text('Görev-içi sembolik rota bağlamı · sentetik')
                    expect(panel).to_contain_text('Tasks ekranında iki aşamalı veya CLI ile')
                    expect(panel).to_contain_text('Statik görev için açık metadata akışı')
                    expect(panel).to_contain_text('izin-başına özel outbox')
                    expect(panel).to_contain_text('exact izni iptal')
                    expect(panel).to_contain_text('açılışta ve saatte bir')
                    expect(panel).to_contain_text('S1 doğrulanmış eylem / downstream-bağlı S2 sahne adayları')
                    expect(panel).to_contain_text('izinli metadata akışı ayrı kartta')
                    expect(onboarding).to_contain_text('Kayıtlı profil taslağı: 1')
                    page.route('**/api/web-applications', lambda route: route.fulfill(status=404, body=''))
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(onboarding).to_contain_text('Bu backend profil envanterini sunmuyor.')
                    expect(onboarding.get_by_test_id('development-next-setup-step')).to_contain_text(
                        'Profil envanteri yok; kayıt öncesi backend sürümünü güvenli yeni oturumda güncelleyin.')
                    onboarding.get_by_role('button', name='Web uygulamalarını aç').click()
                    expect(page.get_by_role('heading', name='Web uygulaması profil taslakları')).to_be_visible()
                    page.get_by_role('button', name='Geliştirme', exact=True).click()
                    expect(panel).to_be_visible()
                    retention_status.update(json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())['stale_retention_status'])
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Son tarama güncel değil')
                    retention_status.update(json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())['retention_status'])
                    retention_status['state'] = 'incomplete'
                    retention_status['failed_session_count'] = 1
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Son tarama eksik')
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Engel: 1')
                    page.route('**/api/retention', lambda route: route.fulfill(status=404, body=''))
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(panel.get_by_test_id('development-retention')).to_contain_text('Bu backend saklama süresi durumunu sunmuyor.')
                    legacy_repeat = {key: value for key, value in repeat_report.items()
                                     if key not in {'approval_window_count', 'approval_window_sum_ms',
                                                    'elapsed_excluding_approval_p50_ms',
                                                    'elapsed_excluding_approval_p95_ms'}}
                    legacy_repeat['schema_version'] = '1.0'
                    page.route('**/api/remote-form/repeats',
                               lambda route: route.fulfill(json=legacy_repeat))
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(repeat).to_contain_text('ölçüm kullanılamıyor')
                    expect(panel.get_by_test_id('development-form-repeat-approval')).to_have_count(0)
                    page.route('**/api/remote-form/repeats', lambda route: route.fulfill(status=409, body=''))
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(repeat).to_contain_text('ölçüm kullanılamıyor')
                    expect(repeat).to_contain_text('Temel ve durum planları ayrı ölçülür')
                    expect(panel.get_by_test_id('development-form-repeat-outcomes')).to_have_count(0)
                    page.route('**/api/remote-form/repeats', lambda route: route.fulfill(status=404, body=''))
                    page.get_by_role('button', name='Yenile', exact=True).click()
                    expect(repeat).to_contain_text('HTTPS form planı pinli değil')
                    expect(repeat).not_to_contain_text('durum planı kapsam dışı')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'))
                    panel.get_by_role('button', name='Çalışma kayıtlarını aç').click()
                    expect(page.get_by_role('heading', name='Kalıcı çalışma kayıtları')).to_be_visible()
                    page.get_by_role('button', name='Çıkış', exact=True).click()
                    page.get_by_label('Yerel oturum anahtarı', exact=True).fill(token)
                    page.get_by_role('button', name='Giriş yap', exact=True).click()
                    expect(page.get_by_test_id('development-status')).to_be_visible()
                    expect(page.get_by_role('button', name='Geliştirme', exact=True)).to_have_attribute('aria-current', 'page')
                    self.assertEqual(writes, [origin + '/api/login', origin + '/api/logout', origin + '/api/login'])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

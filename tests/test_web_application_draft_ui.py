import json
import os
from pathlib import Path
import shlex
from io import StringIO
import tempfile
import unittest
from unittest.mock import patch

from aos import local_app
from aos.contracts import REPO_ROOT, digest
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires isolated Docker desktop and built UI')
class WebApplicationDraftUITests(unittest.TestCase):
    def test_old_backend_does_not_offer_unconfirmed_image_drafts(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='web-capabilities-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
            fixture['profile']['entry_url'] = 'https://example.com/app/'
            fixture['profile']['allowed_origins'] = ['https://example.com']
            profile = WebApplicationProfile.model_validate(fixture['profile'])
            profiles = WebApplicationProfiles(root / 'profiles')
            profile_sha256 = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=profile_sha256)
            flags = ('--web-profiles-root', str(profiles.root), '--web-task-root', str(root / 'tasks'))
            with task_server(root, 'fixture', flags) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    page.route('**/api/web-applications/capabilities', lambda route: route.fulfill(status=404))
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Web applications', exact=True).click()
                    task = page.get_by_test_id('web-task-draft')
                    task.get_by_label('Task profile').select_option(profile_sha256)
                    task.get_by_label('Task key in profile').fill(profile.task_keys[0])
                    task.get_by_label('Independent verifier reference').fill('synthetic-entry-check')
                    task.get_by_role('button', name='Preview task').click()
                    preview = page.get_by_test_id('web-task-preview')
                    preview.get_by_label('Confirm exact task SHA-256').fill(preview.locator('code').inner_text())
                    preview.get_by_role('button', name='Register private task file').click()
                    static_form = page.get_by_test_id('web-static-draft')
                    expect(static_form.get_by_test_id('web-image-capability-unavailable')).to_be_visible()
                    choices = static_form.get_by_label('Content type 1').locator('option')
                    self.assertEqual(choices.all_text_contents(), [
                        'application/javascript', 'text/javascript', 'text/css'])
                    static_form.get_by_label('Static asset URL 1').fill(
                        'https://example.com/assets/app.js?v=1')
                    expect(static_form.get_by_test_id('web-static-query-capability-unavailable')).to_be_visible()
                    expect(static_form.get_by_role('button', name='Preview static plan')).to_be_disabled()
                    page.get_by_test_id('web-readonly-data-draft').get_by_label('JSON GET URL 1').fill(
                        'https://example.com/api/summary?view=compact')
                    expect(page.get_by_test_id('web-readonly-data-draft').get_by_role(
                        'button', name='Preview JSON plan')).to_be_disabled()
                    static_form.get_by_label('Static asset URL 1').fill(
                        'https://example.com/assets/app.js')
                    expect(static_form.get_by_role('button', name='Preview static plan')).to_be_enabled()
                    response = client.get('/api/web-applications/capabilities')
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('image/png', response.json()['static_asset_content_types'])
                    legacy = {**response.json(), 'schema_version': '1.0'}
                    legacy.pop('static_asset_canonical_query')
                    page.unroute('**/api/web-applications/capabilities')
                    page.route('**/api/web-applications/capabilities',
                               lambda route: route.fulfill(json=legacy))
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(static_form.get_by_test_id('web-image-capability-unavailable')).to_have_count(0)
                    self.assertEqual(len(choices.all_text_contents()), 7)
                    static_form.get_by_label('Static asset URL 1').fill(
                        'https://example.com/assets/app.js?v=1')
                    expect(static_form.get_by_role('button', name='Preview static plan')).to_be_disabled()
                    malformed = {**response.json(), 'static_asset_canonical_query': False}
                    page.unroute('**/api/web-applications/capabilities')
                    page.route('**/api/web-applications/capabilities',
                               lambda route: route.fulfill(json=malformed))
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(static_form.get_by_role('button', name='Preview static plan')).to_be_disabled()
                    page.unroute('**/api/web-applications/capabilities')
                    page.get_by_role('button', name='Refresh', exact=True).click()
                    expect(static_form.get_by_test_id('web-static-query-capability-unavailable')).to_have_count(0)
                    expect(static_form.get_by_role('button', name='Preview static plan')).to_be_enabled()
                finally:
                    browser.close()

    def test_https_form_state_plan_is_private_and_required_for_command(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='web-form-state-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
            fixture['profile']['entry_url'] = 'https://example.com/app/'
            fixture['profile']['allowed_origins'] = ['https://example.com']
            profile = WebApplicationProfile.model_validate(fixture['profile'])
            profiles = WebApplicationProfiles(root / 'profiles')
            profile_sha256 = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=profile_sha256)
            task_root, plan_root, value_root, state_root = (
                root / name for name in ('tasks', 'plans', 'values', 'states'))
            flags = ('--web-profiles-root', str(profiles.root), '--web-task-root', str(task_root),
                     '--web-form-plan-root', str(plan_root), '--web-form-value-root', str(value_root),
                     '--web-form-state-root', str(state_root))
            with task_server(root, 'fixture', flags) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    external = []
                    page.on('request', lambda request: external.append(request.url)
                            if request.url.startswith('https://example.com') else None)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Web applications', exact=True).click()
                    form = page.get_by_test_id('web-form-draft')
                    form.get_by_label('Task profile').select_option(profile_sha256)
                    form.get_by_label('Task key in profile').fill('update-draft')
                    form.get_by_label('Independent verifier reference').fill('synthetic-form-check')
                    form.get_by_label('I will prepare a separate before/after state GET plan (six manual approvals total).').check()
                    form.get_by_role('button', name='Preview form task').click()
                    task_preview = page.get_by_test_id('web-form-task-preview')
                    task_sha256 = task_preview.locator('code').inner_text()
                    task_preview.get_by_label('Confirm exact task SHA-256').fill(task_sha256)
                    task_preview.get_by_role('button', name='Register private form task').click()
                    plan = page.get_by_test_id('web-form-plan')
                    plan.get_by_label('Form POST URL').fill('https://example.com/app/submit')
                    plan.get_by_label('Receipt GET URL').fill('https://example.com/app/receipt')
                    plan.get_by_label('Form field name').fill('message')
                    plan.get_by_label('Non-secret test value').fill('Synthetic hello')
                    plan.get_by_role('checkbox').check()
                    plan.get_by_role('button', name='Preview form plan').click()
                    plan_preview = page.get_by_test_id('web-form-plan-preview')
                    plan_sha256 = plan_preview.locator('code').first.inner_text()
                    plan_preview.get_by_label('Confirm exact plan SHA-256').fill(plan_sha256)
                    plan_preview.get_by_role('button', name='Register private form plan').click()
                    expect(page.get_by_test_id('web-form-plan-registered')).to_be_visible()
                    expect(page.get_by_test_id('web-form-commands')).to_have_count(0)
                    state = page.get_by_test_id('web-form-state')
                    state.get_by_label('State GET URL').fill('https://example.com/app/state')
                    state.get_by_label('Before HTML SHA-256').fill('1' * 64)
                    state.get_by_label('After HTML SHA-256').fill('2' * 64)
                    state.get_by_label('Bind an exact unique HTML marker transition too').check()
                    state.get_by_label('Marker ID').fill('recordStatus')
                    state.get_by_label('Before marker SHA-256').fill('3' * 64)
                    submitted_marker_sha256 = digest({'text': 'Synthetic hello'})
                    state.get_by_label('After marker SHA-256').fill(submitted_marker_sha256)
                    state.get_by_label('Marker matches submitted field · optional').select_option('message')
                    state.get_by_role('button', name='Preview state plan').click()
                    state_preview = page.get_by_test_id('web-form-state-preview')
                    state_sha256 = state_preview.locator('code').inner_text()
                    self.assertFalse(state_root.exists())
                    state.get_by_label('After marker SHA-256').fill('5' * 64)
                    expect(state_preview).to_have_count(0)
                    state.get_by_label('After marker SHA-256').fill(submitted_marker_sha256)
                    state.get_by_role('button', name='Preview state plan').click()
                    state_preview = page.get_by_test_id('web-form-state-preview')
                    self.assertEqual(state_preview.locator('code').inner_text(), state_sha256)
                    register = state_preview.get_by_role('button', name='Register private state plan')
                    expect(register).to_be_disabled()
                    state_preview.get_by_label('Confirm exact state plan SHA-256').fill(state_sha256)
                    register.click()
                    receipt = page.get_by_test_id('web-form-state-registered')
                    expect(receipt).to_contain_text('Private state plan registered')
                    state_file = state_root / (state_sha256 + '.json')
                    self.assertEqual(json.loads(state_file.read_text())['submitted_field_name'], 'message')
                    self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)
                    commands = page.get_by_test_id('web-form-commands').locator('pre').all_inner_texts()
                    self.assertEqual(len(commands), 2)
                    self.assertTrue(commands[0].startswith('./scripts/aos-v1 preview-remote-form-state '))
                    self.assertIn('--remote-form-state-plan-file ', commands[0])
                    self.assertIn('--remote-form-public-state-plan-sha256 ' + state_sha256, commands[0])
                    self.assertNotIn('Synthetic hello', '\n'.join(commands))
                    with (patch('aos.local_app.WEB_PROFILES', profiles.root),
                          patch('sys.argv', shlex.split(commands[0])),
                          patch('sys.stdout', new_callable=StringIO) as preview_output):
                        local_app.main()
                    manager_preview = json.loads(preview_output.getvalue())
                    self.assertEqual(manager_preview['state_plan_sha256'], state_sha256)
                    self.assertEqual(manager_preview['form_plan_sha256'], plan_sha256)
                    self.assertFalse(manager_preview['execution_authorized'])
                    self.assertNotIn('Synthetic hello', preview_output.getvalue())
                    self.assertEqual(external, [])
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(state).to_contain_text('Ayrı HTTPS durum planı')
                finally:
                    browser.close()

    def test_https_form_draft_is_offline_private_and_bilingual(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='web-form-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
            fixture['profile']['entry_url'] = 'https://example.com/app/'
            fixture['profile']['allowed_origins'] = ['https://example.com']
            profile = WebApplicationProfile.model_validate(fixture['profile'])
            profiles = WebApplicationProfiles(root / 'profiles')
            profile_sha256 = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=profile_sha256)
            task_root, plan_root, value_root = (root / name for name in ('tasks', 'plans', 'values'))
            flags = ('--web-profiles-root', str(profiles.root), '--web-task-root', str(task_root),
                     '--web-form-plan-root', str(plan_root), '--web-form-value-root', str(value_root))
            with task_server(root, 'fixture', flags) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page()
                    external = []
                    page.on('request', lambda request: external.append(request.url)
                            if request.url.startswith('https://example.com') else None)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Web applications', exact=True).click()
                    form = page.get_by_test_id('web-form-draft')
                    expect(form).to_contain_text('Private HTTPS form draft')
                    form.get_by_label('Task profile').select_option(profile_sha256)
                    form.get_by_label('Task key in profile').fill('update-draft')
                    form.get_by_label('Independent verifier reference').fill('synthetic-form-check')
                    expect(form.get_by_role('button', name='Preview form task')).to_be_enabled()
                    form.get_by_role('button', name='Preview form task').click()
                    task_preview = page.get_by_test_id('web-form-task-preview')
                    task_sha256 = task_preview.locator('code').inner_text()
                    self.assertFalse(task_root.exists())
                    task_preview.get_by_label('Confirm exact task SHA-256').fill(task_sha256)
                    task_preview.get_by_role('button', name='Register private form task').click()
                    expect(page.get_by_test_id('web-form-task-registered')).to_contain_text(task_sha256)
                    plan = page.get_by_test_id('web-form-plan')
                    plan.get_by_label('Form POST URL').fill('https://example.com/app/submit')
                    plan.get_by_label('Receipt GET URL').fill('https://example.com/app/receipt')
                    plan.get_by_label('Form field name').fill('message')
                    plan.get_by_label('Non-secret test value').fill('Synthetic hello')
                    plan.get_by_role('checkbox').check()
                    plan.get_by_role('button', name='Preview form plan').click()
                    plan_preview = page.get_by_test_id('web-form-plan-preview')
                    plan_sha256 = plan_preview.locator('code').first.inner_text()
                    self.assertFalse(plan_root.exists())
                    self.assertFalse(value_root.exists())
                    plan_preview.get_by_label('Confirm exact plan SHA-256').fill(plan_sha256)
                    plan_preview.get_by_role('button', name='Register private form plan').click()
                    expect(page.get_by_test_id('web-form-plan-registered')).to_contain_text('Private form plan registered')
                    expect(plan.get_by_label('Non-secret test value')).to_have_value('')
                    self.assertNotIn('Synthetic hello', form.inner_text())
                    commands = page.get_by_test_id('web-form-commands').locator('pre').all_inner_texts()
                    self.assertEqual(len(commands), 2)
                    self.assertTrue(commands[0].startswith('./scripts/aos-v1 preview-remote-form '))
                    self.assertIn('--remote-form-public-plan-sha256 ' + plan_sha256, commands[0])
                    self.assertIn('--remote-form-value-file ', commands[0])
                    self.assertNotIn('Synthetic hello', '\n'.join(commands))
                    with (patch('aos.local_app.WEB_PROFILES', profiles.root),
                          patch('sys.argv', shlex.split(commands[0])),
                          patch('sys.stdout', new_callable=StringIO) as base_output):
                        local_app.main()
                    self.assertEqual(json.loads(base_output.getvalue())['plan_sha256'], plan_sha256)
                    self.assertNotIn('Synthetic hello', base_output.getvalue())
                    plan.get_by_label('Form field mode').select_option('multiple')
                    expect(page.get_by_test_id('web-form-commands')).to_have_count(0)
                    ordered = page.get_by_test_id('web-form-fields')
                    ordered.get_by_label('Field name 1').fill('subject')
                    ordered.get_by_label('Test value 1').fill('Synthetic subject')
                    ordered.get_by_label('Field name 2').fill('message')
                    ordered.get_by_label('Test value 2').fill('Synthetic hello')
                    plan.get_by_role('checkbox').check()
                    plan.get_by_role('button', name='Preview form plan').click()
                    multi_preview = page.get_by_test_id('web-form-plan-preview')
                    self.assertFalse('Synthetic subject' in multi_preview.inner_text())
                    ordered.get_by_label('Test value 2').fill('Synthetic changed')
                    expect(multi_preview).to_have_count(0)
                    ordered.get_by_label('Test value 2').fill('Synthetic hello')
                    plan.get_by_role('button', name='Preview form plan').click()
                    multi_preview = page.get_by_test_id('web-form-plan-preview')
                    multi_sha256 = multi_preview.locator('code').first.inner_text()
                    self.assertNotEqual(multi_sha256, plan_sha256)
                    multi_preview.get_by_label('Confirm exact plan SHA-256').fill(multi_sha256)
                    multi_preview.get_by_role('button', name='Register private form plan').click()
                    expect(page.get_by_test_id('web-form-plan-registered')).to_contain_text('Private form plan registered')
                    expect(ordered.get_by_label('Test value 1')).to_have_value('')
                    expect(ordered.get_by_label('Test value 2')).to_have_value('')
                    self.assertNotIn('Synthetic subject', form.inner_text())
                    multi_commands = page.get_by_test_id('web-form-commands').locator('pre').all_inner_texts()
                    self.assertIn('--remote-form-fields-file ', multi_commands[0])
                    self.assertNotIn('--remote-form-field-name ', multi_commands[0])
                    self.assertIn('--remote-form-public-plan-sha256 ' + multi_sha256, multi_commands[0])
                    with (patch('aos.local_app.WEB_PROFILES', profiles.root),
                          patch('sys.argv', shlex.split(multi_commands[0])),
                          patch('sys.stdout', new_callable=StringIO) as multi_output):
                        local_app.main()
                    self.assertEqual(json.loads(multi_output.getvalue())['plan_sha256'], multi_sha256)
                    self.assertNotIn('Synthetic subject', multi_output.getvalue())
                    self.assertEqual(external, [])
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(form).to_contain_text('Özel HTTPS form taslağı')
                    expect(ordered).to_contain_text('DOM sırasıyla')
                    page.get_by_role('button', name='English', exact=True).click()
                    expect(form).to_contain_text('Private HTTPS form draft')
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                finally:
                    browser.close()

    def test_preview_edit_invalidation_and_explicit_registration(self):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='web-draft-ui-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profiles = root / 'profiles'
            task_drafts = root / 'task-drafts'
            route_drafts = root / 'route-drafts'
            static_drafts = root / 'static-drafts'
            readonly_drafts = root / 'readonly-drafts'
            with task_server(root, 'fixture', ('--web-profiles-root', str(profiles),
                    '--web-task-root', str(task_drafts), '--web-route-root',
                    str(route_drafts), '--web-static-root',
                    str(static_drafts), '--web-readonly-data-root',
                    str(readonly_drafts))) as (origin, token, client, server), sync_playwright() as playwright:
                def manager_preview(command):
                    with (patch('aos.local_app.WEB_PROFILES', profiles),
                          patch('sys.argv', shlex.split(command)),
                          patch('sys.stdout', new_callable=StringIO) as output):
                        local_app.main()
                    return json.loads(output.getvalue())

                browser = playwright.chromium.launch(executable_path=str(
                    REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 900})
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Web applications', exact=True).click()
                    page.get_by_label('Application key', exact=True).fill('synthetic-crm')
                    page.get_by_label('Tenant key', exact=True).fill('synthetic-tenant')
                    page.get_by_label('Account role', exact=True).fill('editor')
                    page.get_by_label('Entry URL', exact=True).fill('https://crm.example.invalid/app/')
                    page.get_by_label('Allowed origins · one per line', exact=True).fill('https://crm.example.invalid')
                    tasks = page.locator('.web-applications textarea').nth(1)
                    tasks.fill('update-draft\nfind-record')
                    page.get_by_label('Data rights reference', exact=True).fill('synthetic-rights-review')
                    page.locator('.web-applications fieldset select').nth(0).select_option('requested')
                    page.locator('.web-applications fieldset select').nth(1).select_option('requested')
                    page.get_by_role('button', name='Preview draft', exact=True).click()
                    expect(page.get_by_test_id('web-profile-preview')).to_be_visible()
                    self.assertFalse(profiles.exists())
                    tasks.fill('find-record')
                    expect(page.get_by_test_id('web-profile-preview')).to_have_count(0)
                    tasks.fill('update-draft\nfind-record')
                    page.get_by_role('button', name='Preview draft', exact=True).click()
                    preview = page.get_by_test_id('web-profile-preview')
                    expect(preview).to_be_visible()
                    fingerprint = preview.locator('code').first.inner_text()
                    self.assertRegex(fingerprint, r'^[a-f0-9]{64}$')
                    expect(page.get_by_role('button', name='Register previewed draft')).to_be_disabled()
                    preview.get_by_role('checkbox').check()
                    page.get_by_role('button', name='Register previewed draft').click()
                    expect(page.get_by_test_id('web-profile-registered')).to_contain_text(fingerprint)
                    expect(page.get_by_test_id('web-applications')).to_contain_text('synthetic-crm')
                    self.assertEqual([path.name for path in profiles.iterdir()], [fingerprint + '.json'])
                    report = client.get('/api/web-applications').json()
                    self.assertEqual(len(report), 1)
                    self.assertEqual(report[0]['profile_sha256'], fingerprint)
                    for field in ('execution_authorized', 'collection_authorized',
                                  'training_ready', 'promotion_authorized'):
                        self.assertFalse(report[0][field])
                    page.get_by_label('Previous profile · optional').select_option(fingerprint)
                    expect(page.get_by_label('Application key', exact=True)).to_be_disabled()
                    tasks.fill('find-record')
                    page.get_by_role('button', name='Preview draft', exact=True).click()
                    successor_preview = page.get_by_test_id('web-profile-preview')
                    expect(successor_preview).to_contain_text('Revision 2')
                    expect(successor_preview).to_contain_text(fingerprint)
                    successor_hash = successor_preview.locator('code').first.inner_text()
                    self.assertNotEqual(successor_hash, fingerprint)
                    self.assertEqual(len(list(profiles.iterdir())), 1)
                    successor_preview.get_by_role('checkbox').check()
                    page.get_by_role('button', name='Register previewed draft').click()
                    expect(page.get_by_test_id('web-profile-registered')).to_contain_text(successor_hash)
                    self.assertEqual(len(list(profiles.iterdir())), 2)
                    successor = json.loads((profiles / (successor_hash + '.json')).read_text())
                    self.assertEqual((profiles / (successor_hash + '.json')).stat().st_mode & 0o777, 0o600)
                    self.assertEqual(successor['previous_sha256'], fingerprint)
                    self.assertEqual(successor['revision'], 2)
                    self.assertEqual(successor['task_keys'], ['find-record'])
                    self.assertEqual(len(client.get('/api/web-applications').json()), 2)
                    page.get_by_label('Previous profile · optional').select_option(successor_hash)
                    expect(page.get_by_test_id('web-profile-registered')).to_have_count(0)
                    page.get_by_role('button', name='Preview draft', exact=True).click()
                    expect(page.get_by_test_id('web-profile-preview')).to_contain_text('Revision 3')
                    self.assertEqual(len(list(profiles.iterdir())), 2)
                    page.get_by_label('Previous profile · optional').select_option(fingerprint)
                    expect(page.get_by_test_id('web-profile-preview')).to_have_count(0)
                    task_form = page.get_by_test_id('web-task-draft')
                    save_task = task_form.get_by_role('button', name='Register private task file')
                    expect(save_task).to_have_count(0)
                    task_form.get_by_label('Task profile').select_option(fingerprint)
                    task_form.get_by_label('Task key in profile').fill('update-draft')
                    task_form.get_by_label('Independent verifier reference').fill('synthetic-entry-check')
                    task_form.get_by_role('button', name='Preview task').click()
                    task_preview = page.get_by_test_id('web-task-preview')
                    expect(task_preview).to_be_visible()
                    self.assertFalse(task_drafts.exists())
                    task_hash = task_preview.locator('code').inner_text()
                    expect(save_task).to_be_disabled()
                    task_form.get_by_label('Confirm exact task SHA-256').fill(task_hash)
                    save_task.click()
                    expect(page.get_by_test_id('web-task-registered')).to_contain_text(task_hash)
                    self.assertEqual([item.name for item in task_drafts.iterdir()], [task_hash + '.json'])
                    entry_commands = page.get_by_test_id('web-entry-commands')
                    expect(entry_commands).to_contain_text('Offline manager preview')
                    task_file = (task_drafts / (task_hash + '.json')).relative_to(REPO_ROOT)
                    self.assertEqual(entry_commands.locator('pre').all_inner_texts(), [
                        './scripts/aos-v1 preview-remote-entry --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(task_file),
                        './scripts/aos-v1 start --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(task_file)])
                    self.assertEqual(manager_preview(entry_commands.locator('pre').first.inner_text())['task_sha256'], task_hash)
                    static_form = page.get_by_test_id('web-static-draft')
                    static_form.get_by_label('Static asset URL 1').fill(
                        'https://crm.example.invalid/assets/app.js?v=1')
                    expect(static_form).to_contain_text('Never put secrets or personal data in a fixed query')
                    static_form.get_by_role('button', name='Add asset').click()
                    static_form.get_by_label('Static asset URL 2').fill(
                        'https://crm.example.invalid/assets/app.css')
                    static_form.get_by_label('Content type 2').select_option('text/css')
                    static_form.get_by_role('button', name='Add asset').click()
                    static_form.get_by_label('Static asset URL 3').fill(
                        'https://crm.example.invalid/assets/logo.png')
                    static_form.get_by_label('Content type 3').select_option('image/png')
                    expect(static_form.get_by_test_id('web-image-capability-unavailable')).to_have_count(0)
                    static_form.get_by_role('button', name='Preview static plan').click()
                    static_preview = page.get_by_test_id('web-static-preview')
                    expect(static_preview).to_be_visible()
                    self.assertFalse(static_drafts.exists())
                    static_hash = static_preview.locator('code').inner_text()
                    static_form.get_by_label('Confirm exact plan SHA-256').fill(static_hash)
                    static_form.get_by_role('button', name='Register private static plan').click()
                    expect(page.get_by_test_id('web-static-registered')).to_contain_text(static_hash)
                    self.assertEqual(json.loads((static_drafts / (static_hash + '.json')).read_text())[
                        'assets'][2]['content_type'], 'image/png')
                    self.assertEqual(json.loads((static_drafts / (static_hash + '.json')).read_text())[
                        'assets'][0]['url'], 'https://crm.example.invalid/assets/app.js?v=1')
                    static_file = (static_drafts / (static_hash + '.json')).relative_to(REPO_ROOT)
                    static_commands = page.get_by_test_id('web-static-commands')
                    self.assertEqual(static_commands.locator('pre').all_inner_texts(), [
                        './scripts/aos-v1 preview-remote-static-assets --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(task_file)
                        + ' --remote-static-assets-plan-file ' + str(static_file),
                        './scripts/aos-v1 start --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(task_file)
                        + ' --remote-static-assets-plan-file ' + str(static_file)])
                    self.assertEqual(manager_preview(static_commands.locator('pre').first.inner_text())['plan_sha256'], static_hash)
                    readonly_form = page.get_by_test_id('web-readonly-data-draft')
                    readonly_form.get_by_label('JSON GET URL 1').fill(
                        'https://crm.example.invalid/api/summary?view=compact&page=1')
                    expect(readonly_form).to_contain_text('never put secrets or personal data')
                    readonly_form.get_by_role('button', name='Preview JSON plan').click()
                    readonly_preview = page.get_by_test_id('web-readonly-data-preview')
                    expect(readonly_preview).to_be_visible()
                    self.assertFalse(readonly_drafts.exists())
                    readonly_hash = readonly_preview.locator('code').inner_text()
                    readonly_form.get_by_label('Confirm exact plan SHA-256').fill(readonly_hash)
                    readonly_form.get_by_role('button', name='Register private JSON plan').click()
                    expect(page.get_by_test_id('web-readonly-data-registered')).to_contain_text(readonly_hash)
                    readonly_file = (readonly_drafts / (readonly_hash + '.json')).relative_to(REPO_ROOT)
                    self.assertEqual(json.loads((readonly_drafts / (readonly_hash + '.json')).read_text())[
                        'data_resources'][0]['url'],
                        'https://crm.example.invalid/api/summary?view=compact&page=1')
                    self.assertEqual(json.loads((readonly_drafts / (readonly_hash + '.json')).read_text())[
                        'assets'][0]['url'], 'https://crm.example.invalid/assets/app.js?v=1')
                    readonly_commands = page.get_by_test_id('web-readonly-data-commands')
                    self.assertEqual(readonly_commands.locator('pre').all_inner_texts(), [
                        './scripts/aos-v1 preview-remote-readonly-data --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(task_file)
                        + ' --remote-readonly-data-plan-file ' + str(readonly_file),
                        './scripts/aos-v1 start --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(task_file)
                        + ' --remote-readonly-data-plan-file ' + str(readonly_file)])
                    self.assertEqual(manager_preview(readonly_commands.locator('pre').first.inner_text())['plan_sha256'], readonly_hash)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(static_form).to_contain_text('Exact statik JS/CSS/görsel planı')
                    expect(static_form).to_contain_text('Sabit sorguya sır veya kişisel veri koymayın')
                    expect(static_commands).to_contain_text('tek manuel onay')
                    expect(readonly_form).to_contain_text('Exact salt okunur JSON GET planı')
                    expect(readonly_form).to_contain_text('sorguya sır veya kişisel veri koymayın')
                    expect(readonly_commands).to_contain_text('tek manuel onay')
                    page.get_by_role('button', name='English', exact=True).click()
                    static_form.get_by_label('Static asset URL 1').fill(
                        'https://crm.example.invalid/assets/new.js')
                    expect(static_commands).to_have_count(0)
                    expect(readonly_commands).to_have_count(0)
                    task_form.get_by_label('Planned page count').select_option('2')
                    expect(page.get_by_test_id('web-task-registered')).to_have_count(0)
                    expect(entry_commands).to_have_count(0)
                    task_form.get_by_role('button', name='Preview task').click()
                    two_page_hash = page.get_by_test_id('web-task-preview').locator('code').inner_text()
                    self.assertNotEqual(two_page_hash, task_hash)
                    task_form.get_by_label('Confirm exact task SHA-256').fill(two_page_hash)
                    task_form.get_by_role('button', name='Register private task file').click()
                    route_form = page.get_by_test_id('web-route-draft')
                    expect(route_form).to_be_visible()
                    expect(route_form).to_contain_text('Never put secrets or personal data in a query')
                    route_form.get_by_label('Planned routes · one URL per line').fill(
                        'https://crm.example.invalid/app/\n'
                        'https://crm.example.invalid/details?view=compact&page=1')
                    route_form.get_by_role('button', name='Preview route plan').click()
                    route_preview = page.get_by_test_id('web-route-preview')
                    expect(route_preview).to_be_visible()
                    self.assertFalse(route_drafts.exists())
                    plan_hash = route_preview.locator('code').inner_text()
                    save_routes = route_form.get_by_role('button', name='Register private route plan')
                    expect(save_routes).to_be_disabled()
                    route_form.get_by_label('Confirm exact plan SHA-256').fill(plan_hash)
                    save_routes.click()
                    expect(page.get_by_test_id('web-route-registered')).to_contain_text(plan_hash)
                    self.assertEqual([item.name for item in route_drafts.iterdir()], [plan_hash + '.json'])
                    self.assertEqual(json.loads((route_drafts / (plan_hash + '.json')).read_text())[
                        'routes'][1],
                        'https://crm.example.invalid/details?view=compact&page=1')
                    route_commands = page.get_by_test_id('web-route-commands')
                    expect(route_commands).to_contain_text('New session after safe stop')
                    two_page_file = (task_drafts / (two_page_hash + '.json')).relative_to(REPO_ROOT)
                    route_file = (route_drafts / (plan_hash + '.json')).relative_to(REPO_ROOT)
                    self.assertEqual(route_commands.locator('pre').all_inner_texts(), [
                        './scripts/aos-v1 preview-remote-routes --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(two_page_file)
                        + ' --remote-routes-plan-file ' + str(route_file),
                        './scripts/aos-v1 start --remote-entry-profile-sha256 '
                        + fingerprint + ' --remote-entry-task-file ' + str(two_page_file)
                        + ' --remote-routes-plan-file ' + str(route_file)])
                    self.assertEqual(manager_preview(route_commands.locator('pre').first.inner_text())['plan_sha256'], plan_hash)
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(route_form).to_contain_text('Sorguya sır veya kişisel veri koymayın')
                    page.get_by_role('button', name='English', exact=True).click()
                    preflight = page.get_by_test_id('https-entry-preflight')
                    request_button = preflight.get_by_role('button', name='Make one HTTPS entry request')
                    expect(request_button).to_be_disabled()
                    preflight.get_by_label('Check profile').select_option(fingerprint)
                    preflight.get_by_label('Exact entry URL').fill('https://crm.example.invalid/app/')
                    preflight.get_by_label('Confirm profile SHA-256').fill(fingerprint)
                    expect(request_button).to_be_disabled()
                    preflight.get_by_role('checkbox').check()
                    expect(request_button).to_be_enabled()
                    requests = []

                    def synthetic_preflight(route):
                        requests.append(route.request.post_data_json)
                        if len(requests) == 2:
                            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                                'schema_version': '1.0', 'mode': 'explicit_host_https_entry_probe',
                                'status': 'https_entry_reached', 'profile_sha256': fingerprint,
                                'origin_sha256': '0' * 64, 'request_sha256': '0' * 64,
                                'response_sha256': '0' * 64, 'response_bytes': 27,
                                'tls_hostname_verified': True, 'browser_connected': False,
                                'execution_authorized': False, 'collection_authorized': False,
                                'training_ready': False}))
                            return
                        if len(requests) == 3:
                            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                                'schema_version': '1.1', 'mode': 'explicit_host_https_entry_probe',
                                'status': 'https_entry_reached', 'profile_sha256': fingerprint,
                                'origin_sha256': '0' * 64, 'request_sha256': '0' * 64,
                                'response_sha256': '0' * 64, 'response_bytes': 27,
                                'entry_html_signals': {
                                    'form_count': 1, 'script_count': 2, 'stylesheet_count': 1,
                                    'image_count': 1, 'password_input_count': 0,
                                    'cross_origin_resource_count': 1,
                                    'unclassified_resource_count': 0,
                                    'static_html_only': True, 'browser_or_account_verified': False},
                                'tls_hostname_verified': True, 'browser_connected': False,
                                'execution_authorized': False, 'collection_authorized': False,
                                'training_ready': False}))
                            return
                        if len(requests) == 4:
                            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                                'schema_version': '1.2', 'mode': 'explicit_host_https_entry_probe',
                                'status': 'https_entry_reached', 'profile_sha256': fingerprint,
                                'origin_sha256': '0' * 64, 'request_sha256': '0' * 64,
                                'response_sha256': '0' * 64, 'response_bytes': 27,
                                'entry_html_signals': {
                                    'form_count': 1, 'post_form_count': 1,
                                    'cross_origin_form_action_count': 0,
                                    'unclassified_form_action_count': 0,
                                    'script_count': 2, 'stylesheet_count': 1,
                                    'image_count': 1, 'password_input_count': 0,
                                    'cross_origin_resource_count': 1,
                                    'unclassified_resource_count': 0,
                                    'static_html_only': True, 'browser_or_account_verified': False},
                                'tls_hostname_verified': True, 'browser_connected': False,
                                'execution_authorized': False, 'collection_authorized': False,
                                'training_ready': False}))
                            return
                        if len(requests) == 5:
                            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                                'schema_version': '1.3', 'mode': 'explicit_host_https_entry_probe',
                                'status': 'https_entry_reached', 'profile_sha256': fingerprint,
                                'origin_sha256': '0' * 64, 'request_sha256': '0' * 64,
                                'response_sha256': '0' * 64, 'response_bytes': 27,
                                'entry_html_signals': {'form_count': -1},
                                'tls_hostname_verified': True, 'browser_connected': False,
                                'execution_authorized': False, 'collection_authorized': False,
                                'training_ready': False}))
                            return
                        if len(requests) == 6:
                            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                                'schema_version': '1.3', 'mode': 'explicit_host_https_entry_probe',
                                'status': 'https_entry_reached', 'profile_sha256': fingerprint,
                                'origin_sha256': '0' * 64, 'request_sha256': '0' * 64,
                                'response_sha256': '0' * 64, 'response_bytes': 27,
                                'entry_html_signals': {
                                    'form_count': 0, 'post_form_count': 0,
                                    'cross_origin_form_action_count': 0,
                                    'unclassified_form_action_count': 0,
                                    'meta_refresh_count': 1,
                                    'cross_origin_meta_refresh_count': 1,
                                    'unclassified_meta_refresh_count': 1,
                                    'script_count': 0, 'stylesheet_count': 0,
                                    'image_count': 0, 'password_input_count': 0,
                                    'cross_origin_resource_count': 0,
                                    'unclassified_resource_count': 0,
                                    'static_html_only': True, 'browser_or_account_verified': False},
                                'tls_hostname_verified': True, 'browser_connected': False,
                                'execution_authorized': False, 'collection_authorized': False,
                                'training_ready': False}))
                            return
                        if len(requests) > 6:
                            route.fulfill(status=404 if len(requests) == 7 else 422,
                                          content_type='application/json',
                                          body='{"detail":"No request made"}')
                            return
                        route.fulfill(status=200, content_type='application/json', body=json.dumps({
                            'schema_version': '1.3', 'mode': 'explicit_host_https_entry_probe',
                            'status': 'https_entry_reached', 'profile_sha256': fingerprint,
                            'origin_sha256': '0' * 64, 'request_sha256': '0' * 64,
                            'response_sha256': '0' * 64, 'response_bytes': 27,
                            'entry_html_signals': {
                                'form_count': 1, 'post_form_count': 1,
                                'cross_origin_form_action_count': 0,
                                'unclassified_form_action_count': 0,
                                'meta_refresh_count': 2,
                                'cross_origin_meta_refresh_count': 1,
                                'unclassified_meta_refresh_count': 1,
                                'script_count': 2, 'stylesheet_count': 1,
                                'image_count': 1, 'password_input_count': 0,
                                'cross_origin_resource_count': 1, 'unclassified_resource_count': 0,
                                'static_html_only': True, 'browser_or_account_verified': False},
                            'tls_hostname_verified': True, 'browser_connected': False,
                            'execution_authorized': False, 'collection_authorized': False,
                            'training_ready': False}))

                    page.route('**/api/web-applications/preflight', synthetic_preflight)
                    self.assertEqual(requests, [])
                    request_button.click()
                    expect(page.get_by_test_id('https-entry-preflight-result')).to_contain_text(
                        'HTTPS entry response verified:')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text(
                        'forms 1, scripts 2, stylesheets 1, images 1')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text('POST forms 1')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text('meta refresh declarations 2')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text('cross-origin meta refreshes 1')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text(
                        'Only the initial HTML response was counted')
                    self.assertEqual(requests, [{'profile_sha256': fingerprint,
                                                 'confirm_sha256': fingerprint,
                                                 'entry_url': 'https://crm.example.invalid/app/',
                                                 'authorized_get': True}])
                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_text('Yeni profil taslağı')).to_be_visible()
                    expect(page.get_by_label('Önceki profil · isteğe bağlı')).to_have_value(fingerprint)
                    expect(page.get_by_test_id('web-task-registered')).to_contain_text('Özel taslak kaydedildi:')
                    expect(page.get_by_test_id('web-route-registered')).to_contain_text('Özel rota planı kaydedildi:')
                    expect(route_commands).to_contain_text('Güvenli durdurma sonrası yeni oturum')
                    expect(page.get_by_test_id('https-entry-preflight-result')).to_contain_text(
                        'HTTPS giriş yanıtı doğrulandı:')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text(
                        'form 1, betik 2, stil 1, görsel 1')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text('POST formu 1')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text('meta yenileme 2')
                    expect(page.get_by_test_id('https-entry-html-signals')).to_contain_text('başka origin meta yönlendirmesi 1')
                    preflight.get_by_label('Exact giriş URL’si').fill('https://crm.example.invalid/app/next')
                    preflight.get_by_role('button', name='Tek HTTPS giriş isteğini yap').click()
                    expect(page.get_by_test_id('https-entry-preflight-result')).to_contain_text(
                        'HTTPS giriş yanıtı doğrulandı:')
                    expect(preflight.get_by_test_id('https-entry-html-signals')).to_have_count(0)
                    preflight.get_by_label('Exact giriş URL’si').fill('https://crm.example.invalid/app/legacy')
                    preflight.get_by_role('button', name='Tek HTTPS giriş isteğini yap').click()
                    expect(preflight.get_by_test_id('https-entry-html-signals')).to_contain_text('form 1, betik 2')
                    expect(preflight.get_by_test_id('https-entry-html-signals')).not_to_contain_text('POST formu')
                    preflight.get_by_label('Exact giriş URL’si').fill('https://crm.example.invalid/app/form-legacy')
                    preflight.get_by_role('button', name='Tek HTTPS giriş isteğini yap').click()
                    expect(preflight.get_by_test_id('https-entry-html-signals')).to_contain_text('POST formu 1')
                    expect(preflight.get_by_test_id('https-entry-html-signals')).not_to_contain_text('meta yenileme')
                    preflight.get_by_label('Exact giriş URL’si').fill('https://crm.example.invalid/app/bad')
                    preflight.get_by_role('button', name='Tek HTTPS giriş isteğini yap').click()
                    expect(preflight).to_contain_text('HTTPS giriş kontrolü kullanılamıyor; ağ denemesi yapılmış olabilir.')
                    expect(preflight.get_by_test_id('https-entry-html-signals')).to_have_count(0)
                    preflight.get_by_label('Exact giriş URL’si').fill('https://crm.example.invalid/app/bad-refresh')
                    preflight.get_by_role('button', name='Tek HTTPS giriş isteğini yap').click()
                    expect(preflight).to_contain_text('HTTPS giriş kontrolü kullanılamıyor; ağ denemesi yapılmış olabilir.')
                    expect(preflight.get_by_test_id('https-entry-html-signals')).to_have_count(0)
                    preflight.get_by_label('Exact giriş URL’si').fill('https://crm.example.invalid/app/again')
                    preflight.get_by_role('button', name='Tek HTTPS giriş isteğini yap').click()
                    expect(preflight).to_contain_text('Bu sunucu sürümünde HTTPS giriş kontrolü yok; istek yapılmadı.')
                    preflight.get_by_label('Exact giriş URL’si').fill('https://crm.example.invalid/app/other')
                    preflight.get_by_role('button', name='Tek HTTPS giriş isteğini yap').click()
                    expect(preflight).to_contain_text('Profil, URL veya onay kapsamı geçersiz; ağ isteği yapılmadı.')
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()

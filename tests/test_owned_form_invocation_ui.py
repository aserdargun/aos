import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_UI_TESTS') == '1',
                     'Requires real Docker and built bilingual UI')
class OwnedFormInvocationUITests(unittest.TestCase):
    def test_single_use_start_is_explicit_and_audit_is_manual_and_stays_bound(self):
        self._check_single_use_start_and_audit(recipe=False)

    def test_recipe_steps_and_version_bound_audit_in_english_and_turkish(self):
        self._check_single_use_start_and_audit(recipe=True)

    def _check_single_use_start_and_audit(self, *, recipe):
        from playwright.sync_api import expect, sync_playwright
        from test_desktop_task_ui import task_server

        profile_sha256 = 'e' * 64
        skill_sha256 = 'b' * 64
        invocation_sha256 = 'a' * 64
        run_ref = '4' * 64
        report_sha256 = '9' * 64
        fixture_name = 'site_skill_form_recipe_audit' if recipe else 'site_skill_form_invocation_audit'
        audit_fixture = json.loads((REPO_ROOT / f'examples/{fixture_name}.json').read_text())['report']
        recipe_sha256 = '8' * 64
        if recipe:
            audit_fixture['recipe_sha256'] = recipe_sha256
            audit_fixture['stages'][1], audit_fixture['stages'][2] = audit_fixture['stages'][2], audit_fixture['stages'][1]
            audit_fixture['symbolic_step_keys'][1], audit_fixture['symbolic_step_keys'][2] = audit_fixture['symbolic_step_keys'][2], audit_fixture['symbolic_step_keys'][1]
        mode = 'owned_synthetic_form_recipe' if recipe else 'owned_synthetic_form_invocation'
        steps = ([{'step_key': key, 'operation': stage['stage']}
                  for key, stage in zip(audit_fixture['symbolic_step_keys'], audit_fixture['stages'], strict=True)]
                 if recipe else [])
        claim = 'executable_recipe_executed' if recipe else 'invocation_execution_verified'
        skill_claim = 'skill_executed=true' if recipe else 'skill_executed=false'
        starts, audits = [], []

        with tempfile.TemporaryDirectory(prefix='owned-form-invocation-ui-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, client, server), sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    unavailable_endpoints = []
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def console_message(message):
                        if message.type != 'error':
                            return
                        if (message.location.get('url') == origin + '/api/remote-form/repeats'
                                and '404 (Not Found)' in message.text):
                            unavailable_endpoints.append(message.location['url'])
                        else:
                            errors.append(message.text + ' ' + str(message.location))

                    page.on('console', console_message)

                    def status_payload():
                        started = bool(starts)
                        audited = bool(audits)
                        return {
                            'available': True, 'busy': False, 'paused': False, 'reserved': False,
                            'supports_approve_all': True, 'supports_learning_metadata': False,
                            'kinds': ['browser_remote_form'], 'browser_display': 'desktop',
                            'owned_form_invocation': {
                                'mode': mode,
                                **({'recipe_sha256': recipe_sha256, 'steps': steps} if recipe else {}),
                                'lifecycle': 'audited' if audited else 'completed' if started else 'ready',
                                'profile_sha256': profile_sha256,
                                'skill_sha256': skill_sha256,
                                'invocation_sha256': invocation_sha256,
                                'run_ref': run_ref if started else None,
                                'report_sha256': report_sha256 if audited else None,
                            },
                            'remote_form': {
                                'plan_sha256': '1' * 64, 'entry_url': 'https://form.example.invalid/',
                                'submit_url': 'https://form.example.invalid/submit',
                                'receipt_url': 'https://form.example.invalid/receipt',
                                'field_name': 'message', 'field_names': ['message'],
                                'body_sha256': '2' * 64, 'state_plan_sha256': '3' * 64,
                                'state_url': 'https://form.example.invalid/state',
                                'mode': 'synthetic_one_post',
                            },
                            'jobs': ([{
                                'job_id': 'owned-job', 'run_id': run_ref, 'kind': 'browser_remote_form',
                                'status': 'succeeded', 'real_model': 0, 'runtime_id': 'runtime-owned',
                            }] if started else []),
                            'approval': None,
                        }

                    def route_tasks(route):
                        request = route.request
                        if request.method == 'POST':
                            starts.append(request.post_data_json)
                            route.fulfill(status=200, json={'accepted': True})
                        else:
                            route.fulfill(status=200, json=status_payload())

                    def route_audit(route):
                        attempt = len(audits)
                        audits.append(route.request.url)
                        report = copy.deepcopy(audit_fixture)
                        if attempt == 0:
                            report['skill_sha256'] = 'c' * 64
                        elif attempt == 1:
                            report['skill_executed'] = not recipe
                        elif recipe and attempt == 2:
                            report['recipe_sha256'] = '7' * 64
                        elif recipe and attempt == 3:
                            report['symbolic_step_keys'].reverse()
                        elif recipe and attempt == 4:
                            report = json.loads((REPO_ROOT / 'examples/site_skill_form_invocation_audit.json').read_text())['report']
                        route.fulfill(status=200, json={
                            **({'mode': mode} if recipe else {}),
                            'available': True, 'status': 'verified', 'report': report,
                            'report_sha256': report_sha256,
                        })

                    page.route('**/api/tasks', route_tasks)
                    page.route('**/api/tasks/owned-form-invocation-audit', route_audit)
                    page.goto(origin + '/ui/')
                    page.get_by_label('Local session token', exact=True).fill(token)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    self.assertEqual(page.url, origin + '/ui/')
                    self.assertTrue(page.title())
                    expect(page.locator('vite-error-overlay')).to_have_count(0)
                    if recipe:
                        expect(page.get_by_test_id('owned-recipe-steps').locator('li')).to_have_count(6)
                        self.assertEqual(page.get_by_test_id('owned-recipe-steps').locator('li').evaluate_all(
                            '(items) => items.map(item => item.dataset.operation)'),
                            [step['operation'] for step in steps])

                    start_button = page.get_by_test_id('start-task')
                    expect(start_button).to_be_disabled()
                    expect(page.get_by_test_id('approve-all')).to_be_disabled()
                    expect(page.get_by_test_id('owned-invocation-audit')).to_have_count(0)
                    self.assertEqual(audits, [])

                    page.get_by_test_id('owned-invocation-opt-in').check()
                    expect(start_button).to_be_enabled()
                    start_button.click()
                    expect(page.get_by_test_id('owned-invocation-audit')).to_be_visible(timeout=10000)
                    expect(start_button).to_be_disabled()
                    self.assertEqual(starts, [{
                        'kind': 'browser_remote_form',
                        'lease_id': client.get('/api/state').json()['control']['lease_id'],
                        'generation': client.get('/api/state').json()['control']['generation'],
                    }])
                    self.assertEqual(audits, [])

                    for _attempt in range(5 if recipe else 2):
                        page.get_by_test_id('owned-invocation-audit').click()
                        expect(page.get_by_test_id('owned-form-invocation')).to_contain_text('Invocation audit is unavailable or malformed')
                        expect(page.get_by_test_id('owned-invocation-audit-result')).to_have_count(0)
                    page.get_by_test_id('owned-invocation-audit').click()
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text(claim)
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text(skill_claim)
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text('No site outcome')
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_be_visible()
                    self.assertEqual(len(audits), 6 if recipe else 3)
                    if recipe:
                        page.get_by_test_id('owned-form-recipe').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-managed-recipe-ui-en.png')

                    page.get_by_role('button', name='Türkçe', exact=True).click()
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text(claim)
                    expect(page.get_by_test_id('owned-invocation-audit-result')).to_contain_text(skill_claim)
                    if recipe:
                        expect(page.get_by_test_id('owned-form-invocation')).to_contain_text('Yönetilen sentetik çalıştırılabilir skill')
                        page.set_viewport_size({'width': 390, 'height': 844})
                        page.get_by_test_id('owned-form-recipe').scroll_into_view_if_needed()
                        page.screenshot(path='/tmp/aos-managed-recipe-ui-tr-mobile.png')
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    self.assertEqual(errors, [])
                    self.assertTrue(set(unavailable_endpoints) <= {origin + '/api/remote-form/repeats'})
                    self.assertEqual(client.get('/api/tasks').json()['jobs'], [])
                finally:
                    browser.close()

    def test_delayed_audit_result_is_ignored_after_runtime_and_run_pin_drift(self):
        asyncio.run(self._check_delayed_audit_result_is_ignored_after_identity_drift())

    def test_delayed_recipe_audit_rejects_mode_recipe_and_step_drift(self):
        for drift in ('mode', 'recipe', 'steps'):
            with self.subTest(drift=drift):
                asyncio.run(self._check_delayed_audit_result_is_ignored_after_identity_drift(
                    recipe=True, drift=drift))

    async def _check_delayed_audit_result_is_ignored_after_identity_drift(self, *, recipe=False, drift='runtime'):
        from playwright.async_api import async_playwright, expect
        from test_desktop_task_ui import task_server

        profile_sha256 = 'e' * 64
        skill_sha256 = 'b' * 64
        invocation_sha256 = 'a' * 64
        original_run_ref = '4' * 64
        changed_run_ref = '5' * 64
        fixture_name = 'site_skill_form_recipe_audit' if recipe else 'site_skill_form_invocation_audit'
        audit_fixture = json.loads((REPO_ROOT / f'examples/{fixture_name}.json').read_text())['report']
        phase = {'started': False, 'drift': False}
        audit_started = asyncio.Event()
        release_audit = asyncio.Event()
        drift_tasks_read = asyncio.Event()
        drift_state_read = asyncio.Event()

        def status_payload():
            started = phase['started']
            run_ref = changed_run_ref if phase['drift'] and drift == 'runtime' else original_run_ref
            recipe_fields = {}
            mode = 'owned_synthetic_form_invocation'
            if recipe and not (phase['drift'] and drift == 'mode'):
                mode = 'owned_synthetic_form_recipe'
                steps = [{'step_key': key, 'operation': stage['stage']}
                         for key, stage in zip(audit_fixture['symbolic_step_keys'], audit_fixture['stages'], strict=True)]
                if phase['drift'] and drift == 'steps':
                    steps[0]['step_key'] = 'changed-step-key'
                recipe_fields = {
                    'recipe_sha256': '6' * 64 if phase['drift'] and drift == 'recipe' else audit_fixture['recipe_sha256'],
                    'steps': steps}
            return {
                'available': True, 'busy': False, 'paused': False, 'reserved': False,
                'supports_approve_all': True, 'supports_learning_metadata': False,
                'kinds': ['browser_remote_form'], 'browser_display': 'desktop',
                'owned_form_invocation': {
                    'mode': mode, **recipe_fields,
                    'lifecycle': 'completed' if started else 'ready',
                    'profile_sha256': profile_sha256, 'skill_sha256': skill_sha256,
                    'invocation_sha256': invocation_sha256,
                    'run_ref': run_ref if started else None,
                },
                'remote_form': {
                    'plan_sha256': '1' * 64, 'entry_url': 'https://form.example.invalid/',
                    'submit_url': 'https://form.example.invalid/submit',
                    'receipt_url': 'https://form.example.invalid/receipt',
                    'field_name': 'message', 'field_names': ['message'],
                    'body_sha256': '2' * 64, 'state_plan_sha256': '3' * 64,
                    'state_url': 'https://form.example.invalid/state', 'mode': 'synthetic_one_post',
                },
                'jobs': ([{
                    'job_id': 'owned-job-drift' if phase['drift'] and drift == 'runtime' else 'owned-job',
                    'run_id': run_ref, 'kind': 'browser_remote_form', 'status': 'succeeded',
                    'real_model': 0, 'runtime_id': 'runtime-owned',
                }] if started else []),
                'approval': None,
            }

        with tempfile.TemporaryDirectory(prefix='owned-form-audit-drift-', dir=REPO_ROOT / 'data') as directory:
            with task_server(Path(directory), 'fixture', ('--browser-tasks',)) as (origin, token, client, server):
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(executable_path=str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'))
                try:
                    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})

                    async def route_tasks(route):
                        if route.request.method == 'POST':
                            phase['started'] = True
                            await route.fulfill(status=200, json={'accepted': True})
                        else:
                            if phase['drift']:
                                drift_tasks_read.set()
                            await route.fulfill(status=200, json=status_payload())

                    async def route_state(route):
                        response = await route.fetch()
                        payload = await response.json()
                        if phase['drift']:
                            if drift == 'runtime':
                                payload['runtime']['runtime_id'] = 'runtime-owned-drift'
                            drift_state_read.set()
                        await route.fulfill(response=response, json=payload)

                    async def route_audit(route):
                        audit_started.set()
                        await release_audit.wait()
                        await route.fulfill(status=200, json={
                            **({'mode': 'owned_synthetic_form_recipe'} if recipe else {}),
                            'available': True, 'status': 'verified', 'report': audit_fixture,
                            'report_sha256': '9' * 64,
                        })

                    await page.route('**/api/tasks', route_tasks)
                    await page.route('**/api/state', route_state)
                    await page.route('**/api/tasks/owned-form-invocation-audit', route_audit)
                    await page.goto(origin + '/ui/')
                    await page.get_by_label('Local session token', exact=True).fill(token)
                    await page.get_by_role('button', name='Sign in', exact=True).click()
                    await page.get_by_role('button', name='Tasks', exact=True).click()
                    await page.get_by_test_id('owned-invocation-opt-in').check()
                    await page.get_by_test_id('start-task').click()
                    await expect(page.get_by_test_id('owned-invocation-audit')).to_be_visible(timeout=10000)
                    await page.get_by_test_id('owned-invocation-audit').click()
                    await audit_started.wait()

                    phase['drift'] = True
                    release_audit.set()
                    await drift_tasks_read.wait()
                    await drift_state_read.wait()
                    await expect(page.get_by_test_id('owned-invocation-audit-result')).to_have_count(0)
                    await expect(page.get_by_test_id('owned-form-invocation')).to_contain_text('Invocation audit is unavailable or malformed')
                    section_text = await page.get_by_test_id('owned-form-invocation').inner_text()
                    self.assertNotIn('executable_recipe_executed' if recipe else 'invocation_execution_verified', section_text)
                finally:
                    release_audit.set()
                    await page.unroute_all(behavior='ignoreErrors')
                    await browser.close()
                    await playwright.stop()


if __name__ == '__main__':
    unittest.main()

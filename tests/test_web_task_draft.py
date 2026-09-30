import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx
import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.desktop_console import create_console
from aos.local_app import (prepare_remote_entry, prepare_remote_form,
                           prepare_remote_form_state, prepare_remote_routes,
                           prepare_remote_static_assets, private_read)
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import WebReadOnlyRoutePlan, WebTaskContract
from aos.web_https_form_transport import parse_form_fields_document
from aos.web_https_form_state_probe import WebHTTPSFormStatePlan
from aos.web_static_assets import STATIC_CONTENT_TYPES, WebStaticAssetPlan
from aos.web_readonly_data import WebReadOnlyDataBundlePlan


class WebTaskDraftTests(unittest.TestCase):
    def console_assets(self):
        directory = tempfile.TemporaryDirectory(prefix='web-task-console-assets-')
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def test_static_draft_capabilities_are_authenticated_and_match_validation(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(SimpleNamespace(session_id='synthetic', runtime=None),
                             'synthetic-token', origin, self.console_assets())

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url=origin) as client:
                endpoint = '/api/web-applications/capabilities'
                self.assertEqual((await client.get(endpoint)).status_code, 401)
                self.assertEqual((await client.post('/api/login', json={'token': 'synthetic-token'},
                    headers={'Origin': origin})).status_code, 200)
                self.assertEqual((await client.get(endpoint + '?type=image/png')).status_code, 400)
                response = await client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {
                    'schema_version': '1.1', 'mode': 'web_application_draft_capabilities',
                    'static_asset_content_types': list(STATIC_CONTENT_TYPES),
                    'static_asset_canonical_query': True})
                fixture = json.loads((REPO_ROOT / 'examples/web_application_draft_capabilities.json').read_text())
                schema = json.loads((REPO_ROOT / 'schemas/web_application_draft_capabilities.schema.json').read_text())
                self.assertTrue(fixture['synthetic'])
                self.assertEqual(response.json(), fixture['capabilities'])
                jsonschema.Draft202012Validator(schema).validate(response.json())

        asyncio.run(check())

    def test_console_form_draft_requires_exact_scope_and_private_value(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
        fixture['profile']['entry_url'] = 'https://example.com/app/'
        fixture['profile']['allowed_origins'] = ['https://example.com']
        profile = WebApplicationProfile.model_validate(fixture['profile'])
        with tempfile.TemporaryDirectory(prefix='web-form-draft-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profiles = WebApplicationProfiles(root / 'profiles')
            profile_sha256 = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=profile_sha256)
            task_root = root / 'tasks'
            plan_root = root / 'plans'
            value_root = root / 'values'
            state_root = root / 'states'
            origin = 'http://127.0.0.1:8765'
            app = create_console(SimpleNamespace(session_id='synthetic', runtime=None),
                                 'synthetic-token', origin, self.console_assets(),
                                 web_profiles_root=profiles.root, web_task_root=task_root,
                                 web_form_plan_root=plan_root, web_form_value_root=value_root,
                                 web_form_state_root=state_root)

            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                        base_url=origin, headers={'Origin': origin}) as client:
                    task_payload = {'profile_sha256': profile_sha256,
                                    'task_key': profile.task_keys[0],
                                    'verification_ref': 'synthetic-form-check'}
                    task_preview_url = '/api/web-applications/form-task-preview'
                    task_register_url = '/api/web-applications/form-task-register'
                    plan_preview_url = '/api/web-applications/form-plan-preview'
                    plan_register_url = '/api/web-applications/form-plan-register'
                    self.assertEqual((await client.post(task_preview_url, json=task_payload)).status_code, 401)
                    await client.post('/api/login', json={'token': 'synthetic-token'})
                    self.assertEqual((await client.post(task_preview_url, json=task_payload,
                        headers={'Origin': 'http://other.invalid'})).status_code, 403)
                    self.assertEqual((await client.post(task_preview_url, json={
                        **task_payload, 'task_key': 'outside'})).status_code, 422)
                    task_preview = await client.post(task_preview_url, json=task_payload)
                    self.assertEqual(task_preview.status_code, 200)
                    task_sha256 = task_preview.json()['task_sha256']
                    self.assertFalse(task_root.exists())
                    self.assertEqual((await client.post(task_register_url, json={
                        **task_payload, 'confirm_sha256': '0' * 64})).status_code, 409)
                    self.assertFalse(task_root.exists())
                    task_receipt = (await client.post(task_register_url, json={
                        **task_payload, 'confirm_sha256': task_sha256})).json()
                    task_file = REPO_ROOT / task_receipt['task_file']
                    task = WebTaskContract.model_validate_json(private_read(task_file))
                    self.assertEqual(set(task.tools), {'browser.navigate', 'browser.snapshot',
                                                       'browser.fill', 'browser.click', 'browser.verify'})
                    self.assertEqual(os.stat(task_file).st_mode & 0o777, 0o600)
                    payload = {'profile_sha256': profile_sha256, 'task_sha256': task_sha256,
                               'submit_url': 'https://example.com/app/submit',
                               'receipt_url': 'https://example.com/app/receipt',
                               'field_name': 'message', 'value': 'Synthetic hello',
                               'attest_non_secret': True}
                    self.assertEqual((await client.post(plan_preview_url, json={
                        **payload, 'attest_non_secret': False})).status_code, 400)
                    self.assertEqual((await client.post(plan_preview_url, json={
                        **payload, 'submit_url': 'https://other.example/submit'})).status_code, 422)
                    self.assertEqual((await client.post(plan_preview_url, json={
                        **payload, 'task_sha256': '0' * 64})).status_code, 422)
                    duplicate = json.dumps(payload).replace('"value":', '"value":"wrong", "value":')
                    self.assertEqual((await client.post(plan_preview_url, content=duplicate)).status_code, 400)
                    plan_preview = await client.post(plan_preview_url, json=payload)
                    self.assertEqual(plan_preview.status_code, 200)
                    self.assertNotIn('Synthetic hello', plan_preview.text)
                    self.assertTrue(plan_preview.json()['public_grant_required'])
                    plan_sha256 = plan_preview.json()['plan_sha256']
                    self.assertFalse(plan_root.exists())
                    self.assertFalse(value_root.exists())
                    self.assertEqual((await client.post(plan_register_url, json={
                        **payload, 'confirm_sha256': '0' * 64})).status_code, 409)
                    self.assertFalse(plan_root.exists())
                    self.assertFalse(value_root.exists())
                    registered = await client.post(plan_register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})
                    self.assertEqual(registered.status_code, 200)
                    self.assertNotIn('Synthetic hello', registered.text)
                    receipt = registered.json()
                    plan_file = REPO_ROOT / receipt['plan_file']
                    value_file = REPO_ROOT / receipt['value_file']
                    self.assertEqual(os.stat(plan_root).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(value_root).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(plan_file).st_mode & 0o777, 0o600)
                    self.assertEqual(os.stat(value_file).st_mode & 0o777, 0o600)
                    self.assertNotIn(b'Synthetic hello', private_read(plan_file))
                    self.assertEqual(private_read(value_file), b'Synthetic hello')
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        managed = prepare_remote_form('real', profile_sha256, task_file,
                                                      plan_file, 'message', value_file,
                                                      plan_sha256)
                    self.assertEqual(managed[1], plan_sha256)
                    self.assertEqual((await client.post(plan_register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})).status_code, 200)
                    self.assertEqual(len(list(plan_root.iterdir())), 1)
                    self.assertEqual(len(list(value_root.iterdir())), 1)
                    multi_payload = {key: payload[key] for key in (
                        'profile_sha256', 'task_sha256', 'submit_url', 'receipt_url',
                        'attest_non_secret')}
                    multi_payload['fields'] = [
                        {'name': 'subject', 'value': 'Synthetic subject'},
                        {'name': 'message', 'value': 'Synthetic hello'}]
                    self.assertEqual((await client.post(plan_preview_url, json={
                        **multi_payload, 'field_name': 'extra'})).status_code, 400)
                    self.assertEqual((await client.post(plan_preview_url, json={
                        **multi_payload, 'fields': [multi_payload['fields'][0]]})).status_code, 400)
                    self.assertEqual((await client.post(plan_preview_url, json={
                        **multi_payload, 'fields': [multi_payload['fields'][0]] * 2})).status_code, 422)
                    self.assertEqual((await client.post(plan_preview_url, json={
                        **multi_payload, 'fields': [
                            multi_payload['fields'][0],
                            {'name': 'message', 'value': 'bad\nvalue'}]})).status_code, 400)
                    before_plans = len(list(plan_root.iterdir()))
                    before_values = len(list(value_root.iterdir()))
                    multi_preview = await client.post(plan_preview_url, json=multi_payload)
                    self.assertEqual(multi_preview.status_code, 200)
                    self.assertEqual(multi_preview.json()['field_names'], ['subject', 'message'])
                    self.assertIsNone(multi_preview.json()['field_name'])
                    self.assertNotIn('Synthetic hello', multi_preview.text)
                    multi_sha256 = multi_preview.json()['plan_sha256']
                    reversed_preview = await client.post(plan_preview_url, json={
                        **multi_payload, 'fields': list(reversed(multi_payload['fields']))})
                    self.assertEqual(reversed_preview.status_code, 200)
                    self.assertNotEqual(reversed_preview.json()['plan_sha256'], multi_sha256)
                    self.assertEqual(len(list(plan_root.iterdir())), before_plans)
                    self.assertEqual(len(list(value_root.iterdir())), before_values)
                    self.assertEqual((await client.post(plan_register_url, json={
                        **multi_payload, 'confirm_sha256': '0' * 64})).status_code, 409)
                    self.assertEqual(len(list(plan_root.iterdir())), before_plans)
                    self.assertEqual(len(list(value_root.iterdir())), before_values)
                    multi_registered = await client.post(plan_register_url, json={
                        **multi_payload, 'confirm_sha256': multi_sha256})
                    self.assertEqual(multi_registered.status_code, 200)
                    self.assertNotIn('Synthetic hello', multi_registered.text)
                    multi_receipt = multi_registered.json()
                    self.assertNotIn('value_file', multi_receipt)
                    self.assertEqual(multi_receipt['field_names'], ['subject', 'message'])
                    fields_file = REPO_ROOT / multi_receipt['fields_file']
                    self.assertEqual(fields_file.suffix, '.json')
                    self.assertEqual(os.stat(fields_file).st_mode & 0o777, 0o600)
                    self.assertEqual(parse_form_fields_document(private_read(fields_file)), (
                        ('subject', 'Synthetic subject'), ('message', 'Synthetic hello')))
                    self.assertNotIn(b'Synthetic hello', private_read(
                        REPO_ROOT / multi_receipt['plan_file']))
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        managed_multi = prepare_remote_form(
                            'real', profile_sha256, task_file,
                            REPO_ROOT / multi_receipt['plan_file'], None, None,
                            multi_sha256, fields_file=fields_file)
                    self.assertEqual(managed_multi[1], multi_sha256)
                    state_task_payload = {**task_payload, 'state_readback': True}
                    state_task_preview = await client.post(task_preview_url, json=state_task_payload)
                    self.assertEqual(state_task_preview.status_code, 200)
                    self.assertTrue(state_task_preview.json()['state_readback'])
                    state_task_sha256 = state_task_preview.json()['task_sha256']
                    self.assertNotEqual(state_task_sha256, task_sha256)
                    state_task_receipt = (await client.post(task_register_url, json={
                        **state_task_payload, 'confirm_sha256': state_task_sha256})).json()
                    state_task_file = REPO_ROOT / state_task_receipt['task_file']
                    self.assertEqual(WebTaskContract.model_validate_json(
                        private_read(state_task_file)).max_actions, 6)
                    state_form_payload = {**multi_payload, 'task_sha256': state_task_sha256}
                    state_form_sha256 = (await client.post(plan_preview_url,
                        json=state_form_payload)).json()['plan_sha256']
                    state_form_receipt = (await client.post(plan_register_url, json={
                        **state_form_payload, 'confirm_sha256': state_form_sha256})).json()
                    state_payload = {'profile_sha256': profile_sha256,
                                     'task_sha256': state_task_sha256,
                                     'form_plan_sha256': state_form_sha256,
                                     'state_url': 'https://example.com/app/state',
                                     'before_sha256': '1' * 64, 'after_sha256': '2' * 64,
                                     'marker_id': 'recordStatus',
                                     'before_marker_sha256': '3' * 64,
                                     'after_marker_sha256': '4' * 64}
                    state_preview_url = '/api/web-applications/form-state-preview'
                    state_register_url = '/api/web-applications/form-state-register'
                    client.cookies.clear()
                    self.assertEqual((await client.post(state_preview_url,
                        json=state_payload)).status_code, 401)
                    await client.post('/api/login', json={'token': 'synthetic-token'})
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                            base_url=origin, headers={'Origin': 'http://untrusted.invalid'}) as foreign_client:
                        self.assertEqual((await foreign_client.post(state_preview_url,
                            json=state_payload)).status_code, 403)
                    self.assertEqual((await client.post(state_preview_url, json={
                        **state_payload, 'state_url': 'https://other.example/state'})).status_code, 422)
                    self.assertEqual((await client.post(state_preview_url, json={
                        **state_payload, 'task_sha256': task_sha256})).status_code, 422)
                    self.assertEqual((await client.post(state_preview_url, json={
                        **state_payload, 'after_sha256': state_payload['before_sha256']})).status_code, 422)
                    self.assertEqual((await client.post(state_preview_url, json={
                        **state_payload, 'marker_id': None})).status_code, 400)
                    self.assertEqual((await client.post(state_preview_url, json={
                        **state_payload, 'before_marker_sha256': '4' * 64})).status_code, 422)
                    self.assertEqual((await client.post(state_preview_url + '?x=1',
                        json=state_payload)).status_code, 400)
                    self.assertFalse(state_root.exists())
                    state_preview = await client.post(state_preview_url, json=state_payload)
                    self.assertEqual(state_preview.status_code, 200)
                    state_sha256 = state_preview.json()['state_plan_sha256']
                    self.assertFalse(state_root.exists())
                    self.assertEqual((await client.post(state_register_url, json={
                        **state_payload, 'confirm_sha256': '0' * 64})).status_code, 409)
                    self.assertFalse(state_root.exists())
                    state_registered = await client.post(state_register_url, json={
                        **state_payload, 'confirm_sha256': state_sha256})
                    self.assertEqual(state_registered.status_code, 200)
                    state_file = REPO_ROOT / state_registered.json()['state_plan_file']
                    self.assertEqual(os.stat(state_root).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(state_file).st_mode & 0o777, 0o600)
                    self.assertEqual(WebHTTPSFormStatePlan.model_validate_json(
                        private_read(state_file)).marker_id, 'recordStatus')
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        prepared_state = prepare_remote_form_state(
                            'real', profile_sha256, state_task_file,
                            REPO_ROOT / state_form_receipt['plan_file'], None, None,
                            state_form_sha256, state_file, state_sha256,
                            fields_file=REPO_ROOT / state_form_receipt['fields_file'])
                    self.assertEqual(prepared_state[1], state_sha256)
                    self.assertEqual((await client.post(state_preview_url, json={
                        **state_payload, 'submitted_field_name': 'bad-name'})).status_code, 400)
                    self.assertEqual((await client.post(state_preview_url, json={
                        **{key: value for key, value in state_payload.items()
                           if key not in {'marker_id', 'before_marker_sha256', 'after_marker_sha256'}},
                        'submitted_field_name': 'message'})).status_code, 400)
                    bound_payload = {**state_payload, 'submitted_field_name': 'message',
                                     'after_marker_sha256': digest({'text': 'Synthetic hello'})}
                    bound_preview = await client.post(state_preview_url, json=bound_payload)
                    self.assertEqual(bound_preview.status_code, 200)
                    self.assertEqual(bound_preview.json()['submitted_field_name'], 'message')
                    bound_sha256 = bound_preview.json()['state_plan_sha256']
                    bound_registered = await client.post(state_register_url, json={
                        **bound_payload, 'confirm_sha256': bound_sha256})
                    self.assertEqual(bound_registered.status_code, 200)
                    bound_file = REPO_ROOT / bound_registered.json()['state_plan_file']
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        self.assertEqual(prepare_remote_form_state(
                            'real', profile_sha256, state_task_file,
                            REPO_ROOT / state_form_receipt['plan_file'], None, None,
                            state_form_sha256, bound_file, bound_sha256,
                            fields_file=REPO_ROOT / state_form_receipt['fields_file'])[1], bound_sha256)
                    changed_payload = {**bound_payload, 'after_marker_sha256': 'f' * 64}
                    changed_preview = await client.post(state_preview_url, json=changed_payload)
                    changed_sha256 = changed_preview.json()['state_plan_sha256']
                    changed_registered = await client.post(state_register_url, json={
                        **changed_payload, 'confirm_sha256': changed_sha256})
                    changed_file = REPO_ROOT / changed_registered.json()['state_plan_file']
                    with patch('aos.local_app.WEB_PROFILES', profiles.root), self.assertRaises(ValueError):
                        prepare_remote_form_state(
                            'real', profile_sha256, state_task_file,
                            REPO_ROOT / state_form_receipt['plan_file'], None, None,
                            state_form_sha256, changed_file, changed_sha256,
                            fields_file=REPO_ROOT / state_form_receipt['fields_file'])

            asyncio.run(check())

    def test_console_readonly_data_plan_requires_exact_scope_and_hash(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
        profile = WebApplicationProfile.model_validate(fixture['profile'])
        with tempfile.TemporaryDirectory(prefix='web-readonly-draft-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profiles = WebApplicationProfiles(root / 'profiles')
            profile_sha256 = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=profile_sha256)
            tasks = root / 'task-drafts'
            plans = root / 'readonly-drafts'
            origin = 'http://127.0.0.1:8765'
            app = create_console(SimpleNamespace(session_id='synthetic', runtime=None),
                                 'synthetic-token', origin, self.console_assets(),
                                 web_profiles_root=profiles.root, web_task_root=tasks,
                                 web_readonly_data_root=plans)

            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                        base_url=origin, headers={'Origin': origin}) as client:
                    preview_url = '/api/web-applications/readonly-data-preview'
                    register_url = '/api/web-applications/readonly-data-register'
                    self.assertEqual((await client.post(preview_url, json={})).status_code, 401)
                    task_payload = {'profile_sha256': profile_sha256,
                                    'task_key': profile.task_keys[0],
                                    'verification_ref': 'synthetic-entry-check'}
                    await client.post('/api/login', json={'token': 'synthetic-token'})
                    task_sha256 = (await client.post('/api/web-applications/task-preview',
                                                     json=task_payload)).json()['task_sha256']
                    task_receipt = (await client.post('/api/web-applications/task-register',
                        json={**task_payload, 'confirm_sha256': task_sha256})).json()
                    task_path = REPO_ROOT / task_receipt['task_file']
                    payload = {'profile_sha256': profile_sha256, 'task_sha256': task_sha256,
                               'assets': [{'url': profile.allowed_origins[0] + '/assets/app.js',
                                           'content_type': 'application/javascript'}],
                               'data_resources': [{'url': profile.allowed_origins[0] + '/api/summary'}]}
                    self.assertEqual((await client.post(preview_url, json=payload,
                        headers={'Origin': 'http://other.invalid'})).status_code, 403)
                    self.assertEqual((await client.post(preview_url + '?path=x', json=payload)).status_code, 400)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'path': '/tmp/other'})).status_code, 400)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'assets': [{**payload['assets'][0], 'content_type': []}]
                    })).status_code, 400)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'data_resources': [{'url': '/relative'}]
                    })).status_code, 422)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'data_resources': payload['data_resources'] * 2
                    })).status_code, 422)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'task_sha256': '0' * 64})).status_code, 422)
                    duplicate = json.dumps(payload).replace('"data_resources":',
                                                           '"data_resources":[], "data_resources":')
                    self.assertEqual((await client.post(preview_url, content=duplicate)).status_code, 400)
                    self.assertFalse(plans.exists())
                    preview = await client.post(preview_url, json=payload)
                    self.assertEqual(preview.status_code, 200)
                    plan_sha256 = preview.json()['plan_sha256']
                    self.assertEqual((preview.json()['asset_count'], preview.json()['data_count']), (1, 1))
                    self.assertFalse(preview.json()['execution_authorized'])
                    self.assertFalse(plans.exists())
                    self.assertEqual((await client.post(register_url, json={
                        **payload, 'confirm_sha256': '0' * 64})).status_code, 409)
                    receipt = await client.post(register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})
                    self.assertEqual(receipt.status_code, 200)
                    plan_path = REPO_ROOT / receipt.json()['readonly_data_plan_file']
                    self.assertEqual(plan_path, plans / (plan_sha256 + '.json'))
                    self.assertEqual(os.stat(plans).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(plan_path).st_mode & 0o777, 0o600)
                    plan = WebReadOnlyDataBundlePlan.model_validate_json(private_read(plan_path))
                    self.assertEqual(plan.data_resources[0].url, payload['data_resources'][0]['url'])
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        managed = prepare_remote_static_assets('real', profile_sha256,
                                                               task_path, plan_path)
                    self.assertEqual(managed[1], plan_sha256)
                    self.assertEqual((await client.post(register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})).status_code, 200)
                    self.assertEqual(len(list(plans.iterdir())), 1)
                    os.chmod(plan_path, 0o644)
                    self.assertEqual((await client.post(register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})).status_code, 409)

            asyncio.run(check())

    def test_console_static_asset_plan_requires_registered_single_page_task_and_exact_hash(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
        profile = WebApplicationProfile.model_validate(fixture['profile'])
        with tempfile.TemporaryDirectory(prefix='web-static-draft-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profiles = WebApplicationProfiles(root / 'profiles')
            profile_sha256 = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=profile_sha256)
            tasks = root / 'task-drafts'
            plans = root / 'static-drafts'
            origin = 'http://127.0.0.1:8765'
            app = create_console(SimpleNamespace(session_id='synthetic', runtime=None),
                                 'synthetic-token', origin, self.console_assets(),
                                 web_profiles_root=profiles.root, web_task_root=tasks,
                                 web_static_root=plans)

            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                        base_url=origin, headers={'Origin': origin}) as client:
                    task_payload = {'profile_sha256': profile_sha256,
                                    'task_key': profile.task_keys[0],
                                    'verification_ref': 'synthetic-entry-check'}
                    task_preview = (await client.post('/api/web-applications/task-preview',
                                                      json=task_payload)).status_code
                    self.assertEqual(task_preview, 401)
                    await client.post('/api/login', json={'token': 'synthetic-token'})
                    task_sha256 = (await client.post('/api/web-applications/task-preview',
                                                     json=task_payload)).json()['task_sha256']
                    task_receipt = (await client.post('/api/web-applications/task-register', json={
                        **task_payload, 'confirm_sha256': task_sha256})).json()
                    task_path = REPO_ROOT / task_receipt['task_file']
                    payload = {'profile_sha256': profile_sha256, 'task_sha256': task_sha256,
                               'assets': [{'url': profile.allowed_origins[0] + '/assets/app.js',
                                           'content_type': 'application/javascript'},
                                          {'url': profile.allowed_origins[0] + '/assets/app.css',
                                           'content_type': 'text/css'}]}
                    preview_url = '/api/web-applications/static-assets-preview'
                    register_url = '/api/web-applications/static-assets-register'
                    self.assertEqual((await client.post(preview_url, json=payload,
                        headers={'Origin': 'http://other.invalid'})).status_code, 403)
                    self.assertEqual((await client.post(preview_url + '?path=x', json=payload)).status_code, 400)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'path': '/tmp/other'})).status_code, 400)
                    duplicate = json.dumps(payload).replace('"assets":', '"assets":[], "assets":')
                    self.assertEqual((await client.post(preview_url, content=duplicate)).status_code, 400)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'task_sha256': '0' * 64})).status_code, 422)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'assets': [{**payload['assets'][0],
                                               'url': 'https://other.example.invalid/app.js'}]
                    })).status_code, 422)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'assets': [payload['assets'][0], payload['assets'][0]]
                    })).status_code, 422)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'assets': [{'url': payload['assets'][0]['url'],
                                               'content_type': []}]
                    })).status_code, 400)
                    self.assertFalse(plans.exists())
                    preview = await client.post(preview_url, json=payload)
                    self.assertEqual(preview.status_code, 200)
                    plan_sha256 = preview.json()['plan_sha256']
                    self.assertEqual(preview.json()['asset_count'], 2)
                    self.assertFalse(preview.json()['execution_authorized'])
                    self.assertFalse(plans.exists())
                    self.assertEqual((await client.post(register_url, json={
                        **payload, 'confirm_sha256': '0' * 64})).status_code, 409)
                    self.assertFalse(plans.exists())
                    receipt = await client.post(register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})
                    self.assertEqual(receipt.status_code, 200)
                    plan_path = REPO_ROOT / receipt.json()['static_plan_file']
                    self.assertEqual(plan_path, plans / (plan_sha256 + '.json'))
                    self.assertEqual(os.stat(plans).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(plan_path).st_mode & 0o777, 0o600)
                    plan = WebStaticAssetPlan.model_validate_json(private_read(plan_path))
                    self.assertEqual([asset.url for asset in plan.assets],
                                     [asset['url'] for asset in payload['assets']])
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        managed = prepare_remote_static_assets('real', profile_sha256,
                                                               task_path, plan_path)
                    self.assertEqual(managed[1], plan_sha256)
                    self.assertEqual((await client.post(register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})).status_code, 200)
                    self.assertEqual(len(list(plans.iterdir())), 1)
                    os.chmod(plan_path, 0o644)
                    self.assertEqual((await client.post(register_url, json={
                        **payload, 'confirm_sha256': plan_sha256})).status_code, 409)
                    os.chmod(plan_path, 0o600)
                    two_page_payload = {**task_payload, 'route_count': 2}
                    two_page_hash = (await client.post('/api/web-applications/task-preview',
                                                       json=two_page_payload)).json()['task_sha256']
                    await client.post('/api/web-applications/task-register', json={
                        **two_page_payload, 'confirm_sha256': two_page_hash})
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'task_sha256': two_page_hash})).status_code, 422)

            asyncio.run(check())

    def test_console_previews_and_writes_exact_private_managed_task(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
        profile = WebApplicationProfile.model_validate(fixture['profile'])
        with tempfile.TemporaryDirectory(prefix='web-task-draft-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profiles = WebApplicationProfiles(root / 'profiles')
            checksum = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=checksum)
            tasks = root / 'task-drafts'
            route_plans = root / 'route-drafts'
            origin = 'http://127.0.0.1:8765'
            app = create_console(SimpleNamespace(session_id='synthetic', runtime=None),
                                 'synthetic-token', origin, self.console_assets(),
                                 web_profiles_root=profiles.root, web_task_root=tasks,
                                 web_route_root=route_plans)

            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                        base_url=origin, headers={'Origin': origin}) as client:
                    payload = {'profile_sha256': checksum, 'task_key': profile.task_keys[0],
                               'verification_ref': 'synthetic-entry-check'}
                    preview_url = '/api/web-applications/task-preview'
                    register_url = '/api/web-applications/task-register'
                    self.assertEqual((await client.post(preview_url, json=payload)).status_code, 401)
                    self.assertEqual((await client.post('/api/login', json={
                        'token': 'synthetic-token'})).status_code, 200)
                    self.assertEqual((await client.post(preview_url + '?path=x',
                                                        json=payload)).status_code, 400)
                    duplicate = ('{"profile_sha256":"%s","task_key":"%s",'
                                 '"task_key":"%s","verification_ref":"synthetic-entry-check"}'
                                 % (checksum, profile.task_keys[0], profile.task_keys[0]))
                    self.assertEqual((await client.post(preview_url, content=duplicate)).status_code, 400)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'task_key': 'outside'})).status_code, 422)
                    self.assertEqual((await client.post(preview_url, json={
                        **payload, 'profile_sha256': '0' * 64})).status_code, 422)
                    response = await client.post(preview_url, json=payload)
                    self.assertEqual(response.status_code, 200)
                    report = response.json()
                    self.assertEqual(report['status'], 'unregistered_draft')
                    self.assertFalse(report['execution_authorized'])
                    self.assertFalse(tasks.exists())
                    wrong = await client.post(register_url, json={**payload,
                        'confirm_sha256': '0' * 64})
                    self.assertEqual(wrong.status_code, 409)
                    self.assertFalse(tasks.exists())
                    registered = await client.post(register_url, json={**payload,
                        'confirm_sha256': report['task_sha256']})
                    self.assertEqual(registered.status_code, 200)
                    receipt = registered.json()
                    self.assertEqual(receipt['status'], 'private_unactivated_draft')
                    self.assertFalse(receipt['execution_authorized'])
                    self.assertFalse(receipt['collection_authorized'])
                    path = REPO_ROOT / receipt['task_file']
                    self.assertEqual(path, tasks / (report['task_sha256'] + '.json'))
                    self.assertEqual(os.stat(tasks).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                    task = WebTaskContract.model_validate_json(private_read(path))
                    self.assertEqual(task.profile_sha256, checksum)
                    self.assertEqual(task.allowed_origins, [profile.allowed_origins[0]])
                    self.assertEqual(task.tools, ['browser.navigate', 'browser.snapshot', 'browser.verify'])
                    self.assertFalse(task.network_access)
                    self.assertFalse(task.collection_authorized)
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        managed = prepare_remote_entry('real', checksum, path)
                    self.assertEqual(managed[1], report['task_sha256'])
                    self.assertEqual((await client.post(register_url, json={**payload,
                        'confirm_sha256': report['task_sha256']})).status_code, 200)
                    self.assertEqual(len(list(tasks.iterdir())), 1)
                    two_page = {**payload, 'route_count': 2}
                    task_preview = (await client.post(preview_url, json=two_page)).json()
                    self.assertEqual(task_preview['route_count'], 2)
                    self.assertNotEqual(task_preview['task_sha256'], report['task_sha256'])
                    task_receipt = (await client.post(register_url, json={**two_page,
                        'confirm_sha256': task_preview['task_sha256']})).json()
                    self.assertEqual(task_receipt['route_count'], 2)
                    task_path = REPO_ROOT / task_receipt['task_file']
                    route_payload = {'profile_sha256': checksum,
                                     'task_sha256': task_preview['task_sha256'],
                                     'routes': [profile.entry_url,
                                                profile.allowed_origins[0] + '/details']}
                    route_preview_url = '/api/web-applications/routes-preview'
                    route_register_url = '/api/web-applications/routes-register'
                    self.assertEqual((await client.post(route_preview_url + '?path=x',
                        json=route_payload)).status_code, 400)
                    self.assertEqual((await client.post(route_preview_url, json={
                        **route_payload, 'routes': list(reversed(route_payload['routes']))
                    })).status_code, 422)
                    self.assertEqual((await client.post(route_preview_url, json={
                        **route_payload, 'routes': [profile.entry_url,
                            'https://other.example.invalid/details']
                    })).status_code, 422)
                    self.assertEqual((await client.post(route_preview_url, json={
                        **route_payload, 'task_sha256': '0' * 64
                    })).status_code, 422)
                    self.assertFalse(route_plans.exists())
                    route_preview_response = await client.post(route_preview_url, json=route_payload)
                    self.assertEqual(route_preview_response.status_code, 200)
                    plan_hash = route_preview_response.json()['plan_sha256']
                    self.assertFalse(route_plans.exists())
                    self.assertEqual((await client.post(route_register_url, json={
                        **route_payload, 'confirm_sha256': '0' * 64})).status_code, 409)
                    self.assertFalse(route_plans.exists())
                    route_receipt_response = await client.post(route_register_url, json={
                        **route_payload, 'confirm_sha256': plan_hash})
                    self.assertEqual(route_receipt_response.status_code, 200)
                    route_receipt = route_receipt_response.json()
                    plan_path = REPO_ROOT / route_receipt['route_plan_file']
                    self.assertEqual(plan_path, route_plans / (plan_hash + '.json'))
                    self.assertEqual(os.stat(route_plans).st_mode & 0o777, 0o700)
                    self.assertEqual(os.stat(plan_path).st_mode & 0o777, 0o600)
                    plan = WebReadOnlyRoutePlan.model_validate_json(private_read(plan_path))
                    self.assertEqual(plan.routes, route_payload['routes'])
                    self.assertFalse(plan.execution_authorized)
                    with patch('aos.local_app.WEB_PROFILES', profiles.root):
                        managed_routes = prepare_remote_routes('real', checksum, task_path,
                                                               plan_path)
                    self.assertEqual(managed_routes[1], plan_hash)
                    self.assertEqual((await client.post(route_register_url, json={
                        **route_payload, 'confirm_sha256': plan_hash})).status_code, 200)
                    os.chmod(plan_path, 0o644)
                    self.assertEqual((await client.post(route_register_url, json={
                        **route_payload, 'confirm_sha256': plan_hash})).status_code, 409)
                    os.chmod(path, 0o644)
                    self.assertEqual((await client.post(register_url, json={**payload,
                        'confirm_sha256': report['task_sha256']})).status_code, 409)

            asyncio.run(check())


if __name__ == '__main__':
    unittest.main()

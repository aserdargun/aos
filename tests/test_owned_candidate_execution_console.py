import asyncio
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop_console import create_console


class OwnedCandidateExecutionConsoleTests(unittest.TestCase):
    def setUp(self):
        self.origin = 'http://127.0.0.1:19049'
        self.scheduler = Mock()
        assets = tempfile.TemporaryDirectory(prefix='owned-candidate-console-assets-')
        self.addCleanup(assets.cleanup)
        self.app = create_console(object(), 'synthetic-token', self.origin,
                                  Path(assets.name), scheduler=self.scheduler)
        self.selection = {
            'schema_version': '1.0', 'candidate_sha256': 'c' * 64,
            'source_run_ref': '4' * 64, 'source_invocation_sha256': 'a' * 64,
            'case_key': 'dev-next', 'development_value': 'beta',
        }
        recipe = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe.json').read_text())['recipe']
        self.preview = {key: value for key, value in self.selection.items() if key != 'development_value'}
        self.preview.update({
            'available': True, 'status': 'preview', 'source_group_sha256': 'f' * 64,
            'profile_sha256': 'e' * 64, 'skill_sha256': 'b' * 64,
            'parameter_variant_sha256': '7' * 64, 'form_plan_sha256': '2' * 64,
            'state_plan_sha256': '3' * 64, 'invocation_sha256': '8' * 64,
            'recipe_sha256': 'd' * 64, 'steps': recipe['steps'],
            'purpose': 'development_variation', 'independent_held_out': False, 'report': None,
        })
        self.preview['preview_sha256'] = digest(self.preview)
        self.scheduler.preview_owned_form_candidate_execution.return_value = self.preview
        self.started = {'schema_version': '1.0', 'accepted': True, 'lifecycle': 'running',
                        'candidate_execution_sha256': '6' * 64, 'job_id': 'synthetic-job', 'run_id': None,
                        'candidate_sha256': self.selection['candidate_sha256'],
                        'case_key': self.selection['case_key'], 'invocation_sha256': self.preview['invocation_sha256']}
        self.scheduler.start_owned_form_candidate_execution.return_value = self.started
        self.prefix = '/api/tasks/owned-form-candidate/execution-'

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin,
                                 headers={'Origin': self.origin})

    def test_authentication_strict_selection_and_explicit_start(self):
        async def check():
            async with self.client() as client:
                self.assertEqual((await client.post(self.prefix + 'preview', json=self.selection)).status_code, 401)
                await client.post('/api/login', json={'token': 'synthetic-token'})
                response = await client.post(self.prefix + 'preview', json=self.selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), self.preview)
                self.scheduler.start_owned_form_candidate_execution.assert_not_called()
                confirmed = {**self.selection, 'preview_sha256': self.preview['preview_sha256'],
                             'confirm_sha256': self.preview['preview_sha256'], 'lease_id': 'lease-synthetic', 'generation': 1}
                response = await client.post(self.prefix + 'start', json=confirmed)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), self.started)
                self.scheduler.start_owned_form_candidate_execution.assert_called_once_with(
                    candidate_sha256=self.selection['candidate_sha256'], source_run_ref=self.selection['source_run_ref'],
                    invocation_sha256=self.selection['source_invocation_sha256'], case_key='dev-next',
                    development_value='beta', preview_sha256=self.preview['preview_sha256'],
                    confirm_sha256=self.preview['preview_sha256'], lease_id='lease-synthetic', generation=1)
                for extra in ({'path': '/private'}, {'url': 'https://example.invalid'}, {'purpose': 'test'}):
                    response = await client.post(self.prefix + 'preview', json={**self.selection, **extra})
                    self.assertEqual(response.status_code, 400)
                for change in ({'development_value': ' beta'}, {'development_value': 'x' * 129},
                               {'case_key': 'test_case'}, {'candidate_sha256': 'wrong'}):
                    response = await client.post(self.prefix + 'preview', json={**self.selection, **change})
                    self.assertEqual(response.status_code, 422)
                duplicate = canonical(self.selection)[:-1] + ',"case_key":"other"}'
                self.assertEqual((await client.post(self.prefix + 'preview', content=duplicate,
                    headers={'Content-Type': 'application/json'})).status_code, 400)
                self.assertEqual((await client.post(self.prefix + 'preview?run_id=other', json=self.selection)).status_code, 400)
                self.assertEqual((await client.post(self.prefix + 'start', json={**confirmed, 'generation': True})).status_code, 422)

        asyncio.run(check())

    def test_scheduler_checks_stay_on_owner_thread_and_malformed_responses_fail_closed(self):
        owner_thread = threading.get_ident()

        def preview(*_arguments):
            self.assertEqual(threading.get_ident(), owner_thread)
            return self.preview

        self.scheduler.preview_owned_form_candidate_execution.side_effect = preview

        async def check():
            async with self.client() as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                self.assertEqual((await client.post(self.prefix + 'preview', json=self.selection)).status_code, 200)
                baseline = copy.deepcopy(self.preview)
                for change in ({'independent_held_out': True}, {'source_run_ref': '0' * 64},
                               {'development_value': 'beta'}, {'preview_sha256': '0' * 64}, {'steps': []}):
                    self.preview = {**baseline, **change}
                    response = await client.post(self.prefix + 'preview', json=self.selection)
                    self.assertEqual(response.status_code, 409, response.text)
                self.scheduler.start_owned_form_candidate_execution.assert_not_called()

        asyncio.run(check())

    def test_audit_report_is_bound_to_exact_execution_and_never_claims_training(self):
        report = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_candidate_execution.json').read_text())['report']
        run_id = 'synthetic-execution-run'
        report['execution_run_ref'] = digest({'run_id': run_id})
        result = {
            'schema_version': '1.0', 'available': True, 'mode': 'owned_candidate_development', 'status': 'verified',
            'candidate_execution_sha256': '6' * 64, 'source_invocation_sha256': 'a' * 64,
            'case_key': 'dev-next', 'run_id': run_id, 'run_ref': report['execution_run_ref'],
            'report_sha256': digest(report), 'report': report,
            **{key: report['admission'][key] for key in ('candidate_sha256', 'source_run_ref',
                'source_group_sha256', 'profile_sha256', 'skill_sha256', 'parameter_variant_sha256',
                'invocation_sha256', 'recipe_sha256')},
        }
        self.scheduler.audit_owned_form_candidate_execution.return_value = result

        async def check():
            async with self.client() as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                selection = {'candidate_execution_sha256': '6' * 64}
                response = await client.post(self.prefix + 'audit', json=selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), result)
                for key in ('candidate_execution_sha256', 'report_sha256', 'run_ref', 'candidate_sha256'):
                    self.scheduler.audit_owned_form_candidate_execution.return_value = {**result, key: '0' * 64}
                    self.assertEqual((await client.post(self.prefix + 'audit', json=selection)).status_code, 409)
                changed = copy.deepcopy(result)
                changed['report']['training_ready'] = True
                changed['report_sha256'] = digest(changed['report'])
                self.scheduler.audit_owned_form_candidate_execution.return_value = changed
                self.assertEqual((await client.post(self.prefix + 'audit', json=selection)).status_code, 409)
                self.scheduler.audit_owned_form_candidate_execution.return_value = {
                    **result, 'available': False, 'status': 'unavailable'}
                unavailable = (await client.post(self.prefix + 'audit', json=selection)).json()
                self.assertIsNone(unavailable['report'])
                self.assertIsNone(unavailable['report_sha256'])
                reviewed = {**result, 'schema_version': '1.1', 'review_sha256': 'b' * 64,
                            'review_admission_verified': True, 'review_status': 'revoked'}
                self.scheduler.audit_owned_form_candidate_execution.return_value = reviewed
                response = await client.post(self.prefix + 'audit', json=selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['review_status'], 'revoked')
                for change in ({'review_admission_verified': False}, {'review_status': 'active'},
                               {'review_sha256': None}, {'schema_version': '1.0'}):
                    self.scheduler.audit_owned_form_candidate_execution.return_value = {**reviewed, **change}
                    self.assertEqual((await client.post(self.prefix + 'audit', json=selection)).status_code, 409)
                adapted = reviewed | {'schema_version': '1.5', 'release_sha256': 'c' * 64,
                    'selection_sha256': 'd' * 64, 'family_sha256': 'e' * 64, 'release_admission_verified': True,
                    'selection_status': 'current', 'reuse_admission_sha256': 'f' * 64,
                    'reuse_admission_verified': True, 'adapter_admission_sha256': '1' * 64,
                    'adapter_admission_verified': True, 'verification_scope': 'historical_execution',
                    'current_source_status': 'unchecked', 'runtime_reuse_authorized': False}
                self.scheduler.audit_owned_form_candidate_execution.return_value = adapted
                response = await client.post(self.prefix + 'audit', json=selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), adapted)
                for change in ({'adapter_admission_verified': False}, {'current_source_status': 'available'},
                               {'runtime_reuse_authorized': True}, {'schema_version': '1.3'}):
                    self.scheduler.audit_owned_form_candidate_execution.return_value = adapted | change
                    self.assertEqual((await client.post(self.prefix + 'audit', json=selection)).status_code, 409)
                unavailable_adapter = adapted | {'available': False, 'status': 'unavailable',
                    'report': None, 'report_sha256': None, 'review_admission_verified': False,
                    'release_admission_verified': False, 'reuse_admission_verified': False,
                    'adapter_admission_verified': False, 'selection_status': 'unavailable'}
                self.scheduler.audit_owned_form_candidate_execution.return_value = unavailable_adapter
                response = await client.post(self.prefix + 'audit', json=selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['schema_version'], '1.5')
                self.assertFalse(response.json()['adapter_admission_verified'])

        asyncio.run(check())

    def test_review_pin_requires_versioned_preview_and_cannot_downgrade(self):
        selection = {**self.selection, 'schema_version': '1.1', 'review_sha256': '9' * 64}
        preview = {**self.preview, 'schema_version': '1.1', 'review_sha256': '9' * 64}
        preview['preview_sha256'] = digest({key: value for key, value in preview.items() if key != 'preview_sha256'})
        self.scheduler.preview_owned_form_candidate_execution.return_value = preview

        async def check():
            async with self.client() as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                response = await client.post(self.prefix + 'preview', json=selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.scheduler.preview_owned_form_candidate_execution.assert_called_once_with(
                    selection['candidate_sha256'], selection['source_run_ref'], selection['source_invocation_sha256'],
                    selection['case_key'], selection['development_value'], review_sha256='9' * 64)
                for change in ({'schema_version': '1.0'}, {'review_sha256': None}):
                    self.assertIn((await client.post(self.prefix + 'preview', json={**selection, **change})).status_code, (400, 422))
                for changed in (self.preview, {**preview, 'review_sha256': '8' * 64}):
                    self.scheduler.preview_owned_form_candidate_execution.return_value = changed
                    self.assertEqual((await client.post(self.prefix + 'preview', json=selection)).status_code, 409)
                started = {**self.started, 'schema_version': '1.1', 'review_sha256': '9' * 64}
                self.scheduler.start_owned_form_candidate_execution.return_value = started
                confirmed = {**selection, 'preview_sha256': preview['preview_sha256'],
                    'confirm_sha256': preview['preview_sha256'], 'lease_id': 'lease-synthetic', 'generation': 1}
                self.assertEqual((await client.post(self.prefix + 'start', json=confirmed)).status_code, 200)
                self.assertEqual(self.scheduler.start_owned_form_candidate_execution.call_args.kwargs['review_sha256'], '9' * 64)
                self.scheduler.start_owned_form_candidate_execution.return_value = self.started
                self.assertEqual((await client.post(self.prefix + 'start', json=confirmed)).status_code, 409)

        asyncio.run(check())

    def test_review_receipt_authentication_exact_confirmation_and_response_pins(self):
        selection = {key: self.selection[key] for key in ('candidate_sha256', 'source_run_ref', 'source_invocation_sha256')}
        selection['candidate_execution_sha256'] = '6' * 64
        summary = {'skill_key': 'save_record', 'expected_outcome_key': 'record-saved',
                   'parameter_key': 'record-query', 'form_field_name': '_message', 'steps': self.preview['steps']}
        receipt = {**selection, 'schema_version': '1.0', 'synthetic': True, 'decision': 'accept',
                   'purpose': 'development_review', 'reviewer': 'local_authenticated_user',
                   'summary': summary, 'source_group_sha256': '1' * 64, 'source_fingerprint_sha256': '2' * 64,
                   'execution_run_ref': '3' * 64, 'invocation_sha256': '4' * 64, 'recipe_sha256': '5' * 64,
                   'skill_sha256': '7' * 64, 'profile_sha256': '8' * 64, 'case_key': 'dev-beta',
                   'parameter_variant_sha256': '9' * 64, 'activation_authorized': False,
                   'training_ready': False, 'independent_held_out': False}
        preview = {'schema_version': '1.0', 'available': True, 'status': 'preview',
                   'review_sha256': digest(receipt), 'receipt': receipt, 'summary': summary, 'revocation_sha256': None}
        accepted = {**preview, 'status': 'accepted'}
        revoked = {**preview, 'status': 'revoked', 'revocation_sha256': 'f' * 64}
        self.scheduler.preview_owned_candidate_review.return_value = preview
        self.scheduler.accept_owned_candidate_review.return_value = accepted
        self.scheduler.inspect_owned_candidate_review.return_value = accepted
        self.scheduler.revoke_owned_candidate_review.return_value = revoked
        prefix = '/api/tasks/owned-form-candidate/review-'
        confirmation = {'review_sha256': preview['review_sha256'], 'confirm_sha256': preview['review_sha256']}

        async def check():
            async with self.client() as client:
                self.assertEqual((await client.post(prefix + 'preview', json=selection)).status_code, 401)
                await client.post('/api/login', json={'token': 'synthetic-token'})
                response = await client.post(prefix + 'preview', json=selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.scheduler.accept_owned_candidate_review.assert_not_called()
                self.assertEqual((await client.post(prefix + 'accept', json={**selection, **confirmation, 'confirm_sha256': '0' * 64})).status_code, 409)
                self.scheduler.accept_owned_candidate_review.assert_not_called()
                self.assertEqual((await client.post(prefix + 'accept', json={**selection, **confirmation})).status_code, 200)
                self.scheduler.accept_owned_candidate_review.assert_called_once_with(**selection, **confirmation)
                self.assertEqual((await client.post(prefix + 'inspect', json={'review_sha256': preview['review_sha256']})).status_code, 200)
                self.assertEqual((await client.post(prefix + 'revoke', json=confirmation)).status_code, 200)
                for extra in ({'reviewer': 'admin'}, {'path': '/private'}, {'activation_authorized': True}):
                    self.assertEqual((await client.post(prefix + 'preview', json={**selection, **extra})).status_code, 400)
                self.assertEqual((await client.post(prefix + 'preview?source=other', json=selection)).status_code, 400)
                duplicate = canonical(selection)[:-1] + ',"candidate_sha256":"' + 'a' * 64 + '"}'
                self.assertEqual((await client.post(prefix + 'preview', content=duplicate, headers={'Content-Type': 'application/json'})).status_code, 400)
                for changed in ({**preview, 'review_sha256': '0' * 64},
                                {**preview, 'summary': {**summary, 'skill_key': 'other'}},
                                {**preview, 'receipt': {**receipt, 'training_ready': True}}):
                    self.scheduler.preview_owned_candidate_review.return_value = changed
                    self.assertEqual((await client.post(prefix + 'preview', json=selection)).status_code, 409)

        asyncio.run(check())

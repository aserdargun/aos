import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop_console import create_console


class OwnedSkillReleaseConsoleTests(unittest.TestCase):
    def setUp(self):
        self.origin = 'http://127.0.0.1:19050'
        self.scheduler = Mock()
        assets = tempfile.TemporaryDirectory(prefix='owned-skill-release-console-assets-')
        self.addCleanup(assets.cleanup)
        self.app = create_console(object(), 'synthetic-token', self.origin,
                                  Path(assets.name), scheduler=self.scheduler)
        example = json.loads((REPO_ROOT / 'examples/owned_skill_release.json').read_text())
        self.release = copy.deepcopy(example['release'])
        self.release['family_sha256'] = digest(self.release['family'])
        self.release_hash = digest(self.release)
        self.review_hash = self.release['review_sha256']
        self.family = self.release['family']
        self.family_hash = self.release['family_sha256']
        self.release_response = {
            'schema_version': '1.0', 'available': True, 'status': 'preview',
            'persisted': False, 'release_sha256': self.release_hash, 'release': self.release,
        }
        self.selection_event = copy.deepcopy(example['selection'])
        self.selection_event.update(family_sha256=self.family_hash,
            previous_selection_sha256=None, previous_release_sha256=None,
            release_sha256=self.release_hash, rollback_evidence_execution_sha256=None)
        self.selection_hash = digest(self.selection_event)
        self.selection_response = {
            'schema_version': '1.0', 'available': True, 'status': 'preview', 'persisted': False,
            'selection_sha256': self.selection_hash, 'selection': self.selection_event,
            'release_sha256': self.release_hash, 'review_sha256': self.review_hash,
            'family_sha256': self.family_hash,
        }

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin,
                                 headers={'Origin': self.origin})

    def test_release_and_selection_require_authentication_exact_confirmation_and_no_query_scope(self):
        async def check():
            async with self.client() as client:
                catalog_path = '/api/tasks/owned-form-candidate/release-catalog'
                preview_path = '/api/tasks/owned-form-candidate/release-preview'
                publish_path = '/api/tasks/owned-form-candidate/release-publish'
                selection_path = '/api/tasks/owned-form-candidate/selection-preview'
                commit_path = '/api/tasks/owned-form-candidate/selection-commit'
                self.assertEqual((await client.get(catalog_path)).status_code, 401)
                await client.post('/api/login', json={'token': 'synthetic-token'})

                family_record = {'family_sha256': self.family_hash, 'family': self.family,
                    'selection_sha256': None, 'selected_release_sha256': None, 'sequence': 0,
                    'releases': [{'release_sha256': self.release_hash, 'release': self.release,
                                  'review_status': 'accepted', 'rollback_eligible': False}]}
                catalog = {'schema_version': '1.0', 'available': True, 'families': [family_record]}
                self.scheduler.catalog_owned_skill_releases.return_value = catalog
                self.assertEqual((await client.get(catalog_path + '?family_sha256=other')).status_code, 400)
                self.assertEqual((await client.request('GET', catalog_path, content=b'{}')).status_code, 400)
                self.assertEqual((await client.get(catalog_path)).json(), catalog)

                preview_request = {'schema_version': '1.0', 'review_sha256': self.review_hash,
                                   'parent_release_sha256': None}
                self.scheduler.preview_owned_skill_release.return_value = self.release_response
                response = await client.post(preview_path, json=preview_request)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), self.release_response)
                self.scheduler.preview_owned_skill_release.assert_called_once_with(
                    review_sha256=self.review_hash, parent_release_sha256=None)
                self.scheduler.publish_owned_skill_release.return_value = {
                    **self.release_response, 'status': 'published', 'persisted': True}
                bad_publish = await client.post(publish_path, json={**preview_request,
                    'confirm_sha256': '0' * 64})
                self.assertEqual(bad_publish.status_code, 409, bad_publish.text)
                self.assertEqual((await client.post(publish_path, json={**preview_request,
                    'confirm_sha256': self.release_hash})).status_code, 200)
                self.assertEqual(self.scheduler.publish_owned_skill_release.call_count, 2)
                self.scheduler.publish_owned_skill_release.assert_called_with(
                    review_sha256=self.review_hash, parent_release_sha256=None,
                    confirm_sha256=self.release_hash)

                selection_request = {'schema_version': '1.0', 'release_sha256': self.release_hash,
                    'expected_selection_sha256': None, 'operation': 'select'}
                self.scheduler.preview_owned_skill_selection.return_value = self.selection_response
                response = await client.post(selection_path, json=selection_request)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), self.selection_response)
                self.scheduler.preview_owned_skill_selection.assert_called_once_with(
                    release_sha256=self.release_hash, expected_selection_sha256=None, operation='select')
                self.scheduler.commit_owned_skill_selection.return_value = {
                    **self.selection_response, 'status': 'selected', 'persisted': True}
                self.assertEqual((await client.post(commit_path, json={**selection_request,
                    'confirm_sha256': '0' * 64})).status_code, 409)
                self.assertEqual((await client.post(commit_path, json={**selection_request,
                    'confirm_sha256': self.selection_hash})).status_code, 200)
                self.assertEqual(self.scheduler.commit_owned_skill_selection.call_count, 2)
                self.scheduler.commit_owned_skill_selection.assert_called_with(
                    release_sha256=self.release_hash, expected_selection_sha256=None,
                    operation='select', confirm_sha256=self.selection_hash)

                extra_cases = (
                    (preview_path, preview_request, {'family': self.family}),
                    (selection_path, selection_request, {'activation_authorized': True}),
                )
                for path, request, extra in extra_cases:
                    self.assertEqual((await client.post(path, json={**request, **extra})).status_code, 400)
                duplicate = canonical(preview_request)[:-1] + ',"schema_version":"1.0"}'
                self.assertEqual((await client.post(preview_path, content=duplicate,
                    headers={'Content-Type': 'application/json'})).status_code, 400)
                self.assertEqual((await client.post(selection_path + '?release_sha256=x',
                    json=selection_request)).status_code, 400)
                self.assertEqual((await client.post(selection_path, json={**selection_request,
                    'operation': 'activate'})).status_code, 422)
                self.assertEqual((await client.post(selection_path, json={**selection_request,
                    'expected_selection_sha256': 'bad'})).status_code, 422)

        asyncio.run(check())

    def test_catalog_and_operation_responses_reject_false_claims_and_hash_mismatches(self):
        async def check():
            async with self.client() as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                catalog_path = '/api/tasks/owned-form-candidate/release-catalog'
                item = {'release_sha256': self.release_hash, 'release': self.release,
                        'review_status': 'accepted', 'rollback_eligible': False}
                family = {'family_sha256': self.family_hash, 'family': self.family,
                    'selection_sha256': None, 'selected_release_sha256': None, 'sequence': 0,
                    'releases': [item]}
                self.scheduler.catalog_owned_skill_releases.return_value = {
                    'schema_version': '1.0', 'available': True, 'families': [family]}
                self.assertEqual((await client.get(catalog_path)).status_code, 200)
                for mutation in ('release_hash', 'false_claim', 'duplicate_family'):
                    altered = copy.deepcopy(family)
                    if mutation == 'release_hash':
                        altered['releases'][0]['release_sha256'] = '0' * 64
                    elif mutation == 'false_claim':
                        altered['releases'][0]['release']['training_ready'] = True
                    else:
                        self.scheduler.catalog_owned_skill_releases.return_value['families'].append(copy.deepcopy(family))
                    if mutation != 'duplicate_family':
                        self.scheduler.catalog_owned_skill_releases.return_value['families'] = [altered]
                    self.assertEqual((await client.get(catalog_path)).status_code, 409, mutation)
                self.scheduler.catalog_owned_skill_releases.return_value = {
                    'schema_version': '1.0', 'available': True, 'families': [family]}

                self.scheduler.preview_owned_skill_release.return_value = {
                    **self.release_response, 'release_sha256': '0' * 64}
                response = await client.post('/api/tasks/owned-form-candidate/release-preview', json={
                    'schema_version': '1.0', 'review_sha256': self.review_hash,
                    'parent_release_sha256': None})
                self.assertEqual(response.status_code, 409)

                forged = copy.deepcopy(self.selection_response)
                forged['selection']['activation_authorized'] = True
                forged['selection_sha256'] = digest(forged['selection'])
                self.scheduler.preview_owned_skill_selection.return_value = forged
                response = await client.post('/api/tasks/owned-form-candidate/selection-preview', json={
                    'schema_version': '1.0', 'release_sha256': self.release_hash,
                    'expected_selection_sha256': None, 'operation': 'select'})
                self.assertEqual(response.status_code, 409)

        asyncio.run(check())

    def test_release_bound_execution_requires_all_schema_12_pins_for_preview_and_start(self):
        recipe = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe.json').read_text())['recipe']
        steps = recipe['steps']
        selection = {'schema_version': '1.2', 'candidate_sha256': 'c' * 64,
            'source_run_ref': '4' * 64, 'source_invocation_sha256': 'a' * 64,
            'case_key': 'dev-next', 'development_value': 'beta', 'review_sha256': self.review_hash,
            'release_sha256': self.release_hash, 'selection_sha256': '3' * 64}
        preview = {'schema_version': '1.2', 'available': True, 'status': 'preview',
            'candidate_sha256': selection['candidate_sha256'], 'source_run_ref': selection['source_run_ref'],
            'source_invocation_sha256': selection['source_invocation_sha256'], 'case_key': selection['case_key'],
            'source_group_sha256': 'f' * 64, 'profile_sha256': 'e' * 64, 'skill_sha256': 'b' * 64,
            'parameter_variant_sha256': '7' * 64, 'form_plan_sha256': '2' * 64,
            'state_plan_sha256': '5' * 64, 'invocation_sha256': '8' * 64,
            'recipe_sha256': 'd' * 64, 'steps': steps,
            'purpose': 'development_variation', 'independent_held_out': False, 'report': None,
            'review_sha256': selection['review_sha256'], 'release_sha256': selection['release_sha256'],
            'selection_sha256': selection['selection_sha256']}
        preview['preview_sha256'] = digest(preview)
        started = {'schema_version': '1.2', 'accepted': True, 'lifecycle': 'running',
            'candidate_execution_sha256': '6' * 64, 'job_id': 'synthetic-job', 'run_id': None,
            'candidate_sha256': selection['candidate_sha256'], 'case_key': selection['case_key'],
            'invocation_sha256': preview['invocation_sha256'], 'review_sha256': selection['review_sha256'],
            'release_sha256': selection['release_sha256'], 'selection_sha256': selection['selection_sha256']}
        self.scheduler.preview_owned_form_candidate_execution.return_value = preview
        self.scheduler.start_owned_form_candidate_execution.return_value = started

        async def check():
            async with self.client() as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                path = '/api/tasks/owned-form-candidate/execution-'
                response = await client.post(path + 'preview', json=selection)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), preview)
                self.scheduler.preview_owned_form_candidate_execution.assert_called_once_with(
                    selection['candidate_sha256'], selection['source_run_ref'],
                    selection['source_invocation_sha256'], selection['case_key'],
                    selection['development_value'], review_sha256=selection['review_sha256'],
                    release_sha256=selection['release_sha256'], selection_sha256=selection['selection_sha256'])

                control = {'preview_sha256': preview['preview_sha256'],
                    'confirm_sha256': preview['preview_sha256'], 'lease_id': 'lease-synthetic', 'generation': 1}
                response = await client.post(path + 'start', json={**selection, **control})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), started)
                self.scheduler.start_owned_form_candidate_execution.assert_called_once_with(
                    candidate_sha256=selection['candidate_sha256'], source_run_ref=selection['source_run_ref'],
                    invocation_sha256=selection['source_invocation_sha256'], case_key=selection['case_key'],
                    development_value=selection['development_value'], preview_sha256=preview['preview_sha256'],
                    confirm_sha256=preview['preview_sha256'], lease_id='lease-synthetic', generation=1,
                    review_sha256=selection['review_sha256'], release_sha256=selection['release_sha256'],
                    selection_sha256=selection['selection_sha256'])

                downgrade = {**selection, 'schema_version': '1.1'}
                self.assertEqual((await client.post(path + 'preview', json=downgrade)).status_code, 400)
                missing_pin = {key: value for key, value in selection.items() if key != 'selection_sha256'}
                self.assertEqual((await client.post(path + 'preview', json=missing_pin)).status_code, 400)
                extra_family = {**selection, 'family_sha256': '9' * 64}
                self.assertEqual((await client.post(path + 'preview', json=extra_family)).status_code, 400)

        asyncio.run(check())

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import ValidationError

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.owned_form_candidate_execution import (
    _assert_planning_execution_pins, assert_planning_execution_binding,
    load_candidate_execution_bundle, persist_candidate_execution_bundle,
)
from aos.owned_skill_planning import validate_planning_bundle
from aos.owned_skill_planner import BonsaiOwnedSkillPlanner
from aos.site_skill_form_recipe_candidate_execution import CandidateExecutionAdmission
from aos.web_https_form_transport import exact_form_fields, form_body


class ValueBox:
    def __init__(self, value):
        self.value = value

    def model_dump(self, mode='json'):
        return self.value


def synthetic_native_contract_bundle(case_key, value, steps, evidence, lease_id, generation):
    bundle = json.loads(
        (REPO_ROOT / 'examples/owned_skill_planning_bundle.json').read_text())['bundle']
    bundle['real_model'] = True
    pins = bundle['model_pins']
    pins['owned_skill_plan_protocol'] = 'aos-owned-skill-plan-v2'
    bundle['deployment'] = {
        'real_model': True, 'kind': 'bonsai_native_owned_skill_planner',
        'deployment_id': 'bonsai-' + digest(pins), 'pins': copy.deepcopy(pins),
    }
    request = bundle['request']
    request.update({'case_key': case_key, 'lease_id': lease_id,
                    'generation': generation})
    bundle['authority'].update({'lease_id': lease_id, 'generation': generation})
    bundle['evidence'] = copy.deepcopy(evidence)
    bundle['model_response'].update({
        'decision': 'invoke_selected_skill', 'case_key': case_key,
        'parameter_value': value, 'steps': copy.deepcopy(steps),
        'evidence_refs': ['admitted-skill'], 'reason_code': 'skill_match',
        'execution_authorized': False, 'activation_authorized': False,
        'training_ready': False,
    })
    planner = BonsaiOwnedSkillPlanner.__new__(BonsaiOwnedSkillPlanner)
    planner.identity, planner.pins = bundle['deployment'], pins
    bundle['model_request'] = planner.request_body(request['goal'], bundle['evidence'])
    validate_planning_bundle(bundle, planner=None)
    return bundle


class OwnedSkillPlannedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.example = json.loads(
            (REPO_ROOT / 'examples/owned_candidate_planned_execution_bundle.json').read_text())
        self.bundle = json.loads(
            (REPO_ROOT / 'examples/owned_skill_planning_bundle.json').read_text())['bundle']
        self.bundle = copy.deepcopy(self.bundle)
        self.hash_pin = 'a' * 64
        self.case_key = self.bundle['model_response']['case_key']
        self.development_value = self.bundle['model_response']['parameter_value']
        self.steps = self.bundle['model_response']['steps']
        self.candidate = {
            'source_run_ref': self.hash_pin,
            'source_group_sha256': self.hash_pin,
            'source_context': {'manifest_sha256': self.hash_pin,
                               'invocation_sha256': self.hash_pin},
            'source_fingerprint_sha256': self.hash_pin,
            'skill': {'skill_key': 'save_record'},
            'field_bindings': [{'parameter_key': 'record-query',
                                'form_field_name': 'message'}],
            'recipe': {'skill_sha256': digest({'skill_key': 'save_record'}),
                       'outcome': {'parameter_key': 'record-query',
                                   'form_field_name': 'message'},
                       'steps': self.steps},
        }
        candidate_sha256 = digest(self.candidate)
        recipe_sha256 = digest(self.candidate['recipe'])
        self.preview = {
            'source_manifest_sha256': self.hash_pin,
            'source_run_ref': self.hash_pin,
            'source_invocation_sha256': self.hash_pin,
            'source_fingerprint_sha256': self.hash_pin,
            'source_group_sha256': self.hash_pin,
            'family_sha256': self.hash_pin,
            'release_sha256': self.hash_pin,
            'selection_sha256': self.hash_pin,
            'review_sha256': self.hash_pin,
            'candidate_sha256': candidate_sha256,
            'recipe_sha256': recipe_sha256,
        }
        self.reuse_admission = {
            'manager_session': 'synthetic-manager',
            'desktop_session_id': 'synthetic-desktop',
            'runtime_id': 'synthetic-runtime',
            'lease_id': 'synthetic-lease',
            'generation': 0,
            'preview': self.preview,
        }
        self.manifest = {
            'schema_version': '1.4',
            'planning_bundle_sha256': digest(self.bundle),
            'reuse_admission_sha256': digest(self.reuse_admission),
            'source_run_ref': self.hash_pin,
            'source_invocation_sha256': self.hash_pin,
            'source_group_sha256': self.hash_pin,
            'candidate_sha256': candidate_sha256,
            'recipe_sha256': recipe_sha256,
            'release_sha256': self.hash_pin,
            'selection_sha256': self.hash_pin,
            'review_sha256': self.hash_pin,
        }
        authority = self.bundle['authority']
        authority.update({
            'manager_session': self.reuse_admission['manager_session'],
            'desktop_session_id': self.reuse_admission['desktop_session_id'],
            'runtime_id': self.reuse_admission['runtime_id'],
            'lease_id': self.reuse_admission['lease_id'],
            'generation': self.reuse_admission['generation'],
            'reuse_admission_sha256': digest(self.reuse_admission),
            'source_manifest_sha256': self.preview['source_manifest_sha256'],
            'source_run_ref': self.manifest['source_run_ref'],
            'source_invocation_sha256': self.manifest['source_invocation_sha256'],
            'source_fingerprint_sha256': self.preview['source_fingerprint_sha256'],
            'family_sha256': self.preview['family_sha256'],
            'release_sha256': self.manifest['release_sha256'],
            'selection_sha256': self.manifest['selection_sha256'],
            'review_sha256': self.manifest['review_sha256'],
            'candidate_sha256': self.manifest['candidate_sha256'],
            'recipe_sha256': self.manifest['recipe_sha256'],
        })
        self.manifest['planning_bundle_sha256'] = digest(self.bundle)

    def _valid_v14_inputs(self):
        case_key = self.case_key
        value = self.development_value
        steps = copy.deepcopy(self.steps)
        profile_sha256 = 'b' * 64
        source_manifest_sha256 = 'c' * 64
        source_invocation_sha256 = 'd' * 64
        source_run_id = 'run-planned-test'
        source_run_ref = digest({'run_id': source_run_id})
        source_group_sha256 = 'e' * 64
        source_fingerprint_sha256 = 'f' * 64
        hash_pin = '9' * 64
        skill = {'skill_key': 'save_record'}
        skill_sha256 = digest(skill)
        field_binding = {'parameter_key': 'record-query',
                         'form_field_name': 'message'}
        recipe = {
            'schema_version': '1.0', 'skill_sha256': skill_sha256,
            'outcome': {'parameter_key': 'record-query',
                        'form_field_name': 'message'},
            'steps': steps,
        }
        candidate = {
            'source_run_id': source_run_id,
            'source_run_ref': source_run_ref,
            'source_group_sha256': source_group_sha256,
            'source_fingerprint_sha256': source_fingerprint_sha256,
            'source_context': {'manifest_sha256': source_manifest_sha256,
                               'invocation_sha256': source_invocation_sha256},
            'profile_sha256': profile_sha256, 'skill': skill,
            'field_bindings': [field_binding], 'recipe': recipe,
        }
        candidate_sha256 = digest(candidate)
        recipe_sha256 = digest(recipe)
        reuse_admission = copy.deepcopy(json.loads(
            (REPO_ROOT / 'examples/owned_skill_reuse_admission.json').read_text())['admission'])
        reuse_preview = reuse_admission['preview']
        reuse_admission['manager_session'] = 'app-' + 'b' * 32
        reuse_preview.update({
            'source_manifest_sha256': source_manifest_sha256,
            'source_run_ref': source_run_ref,
            'source_invocation_sha256': source_invocation_sha256,
            'source_fingerprint_sha256': source_fingerprint_sha256,
            'family_sha256': hash_pin, 'release_sha256': hash_pin,
            'selection_sha256': hash_pin, 'review_sha256': hash_pin,
            'candidate_sha256': candidate_sha256, 'recipe_sha256': recipe_sha256,
        })
        reuse_admission['preview_sha256'] = digest(reuse_preview)
        reuse_admission_sha256 = digest(reuse_admission)
        body = form_body(exact_form_fields('message', value))
        form_plan = {
            'schema_version': '1.0', 'profile_sha256': profile_sha256,
            'task_sha256': '8' * 64, 'entry_url': 'https://demo.invalid/entry',
            'submit_url': 'https://demo.invalid/save',
            'receipt_url': 'https://demo.invalid/receipt',
            'body_sha256': hashlib.sha256(body).hexdigest(),
            'body_bytes': len(body),
        }
        state_plan = {
            'schema_version': '1.0', 'profile_sha256': profile_sha256,
            'task_sha256': '8' * 64, 'form_plan_sha256': digest(form_plan),
            'state_url': 'https://demo.invalid/state',
            'expected_before_sha256': '7' * 64,
            'expected_after_sha256': hashlib.sha256(
                b'<html>synthetic</html>').hexdigest(),
        }
        plan = {'schema_version': '1.0', 'synthetic': True, 'cases': []}
        inputs = {
            'schema_version': '1.0', 'synthetic': True,
            'plan_sha256': digest(plan),
            'cases': [{'case_key': case_key,
                       'parameters': {'record-query': value}}],
        }
        invocation = {
            'schema_version': '2.0', 'synthetic': True,
            'recipe_sha256': recipe_sha256, 'skill_sha256': skill_sha256,
            'form_plan_sha256': digest(form_plan),
            'state_plan_sha256': digest(state_plan),
            'skill_plan_sha256': digest(plan),
            'case_inputs_sha256': digest(inputs), 'case_key': case_key,
            'steps': steps,
        }
        invocation_sha256 = digest(invocation)
        parameter_variant_sha256 = '5' * 64
        admission = CandidateExecutionAdmission(
            schema_version='1.0', synthetic=True,
            candidate_sha256=candidate_sha256,
            source_group_sha256=source_group_sha256,
            source_run_ref=source_run_ref,
            source_parameter_variant_sha256='4' * 64,
            parameter_variant_sha256=parameter_variant_sha256,
            invocation_sha256=invocation_sha256, recipe_sha256=recipe_sha256,
            skill_sha256=skill_sha256, profile_sha256=profile_sha256,
            purpose='development_variation', independent_held_out=False,
            dataset_ingestion_authorized=False, execution_authorized=False)
        candidate_preview = {
            'candidate_sha256': candidate_sha256, 'source_run_ref': source_run_ref,
            'source_invocation_sha256': source_invocation_sha256,
            'source_group_sha256': source_group_sha256,
            'profile_sha256': profile_sha256, 'skill_sha256': skill_sha256,
            'case_key': case_key, 'parameter_variant_sha256': parameter_variant_sha256,
            'invocation_sha256': invocation_sha256, 'recipe_sha256': recipe_sha256,
            'steps': steps, 'purpose': 'development_variation',
            'independent_held_out': False, 'schema_version': '1.4',
            'available': True, 'status': 'preview',
            'form_plan_sha256': digest(form_plan),
            'state_plan_sha256': digest(state_plan), 'report': None,
            'review_sha256': hash_pin, 'release_sha256': hash_pin,
            'selection_sha256': hash_pin,
            'reuse_admission_sha256': reuse_admission_sha256,
            'planning_bundle_sha256': None,
        }
        planning_bundle = synthetic_native_contract_bundle(
            case_key, value, steps,
            [{'id': 'admitted-skill', 'skill_key': 'save_record',
              'parameter_key': 'record-query', 'form_field_name': 'message',
              'ordered_steps': steps, 'requested_case_key': case_key,
              'requested_value': value}],
            reuse_admission['lease_id'], reuse_admission['generation'])
        authority = planning_bundle['authority']
        authority.update({
            'manager_session': reuse_admission['manager_session'],
            'desktop_session_id': reuse_admission['desktop_session_id'],
            'runtime_id': reuse_admission['runtime_id'],
            'lease_id': reuse_admission['lease_id'],
            'generation': reuse_admission['generation'],
            'reuse_admission_sha256': reuse_admission_sha256,
            'source_manifest_sha256': source_manifest_sha256,
            'source_run_ref': source_run_ref,
            'source_invocation_sha256': source_invocation_sha256,
            'source_fingerprint_sha256': source_fingerprint_sha256,
            'family_sha256': hash_pin, 'release_sha256': hash_pin,
            'selection_sha256': hash_pin, 'review_sha256': hash_pin,
            'candidate_sha256': candidate_sha256, 'recipe_sha256': recipe_sha256,
        })
        planning_bundle['authority'] = authority
        planning_hash = digest(planning_bundle)
        candidate_preview['planning_bundle_sha256'] = planning_hash
        preview_sha256 = digest(candidate_preview)
        execution_sha256 = digest({
            'preview_sha256': preview_sha256,
            'admission_sha256': digest(admission.model_dump(mode='json')),
            'source_run_ref': source_run_ref,
            'release_sha256': hash_pin, 'selection_sha256': hash_pin,
            'reuse_admission_sha256': reuse_admission_sha256,
            'planning_bundle_sha256': planning_hash,
        })
        prepared = {
            'candidate': candidate, 'plan': ValueBox(plan), 'inputs': ValueBox(inputs),
            'form_plan': ValueBox(form_plan), 'state_plan': ValueBox(state_plan),
            'field_bindings': [ValueBox(field_binding)], 'recipe': ValueBox(recipe),
            'invocation': invocation, 'form_body': body,
            'state_after': b'<html>synthetic</html>', 'source_run_id': source_run_id,
            'source_run_ref': source_run_ref,
            'source_invocation_sha256': source_invocation_sha256,
            'source_group_sha256': source_group_sha256,
            'parameter_variant_sha256': parameter_variant_sha256,
            'invocation_sha256': invocation_sha256, 'recipe_sha256': recipe_sha256,
            'preview_sha256': preview_sha256,
        }
        return {
            'candidate_sha256': candidate_sha256,
            'execution_sha256': execution_sha256, 'prepared': prepared,
            'admission': admission, 'reuse_admission': reuse_admission,
            'reuse_admission_sha256': reuse_admission_sha256,
            'planning_bundle': planning_bundle, 'planning_hash': planning_hash,
            'preview': candidate_preview,
        }

    def _reseal_v14_bundle_after_plan_edit(self, bundle_path, item, edit):
        manifest_path = bundle_path / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        planning_path = bundle_path / 'planning-bundle.json'
        planning_bundle = json.loads(planning_path.read_bytes())
        edit(planning_bundle)
        planning_bytes = json.dumps(planning_bundle, sort_keys=True,
                                    separators=(',', ':')).encode()
        planning_path.write_bytes(planning_bytes)
        planning_path.chmod(0o600)
        planning_hash = digest(planning_bundle)
        preview = copy.deepcopy(item['preview'])
        preview['planning_bundle_sha256'] = planning_hash
        preview_hash = digest(preview)
        execution_hash = digest({
            'preview_sha256': preview_hash,
            'admission_sha256': manifest['admission_sha256'],
            'source_run_ref': manifest['source_run_ref'],
            'release_sha256': manifest['release_sha256'],
            'selection_sha256': manifest['selection_sha256'],
            'reuse_admission_sha256': manifest['reuse_admission_sha256'],
            'planning_bundle_sha256': planning_hash,
        })
        manifest.update({
            'planning_bundle_sha256': planning_hash,
            'preview_sha256': preview_hash,
            'candidate_execution_sha256': execution_hash,
        })
        manifest['files']['planning-bundle.json'] = hashlib.sha256(
            planning_bytes).hexdigest()
        moved = bundle_path.with_name(execution_hash)
        bundle_path.rename(moved)
        (moved / 'manifest.json').write_text(json.dumps(
            manifest, sort_keys=True, separators=(',', ':')))
        (moved / 'manifest.json').chmod(0o600)
        return execution_hash

    def test_v14_bundle_persists_and_loads_exact_planning_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'candidate-execution-bundles'
            item = self._valid_v14_inputs()
            bundle_path, _ = persist_candidate_execution_bundle(
                root, item['candidate_sha256'], item['execution_sha256'],
                item['prepared'], item['admission'], review_sha256='9' * 64,
                release_sha256='9' * 64, selection_sha256='9' * 64,
                reuse_admission_sha256=item['reuse_admission_sha256'],
                reuse_admission=item['reuse_admission'],
                planning_bundle_sha256=item['planning_hash'],
                planning_bundle=item['planning_bundle'])
            loaded, _ = load_candidate_execution_bundle(root, item['execution_sha256'])
            self.assertEqual(bundle_path.name, item['execution_sha256'])
            self.assertEqual(loaded['manifest']['schema_version'], '1.4')
            self.assertEqual(loaded['planning-bundle'], item['planning_bundle'])
            self.assertEqual(loaded['manifest']['planning_bundle_sha256'],
                             item['planning_hash'])

    def test_v14_missing_or_tampered_planning_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'candidate-execution-bundles'
            item = self._valid_v14_inputs()
            bundle_path, _ = persist_candidate_execution_bundle(
                root, item['candidate_sha256'], item['execution_sha256'],
                item['prepared'], item['admission'], review_sha256='9' * 64,
                release_sha256='9' * 64, selection_sha256='9' * 64,
                reuse_admission_sha256=item['reuse_admission_sha256'],
                reuse_admission=item['reuse_admission'],
                planning_bundle_sha256=item['planning_hash'],
                planning_bundle=item['planning_bundle'])
            artifact = bundle_path / 'planning-bundle.json'
            artifact.unlink()
            with self.assertRaises(ValueError):
                load_candidate_execution_bundle(root, item['execution_sha256'])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'candidate-execution-bundles'
            item = self._valid_v14_inputs()
            bundle_path, _ = persist_candidate_execution_bundle(
                root, item['candidate_sha256'], item['execution_sha256'],
                item['prepared'], item['admission'], review_sha256='9' * 64,
                release_sha256='9' * 64, selection_sha256='9' * 64,
                reuse_admission_sha256=item['reuse_admission_sha256'],
                reuse_admission=item['reuse_admission'],
                planning_bundle_sha256=item['planning_hash'],
                planning_bundle=item['planning_bundle'])
            artifact = bundle_path / 'planning-bundle.json'
            artifact.write_bytes(artifact.read_bytes() + b' ')
            artifact.chmod(0o600)
            with self.assertRaises(ValueError):
                load_candidate_execution_bundle(root, item['execution_sha256'])

    def test_v14_manifest_downgrade_and_hash_relabel_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'candidate-execution-bundles'
            item = self._valid_v14_inputs()
            bundle_path, _ = persist_candidate_execution_bundle(
                root, item['candidate_sha256'], item['execution_sha256'],
                item['prepared'], item['admission'], review_sha256='9' * 64,
                release_sha256='9' * 64, selection_sha256='9' * 64,
                reuse_admission_sha256=item['reuse_admission_sha256'],
                reuse_admission=item['reuse_admission'],
                planning_bundle_sha256=item['planning_hash'],
                planning_bundle=item['planning_bundle'])
            manifest_path = bundle_path / 'manifest.json'
            manifest = json.loads(manifest_path.read_bytes())
            manifest['schema_version'] = '1.3'
            manifest.pop('planning_bundle_sha256')
            manifest_path.write_text(json.dumps(manifest, sort_keys=True,
                                                separators=(',', ':')))
            manifest_path.chmod(0o600)
            with self.assertRaises(ValueError):
                load_candidate_execution_bundle(root, item['execution_sha256'])

    def test_v14_resealed_runtime_lease_value_and_step_drift_is_rejected(self):
        mutations = (
            lambda bundle: bundle['authority'].__setitem__('runtime_id', 'other-runtime'),
            lambda bundle: (bundle['request'].__setitem__('lease_id', 'other-lease'),
                            bundle['authority'].__setitem__('lease_id', 'other-lease')),
            lambda bundle: bundle['model_response'].__setitem__(
                'parameter_value', 'other-value'),
            lambda bundle: bundle['model_response']['steps'].reverse(),
        )
        for edit in mutations:
            with self.subTest(edit=edit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'candidate-execution-bundles'
                item = self._valid_v14_inputs()
                bundle_path, _ = persist_candidate_execution_bundle(
                    root, item['candidate_sha256'], item['execution_sha256'],
                    item['prepared'], item['admission'], review_sha256='9' * 64,
                    release_sha256='9' * 64, selection_sha256='9' * 64,
                    reuse_admission_sha256=item['reuse_admission_sha256'],
                    reuse_admission=item['reuse_admission'],
                    planning_bundle_sha256=item['planning_hash'],
                    planning_bundle=item['planning_bundle'])
                execution_hash = self._reseal_v14_bundle_after_plan_edit(
                    bundle_path, item, edit)
                with self.assertRaises(ValueError):
                    load_candidate_execution_bundle(root, execution_hash)

    def test_v14_planning_hash_pin_cannot_be_relabelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'candidate-execution-bundles'
            item = self._valid_v14_inputs()
            bundle_path, _ = persist_candidate_execution_bundle(
                root, item['candidate_sha256'], item['execution_sha256'],
                item['prepared'], item['admission'], review_sha256='9' * 64,
                release_sha256='9' * 64, selection_sha256='9' * 64,
                reuse_admission_sha256=item['reuse_admission_sha256'],
                reuse_admission=item['reuse_admission'],
                planning_bundle_sha256=item['planning_hash'],
                planning_bundle=item['planning_bundle'])
            manifest_path = bundle_path / 'manifest.json'
            manifest = json.loads(manifest_path.read_bytes())
            manifest['planning_bundle_sha256'] = '0' * 64
            manifest_path.write_text(json.dumps(manifest, sort_keys=True,
                                                separators=(',', ':')))
            manifest_path.chmod(0o600)
            with self.assertRaises(ValueError):
                load_candidate_execution_bundle(root, item['execution_sha256'])

    def test_v14_manifest_fixture_is_canonical_and_requires_all_pins(self):
        manifest = self.example['manifest']
        validator('owned_candidate_execution_bundle').validate(manifest)
        self.assertTrue(self.example['synthetic'])
        self.assertIn('planning-bundle.json', manifest['files'])
        self.assertEqual(manifest['schema_version'], '1.4')
        for changed in (
            {**manifest, 'schema_version': '1.3'},
            {key: value for key, value in manifest.items()
             if key != 'planning_bundle_sha256'},
            {**manifest, 'files': {key: value for key, value in manifest['files'].items()
                                   if key != 'planning-bundle.json'}},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                validator('owned_candidate_execution_bundle').validate(changed)

    def test_pure_pin_join_accepts_only_exact_candidate_execution_binding(self):
        _assert_planning_execution_pins(
            self.bundle, manifest=self.manifest,
            reuse_admission=self.reuse_admission, candidate=self.candidate,
            case_key=self.case_key, development_value=self.development_value,
            steps=self.steps)

    def test_plan_value_steps_and_authority_mutations_break_the_pin_join(self):
        mutations = []
        changed_response = copy.deepcopy(self.bundle)
        changed_response['model_response']['parameter_value'] = 'different'
        mutations.append(changed_response)
        changed_steps = copy.deepcopy(self.bundle)
        changed_steps['model_response']['steps'].reverse()
        mutations.append(changed_steps)
        changed_evidence = copy.deepcopy(self.bundle)
        changed_evidence['evidence'][0]['form_field_name'] = 'alternate'
        mutations.append(changed_evidence)
        for bundle in mutations:
            manifest = {**self.manifest, 'planning_bundle_sha256': digest(bundle)}
            with self.subTest(bundle=bundle), self.assertRaises(ValueError):
                _assert_planning_execution_pins(
                    bundle, manifest=manifest,
                    reuse_admission=self.reuse_admission, candidate=self.candidate,
                    case_key=self.case_key, development_value=self.development_value,
                    steps=self.steps)

        changed_reuse = copy.deepcopy(self.reuse_admission)
        changed_reuse['lease_id'] = 'other-lease'
        with self.assertRaises(ValueError):
            _assert_planning_execution_pins(
                self.bundle, manifest={**self.manifest,
                                       'reuse_admission_sha256': digest(changed_reuse)},
                reuse_admission=changed_reuse, candidate=self.candidate,
                case_key=self.case_key, development_value=self.development_value,
                steps=self.steps)

    def test_public_binding_rejects_synthetic_fixture_as_model_evidence(self):
        self.assertIs(self.bundle['real_model'], False)
        with self.assertRaises(ValueError):
            assert_planning_execution_binding(
                self.bundle, manifest=self.manifest,
                reuse_admission=self.reuse_admission, candidate=self.candidate,
                case_key=self.case_key, development_value=self.development_value,
                steps=self.steps)

    def test_planning_artifact_hash_and_selected_scope_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'candidate-execution-bundles'
            with self.assertRaises(ValueError):
                persist_candidate_execution_bundle(
                    root, self.hash_pin, self.hash_pin, {}, None,
                    planning_bundle_sha256=self.hash_pin)
            with self.assertRaises(ValueError):
                persist_candidate_execution_bundle(
                    root, self.hash_pin, self.hash_pin, {}, None,
                    planning_bundle_sha256=digest(self.bundle),
                    planning_bundle=self.bundle)
            self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import canonical, digest
from aos.owned_form_candidate_execution import (
    load_candidate_execution_bundle, persist_candidate_execution_bundle,
)


class DumpValue:
    def __init__(self, value):
        self.value = value

    def model_dump(self, mode='json'):
        return self.value


class OwnedCandidateAdapterBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidate_directory = self.root / 'candidate-execution-bundles'
        run_id = 'run-adapter-source'
        source_run_ref = digest({'run_id': run_id})
        recipe = {'schema_version': '1.0', 'skill_sha256': '3' * 64,
                  'outcome': {'parameter_key': 'message-value',
                              'form_field_name': 'message'}}
        plan = {'schema_version': '1.0', 'cases': []}
        inputs = {'schema_version': '1.0', 'plan_sha256': digest(plan), 'cases': [
            {'case_key': 'dev-gamma', 'parameters': {'message-value': 'gamma'}}]}
        form_body = b'message=gamma'
        state_after = b'<p>gamma</p>'
        form_plan = {'body_sha256': hashlib.sha256(form_body).hexdigest(),
                     'body_bytes': len(form_body)}
        state_plan = {'expected_after_sha256': hashlib.sha256(state_after).hexdigest()}
        invocation = {
            'recipe_sha256': digest(recipe), 'skill_sha256': '3' * 64,
            'form_plan_sha256': digest(form_plan), 'state_plan_sha256': digest(state_plan),
            'skill_plan_sha256': digest(plan), 'case_inputs_sha256': digest(inputs),
            'case_key': 'dev-gamma',
            'steps': [{'step_key': 'save-record', 'operation': 'submit_form'}],
        }
        candidate = {
            'source_run_ref': source_run_ref,
            'source_context': {'manifest_sha256': '6' * 64,
                               'invocation_sha256': '5' * 64},
            'source_group_sha256': '1' * 64, 'profile_sha256': '2' * 64,
            'recipe': recipe,
            'field_bindings': [{'parameter_key': 'message-value',
                                'form_field_name': 'message'}],
        }
        self.candidate_sha256 = digest(candidate)
        self.recipe = recipe
        self.invocation = invocation
        self.source_run_ref = source_run_ref
        self.reuse_admission = {
            'schema_version': '1.0', 'synthetic': True,
            'purpose': 'owned_selected_skill_reuse_admission',
            'preview': {
                'candidate_sha256': self.candidate_sha256,
                'source_run_ref': source_run_ref,
                'source_invocation_sha256': '5' * 64,
                'source_manifest_sha256': '6' * 64,
                'recipe_sha256': digest(recipe), 'review_sha256': '9' * 64,
                'release_sha256': 'a' * 64, 'selection_sha256': 'b' * 64,
            },
            'manager_session': 'manager-new', 'desktop_session_id': 'session-new',
            'runtime_id': 'runtime-new', 'lease_id': 'lease-new', 'generation': 7,
        }
        self.base_preview = {
            'candidate_sha256': self.candidate_sha256,
            'source_run_ref': source_run_ref,
            'source_invocation_sha256': '5' * 64,
            'source_group_sha256': candidate['source_group_sha256'],
            'profile_sha256': candidate['profile_sha256'],
            'skill_sha256': invocation['skill_sha256'],
            'case_key': invocation['case_key'], 'parameter_variant_sha256': '4' * 64,
            'invocation_sha256': digest(invocation), 'recipe_sha256': digest(recipe),
            'steps': invocation['steps'], 'purpose': 'development_variation',
            'independent_held_out': False, 'schema_version': '1.3',
            'available': True, 'status': 'preview',
            'form_plan_sha256': digest(form_plan), 'state_plan_sha256': digest(state_plan),
            'report': None, 'review_sha256': '9' * 64, 'release_sha256': 'a' * 64,
            'selection_sha256': 'b' * 64, 'reuse_admission_sha256': digest(self.reuse_admission),
        }
        runtime_binding = {
            'protocol': 'owned-adapter-runtime-v1', 'authorization_sha256': 'c' * 64,
            'adaptation_report_sha256': 'd' * 64, 'artifact_sha256': 'e' * 64,
            'input_sha256': 'f' * 64, 'deployment_manifest_sha256': '0' * 64,
            'base_deployment_id': 'decider-' + '1' * 64,
            'runtime_worker_sha256': '2' * 64,
        }
        self.adapter_admission = {
            'schema_version': '1.0', 'mode': 'owned_adapter_runtime_admission',
            'episode_id': 'episode-' + '3' * 32,
            'authorization_sha256': runtime_binding['authorization_sha256'],
            'adaptation_report_sha256': runtime_binding['adaptation_report_sha256'],
            'adapter_sha256': runtime_binding['artifact_sha256'],
            'runtime_binding': runtime_binding,
            'adapter_deployment_id': 'owned-adapter-' + digest(runtime_binding),
            'base_preview_sha256': digest(self.base_preview), 'case_key': 'dev-gamma',
            'development_value_sha256': digest({'value': 'gamma'}),
            'lease_id': 'lease-new', 'generation': 7,
            'experimental_runtime_authorized': True, 'automatic_fallback': False,
            'promotion_authorized': False, 'training_ready': False, 'synthetic': True,
        }
        self.adapter_sha256 = digest(self.adapter_admission)
        final_preview = {**self.base_preview, 'schema_version': '1.5',
                         'adapter_admission_sha256': self.adapter_sha256}
        preview_sha256 = digest(final_preview)
        admission = {
            'candidate_sha256': self.candidate_sha256,
            'invocation_sha256': digest(invocation),
            'parameter_variant_sha256': '4' * 64, 'skill_sha256': '3' * 64,
        }
        self.execution_sha256 = digest({
            'preview_sha256': preview_sha256,
            'admission_sha256': digest(admission), 'source_run_ref': source_run_ref,
            'release_sha256': 'a' * 64, 'selection_sha256': 'b' * 64,
            'reuse_admission_sha256': digest(self.reuse_admission),
            'adapter_admission_sha256': self.adapter_sha256,
        })
        self.prepared = {
            'candidate': candidate, 'plan': DumpValue(plan), 'inputs': DumpValue(inputs),
            'form_plan': DumpValue(form_plan), 'state_plan': DumpValue(state_plan),
            'field_bindings': [DumpValue({'parameter_key': 'message-value',
                                          'form_field_name': 'message'})],
            'recipe': DumpValue(recipe), 'invocation': invocation,
            'form_body': form_body, 'state_after': state_after,
            'source_run_id': run_id, 'source_run_ref': source_run_ref,
            'source_invocation_sha256': '5' * 64,
            'source_group_sha256': candidate['source_group_sha256'],
            'parameter_variant_sha256': '4' * 64,
            'invocation_sha256': digest(invocation), 'recipe_sha256': digest(recipe),
            'preview_sha256': preview_sha256,
            'profile_sha256': candidate['profile_sha256'],
            'skill_sha256': invocation['skill_sha256'],
        }
        self.admission = DumpValue(admission)
        self.reuse_check = patch(
            'aos.owned_skill_reuse_admission.validate_owned_skill_reuse_admission')
        self.reuse_check.start()
        self.addCleanup(self.reuse_check.stop)

    def persist(self, execution_sha256=None, **changes):
        return persist_candidate_execution_bundle(
            self.candidate_directory, self.candidate_sha256,
            execution_sha256 or self.execution_sha256, self.prepared, self.admission,
            review_sha256='9' * 64, release_sha256='a' * 64,
            selection_sha256='b' * 64,
            reuse_admission_sha256=digest(self.reuse_admission),
            reuse_admission=self.reuse_admission,
            adapter_admission_sha256=self.adapter_sha256,
            adapter_admission=self.adapter_admission, **changes)

    def test_v15_roundtrip_pins_reuse_and_adapter_admissions(self):
        directory, _manifest_sha = self.persist()
        loaded, _ = load_candidate_execution_bundle(
            self.candidate_directory, self.execution_sha256)

        self.assertEqual(loaded['manifest']['schema_version'], '1.5')
        self.assertEqual(loaded['manifest']['adapter_admission_sha256'], self.adapter_sha256)
        self.assertEqual(loaded['adapter-admission'], self.adapter_admission)
        self.assertTrue((directory / 'adapter-admission.json').is_file())
        self.assertEqual(loaded['adapter-admission']['base_preview_sha256'],
                         digest(self.base_preview))

    def test_adapter_artifact_tamper_and_missing_pin_fail_closed(self):
        directory, _ = self.persist()
        path = directory / 'adapter-admission.json'
        path.write_bytes(canonical({'changed': True}).encode())
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'file_changed'):
            load_candidate_execution_bundle(self.candidate_directory, self.execution_sha256)

        clean_directory = self.root / 'second-bundles'
        with self.assertRaisesRegex(ValueError, 'adapter_admission_pin_invalid'):
            persist_candidate_execution_bundle(
                clean_directory, self.candidate_sha256, self.execution_sha256,
                self.prepared, self.admission, review_sha256='9' * 64,
                release_sha256='a' * 64, selection_sha256='b' * 64,
                reuse_admission_sha256=digest(self.reuse_admission),
                reuse_admission=self.reuse_admission,
                adapter_admission=self.adapter_admission)
        self.assertFalse(clean_directory.exists())

    def test_wrong_execution_identity_and_v14_downgrade_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'adapter_execution_identity_changed'):
            self.persist('f' * 64)
        directory, _ = self.persist()
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        manifest['schema_version'] = '1.4'
        manifest.pop('adapter_admission_sha256')
        manifest_path.write_bytes(canonical(manifest).encode())
        manifest_path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'manifest_invalid'):
            load_candidate_execution_bundle(self.candidate_directory, self.execution_sha256)

    def test_planning_bundle_and_adapter_admission_cannot_share_version(self):
        with self.assertRaisesRegex(ValueError, 'adapter_admission_pin_invalid'):
            persist_candidate_execution_bundle(
                self.candidate_directory, self.candidate_sha256, self.execution_sha256,
                self.prepared, self.admission, review_sha256='9' * 64,
                release_sha256='a' * 64, selection_sha256='b' * 64,
                reuse_admission_sha256=digest(self.reuse_admission),
                reuse_admission=self.reuse_admission, planning_bundle_sha256='d' * 64,
                planning_bundle={}, adapter_admission_sha256=self.adapter_sha256,
                adapter_admission=self.adapter_admission)


if __name__ == '__main__':
    unittest.main()

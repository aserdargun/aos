import copy
import json
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, canonical, digest
from aos.site_skill_form_recipe_candidate_execution import (
    CandidateExecutionAdmission, CandidateExecutionReport,
    candidate_admission_payload, verify_candidate_admission)


class CandidateAdmissionTests(unittest.TestCase):
    def fixture(self, name, key):
        return json.loads((REPO_ROOT / f'examples/{name}.json').read_text())[key]

    def test_canonical_synthetic_admission_and_report(self):
        for name, key, model in (
                ('site_skill_form_recipe_candidate_admission', 'admission', CandidateExecutionAdmission),
                ('site_skill_form_recipe_candidate_execution', 'report', CandidateExecutionReport)):
            value = self.fixture(name, key)
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            jsonschema.Draft202012Validator(schema).validate(value)
            parsed = model.model_validate_json(canonical(value))
            self.assertEqual(parsed.model_dump(mode='json'), value)
        admission = self.fixture('site_skill_form_recipe_candidate_admission', 'admission')
        invocation = self.fixture('site_skill_form_recipe_invocation', 'invocation')
        verified = verify_candidate_admission(admission, invocation)
        self.assertEqual(candidate_admission_payload(verified),
                         {'admission': admission, 'admission_sha256': digest(admission)})

    def test_exact_pins_fresh_variant_and_false_authority(self):
        admission = self.fixture('site_skill_form_recipe_candidate_admission', 'admission')
        invocation = self.fixture('site_skill_form_recipe_invocation', 'invocation')
        for key in ('invocation_sha256', 'recipe_sha256', 'skill_sha256', 'profile_sha256'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_candidate_admission({**admission, key: '0' * 64}, invocation)
        with self.assertRaises(ValueError):
            verify_candidate_admission({**admission,
                'parameter_variant_sha256': admission['source_parameter_variant_sha256']}, invocation)
        for partition in ('test', 'validation', 'independent_held_out'):
            with self.subTest(partition=partition), self.assertRaises(ValueError):
                verify_candidate_admission({**admission, 'purpose': partition}, invocation)
        for key in ('independent_held_out', 'dataset_ingestion_authorized', 'execution_authorized'):
            for value in (True, 0, 'false'):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    verify_candidate_admission({**admission, key: value}, invocation)
        v1 = {**invocation, 'schema_version': '1.0'}
        with self.assertRaises(ValueError):
            verify_candidate_admission(admission, v1)

    def test_report_cannot_swap_lineage_or_claim_training(self):
        report = self.fixture('site_skill_form_recipe_candidate_execution', 'report')
        changes = {'source_group_sha256': '0' * 64, 'source_run_ref': '0' * 64,
                   'execution_run_ref': report['source_run_ref'],
                   'admission_sha256': '0' * 64, 'training_ready': True,
                   'site_outcome_verified': True, 'dataset_ingestion_authorized': True,
                   'held_out_independence_verified': True, 'source_bound': 1}
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                CandidateExecutionReport.model_validate_json(canonical({**report, key: value}))
        changed = copy.deepcopy(report)
        changed['admission']['candidate_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            CandidateExecutionReport.model_validate_json(canonical(changed))


if __name__ == '__main__':
    unittest.main()

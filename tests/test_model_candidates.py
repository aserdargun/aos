from copy import deepcopy
import json
import unittest

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from aos.contracts import REPO_ROOT
from scripts.inspect_unsloth_candidate import inspect_candidate, validate_recipe


class ModelCandidateTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((REPO_ROOT / 'config/model_candidates.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/model_candidates.schema.json').read_text())
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def test_reviewed_roles_capacity_tiers_and_sources(self):
        self.validator.validate(self.catalog)
        candidates = self.catalog['candidates']
        self.assertEqual(len({item['id'] for item in candidates}), len(candidates))
        self.assertEqual({(item['role'], item['capacity_tier']) for item in candidates}, {
            ('system1', 'local_16gb_candidate'), ('system2', 'local_16gb_candidate'),
            ('system1', 'larger_gpu_reserve'), ('system2', 'larger_gpu_reserve')})
        for item in candidates:
            self.assertEqual(item['source_url'], 'https://huggingface.co/' + item['repository'])
        clef = next(item for item in candidates if item['repository'] == 'Cloudflare/clef')
        self.assertEqual(clef['execution_kind'], 'typed_choice_probabilities')

    def test_catalog_cannot_be_promoted_or_claim_unmeasured_results(self):
        for field, value in [('runtime_selectable', True), ('aos_adapter_verified', True),
                             ('status', 'active'), ('measured_latency_ms', 0),
                             ('measured_vram_bytes', 0), ('revision', 'main'),
                             ('source_url', 'javascript:alert(1)'), ('endpoint', 'http://localhost:8000')]:
            with self.subTest(field=field):
                invalid = deepcopy(self.catalog)
                invalid['candidates'][0][field] = value
                with self.assertRaises(ValidationError):
                    self.validator.validate(invalid)
        invalid = dict(self.catalog, runtime_authority=True)
        with self.assertRaises(ValidationError):
            self.validator.validate(invalid)

    def test_upstream_file_sizes_are_not_vram_measurements(self):
        candidate = next(item for item in self.catalog['candidates'] if item['id'] == 'gemma4_12b_s2')
        artifacts = candidate['quantization_reference']['artifacts']
        self.assertEqual(sum(item['bytes'] for item in artifacts), 7150994912)
        self.assertIsNone(candidate['measured_vram_bytes'])
        self.assertFalse(candidate['runtime_selectable'])

    def test_each_method_has_a_separate_inert_adapter_namespace(self):
        namespaces = set()
        for candidate in self.catalog['candidates']:
            for method in ('lora', 'qlora'):
                plan = inspect_candidate(candidate['id'], method)
                self.assertFalse(plan['training_ready'])
                self.assertFalse(plan['runtime_authority'])
                self.assertFalse(plan['automatic_promotion'])
                self.assertEqual(plan['base']['repository'], candidate['repository'])
                self.assertNotIn('gguf', plan['base']['repository'].lower())
                namespaces.add(plan['adapter_namespace_template'])
        self.assertEqual(len(namespaces), 8)
        with self.assertRaises(ValueError):
            inspect_candidate('../private', 'qlora')
        with self.assertRaises(ValueError):
            inspect_candidate(self.catalog['candidates'][0]['id'], 'full')

    def test_cross_base_and_joint_head_shortcuts_are_rejected(self):
        candidate = next(item for item in self.catalog['candidates'] if item['id'] == 'cloudflare_clef_s1')
        path = REPO_ROOT / 'training/recipes/unsloth-cloudflare_clef_s1-v001.json'
        recipe = json.loads(path.read_text())
        invalid = deepcopy(recipe)
        invalid['base']['repository'] = 'Qwen/Qwen3.8-27B'
        with self.assertRaises(ValueError):
            validate_recipe(invalid, candidate)
        invalid = deepcopy(recipe)
        invalid['compatibility']['custom_joint_head_required'] = False
        with self.assertRaises(ValueError):
            validate_recipe(invalid, candidate)
        invalid = deepcopy(recipe)
        invalid['blocked_until'].remove('joint_head_loss_and_serialization_probe')
        with self.assertRaises(ValueError):
            validate_recipe(invalid, candidate)
        invalid = deepcopy(recipe)
        invalid['compatibility']['aos_training_verified'] = True
        with self.assertRaises(ValidationError):
            validate_recipe(invalid, candidate)


if __name__ == '__main__':
    unittest.main()

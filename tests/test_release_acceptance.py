import copy
import json
import unittest

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from aos.contracts import REPO_ROOT


class ReleaseAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((REPO_ROOT / 'docs/release_acceptance.json').read_text())
        cls.schema = json.loads((REPO_ROOT / 'schemas/release_acceptance_snapshot.schema.json').read_text())
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def test_current_snapshot_has_six_scoped_non_authorizing_stages(self):
        self.validator.validate(self.snapshot)
        self.assertEqual([stage['id'] for stage in self.snapshot['stages']],
                         ['candidate', 'scientist', 'native_workflow', 'user_delivery', 'learning', 'swapp'])
        for stage in self.snapshot['stages']:
            for evidence in stage['evidence']:
                if evidence.startswith('docs/'):
                    self.assertTrue((REPO_ROOT / evidence).is_file(), evidence)

    def test_rejects_authority_completion_and_execution_fields(self):
        for field, value in [('runtime_authority', True), ('product_complete', True),
                             ('execution_authorized', True), ('scope', 'live_runtime')]:
            with self.subTest(field=field):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot[field] = value
                with self.assertRaises(ValidationError):
                    self.validator.validate(snapshot)

    def test_rejects_duplicate_missing_or_reordered_stages(self):
        for stages in [self.snapshot['stages'][:-1], self.snapshot['stages'][::-1],
                       [self.snapshot['stages'][0]] * 6]:
            with self.subTest(stages=[stage['id'] for stage in stages]):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['stages'] = stages
                with self.assertRaises(ValidationError):
                    self.validator.validate(snapshot)

    def test_rejects_unproven_statuses_and_mismatched_labels(self):
        for field, value in [('source', True), ('verification', 'native_verified'),
                             ('delivery', 'delivered'), ('title_key', 'release_acceptance.swapp.title'),
                             ('next_action_key', 'release_acceptance.swapp.next')]:
            with self.subTest(field=field):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['stages'][0][field] = value
                with self.assertRaises(ValidationError):
                    self.validator.validate(snapshot)

    def test_rejects_invalid_dates_and_unsafe_evidence_paths(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['observed_at'] = '2026-02-30T06:00:00Z'
        with self.assertRaises(ValidationError):
            self.validator.validate(snapshot)
        for evidence in ['docs/../README.md', '/tmp/private.log', 'https://example.invalid',
                         'docs//STATUS.md', 'data/./receipt.json']:
            with self.subTest(evidence=evidence):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['stages'][0]['evidence'] = [evidence]
                with self.assertRaises(ValidationError):
                    self.validator.validate(snapshot)

    def test_all_stage_labels_are_available_in_ui(self):
        translations = (REPO_ROOT / 'ui/src/translations_core.ts').read_text()
        for stage in self.snapshot['stages']:
            for key in [stage['title_key'], stage['next_action_key'], *stage['blockers']]:
                self.assertIn(json.dumps(key) + ':', translations)

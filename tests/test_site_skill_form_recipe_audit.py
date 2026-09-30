import copy
import json
import sqlite3
from types import SimpleNamespace
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, canonical
from aos.site_skill_form_recipe import SUPPORTED_RECIPE_ORDERS
from aos.site_skill_form_recipe_audit import (
    SiteSkillFormRecipeAuditReport, _recipe_observations)


class SiteSkillFormRecipeAuditTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = sqlite3.connect(':memory:')
        self.snapshot.row_factory = sqlite3.Row
        self.addCleanup(self.snapshot.close)
        self.snapshot.execute('''CREATE TABLE observations(
            observation_id TEXT, run_id TEXT, step_id TEXT, action_id TEXT,
            kind TEXT, payload_json TEXT, created_at TEXT)''')
        self.snapshot.execute('''CREATE TABLE desktop_remote_form_bindings(
            run_id TEXT, binding_sha256 TEXT)''')
        self.snapshot.execute('INSERT INTO desktop_remote_form_bindings VALUES(?,?)',
                              ('run-recipe', 'b' * 64))
        self.invocation = SimpleNamespace(
            recipe_sha256='a' * 64, profile_sha256='c' * 64, form_plan_sha256='d' * 64,
            steps=tuple(SimpleNamespace(step_key=operation.replace('_', '-'), operation=operation)
                        for operation in SUPPORTED_RECIPE_ORDERS[1]))
        self.states = {}
        self.payloads = []
        for ordinal, step in enumerate(self.invocation.steps):
            payload = {'profile_sha256': self.invocation.profile_sha256,
                       'binding_sha256': 'b' * 64,
                       'plan_sha256': self.invocation.form_plan_sha256, 'next_stage': ordinal,
                       'recipe_step': {'recipe_sha256': self.invocation.recipe_sha256,
                                       'step_key': step.step_key, 'operation': step.operation,
                                       'ordinal': ordinal}}
            self.payloads.append(payload)
            prefix = f'2026-09-27T00:00:{ordinal:02d}'
            self.states[1 + 5 * ordinal] = ({'created_at': prefix + '.000000Z'},
                                            SimpleNamespace(step_id='step-shared'))
            self.states[2 + 5 * ordinal] = ({'created_at': prefix + '.900000Z'},
                                            SimpleNamespace(step_id='step-shared'))
            self.snapshot.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)', (
                f'observation-{ordinal}', 'run-recipe', 'step-shared', None,
                'browser.https_form', canonical(payload), prefix + '.500000Z'))

    def audit(self):
        return _recipe_observations(self.snapshot, 'run-recipe', self.invocation, self.states)

    def test_unique_evidence_is_bound_even_when_all_actions_share_a_step_id(self):
        self.assertEqual(len(set(self.audit())), 6)

    def test_changed_keys_operations_pins_and_boolean_ordinals_are_rejected(self):
        for field, value in (('step_key', 'wrong'), ('operation', 'submit_form'),
                             ('recipe_sha256', 'f' * 64), ('ordinal', True)):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payloads[1])
                payload['recipe_step'][field] = value
                self.snapshot.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                                      (canonical(payload), 'observation-1'))
                with self.assertRaises(ValueError):
                    self.audit()
        self.snapshot.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                              (canonical(self.payloads[1]), 'observation-1'))
        self.assertEqual(len(self.audit()), 6)

    def test_moved_or_action_bound_observation_is_rejected(self):
        for column, value in (('created_at', '2026-09-27T00:00:03.500000Z'),
                              ('step_id', 'wrong-step'), ('action_id', 'wrong-action')):
            with self.subTest(column=column):
                self.snapshot.execute('SAVEPOINT changed')
                self.snapshot.execute(f'UPDATE observations SET {column}=? WHERE observation_id=?',
                                      (value, 'observation-1'))
                with self.assertRaises(ValueError):
                    self.audit()
                self.snapshot.execute('ROLLBACK TO changed')
                self.snapshot.execute('RELEASE changed')

    def test_missing_and_duplicate_observation_are_rejected(self):
        self.snapshot.execute('SAVEPOINT changed')
        self.snapshot.execute('DELETE FROM observations WHERE observation_id=?', ('observation-1',))
        with self.assertRaises(ValueError):
            self.audit()
        self.snapshot.execute('ROLLBACK TO changed')
        self.snapshot.execute('INSERT INTO observations SELECT * FROM observations LIMIT 1')
        with self.assertRaises(ValueError):
            self.audit()

    def test_extra_metadata_and_boolean_stage_are_rejected(self):
        for changed in ({'next_stage': True}, {'unexpected': 'private-content'},
                        {'binding_sha256': 'f' * 64}):
            with self.subTest(changed=changed):
                payload = {**self.payloads[1], **changed}
                self.snapshot.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                                      (canonical(payload), 'observation-1'))
                with self.assertRaises(ValueError):
                    self.audit()

    def test_report_fixture_and_canonical_schema(self):
        report = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_audit.json').read_text())['report']
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_form_recipe_audit.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SiteSkillFormRecipeAuditReport.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(report)
        parsed = SiteSkillFormRecipeAuditReport.model_validate_json(canonical(report))
        self.assertTrue(parsed.executable_recipe_executed)
        self.assertFalse(parsed.skill_validated)
        for field in ('site_outcome_verified', 'reviewed', 'training_ready', 'activation_authorized'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                SiteSkillFormRecipeAuditReport.model_validate_json(canonical({**report, field: True}))
        for field, value in (('skill_validated', 0), ('executable_recipe_executed', 1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                SiteSkillFormRecipeAuditReport.model_validate_json(canonical({**report, field: value}))


if __name__ == '__main__':
    unittest.main()

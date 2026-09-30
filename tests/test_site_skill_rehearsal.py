import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.dataset_audit import audit_snapshot
from aos.site_skill_rehearsal import SiteSkillRehearsalReport, rehearse_site_skill
from aos.site_skill_validation import SiteSkillValidationPlan

from tests import test_site_skill_provenance as provenance_fixture


PLAN = json.loads((REPO_ROOT / 'examples/site_skill_validation_plan.json').read_text())['plan']


class SiteSkillRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.source = provenance_fixture.SiteSkillProvenanceTests(
            'test_fixture_and_s1_exact_source_verification')
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.skill_sha256 = self.source.register()
        self.plan = SiteSkillValidationPlan.model_validate(PLAN)
        self.snapshot_sha256 = self.source.inspect(self.skill_sha256)['snapshot_sha256']

    def rehearse(self, *, plan=None, skill_sha256=None, snapshot_sha256=None, run_id='run-test'):
        plan = plan or self.plan
        return rehearse_site_skill(
            self.source.database, run_id, profiles=self.source.profiles.root,
            pages=self.source.pages.root, skills=self.source.skills.root,
            plan=plan, skill_sha256=skill_sha256 or self.skill_sha256,
            plan_sha256=digest(plan.model_dump()),
            snapshot_sha256=snapshot_sha256 or self.snapshot_sha256)

    def test_audited_synthetic_source_request_bound_but_parameters_unbound(self):
        report = self.rehearse()
        self.assertEqual(report['status'], 'source_request_bound_parameter_unbound')
        self.assertTrue(report['source_request_bound'])
        self.assertEqual(report['source_inspection_status'], 'unreviewed_source_match')
        self.assertEqual(report['structural_status'], 'structure_only')
        self.assertEqual(report['source_event_count'], 1)
        self.assertEqual(report['development_count'], 1)
        self.assertEqual(report['held_out_count'], 2)
        self.assertEqual(report, self.rehearse())
        self.assertNotIn('private-do-not-export', json.dumps(report))
        self.assertTrue(all(report[key] is False for key in (
            'source_variant_bound', 'execution_performed', 'outcomes_verified',
            'skill_validated', 'reviewed', 'activation_authorized', 'training_ready')))
        self.assertTrue(report['held_out_separated_structurally'])
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_rehearsal_report.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SiteSkillRehearsalReport.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(report)
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_rehearsal_report.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        self.assertEqual({key: value for key, value in fixture['report'].items()
                          if key != 'snapshot_sha256'},
                         {key: value for key, value in report.items()
                          if key != 'snapshot_sha256'})
        self.assertFalse(validator('site_skill_rehearsal_report').is_valid(
            {**report, 'skill_validated': True}))

    def test_wrong_role_scope_and_source_are_rejected(self):
        for changes in ({'model_role': 'system2'}, {'task_key': 'update-draft'},
                        {'profile_sha256': '0' * 64}, {'page_draft_sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                self.rehearse(plan=SiteSkillValidationPlan.model_validate({**PLAN, **changes}))
        with self.assertRaises(ValueError):
            self.rehearse(snapshot_sha256='0' * 64)
        with self.assertRaises(ValueError):
            self.rehearse(run_id='missing')
        with self.assertRaises(ValueError):
            rehearse_site_skill(
                self.source.database, 'run-test', profiles=self.source.profiles.root,
                pages=self.source.pages.root, skills=self.source.skills.root,
                plan=self.plan, skill_sha256=self.skill_sha256,
                plan_sha256='0' * 64, snapshot_sha256=self.snapshot_sha256)

    def test_held_out_leakage_and_stale_audit_fail_closed(self):
        cases = copy.deepcopy(PLAN['cases'])
        cases[2]['parameter_variant_sha256'] = PLAN['source_variant_sha256'][0]
        with self.assertRaises(ValueError):
            self.rehearse(plan=SiteSkillValidationPlan.model_validate({**PLAN, 'cases': cases}))
        cases = copy.deepcopy(PLAN['cases'])
        cases[2]['parameter_variant_sha256'] = cases[1]['parameter_variant_sha256']
        with self.assertRaises(ValueError):
            self.rehearse(plan=SiteSkillValidationPlan.model_validate({**PLAN, 'cases': cases}))
        with self.source.trajectory.connection:
            self.source.trajectory.connection.execute(
                "UPDATE tasks SET original_goal='changed-private' WHERE task_id='task-test'")
        with self.assertRaises(ValueError):
            self.rehearse()

    def test_source_request_hash_and_canonical_bytes_are_required(self):
        changed_plan = SiteSkillValidationPlan.model_validate({
            **PLAN, 'source_variant_sha256': ['f' * 64]})
        with self.assertRaisesRegex(ValueError, 'rehearsal_source_request_unbound'):
            self.rehearse(plan=changed_plan)
        with self.source.trajectory.connection:
            self.source.trajectory.connection.execute(
                "UPDATE model_calls SET request_json='{ }' WHERE call_id='call-test'")
        with audit_snapshot(self.source.database) as (_, identity):
            self.snapshot_sha256 = identity['sha256']
        with self.assertRaisesRegex(ValueError, 'skill_source_request_not_canonical'):
            self.rehearse()
        with self.source.trajectory.connection:
            self.source.trajectory.connection.execute(
                'UPDATE model_calls SET request_json=? WHERE call_id=?',
                (canonical({'synthetic_blob': 'x' * (64 * 1024)}), 'call-test'))
        with audit_snapshot(self.source.database) as (_, identity):
            self.snapshot_sha256 = identity['sha256']
        with self.assertRaisesRegex(ValueError, 'skill_source_request_unavailable'):
            self.rehearse()

    def test_non_synthetic_source_cannot_be_labeled_synthetic(self):
        with self.source.trajectory.connection:
            self.source.trajectory.connection.execute(
                "UPDATE runs SET policy_version='other-policy' WHERE run_id='run-test'")
        with audit_snapshot(self.source.database) as (_, identity):
            current_hash = identity['sha256']
        with self.assertRaisesRegex(ValueError, 'rehearsal_requires_synthetic_source'):
            self.rehearse(snapshot_sha256=current_hash)

    def test_cli_exact_selection_and_generic_error(self):
        plan_path = self.source.root / 'plan.json'
        plan_path.write_text(canonical(self.plan.model_dump()))
        os.chmod(plan_path, 0o600)
        command = [sys.executable, '-m', 'aos.site_skill_rehearsal',
                   '--database', str(self.source.database), '--run-id', 'run-test',
                   '--profiles', str(self.source.profiles.root),
                   '--pages', str(self.source.pages.root),
                   '--skills', str(self.source.skills.root),
                   '--plan', str(plan_path), '--skill-sha256', self.skill_sha256,
                   '--plan-sha256', digest(self.plan.model_dump()),
                   '--snapshot-sha256', self.snapshot_sha256]
        success = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout), self.rehearse())
        self.assertNotIn('private-do-not-export', success.stdout)
        stale = subprocess.run([*command[:-1], '0' * 64], capture_output=True, text=True, timeout=10)
        self.assertEqual(stale.returncode, 1)
        self.assertEqual(stale.stdout, '')
        self.assertEqual(stale.stderr,
                         'Site skill rehearsal unavailable: missing, invalid or unsafe source.\n')
        malformed = subprocess.run([*command, '--unknown'], capture_output=True, text=True, timeout=10)
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(malformed.stdout, '')
        self.assertEqual(malformed.stderr,
                         'Site skill rehearsal unavailable: invalid arguments.\n')


if __name__ == '__main__':
    unittest.main()

import copy
import json
from pathlib import Path
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_validation import (SiteSkillValidationPlan, SiteSkillValidationReport,
                                       preview_skill_validation)
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


PLAN = json.loads((REPO_ROOT / 'examples/site_skill_validation_plan.json').read_text())
SKILL = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']


class SiteSkillValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        profiles = WebApplicationProfiles(root / 'profiles')
        profile = WebApplicationProfile.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
        profiles.register(profile, confirm_sha256=profile_report(profile).profile_sha256)
        pages = SiteKnowledgeStore(root / 'pages', profiles)
        page = SitePageDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_page_draft.json').read_text())['page'])
        pages.register(page, confirm_sha256=digest(page.model_dump()))
        self.store = SiteSkillStore(root / 'skills', profiles, pages)
        self.skill = SiteSkillDraft.model_validate(SKILL)
        self.skill_sha256 = digest(self.skill.model_dump())
        self.store.register(self.skill, confirm_sha256=self.skill_sha256)
        self.plan = SiteSkillValidationPlan.model_validate(copy.deepcopy(PLAN['plan']))

    def test_fixture_schema_parity_and_structure_only_report(self):
        self.assertTrue(PLAN['synthetic'])
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_validation_plan.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SiteSkillValidationPlan.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(self.plan.model_dump())
        self.assertEqual(self.plan.skill_sha256, self.skill_sha256)
        before = (self.store.root / (self.skill_sha256 + '.json')).read_bytes()
        report = preview_skill_validation(self.store, self.plan)
        report_fixture = json.loads((REPO_ROOT / 'examples/site_skill_validation_report.json').read_text())
        self.assertTrue(report_fixture['synthetic'])
        report_schema = json.loads((REPO_ROOT / 'schemas/site_skill_validation_report.schema.json').read_text())
        self.assertEqual({key: value for key, value in report_schema.items() if key != '$schema'},
                         SiteSkillValidationReport.model_json_schema())
        jsonschema.Draft202012Validator(report_schema).validate(report)
        self.assertEqual(report, report_fixture['report'])
        self.assertEqual(report['status'], 'structure_only')
        self.assertEqual(report['plan_sha256'], digest(self.plan.model_dump()))
        self.assertEqual((report['case_count'], report['development_count'], report['held_out_count']),
                         (3, 1, 2))
        self.assertEqual(before, (self.store.root / (self.skill_sha256 + '.json')).read_bytes())
        self.assertTrue(all(report[key] is False for key in
                            ('execution_performed', 'outcomes_verified', 'skill_validated',
                             'reviewed', 'activation_authorized', 'training_ready')))
        self.assertNotIn('cases', report)
        self.assertNotIn('source_variant_sha256', report)

    def test_separate_system2_draft_uses_same_structural_gate_without_s1_credit(self):
        supervisor = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'skill_key': 'find-record-plan', 'model_role': 'system2',
            'candidate_kind': 'workflow_plan', 'source_verification_ids': []})
        supervisor_sha256 = digest(supervisor.model_dump())
        self.store.register(supervisor, confirm_sha256=supervisor_sha256)
        s2_plan = SiteSkillValidationPlan.model_validate({**self.plan.model_dump(),
            'skill_sha256': supervisor_sha256, 'model_role': 'system2'})
        report = preview_skill_validation(self.store, s2_plan)
        self.assertEqual(report['model_role'], 'system2')
        self.assertFalse(report['outcomes_verified'])
        self.assertFalse(report['skill_validated'])
        with self.assertRaises(ValueError):
            preview_skill_validation(self.store, SiteSkillValidationPlan.model_validate({
                **self.plan.model_dump(), 'model_role': 'system2'}))

    def test_wrong_skill_profile_page_task_and_role_fail_closed(self):
        for changes in ({'skill_sha256': '0' * 64}, {'profile_sha256': '0' * 64},
                        {'page_draft_sha256': '0' * 64}, {'task_key': 'update-draft'},
                        {'model_role': 'system2'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                changed = SiteSkillValidationPlan.model_validate({**self.plan.model_dump(), **changes})
                preview_skill_validation(self.store, changed)

    def test_parameterless_draft_cannot_claim_parameter_variation(self):
        parameterless = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'skill_key': 'parameterless-choice', 'parameter_keys': []})
        checksum = digest(parameterless.model_dump())
        self.store.register(parameterless, confirm_sha256=checksum)
        cases = [{**case, 'parameter_keys': []} for case in self.plan.model_dump()['cases']]
        plan = SiteSkillValidationPlan.model_validate({**self.plan.model_dump(),
            'skill_sha256': checksum, 'cases': cases})
        with self.assertRaises(ValueError):
            preview_skill_validation(self.store, plan)

    def test_case_contract_and_held_out_leakage_fail_closed(self):
        baseline = self.plan.model_dump()
        changed_cases = [copy.deepcopy(case) for case in baseline['cases']]
        changed_cases[0]['expected_outcome_key'] = 'different-outcome'
        with self.assertRaises(ValueError):
            preview_skill_validation(self.store, SiteSkillValidationPlan.model_validate({
                **baseline, 'cases': changed_cases}))
        changed_cases = [copy.deepcopy(case) for case in baseline['cases']]
        changed_cases[0]['parameter_keys'] = ['other-parameter']
        with self.assertRaises(ValueError):
            preview_skill_validation(self.store, SiteSkillValidationPlan.model_validate({
                **baseline, 'cases': changed_cases}))
        changed_cases = [copy.deepcopy(case) for case in baseline['cases']]
        changed_cases[2]['parameter_variant_sha256'] = changed_cases[1]['parameter_variant_sha256']
        with self.assertRaises(ValueError):
            preview_skill_validation(self.store, SiteSkillValidationPlan.model_validate({
                **baseline, 'cases': changed_cases}))
        changed_cases = [copy.deepcopy(case) for case in baseline['cases']]
        changed_cases[2]['parameter_variant_sha256'] = baseline['source_variant_sha256'][0]
        with self.assertRaises(ValueError):
            preview_skill_validation(self.store, SiteSkillValidationPlan.model_validate({
                **baseline, 'cases': changed_cases}))
        changed_cases = [copy.deepcopy(case) for case in baseline['cases']]
        changed_cases[2]['cohort'] = 'development'
        with self.assertRaises(ValueError):
            preview_skill_validation(self.store, SiteSkillValidationPlan.model_validate({
                **baseline, 'cases': changed_cases}))

    def test_duplicate_unordered_non_synthetic_or_authority_claims_rejected(self):
        baseline = self.plan.model_dump()
        for changes in ({'synthetic': False}, {'source_variant_sha256': []},
                        {'source_variant_sha256': [baseline['source_variant_sha256'][0]] * 2},
                        {'cases': list(reversed(baseline['cases']))},
                        {'cases': [{**baseline['cases'][0], 'case_key': baseline['cases'][1]['case_key']},
                                   *baseline['cases'][1:]]},
                        {'activation_authorized': True}, {'observed_outcome_key': 'success'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SiteSkillValidationPlan.model_validate({**baseline, **changes})


if __name__ == '__main__':
    unittest.main()

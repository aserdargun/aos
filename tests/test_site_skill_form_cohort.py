from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT, canonical, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import SiteSkillCaseInputs, SiteSkillFormFieldBinding
from aos.site_skill_form_cohort import (SiteSkillFormCohortReport,
                                        SiteSkillFormCohortSelection,
                                        audit_site_skill_form_cohort)
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import WebTaskContract
from aos.web_https_form_state_probe import WebHTTPSFormStatePlan
from aos.web_https_form_transport import WebHTTPSFormPlan, form_body


class SiteSkillFormCohortTests(unittest.TestCase):
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
        self.skill = SiteSkillDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill'])
        self.skill_sha256 = digest(self.skill.model_dump())
        self.store.register(self.skill, confirm_sha256=self.skill_sha256)
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
        self.plan = SiteSkillValidationPlan.model_validate(fixture['plan'])
        self.inputs = SiteSkillCaseInputs.model_validate(fixture['case_inputs'])
        form_fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_case_binding.json').read_text())
        self.task = WebTaskContract.model_validate(form_fixture['task'])
        self.bindings = [SiteSkillFormFieldBinding.model_validate(item)
                         for item in form_fixture['field_bindings']]
        runs = []
        for index, case in enumerate(self.inputs.cases):
            value = case.parameters['record-query']
            body = form_body((('message', value),))
            form_plan = WebHTTPSFormPlan.model_validate({**form_fixture['form_plan'],
                'body_sha256': hashlib.sha256(body).hexdigest(), 'body_bytes': len(body)})
            state_plan = WebHTTPSFormStatePlan.model_validate({
                'profile_sha256': self.plan.profile_sha256,
                'task_sha256': digest(self.task.model_dump()),
                'form_plan_sha256': digest(form_plan.model_dump()),
                'state_url': 'https://crm.example.invalid/app/state',
                'expected_before_sha256': 'a' * 64,
                'expected_after_sha256': digest({'after': value}),
                'marker_id': 'outcome',
                'expected_before_marker_sha256': digest({'text': 'old'}),
                'expected_after_marker_sha256': digest({'text': value}),
                'submitted_field_name': 'message'})
            runs.append({'case_key': case.case_key, 'run_id': f'run-synthetic-{index}',
                         'task': self.task.model_dump(), 'form_plan': form_plan.model_dump(),
                         'state_plan': state_plan.model_dump()})
        self.selection = SiteSkillFormCohortSelection.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'skill_sha256': self.skill_sha256,
            'plan_sha256': digest(self.plan.model_dump()),
            'case_inputs_sha256': digest(self.inputs.model_dump()),
            'field_binding_sha256': digest([item.model_dump() for item in self.bindings]),
            'runs': runs})
        self.database = root / 'synthetic.sqlite'

    @contextmanager
    def audited_snapshot(self, _database):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE verifications (run_id TEXT, verification_id TEXT)')
        for index, item in enumerate(self.selection.runs):
            verification_id = ('verification-test' if index == 0
                               else f'verification-heldout-{index}')
            connection.execute('INSERT INTO verifications VALUES (?,?)',
                               (item.run_id, verification_id))
            connection.execute('INSERT INTO verifications VALUES (?,?)',
                               (item.run_id, f'verification-state-{index}'))
        try:
            yield connection, {'sha256': 'd' * 64}
        finally:
            connection.close()

    def source(self, _database, run_id, **arguments):
        index = next(index for index, item in enumerate(self.selection.runs)
                     if item.run_id == run_id)
        self.assertEqual(arguments['_audited_snapshot'][1]['sha256'], 'd' * 64)
        return {'snapshot_sha256': 'd' * 64, 'run_ref': digest({'run_id': run_id}),
                'requested_roles': ['system1', 'system2'],
                'source_event_ids_by_role': {'system1': [
                    self.skill.source_event_ids[0] if index == 0 else
                    'learning-' + str(index) * 64], 'system2': []},
                'transport_readback_verified': True,
                'declared_state_readback_verified': True}

    def audit(self, *, source=None):
        with (patch('aos.site_skill_form_cohort.audit_snapshot',
                    side_effect=self.audited_snapshot) as audited,
              patch('aos.site_skill_form_cohort.inspect_remote_form_learning_source',
                    side_effect=source or self.source) as inspect):
            report = audit_site_skill_form_cohort(
                self.store, self.plan, self.inputs, self.bindings, self.selection,
                self.database)
        self.assertEqual(audited.call_count, 1)
        self.assertEqual(inspect.call_count, 3)
        return report

    def test_distinct_declared_cases_bind_one_snapshot_without_claiming_skill_success(self):
        report = self.audit()
        self.assertEqual(report['case_count'], 3)
        self.assertEqual((report['development_count'], report['held_out_count']), (1, 2))
        self.assertEqual([item['cohort'] for item in report['cases']],
                         ['development', 'held_out', 'held_out'])
        self.assertTrue(report['source_ids_separated_from_held_out'])
        self.assertNotIn('"parameters":', canonical(report))
        self.assertNotIn('"record-query"', canonical(report))
        self.assertNotIn('https://', canonical(report))
        for field in ('source_parameters_bound', 'skill_executed', 'site_outcome_verified',
                      'held_out_independence_verified', 'skill_validated', 'reviewed',
                      'activation_authorized', 'training_ready'):
            self.assertFalse(report[field])

    def test_canonical_schema_and_synthetic_report_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_cohort.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model, value in (
                ('site_skill_form_cohort_selection', SiteSkillFormCohortSelection,
                 self.selection.model_dump(mode='json')),
                ('site_skill_form_cohort_report', SiteSkillFormCohortReport, fixture['report'])):
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            self.assertEqual({key: item for key, item in schema.items() if key != '$schema'},
                             model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(value)
        with self.assertRaises((ValueError, jsonschema.ValidationError)):
            SiteSkillFormCohortReport.model_validate({**fixture['report'],
                                                       'skill_validated': True})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **fixture['report'], 'activation_authorized': True})

    def test_selection_scope_and_missing_marker_fail_closed(self):
        base = self.selection.model_dump()
        for changed in (
                {**base, 'runs': [base['runs'][0], base['runs'][0], base['runs'][2]]},
                {**base, 'plan_sha256': '0' * 64},
                {**base, 'runs': [base['runs'][1], base['runs'][0], base['runs'][2]]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                selected = SiteSkillFormCohortSelection.model_validate(changed)
                audit_site_skill_form_cohort(self.store, self.plan, self.inputs,
                                             self.bindings, selected, self.database)
        without_marker = [dict(item) for item in base['runs']]
        without_marker[0]['state_plan'] = {
            **without_marker[0]['state_plan'], 'submitted_field_name': None,
            'marker_id': None, 'expected_before_marker_sha256': None,
            'expected_after_marker_sha256': None}
        self.selection = SiteSkillFormCohortSelection.model_validate({**base, 'runs': without_marker})
        with self.assertRaisesRegex(ValueError, 'requires_submitted_marker_binding'):
            self.audit()

    def test_source_overlap_or_missing_verified_source_fails_closed(self):
        original = self.source

        def reused(_database, run_id, **arguments):
            result = original(_database, run_id, **arguments)
            if run_id == self.selection.runs[1].run_id:
                result['source_event_ids_by_role']['system1'] = self.skill.source_event_ids
            return result

        with self.assertRaisesRegex(ValueError, 'held_out_source_overlap'):
            self.audit(source=reused)
        changed_skill = self.skill.model_copy(update={
            'source_event_ids': ['learning-' + 'f' * 64]})
        with patch.object(self.store, 'get', return_value=changed_skill):
            with self.assertRaisesRegex(ValueError, 'held_out_source_overlap'):
                self.audit()

    def test_private_cli_rejects_wrong_exact_selection_without_leaking_values(self):
        source_dir = self.database.parent / 'sources'
        source_dir.mkdir(mode=0o700)
        values = {'plan': self.plan.model_dump(mode='json'),
                  'cases': self.inputs.model_dump(mode='json'),
                  'field-bindings': [item.model_dump(mode='json') for item in self.bindings],
                  'selection': self.selection.model_dump(mode='json')}
        for name, value in values.items():
            source = source_dir / (name + '.json')
            source.write_bytes(canonical(value).encode())
            source.chmod(0o600)
        command = [sys.executable, '-m', 'aos.site_skill_form_cohort',
                   '--database', str(self.database),
                   '--profiles', str(self.store.profiles.root),
                   '--pages', str(self.store.pages.root),
                   '--store', str(self.store.root),
                   '--plan', str(source_dir / 'plan.json'),
                   '--cases', str(source_dir / 'cases.json'),
                   '--field-bindings-file', str(source_dir / 'field-bindings.json'),
                   '--selection', str(source_dir / 'selection.json'),
                   '--selection-sha256', '0' * 64]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('unavailable', result.stderr)
        self.assertNotIn('record-query', result.stderr)
        self.assertNotIn('alpha', result.stderr)


if __name__ == '__main__':
    unittest.main()

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, canonical, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import (SiteSkillCaseBindingReport, SiteSkillCaseInputs,
                                         SiteSkillFormCaseReport, SiteSkillFormFieldBinding,
                                         bind_site_skill_cases, bind_site_skill_form_case,
                                         parameter_variant_sha256)
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import WebTaskContract
from aos.web_https_form_transport import (WebHTTPSFormPlan, form_body,
                                          plan_web_https_form)


FIXTURE = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())


class SiteSkillCaseBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        profiles = WebApplicationProfiles(self.root / 'profiles')
        profile = WebApplicationProfile.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
        profiles.register(profile, confirm_sha256=profile_report(profile).profile_sha256)
        pages = SiteKnowledgeStore(self.root / 'pages', profiles)
        page = SitePageDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_page_draft.json').read_text())['page'])
        pages.register(page, confirm_sha256=digest(page.model_dump()))
        self.store = SiteSkillStore(self.root / 'skills', profiles, pages)
        self.skill = SiteSkillDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill'])
        self.skill_sha256 = digest(self.skill.model_dump())
        self.store.register(self.skill, confirm_sha256=self.skill_sha256)
        self.plan = SiteSkillValidationPlan.model_validate(FIXTURE['plan'])
        self.inputs = SiteSkillCaseInputs.model_validate(FIXTURE['case_inputs'])

    def test_canonical_fixture_binds_case_values_without_authority(self):
        self.assertTrue(FIXTURE['synthetic'])
        for name, model, value in (
                ('inputs', SiteSkillCaseInputs, self.inputs.model_dump()),
                ('report', SiteSkillCaseBindingReport, FIXTURE['report'])):
            schema = json.loads((REPO_ROOT / f'schemas/site_skill_case_{name}.schema.json').read_text())
            self.assertEqual({key: item for key, item in schema.items() if key != '$schema'},
                             model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(value)
        original = (self.store.root / (self.skill_sha256 + '.json')).read_bytes()
        report = bind_site_skill_cases(self.store, self.plan, self.inputs)
        self.assertEqual(report, FIXTURE['report'])
        self.assertEqual(report['case_inputs_sha256'], digest(self.inputs.model_dump()))
        self.assertTrue(report['case_inputs_bound'])
        self.assertFalse(any(report[field] for field in (
            'source_parameters_bound', 'held_out_independence_verified',
            'execution_performed', 'outcomes_verified', 'skill_validated',
            'reviewed', 'activation_authorized', 'training_ready')))
        self.assertEqual(original, (self.store.root / (self.skill_sha256 + '.json')).read_bytes())
        self.assertNotIn('alpha', canonical(report))
        self.assertNotIn('parameters', report)

    def test_changed_values_keys_case_inventory_and_plan_fail_closed(self):
        baseline = self.inputs.model_dump()
        for changed in (
                {**baseline, 'plan_sha256': '0' * 64},
                {**baseline, 'cases': [{**baseline['cases'][0],
                    'parameters': {'record-query': 'changed'}}, *baseline['cases'][1:]]},
                {**baseline, 'cases': [{**baseline['cases'][0],
                    'parameters': {'wrong-key': 'alpha'}}, *baseline['cases'][1:]]},
                {**baseline, 'cases': [{**baseline['cases'][0],
                    'case_key': 'other-case'}, *baseline['cases'][1:]]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                bind_site_skill_cases(self.store, self.plan,
                                      SiteSkillCaseInputs.model_validate(changed))
        changed_plan = self.plan.model_dump()
        changed_plan['cases'][0]['parameter_variant_sha256'] = 'f' * 64
        with self.assertRaises(ValueError):
            bind_site_skill_cases(self.store, SiteSkillValidationPlan.model_validate(changed_plan),
                                  self.inputs)

    def test_input_contract_rejects_noncanonical_or_unsafe_values(self):
        baseline = self.inputs.model_dump()
        for changed in (
                {**baseline, 'synthetic': False},
                {**baseline, 'cases': list(reversed(baseline['cases']))},
                {**baseline, 'cases': [{**baseline['cases'][0],
                    'parameters': {'record-query': ''}}, *baseline['cases'][1:]]},
                {**baseline, 'cases': [{**baseline['cases'][0],
                    'parameters': {'record-query': 'line\nbreak'}}, *baseline['cases'][1:]]},
                {**baseline, 'cases': [{**baseline['cases'][0],
                    'parameters': {'record-query': 'x' * 257}}, *baseline['cases'][1:]]},
                {**baseline, 'cases': [{**baseline['cases'][0],
                    'parameters': {'record-query': 1}}, *baseline['cases'][1:]]},
                {**baseline, 'unexpected': 'private'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                SiteSkillCaseInputs.model_validate(changed)

    def test_system2_plan_is_separate_and_still_unverified(self):
        supervisor = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'skill_key': 'find-record-plan', 'model_role': 'system2',
            'candidate_kind': 'workflow_plan', 'source_verification_ids': []})
        supervisor_sha256 = digest(supervisor.model_dump())
        self.store.register(supervisor, confirm_sha256=supervisor_sha256)
        supervisor_plan = SiteSkillValidationPlan.model_validate({**self.plan.model_dump(),
            'skill_sha256': supervisor_sha256, 'model_role': 'system2'})
        supervisor_inputs = SiteSkillCaseInputs.model_validate({**self.inputs.model_dump(),
            'plan_sha256': digest(supervisor_plan.model_dump())})
        report = bind_site_skill_cases(self.store, supervisor_plan, supervisor_inputs)
        self.assertEqual(report['model_role'], 'system2')
        self.assertFalse(report['source_parameters_bound'])
        self.assertFalse(report['outcomes_verified'])
        with self.assertRaises(ValueError):
            bind_site_skill_cases(self.store, supervisor_plan, self.inputs)

    def test_exact_form_plan_binds_selected_case_values_without_execution(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_case_binding.json').read_text())
        self.assertTrue(fixture['synthetic'])
        task = WebTaskContract.model_validate(fixture['task'])
        form_plan = WebHTTPSFormPlan.model_validate(fixture['form_plan'])
        mappings = [SiteSkillFormFieldBinding.model_validate(item)
                    for item in fixture['field_bindings']]
        self.assertEqual(form_plan, plan_web_https_form(
            self.store.profiles, task, submit_url=form_plan.submit_url,
            receipt_url=form_plan.receipt_url, body_sha256=form_plan.body_sha256,
            body_bytes=form_plan.body_bytes))
        self.assertEqual(form_plan.body_sha256,
                         hashlib.sha256(form_body((('message', 'alpha'),))).hexdigest())
        report = bind_site_skill_form_case(
            self.store, self.plan, self.inputs, 'dev-query', self.store.profiles,
            task, form_plan, mappings)
        self.assertEqual(report, fixture['report'])
        self.assertFalse(any(report[field] for field in (
            'source_parameters_bound', 'page_readback_bound', 'execution_performed',
            'outcomes_verified', 'skill_validated', 'reviewed', 'activation_authorized',
            'training_ready')))
        self.assertNotIn('alpha', canonical(report))
        for name, model, value in (
                ('site_skill_form_field_binding', SiteSkillFormFieldBinding, fixture['field_bindings'][0]),
                ('site_skill_form_case_report', SiteSkillFormCaseReport, report)):
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            self.assertEqual({key: item for key, item in schema.items() if key != '$schema'},
                             model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(value)

    def test_form_case_rejects_wrong_case_scope_mapping_and_body(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_case_binding.json').read_text())
        task = WebTaskContract.model_validate(fixture['task'])
        form_plan = WebHTTPSFormPlan.model_validate(fixture['form_plan'])
        mapping = [SiteSkillFormFieldBinding.model_validate(item)
                   for item in fixture['field_bindings']]

        def bind(*, case='dev-query', selected_task=task, selected_plan=form_plan,
                 selected_mapping=mapping, selected_skill_plan=self.plan):
            return bind_site_skill_form_case(
                self.store, selected_skill_plan, self.inputs, case, self.store.profiles,
                selected_task, selected_plan, selected_mapping)

        with self.assertRaises(ValueError):
            bind(case='heldout-alpha')
        with self.assertRaises(ValueError):
            bind(case='not-a-case')
        with self.assertRaises(ValueError):
            bind(selected_task=WebTaskContract.model_validate({**task.model_dump(),
                'task_key': 'update-draft'}))
        with self.assertRaises(ValueError):
            bind(selected_mapping=[SiteSkillFormFieldBinding(
                parameter_key='not-a-parameter', form_field_name='message')])
        with self.assertRaises(ValueError):
            bind(selected_mapping=[SiteSkillFormFieldBinding(
                parameter_key='record-query', form_field_name='other')])
        with self.assertRaises(ValueError):
            bind(selected_plan=WebHTTPSFormPlan.model_validate({**form_plan.model_dump(),
                'body_sha256': '0' * 64}))
        with self.assertRaises(ValueError):
            bind(selected_plan=WebHTTPSFormPlan.model_validate({**form_plan.model_dump(),
                'submit_url': 'https://other.example.invalid/submit'}))
        with self.assertRaises(ValueError):
            SiteSkillFormFieldBinding(parameter_key='record-query', form_field_name='bad\nfield')
        supervisor = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'skill_key': 'find-record-form-plan', 'model_role': 'system2',
            'candidate_kind': 'workflow_plan', 'source_verification_ids': []})
        supervisor_sha256 = digest(supervisor.model_dump())
        self.store.register(supervisor, confirm_sha256=supervisor_sha256)
        supervisor_plan = SiteSkillValidationPlan.model_validate({**self.plan.model_dump(),
            'skill_sha256': supervisor_sha256, 'model_role': 'system2'})
        supervisor_inputs = SiteSkillCaseInputs.model_validate({**self.inputs.model_dump(),
            'plan_sha256': digest(supervisor_plan.model_dump())})
        with self.assertRaisesRegex(ValueError, 'skill_form_case_scope_mismatch'):
            bind_site_skill_form_case(
                self.store, supervisor_plan, supervisor_inputs, 'dev-query',
                self.store.profiles, task, form_plan, mapping)

    def test_multifield_case_requires_declared_dom_order(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_case_binding.json').read_text())
        task = WebTaskContract.model_validate(fixture['task'])
        skill = SiteSkillDraft.model_validate({**self.skill.model_dump(),
            'skill_key': 'find-record-two-fields',
            'parameter_keys': ['message-text', 'subject-line']})
        skill_sha256 = digest(skill.model_dump())
        self.store.register(skill, confirm_sha256=skill_sha256)
        case_values = [
            {'message-text': 'alpha', 'subject-line': 'one'},
            {'message-text': 'beta', 'subject-line': 'two'},
            {'message-text': 'gamma', 'subject-line': 'three'},
        ]
        plan_cases = [{**original.model_dump(),
                       'parameter_keys': skill.parameter_keys,
                       'parameter_variant_sha256': parameter_variant_sha256(values)}
                      for original, values in zip(self.plan.cases, case_values, strict=True)]
        plan = SiteSkillValidationPlan.model_validate({**self.plan.model_dump(),
            'skill_sha256': skill_sha256, 'cases': plan_cases})
        inputs = SiteSkillCaseInputs.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'plan_sha256': digest(plan.model_dump()),
            'cases': [{'case_key': case.case_key, 'parameters': values}
                      for case, values in zip(self.plan.cases, case_values, strict=True)]})
        fields = (('subject', 'one'), ('message', 'alpha'))
        body = form_body(fields)
        form_plan = plan_web_https_form(
            self.store.profiles, task,
            submit_url='https://crm.example.invalid/app/submit',
            receipt_url='https://crm.example.invalid/app/receipt',
            body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
        mapping = [SiteSkillFormFieldBinding(parameter_key='subject-line', form_field_name='subject'),
                   SiteSkillFormFieldBinding(parameter_key='message-text', form_field_name='message')]
        report = bind_site_skill_form_case(
            self.store, plan, inputs, 'dev-query', self.store.profiles,
            task, form_plan, mapping)
        self.assertEqual(report['field_count'], 2)
        self.assertTrue(report['parameter_values_bound_to_plan'])
        with self.assertRaises(ValueError):
            bind_site_skill_form_case(
                self.store, plan, inputs, 'dev-query', self.store.profiles,
                task, form_plan, list(reversed(mapping)))

    def test_private_cli_exact_selection_and_fixed_failures(self):
        plan_path = self.root / 'case-plan.json'
        cases_path = self.root / 'case-inputs.json'
        plan_path.write_text(canonical(self.plan.model_dump()))
        cases_path.write_text(canonical(self.inputs.model_dump()))
        plan_path.chmod(0o600)
        cases_path.chmod(0o600)
        command = [sys.executable, '-m', 'aos.site_skill_case_binding',
                   '--profiles', str(self.store.profiles.root),
                   '--pages', str(self.store.pages.root),
                   '--store', str(self.store.root), '--plan', str(plan_path),
                   '--plan-sha256', digest(self.plan.model_dump()),
                   '--cases', str(cases_path),
                   '--cases-sha256', digest(self.inputs.model_dump()),
                   '--skill-sha256', self.skill_sha256]

        def invoke(arguments):
            environment = dict(os.environ)
            environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
            return subprocess.run(arguments, cwd=REPO_ROOT, env=environment,
                                  capture_output=True, text=True, timeout=10)

        success = invoke(command)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout), FIXTURE['report'])
        self.assertNotIn('alpha', success.stdout)
        self.assertNotIn('record-query', success.stdout)
        for arguments in ([*command[:-1], '0' * 64],
                          [*command[:-3], '0' * 64, *command[-2:]]):
            failed = invoke(arguments)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(failed.stdout, '')
            self.assertEqual(failed.stderr,
                             'Site skill case binding unavailable: missing, invalid or unsafe source.\n')
        shortcut = self.root / 'case-shortcut.json'
        shortcut.symlink_to(cases_path)
        failed = invoke([*command[:14], str(shortcut), *command[15:]])
        self.assertEqual(failed.returncode, 1)
        shortcut.unlink()
        duplicate = self.root / 'case-duplicate.json'
        os.link(cases_path, duplicate)
        self.assertEqual(invoke(command).returncode, 1)
        duplicate.unlink()
        cases_path.chmod(0o644)
        self.assertEqual(invoke(command).returncode, 1)
        cases_path.chmod(0o600)
        cases_path.write_text(json.dumps(self.inputs.model_dump()))
        self.assertEqual(invoke(command).returncode, 1)
        cases_path.write_text(canonical(self.inputs.model_dump())[:-1] + ',"synthetic":true}')
        self.assertEqual(invoke(command).returncode, 1)
        malformed = invoke([*command, '--unknown'])
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(malformed.stderr,
                         'Site skill case binding unavailable: invalid arguments.\n')


if __name__ == '__main__':
    unittest.main()

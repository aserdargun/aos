import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT
from aos.site_skill_form_execution import (SiteSkillFormExecutionReport,
                                           audit_site_skill_form_execution)


class SiteSkillFormExecutionTests(unittest.TestCase):
    def test_synthetic_report_schema_keeps_authority_closed(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_execution.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_form_execution.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SiteSkillFormExecutionReport.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        state_report = {**fixture['report'], **fixture['state_report_overrides']}
        jsonschema.Draft202012Validator(schema).validate(state_report)
        self.assertEqual(SiteSkillFormExecutionReport.model_validate(state_report).model_dump(),
                         state_report)
        for field in fixture['state_report_overrides']:
            with self.subTest(missing_state_field=field):
                incomplete = {key: value for key, value in state_report.items() if key != field}
                with self.assertRaises((ValueError, jsonschema.ValidationError)):
                    SiteSkillFormExecutionReport.model_validate(incomplete)
                    jsonschema.Draft202012Validator(schema).validate(incomplete)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **fixture['report'], 'submitted_value_readback_bound': True})
        self.assertTrue(fixture['synthetic'])
        self.assertFalse(any(fixture['report'][field] for field in (
            'source_parameters_bound', 'page_readback_bound', 'skill_executed',
            'site_outcome_verified', 'held_out_independence_verified',
            'skill_validated', 'reviewed', 'activation_authorized', 'training_ready')))
        for field in ('source_parameters_bound', 'skill_executed',
                      'site_outcome_verified', 'reviewed', 'training_ready'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                SiteSkillFormExecutionReport.model_validate({**fixture['report'], field: True})

    def test_public_target_cannot_be_reported_as_synthetic(self):
        plan = SimpleNamespace(profile_sha256='a' * 64, cases=[
            SimpleNamespace(case_key='dev-query', cohort='development')])
        profiles = SimpleNamespace(get=lambda _sha256: SimpleNamespace(
            entry_url='https://example.com/entry'))
        with patch('aos.site_skill_form_execution.bind_site_skill_form_case',
                   return_value={}):
            with self.assertRaisesRegex(ValueError, 'requires_development_case'):
                audit_site_skill_form_execution(None, plan, None, 'dev-query',
                                                profiles, None, None, [], None, 'run-id')


if __name__ == '__main__':
    unittest.main()

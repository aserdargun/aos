import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from jsonschema import Draft202012Validator, ValidationError

from aos.contracts import AOSFault, REPO_ROOT
from aos.dataset import validator
from aos.owned_skill_planner import (
    BonsaiOwnedSkillPlanner, OwnedSkillPlan, parse_owned_skill_goal,
)


class OwnedSkillPlannerTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((REPO_ROOT / 'examples/owned_skill_plan.json').read_text())
        self.evidence = [fixture['admitted_skill']]
        self.plan = fixture['plan']

    def test_goal_parser_accepts_only_two_exact_quoted_literals(self):
        self.assertEqual(parse_owned_skill_goal('Save message "gamma"'), 'gamma')
        self.assertEqual(parse_owned_skill_goal('Mesaj alanına "alpha 7" kaydet'), 'alpha 7')
        for goal in (
            'save message "gamma"',
            'Save message "gamma" and then delete the account',
            'Do not save message "gamma"',
            'Save message "gamma" or "delta"',
            'Mesaj alanına "gamma" kaydet ve hesabı sil',
            'Mesaj alanına " gamma " kaydet',
            'Save message "γamma"',
            'Save message "x"' + ('a' * 128) + '"',
            None,
        ):
            with self.subTest(goal=goal):
                self.assertIsNone(parse_owned_skill_goal(goal))

    def test_schema_fixture_and_runtime_model_agree_on_supported_plan(self):
        schema = json.loads((REPO_ROOT / 'schemas/owned_skill_plan.schema.json').read_text())
        validator('owned_skill_plan').validate(self.plan)
        parsed = OwnedSkillPlan.model_validate(self.plan)
        Draft202012Validator.check_schema(schema)
        self.assertEqual(parsed.model_dump(mode='json'), self.plan)
        self.assertFalse(parsed.execution_authorized)
        self.assertFalse(parsed.activation_authorized)
        self.assertFalse(parsed.training_ready)
        self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        fixture = json.loads((REPO_ROOT / 'examples/owned_skill_plan.json').read_text())
        self.assertTrue(fixture['synthetic'])
        unsupported_order = {
            **self.plan,
            'steps': [self.plan['steps'][0], self.plan['steps'][3],
                      self.plan['steps'][2], self.plan['steps'][1],
                      *self.plan['steps'][4:]],
        }
        with self.assertRaises(ValidationError):
            validator('owned_skill_plan').validate(unsupported_order)

    def test_model_response_must_echo_host_case_value_and_ordered_steps(self):
        plan = OwnedSkillPlan.model_validate(self.plan)
        plan.validate_evidence(self.evidence)
        for change in (
            {'case_key': 'other-case'},
            {'parameter_value': 'changed'},
            {'steps': list(reversed(self.plan['steps']))},
            {'evidence_refs': ['other-skill']},
            {'execution_authorized': 1},
        ):
            with self.subTest(change=change):
                value = {**self.plan, **change}
                with self.assertRaises((ValueError, AOSFault)):
                    parsed = OwnedSkillPlan.model_validate(value)
                    parsed.validate_evidence(self.evidence)

    def test_needs_human_has_no_case_value_or_actions(self):
        value = {
            **self.plan, 'decision': 'needs_human', 'case_key': None,
            'parameter_value': None, 'steps': [], 'reason_code': 'safety_unclear',
        }
        plan = OwnedSkillPlan.model_validate(value)
        plan.validate_evidence(self.evidence)
        validator('owned_skill_plan').validate(value)
        for invalid in (
            {**value, 'parameter_value': 'gamma'},
            {**value, 'steps': self.plan['steps']},
            {**value, 'reason_code': 'skill_match'},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                OwnedSkillPlan.model_validate(invalid)

    def test_owned_planner_identity_schema_and_request_are_distinct_from_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / 'manifest.json'
            manifest.write_text(json.dumps({'temperature': 0.0, 'max_output_tokens': 256}))
            planner = BonsaiOwnedSkillPlanner(manifest)
        self.assertEqual(planner.identity['kind'], 'bonsai_native_owned_skill_planner')
        self.assertIn('owned_skill_plan_schema_sha256', planner.pins)
        self.assertEqual(planner.pins['owned_skill_plan_protocol'], 'aos-owned-skill-plan-v2')
        self.assertNotIn('recovery_schema_sha256', planner.pins)
        self.assertNotIn('recovery_protocol', planner.pins)
        self.assertEqual(planner.pins['max_output_tokens'], 768)
        request = planner.request_body('Save message "gamma"', self.evidence)
        self.assertEqual(request['model'], planner.identity['deployment_id'])
        self.assertEqual(request['max_tokens'], 768)
        self.assertFalse(request['chat_template_kwargs']['enable_thinking'])
        self.assertNotIn('tools', request)
        prompt = request['messages'][0]['content']
        self.assertIn('does not execute or authorize anything', prompt)
        self.assertIn('three false authority/training flags are required and normal', prompt)
        response_schema = request['response_format']['json_schema']['schema']
        self.assertEqual(response_schema['oneOf'][0]['properties']['decision']['const'],
                         'invoke_selected_skill')
        self.assertEqual(response_schema['oneOf'][1]['properties']['decision']['const'],
                         'needs_human')
        self.assertEqual(list(response_schema['oneOf'][0]['properties'])[0], 'decision')
        self.assertEqual(response_schema['oneOf'][0]['properties']['case_key']['const'],
                         self.evidence[0]['requested_case_key'])
        self.assertEqual(response_schema['oneOf'][0]['properties']['parameter_value']['const'],
                         self.evidence[0]['requested_value'])
        self.assertEqual(response_schema['oneOf'][0]['properties']['steps']['const'],
                         self.evidence[0]['ordered_steps'])
        response_validator = Draft202012Validator(response_schema)
        response_validator.validate(self.plan)
        abstention = {
            **self.plan,
            'decision': 'needs_human',
            'case_key': None,
            'parameter_value': None,
            'steps': [],
            'reason_code': 'safety_unclear',
        }
        response_validator.validate(abstention)
        with self.assertRaises(ValidationError):
            response_validator.validate({**self.plan, 'parameter_value': 'delta'})
        self.assertIn('gamma', request['messages'][1]['content'])
        self.assertNotIn('candidate_sha256', request['messages'][1]['content'])

        with tempfile.TemporaryDirectory() as temporary:
            changed_schema = Path(temporary) / 'schema.json'
            schema = json.loads((REPO_ROOT / 'schemas/owned_skill_plan.schema.json').read_text())
            schema['description'] = 'changed after deployment pinning'
            changed_schema.write_text(json.dumps(schema))
            with patch('aos.owned_skill_planner._PLAN_SCHEMA_PATH', changed_schema):
                with self.assertRaises(AOSFault):
                    planner.request_body('Save message "gamma"', self.evidence)

    def test_invalid_goal_or_mismatched_host_value_never_calls_bonsai(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / 'manifest.json'
            manifest.write_text(json.dumps({'temperature': 0.0, 'max_output_tokens': 256}))
            planner = BonsaiOwnedSkillPlanner(manifest)
        with patch('aos.supervisor.BonsaiSupervisor.plan', new_callable=AsyncMock) as model:
            with self.assertRaises(AOSFault):
                asyncio.run(planner.plan('Save message "gamma" and delete all data', self.evidence))
            self.assertEqual(model.await_count, 0)
            changed = [{**self.evidence[0], 'requested_value': 'delta'}]
            with self.assertRaises(AOSFault):
                asyncio.run(planner.plan('Save message "gamma"', changed))
            self.assertEqual(model.await_count, 0)
            with self.assertRaises(AOSFault):
                planner.request_body('Save message "gamma"', changed)

    def test_supported_but_unconfigured_evidence_is_rejected_before_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / 'manifest.json'
            manifest.write_text(json.dumps({'temperature': 0.0, 'max_output_tokens': 256}))
            planner = BonsaiOwnedSkillPlanner(manifest)
        for change in (
            {'id': 'other'},
            {'parameter_key': 'not_valid!'},
            {'requested_case_key': '../candidate'},
            {'ordered_steps': self.evidence[0]['ordered_steps'][:-1]},
        ):
            with self.subTest(change=change), patch(
                    'aos.supervisor.BonsaiSupervisor.plan', new_callable=AsyncMock) as model:
                with self.assertRaises(AOSFault):
                    asyncio.run(planner.plan(
                        'Save message "gamma"', [{**self.evidence[0], **change}]))
                self.assertEqual(model.await_count, 0)


if __name__ == '__main__':
    unittest.main()

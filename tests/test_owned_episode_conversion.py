import copy
import json
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.owned_episode_conversion import convert_rows
from aos.owned_skill_planner import (
    BonsaiOwnedSkillPlanner,
    OwnedSkillPlan,
    _planner_response_schema,
)


STEPS = [
    {'step_key': 'open-record', 'operation': 'open_entry'},
    {'step_key': 'read-before', 'operation': 'read_state_before'},
    {'step_key': 'fill-message', 'operation': 'fill_form'},
    {'step_key': 'submit-record', 'operation': 'submit_form'},
    {'step_key': 'check-receipt', 'operation': 'read_receipt'},
    {'step_key': 'check-after', 'operation': 'read_state_after'},
]


def system1_record():
    request = {
        'state': 'Synthetic state with no private content.',
        'question': 'Which bounded action matches the reviewed choice?',
        'options': [
            {'id': 'ask_human', 'label': 'Ask a human'},
            {'id': 'save_record', 'label': 'Save authorized record'},
            {'id': 'inspect', 'label': 'Inspect the form'},
        ],
    }
    return {
        'schema_version': '1.0', 'record_kind': 'owned_episode_system1',
        'candidate_sha256': '1' * 64,
        'source': {'run_ref': '2' * 64, 'step_ref': '3' * 64, 'call_ref': '4' * 64,
                   'decision_ref': '5' * 64, 'state_snapshot_ref': '6' * 64,
                   'planning_bundle_sha256': '7' * 64, 'deployment_id': 'synthetic-decider',
                   'deployment_sha256': '8' * 64},
        'input': {'request': request, 'request_sha256': digest(request)},
        'target': {'selected_option': 'save_record',
                   'probabilities': {'ask_human': 0.1, 'save_record': 0.8, 'inspect': 0.1}},
        'review_receipt_sha256': '9' * 64,
        'target_provenance': 'human_reviewed_exact_prediction',
        'split_group': 'a' * 64, 'split': 'development_only',
        'synthetic': True, 'training_ready': False,
    }


def system2_record(decision='invoke_selected_skill'):
    evidence = [{
        'id': 'admitted-skill', 'skill_key': 'save_record',
        'parameter_key': 'record-query', 'form_field_name': 'message',
        'ordered_steps': STEPS, 'requested_case_key': 'development-alpha',
        'requested_value': 'alpha',
    }]
    pins = {'owned_skill_plan_schema_sha256': digest(json.loads(
        (REPO_ROOT / 'schemas/owned_skill_plan.schema.json').read_text()))}
    planner = BonsaiOwnedSkillPlanner.__new__(BonsaiOwnedSkillPlanner)
    planner.pins = pins
    planner.identity = {'deployment_id': 'bonsai-' + 'd' * 64}
    planner.request_body = lambda goal, supplied: {
        'model': planner.identity['deployment_id'], 'temperature': 0.0,
        'max_tokens': 768, 'stream': False,
        'chat_template_kwargs': {'enable_thinking': False},
        'messages': [
            {'role': 'system', 'content': 'Synthetic constrained planner request.'},
            {'role': 'user', 'content': canonical({'goal': goal, 'admitted_skill': supplied[0]})},
        ],
        'response_format': {'type': 'json_schema', 'json_schema': {
            'name': 'aos_owned_skill_plan', 'strict': True,
            'schema': _planner_response_schema(pins, supplied[0])}},
    }
    request = planner.request_body('Save message "alpha"', evidence)
    if decision == 'invoke_selected_skill':
        target = {
            'schema_version': '1.0', 'decision': decision,
            'evidence_refs': ['admitted-skill'], 'case_key': 'development-alpha',
            'parameter_value': 'alpha', 'steps': STEPS, 'reason_code': 'skill_match',
            'execution_authorized': False, 'activation_authorized': False,
            'training_ready': False,
        }
    else:
        target = {
            'schema_version': '1.0', 'decision': 'needs_human',
            'evidence_refs': ['admitted-skill'], 'case_key': None,
            'parameter_value': None, 'steps': [], 'reason_code': 'safety_unclear',
            'execution_authorized': False, 'activation_authorized': False,
            'training_ready': False,
        }
    target = OwnedSkillPlan.model_validate(target).model_dump(mode='json')
    return {
        'schema_version': '1.0', 'record_kind': 'owned_episode_system2',
        'candidate_sha256': 'b' * 64,
        'source': {'planning_id': 'planning-' + 'c' * 32,
                   'planning_bundle_sha256': 'd' * 64,
                   'deployment_id': planner.identity['deployment_id'],
                   'deployment_sha256': 'e' * 64},
        'input': {'request': request, 'request_sha256': digest(request)},
        'target': target, 'review_receipt_sha256': 'f' * 64,
        'target_provenance': 'human_reviewed_exact_prediction',
        'split_group': 'a' * 64, 'split': 'development_only',
        'synthetic': True, 'training_ready': False,
    }


class OwnedEpisodeConversionTests(unittest.TestCase):
    def test_system1_preserves_option_order_and_maps_reviewed_choice_index(self):
        record = system1_record()
        converted, = convert_rows('system1', [record])
        validator('owned_episode_conversion').validate(converted)
        question = converted['payload']['qs'][0]
        self.assertEqual(question['options'], ['Ask a human', 'Save authorized record', 'Inspect the form'])
        self.assertEqual(question['gold'], 1)
        self.assertEqual(converted['payload']['context'], record['input']['request']['state'])
        self.assertEqual(converted['source_record_sha256'], digest(record))
        self.assertEqual(converted['candidate_sha256'], record['candidate_sha256'])
        self.assertEqual(converted['split'], 'development_only')
        self.assertNotIn('target', converted['payload'])
        self.assertNotIn('review_receipt_sha256', converted['payload'])
        self.assertFalse(converted['training_ready'])
        self.assertFalse(converted['trainer_compatible'])

    def test_system1_is_deterministic_and_rejects_bad_input_target_and_probability(self):
        record = system1_record()
        self.assertEqual(convert_rows('system1', [record]), convert_rows('system1', [record]))
        mutations = []
        changed = copy.deepcopy(record)
        changed['input']['request_sha256'] = '0' * 64
        mutations.append(changed)
        changed = copy.deepcopy(record)
        changed['target']['selected_option'] = 'foreign'
        mutations.append(changed)
        changed = copy.deepcopy(record)
        changed['target']['probabilities']['inspect'] = True
        mutations.append(changed)
        changed = copy.deepcopy(record)
        changed['input']['request']['options'][2]['id'] = 'save_record'
        changed['input']['request_sha256'] = digest(changed['input']['request'])
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                convert_rows('system1', [changed])

    def test_system2_retains_exact_messages_and_separate_structured_target(self):
        record = system2_record()
        converted, = convert_rows('system2', [record])
        validator('owned_episode_conversion').validate(converted)
        self.assertEqual(converted['payload']['format'], 'aos-owned-plan-messages-v1')
        self.assertEqual(converted['payload']['messages'], record['input']['request']['messages'])
        self.assertEqual(converted['payload']['target'], record['target'])
        self.assertEqual([message['role'] for message in converted['payload']['messages']], ['system', 'user'])
        self.assertFalse(converted['trainer_compatible'])
        self.assertFalse(converted['supervisor_tokenizer_verified'])
        self.assertFalse(converted['loss_mask_verified'])
        self.assertNotIn('deployment_id', converted['payload'])
        self.assertNotIn('review_receipt_sha256', converted['payload'])

    def test_system2_retains_an_abstention_without_inventing_authority(self):
        converted, = convert_rows('system2', [system2_record('needs_human')])
        self.assertEqual(converted['payload']['target']['decision'], 'needs_human')
        self.assertIsNone(converted['payload']['target']['parameter_value'])
        self.assertEqual(converted['payload']['target']['steps'], [])

    def test_system2_rejects_request_hash_model_role_schema_and_target_drift(self):
        original = system2_record()
        variants = []
        changed = copy.deepcopy(original)
        changed['input']['request_sha256'] = '0' * 64
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['input']['request']['model'] = 'foreign-model'
        changed['input']['request_sha256'] = digest(changed['input']['request'])
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['input']['request']['messages'].append({'role': 'assistant', 'content': 'extra'})
        changed['input']['request_sha256'] = digest(changed['input']['request'])
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['input']['request']['messages'][1]['content'] = canonical({'goal': 'Save message "beta"',
            'admitted_skill': json.loads(changed['input']['request']['messages'][1]['content'])['admitted_skill']})
        changed['input']['request_sha256'] = digest(changed['input']['request'])
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['input']['request']['response_format']['json_schema']['schema']['oneOf'] = []
        changed['input']['request_sha256'] = digest(changed['input']['request'])
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed['target']['parameter_value'] = 'beta'
        variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                convert_rows('system2', [changed])

    def test_batch_and_fixture_contracts_are_bounded_closed_and_synthetic(self):
        for args in (('system1', []), ('unknown', [system1_record()]),
                     ('system2', [system2_record(), system2_record()])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                convert_rows(*args)
        fixture = json.loads((REPO_ROOT / 'examples/owned_episode_conversion.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        for record in fixture['records']:
            validator('owned_episode_conversion').validate(record)
        malformed = copy.deepcopy(fixture['records'][0])
        malformed['trainer_compatible'] = True
        self.assertFalse(validator('owned_episode_conversion').is_valid(malformed))
        self.assertEqual(convert_rows('system1', [system1_record()])[0]['split_group'], 'a' * 64)


if __name__ == '__main__':
    unittest.main()

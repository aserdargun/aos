"""Pure, pinned conversion of reviewed owned episode rows for development only."""

from copy import deepcopy
import json
import math
import re

from .contracts import Option, Prediction, canonical, digest
from .dataset import validator
from .owned_skill_planner import (
    OwnedSkillPlan,
    _owned_skill_plan_schema,
    _planner_response_schema,
    parse_owned_skill_goal,
)


_HASH = re.compile(r'[a-f0-9]{64}\Z', re.ASCII)
_KEY = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z', re.ASCII)
_VALUE = re.compile(r'[A-Za-z0-9 _.-]{1,128}\Z', re.ASCII)
_FIELD = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z', re.ASCII)
_CASE = re.compile(r'[a-z][a-z0-9-]{0,63}\Z', re.ASCII)
_OPERATIONS = ('open_entry', 'read_state_before', 'fill_form', 'submit_form',
               'read_receipt', 'read_state_after')
_ORDERS = (
    ('open_entry', 'read_state_before', 'fill_form', 'submit_form', 'read_receipt', 'read_state_after'),
    ('open_entry', 'fill_form', 'read_state_before', 'submit_form', 'read_receipt', 'read_state_after'),
)
_PROTOCOL = 'aos-owned-episode-conversion-v1'
_S1_FORMAT = 'decider-example-q-v1'
_S2_FORMAT = 'aos-owned-plan-messages-v1'
_MAX_ROWS = 64


def _invalid(reason='owned_episode_conversion_invalid'):
    raise ValueError(reason)


def _valid_hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _validate_export_row(role, record):
    if role not in {'system1', 'system2'} or not isinstance(record, dict):
        _invalid()
    try:
        validator('owned_episode_export_record').validate(record)
        if (record['record_kind'] != 'owned_episode_' + role
                or record['schema_version'] != '1.0'
                or record['split'] != 'development_only'
                or record['synthetic'] is not True
                or record['training_ready'] is not False
                or record['target_provenance'] != 'human_reviewed_exact_prediction'
                or not _valid_hash(record['candidate_sha256'])
                or not _valid_hash(record['split_group'])):
            _invalid()
        if record['input']['request_sha256'] != digest(record['input']['request']):
            _invalid('owned_episode_conversion_input_hash_changed')
        return record
    except Exception as error:
        if isinstance(error, ValueError) and str(error).startswith('owned_episode_conversion_'):
            raise
        raise ValueError('owned_episode_conversion_invalid_export_row') from None


def _convert_system1(record):
    request = record['input']['request']
    prediction = record['target']
    options = request['options']
    identifiers = [option['id'] for option in options]
    labels = [option['label'] for option in options]
    probabilities = prediction['probabilities']
    if (len(set(identifiers)) != len(identifiers)
            or len(set(labels)) != len(labels)
            or len(identifiers) != len(probabilities)
            or set(identifiers) != set(probabilities)
            or prediction['selected_option'] not in identifiers
            or any(type(value) not in {int, float} or not math.isfinite(value)
                   or value < 0 or value > 1 for value in probabilities.values())
            or not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-6)):
        _invalid('owned_episode_conversion_choice_invalid')
    try:
        parsed = Prediction.model_validate(prediction)
        parsed.validate_options([Option.model_validate(option) for option in options])
    except Exception:
        _invalid('owned_episode_conversion_choice_invalid')
    payload = {
        'context': request['state'],
        'qs': [{
            'text': request['question'],
            'options': labels,
            'gold': identifiers.index(prediction['selected_option']),
        }],
        'task': 'aos-owned-synthetic-form',
    }
    return payload


def _validate_planner_evidence(value):
    expected = {'id', 'skill_key', 'parameter_key', 'form_field_name', 'ordered_steps',
                'requested_case_key', 'requested_value'}
    if not isinstance(value, dict) or set(value) != expected:
        _invalid('owned_episode_conversion_planner_evidence_invalid')
    if (value['id'] != 'admitted-skill'
            or type(value['skill_key']) is not str or _KEY.fullmatch(value['skill_key']) is None
            or type(value['parameter_key']) is not str or _KEY.fullmatch(value['parameter_key']) is None
            or type(value['form_field_name']) is not str or _FIELD.fullmatch(value['form_field_name']) is None
            or type(value['requested_case_key']) is not str or _CASE.fullmatch(value['requested_case_key']) is None
            or type(value['requested_value']) is not str or _VALUE.fullmatch(value['requested_value']) is None
            or not isinstance(value['ordered_steps'], list) or len(value['ordered_steps']) != 6):
        _invalid('owned_episode_conversion_planner_evidence_invalid')
    steps = value['ordered_steps']
    if (any(not isinstance(step, dict) or set(step) != {'step_key', 'operation'}
            or type(step['step_key']) is not str or _KEY.fullmatch(step['step_key']) is None
            or step['operation'] not in _OPERATIONS for step in steps)
            or len({step['step_key'] for step in steps}) != 6
            or tuple(step['operation'] for step in steps) not in _ORDERS):
        _invalid('owned_episode_conversion_planner_evidence_invalid')
    return value


def _convert_system2(record):
    request = record['input']['request']
    source = record['source']
    target = record['target']
    required = {'model', 'temperature', 'max_tokens', 'stream', 'chat_template_kwargs',
                'messages', 'response_format'}
    if (not isinstance(request, dict) or set(request) != required
            or request['model'] != source['deployment_id']
            or type(request['temperature']) not in {int, float}
            or not math.isfinite(request['temperature']) or not 0 <= request['temperature'] <= 2
            or request['max_tokens'] != 768 or request['stream'] is not False
            or request['chat_template_kwargs'] != {'enable_thinking': False}):
        _invalid('owned_episode_conversion_planner_request_invalid')
    messages = request['messages']
    if (not isinstance(messages, list) or len(messages) != 2
            or [message.get('role') if isinstance(message, dict) else None for message in messages]
            != ['system', 'user']
            or any(set(message) != {'role', 'content'} or type(message['content']) is not str
                   or not message['content'] for message in messages)):
        _invalid('owned_episode_conversion_planner_messages_invalid')
    response_format = request['response_format']
    if (not isinstance(response_format, dict) or set(response_format) != {'type', 'json_schema'}
            or response_format['type'] != 'json_schema'
            or not isinstance(response_format['json_schema'], dict)
            or set(response_format['json_schema']) != {'name', 'strict', 'schema'}
            or response_format['json_schema']['name'] != 'aos_owned_skill_plan'
            or response_format['json_schema']['strict'] is not True
            or not isinstance(response_format['json_schema']['schema'], dict)
            or not isinstance(response_format['json_schema']['schema'].get('oneOf'), list)):
        _invalid('owned_episode_conversion_planner_request_invalid')
    try:
        user_request = json.loads(messages[1]['content'])
    except (TypeError, ValueError, RecursionError):
        _invalid('owned_episode_conversion_planner_messages_invalid')
    if (not isinstance(user_request, dict) or set(user_request) != {'goal', 'admitted_skill'}
            or canonical(user_request) != messages[1]['content']):
        _invalid('owned_episode_conversion_planner_messages_invalid')
    evidence = _validate_planner_evidence(user_request['admitted_skill'])
    if parse_owned_skill_goal(user_request['goal']) != evidence['requested_value']:
        _invalid('owned_episode_conversion_planner_goal_mismatch')
    schema_pins = {'owned_skill_plan_schema_sha256': digest(_owned_skill_plan_schema())}
    expected_schema = _planner_response_schema(schema_pins, evidence)
    if response_format['json_schema']['schema'] != expected_schema:
        _invalid('owned_episode_conversion_planner_schema_changed')
    try:
        plan = OwnedSkillPlan.model_validate(target)
    except Exception:
        _invalid('owned_episode_conversion_planner_target_invalid')
    if (plan.evidence_refs != ['admitted-skill']
            or plan.decision == 'invoke_selected_skill'
            and (plan.case_key != evidence['requested_case_key']
                 or plan.parameter_value != evidence['requested_value']
                 or [step.model_dump(mode='json') for step in plan.steps] != evidence['ordered_steps'])):
        _invalid('owned_episode_conversion_planner_target_mismatch')
    return {'format': _S2_FORMAT, 'messages': deepcopy(messages), 'target': deepcopy(target)}


def _convert_one(role, record):
    _validate_export_row(role, record)
    payload = _convert_system1(record) if role == 'system1' else _convert_system2(record)
    upstream = _S1_FORMAT if role == 'system1' else _S2_FORMAT
    result = {
        'schema_version': '1.0',
        'converter_identity': {'protocol': _PROTOCOL, 'schema_version': '1.0',
                               'upstream_format': upstream},
        'source_record_sha256': digest(record),
        'candidate_sha256': record['candidate_sha256'],
        'split_group': record['split_group'],
        'split': 'development_only',
        'role': role,
        'payload': payload,
        'synthetic': True,
        'training_ready': False,
        'supervisor_tokenizer_verified': False,
        'loss_mask_verified': False,
        'trainer_compatible': False,
    }
    validator('owned_episode_conversion').validate(result)
    return result


def convert_rows(role: str, records: list[dict]) -> list[dict]:
    """Convert a bounded homogeneous reviewed export batch without fitting/training."""
    if (role not in {'system1', 'system2'} or not isinstance(records, list)
            or not records or len(records) > _MAX_ROWS
            or role == 'system2' and len(records) != 1):
        _invalid('owned_episode_conversion_batch_invalid')
    converted = [_convert_one(role, record) for record in records]
    if len({item['candidate_sha256'] for item in converted}) != len(converted):
        _invalid('owned_episode_conversion_duplicate_candidate')
    return sorted(converted, key=lambda item: item['candidate_sha256'])

"""Immutable synthetic comparison precommit; never a task execution permit."""

import json

from .contracts import canonical, digest
from .dataset import validator
from .owned_adapter_admission import validate_runtime_admission
from .owned_form_candidate_execution import validate_candidate_case_input
from .site_skill_form_recipe import SUPPORTED_RECIPE_ORDERS


PAIR_OBSERVATION_KIND = 'model.owned_adapter_evaluation'


def _base_preview(adapter_preview):
    preview = {key: value for key, value in adapter_preview.items()
               if key not in {'preview_sha256', 'adapter_admission_sha256'}}
    preview['schema_version'] = '1.3'
    return preview | {'preview_sha256': digest(preview)}


def validate_pair(record):
    try:
        validator('owned_adapter_pair').validate(record)
        admission = validate_runtime_admission(record['adapter_admission'])
        adapter = record['adapter_preview']
        base = record['base_preview']
        arguments = record['arguments']
        authority = record['authority']
        validate_candidate_case_input(arguments['case_key'], arguments['development_value'])
        if (type(authority['generation']) is not int
                or type(admission['generation']) is not int
                or type(record['independent_held_out']) is not int
                or base != _base_preview(adapter)
                or adapter['preview_sha256'] != digest({
                    key: value for key, value in adapter.items() if key != 'preview_sha256'})
                or adapter['adapter_admission_sha256'] != digest(admission)
                or admission['base_preview_sha256'] != base['preview_sha256']
                or admission['case_key'] != arguments['case_key']
                or admission['development_value_sha256'] != digest({'value': arguments['development_value']})
                or record['base_deployment_id'] != admission['runtime_binding']['base_deployment_id']
                or record['adapter_deployment_id'] != admission['adapter_deployment_id']
                or any(record[key] != admission[key] for key in ('episode_id', 'authorization_sha256'))
                or any(authority[key] != admission[key] for key in ('lease_id', 'generation'))
                or any(arguments[key] != base[key] for key in (
                    'candidate_sha256', 'source_run_ref', 'review_sha256', 'release_sha256',
                    'selection_sha256', 'reuse_admission_sha256', 'case_key'))
                or arguments['invocation_sha256'] != base['source_invocation_sha256']
                or tuple(step['operation'] for step in base['steps']) not in SUPPORTED_RECIPE_ORDERS
                or len({step['step_key'] for step in base['steps']}) != 6):
            raise ValueError('binding')
    except Exception:
        raise ValueError('owned_adapter_pair_invalid') from None
    return record


def pair_record(runtime_preview, *, session_id, runtime_id, lease_id, generation):
    try:
        if (not isinstance(runtime_preview, dict)
                or set(runtime_preview) != {'admission', 'adapter_admission_sha256', 'preview', 'arguments', 'started'}
                or runtime_preview['started'] is not False
                or runtime_preview['adapter_admission_sha256'] != digest(runtime_preview['admission'])):
            raise ValueError('preview')
        admission = runtime_preview['admission']
        record = {
            'schema_version': '1.0', 'mode': 'owned_adapter_pair', 'synthetic': True,
            'scope': 'development_only', 'independent_held_out': 0,
            'promotion_authorized': False, 'automatic_second_arm': False,
            'cold_worker_each_arm': True,
            'episode_id': admission['episode_id'],
            'authorization_sha256': admission['authorization_sha256'],
            'authority': {'session_id': session_id, 'runtime_id': runtime_id,
                          'lease_id': lease_id, 'generation': generation},
            'arguments': runtime_preview['arguments'],
            'base_preview': _base_preview(runtime_preview['preview']),
            'adapter_preview': runtime_preview['preview'],
            'adapter_admission': admission,
            'base_deployment_id': admission['runtime_binding']['base_deployment_id'],
            'adapter_deployment_id': admission['adapter_deployment_id'],
        }
        return validate_pair(json.loads(canonical(record)))
    except Exception:
        raise ValueError('owned_adapter_pair_invalid') from None


def pair_observation(record, pair_sha256, arm, *, job_id, run_id, step_id,
                     candidate_execution_sha256):
    validate_pair(record)
    if arm not in ('base', 'adapter') or pair_sha256 != digest(record):
        raise ValueError('owned_adapter_pair_observation_invalid')
    payload = {
        'schema_version': '1.0', 'synthetic': True, 'pair_sha256': pair_sha256,
        'arm': arm, 'preview_sha256': record[arm + '_preview']['preview_sha256'],
        'deployment_id': record[arm + '_deployment_id'], 'job_id': job_id,
        'run_id': run_id, 'step_id': step_id,
        'candidate_execution_sha256': candidate_execution_sha256,
        **record['authority'], 'cold_worker': True,
    }
    try:
        validator('owned_adapter_pair_observation').validate(payload)
    except Exception:
        raise ValueError('owned_adapter_pair_observation_invalid') from None
    return payload

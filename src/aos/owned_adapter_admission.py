"""Source-bound, per-run consent for an experimental owned adapter."""

import json

from .contracts import canonical, digest
from .dataset import validator
from .site_skill_form_invocation_audit import _timestamp


def validate_runtime_admission(admission):
    from .owned_adapter_identity import validate_runtime_binding

    try:
        validator('owned_adapter_runtime_admission').validate(admission)
        binding = admission['runtime_binding']
        validate_runtime_binding(binding)
        if (admission['adapter_deployment_id'] != 'owned-adapter-' + digest(binding)
                or admission['authorization_sha256'] != binding['authorization_sha256']
                or admission['adaptation_report_sha256'] != binding['adaptation_report_sha256']
                or admission['adapter_sha256'] != binding['artifact_sha256']):
            raise ValueError('adapter binding changed')
    except Exception:
        raise ValueError('owned_adapter_runtime_admission_invalid') from None
    return admission


def assert_runtime_execution_binding(admission, *, manifest, reuse_admission,
                                     base_preview, case_key, development_value):
    validate_runtime_admission(admission)
    if (manifest['schema_version'] != '1.5'
            or manifest['adapter_admission_sha256'] != digest(admission)
            or 'planning_bundle_sha256' in manifest
            or base_preview.get('schema_version') != '1.3'
            or 'preview_sha256' in base_preview or 'adapter_admission_sha256' in base_preview
            or admission['base_preview_sha256'] != digest(base_preview)
            or admission['case_key'] != case_key
            or admission['development_value_sha256'] != digest({'value': development_value})
            or any(admission[key] != reuse_admission[key] for key in ('lease_id', 'generation'))):
        raise ValueError('owned_adapter_execution_binding_changed')


def runtime_admission_observation(execution, admission, reuse_admission):
    validate_runtime_admission(admission)
    if (execution['adapter_admission_sha256'] != digest(admission)
            or any(admission[key] != reuse_admission[key] for key in ('lease_id', 'generation'))):
        raise ValueError('owned_adapter_runtime_admission_changed')
    payload = {'schema_version': '1.0', 'synthetic': True,
            **{key: execution[key] for key in (
                'candidate_execution_sha256', 'preview_sha256', 'adapter_admission_sha256',
                'reuse_admission_sha256', 'candidate_sha256', 'invocation_sha256', 'job_id', 'run_id')},
            **{key: reuse_admission[key] for key in (
                'manager_session', 'desktop_session_id', 'runtime_id', 'lease_id', 'generation')},
            'adapter_deployment_id': admission['adapter_deployment_id'],
            'adaptation_report_sha256': admission['adaptation_report_sha256'],
            'adapter_sha256': admission['adapter_sha256'],
            'runtime_binding_sha256': digest(admission['runtime_binding']),
            'experimental_runtime_authorized': True, 'automatic_fallback': False,
            'promotion_authorized': False, 'training_ready': False}
    validator('owned_adapter_runtime_observation').validate(payload)
    return payload


def verify_runtime_admission(connection, execution, admission, reuse_admission):
    from .owned_adapter_identity import verify_adapter_run

    expected = runtime_admission_observation(execution, admission, reuse_admission)
    rows = connection.execute(
        "SELECT * FROM observations WHERE run_id=? AND kind='model.owned_adapter_admission'",
        (execution['run_id'],)).fetchall()
    initial = connection.execute(
        'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=0',
        (execution['run_id'],)).fetchone()
    following = connection.execute(
        'SELECT * FROM state_snapshots WHERE run_id=? AND state_version=1',
        (execution['run_id'],)).fetchone()
    first_action = connection.execute('SELECT MIN(created_at) AS created_at FROM actions WHERE run_id=?',
                                      (execution['run_id'],)).fetchone()
    if (len(rows) != 1 or initial is None or following is None
            or first_action is None or first_action['created_at'] is None):
        return False
    return (rows[0]['action_id'] is None and rows[0]['step_id'] == initial['step_id']
            and rows[0]['payload_json'] == canonical(expected)
            and json.loads(initial['state_json']).get('phase') == 'CREATED'
            and _timestamp(initial['created_at']) <= _timestamp(rows[0]['created_at'])
            <= _timestamp(following['created_at']) <= _timestamp(first_action['created_at'])
            and verify_adapter_run(connection, execution['run_id'], admission))

"""Content-free execution-time admission for a confirmed owned skill proposal."""

import json

from .contracts import canonical, digest
from .dataset import validator
from .owned_skill_planning import validate_planning_bundle
from .site_skill_form_invocation_audit import _timestamp


def planning_admission(execution, bundle):
    validate_planning_bundle(bundle)
    checksum = digest(bundle)
    if execution['planning_bundle_sha256'] != checksum:
        raise ValueError('owned_skill_planning_admission_changed')
    payload = {'schema_version': '1.0', 'synthetic': True,
               **{key: execution[key] for key in (
                   'candidate_execution_sha256', 'preview_sha256', 'planning_bundle_sha256',
                   'reuse_admission_sha256', 'candidate_sha256', 'invocation_sha256')},
               **{key: bundle['authority'][key] for key in (
                   'manager_session', 'desktop_session_id', 'runtime_id', 'lease_id', 'generation')},
               'planning_id': bundle['planning_id'],
               'planning_admission_verified': True, 'user_confirmed_plan_sha256': checksum,
               'execution_authorized': False, 'activation_authorized': False,
               'training_ready': False, 'independent_held_out': False}
    validator('site_skill_owned_planning_admission').validate(payload)
    return payload


def verify_planning_admission(connection, execution, bundle):
    expected = planning_admission(execution, bundle)
    rows = connection.execute(
        "SELECT * FROM observations WHERE run_id=? AND kind='skill.owned_planning_admission'",
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
            <= _timestamp(following['created_at']) <= _timestamp(first_action['created_at']))

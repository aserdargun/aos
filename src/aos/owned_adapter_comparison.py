"""Historical two-run timing; the caller separately proves same-case pairing."""

import json
import math
import sqlite3
from datetime import datetime, timezone

from .contracts import Action, canonical, digest
from .owned_adapter_identity import (PROOF_KIND, validate_adapter_identity,
                                     verify_adapter_inference, verify_adapter_registry)


def _fail():
    raise ValueError('owned_adapter_comparison_unavailable')


def _timestamp(value):
    if type(value) is not str:
        _fail()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            _fail()
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        _fail()


def _run_metrics(connection, run_id, deployment_id, *, adapter_identity=None):
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if run is None or run['status'] != 'succeeded' or run['outcome'] != 'passed':
        _fail()
    try:
        deployment = json.loads(run['deployment_snapshot_json'])
    except (TypeError, json.JSONDecodeError):
        _fail()
    if type(deployment) is not dict or deployment.get('deployment_id') != deployment_id:
        _fail()
    if adapter_identity is not None and deployment != adapter_identity:
        _fail()
    if adapter_identity is None and (deployment.get('kind') != 'decider_native_worker'
            or deployment.get('real_model') is not True or type(deployment.get('pins')) is not dict
            or 'owned_adapter_runtime' in deployment['pins']
            or 'decider-' + digest(deployment['pins']) != deployment_id):
        _fail()
    run_started = _timestamp(run['started_at'])
    run_ended = _timestamp(run['ended_at'])
    if run_ended < run_started:
        _fail()

    calls = connection.execute('SELECT * FROM model_calls WHERE run_id=? ORDER BY created_at,call_id',
                               (run_id,)).fetchall()
    if len(calls) != 6:
        _fail()
    call_ids = set()
    call_latency_sum = 0.0
    for call in calls:
        latency = call['latency_ms']
        if (call['role'] != 'system1' or call['status'] != 'ok'
                or call['deployment_id'] != deployment_id
                or type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0
                or type(call['call_id']) is not str or call['call_id'] in call_ids):
            _fail()
        call_ids.add(call['call_id'])
        call_latency_sum += float(latency)
        if not math.isfinite(call_latency_sum):
            _fail()

    actions = connection.execute('SELECT * FROM actions WHERE run_id=? ORDER BY created_at,action_id',
                                 (run_id,)).fetchall()
    approved_tools = {'browser.form.open', 'browser.form.state_before', 'browser.form.fill',
                      'browser.form.submit', 'browser.form.receipt', 'browser.form.state_after'}
    by_tool = {action['tool']: action for action in actions}
    if len(actions) != 7 or set(by_tool) != approved_tools | {'browser.form.observe'}:
        _fail()
    action_ids = set()
    action_hashes = {}
    action_times = {}
    decision_call_ids = set()
    decision_ids = set()
    calls_by_id = {call['call_id']: call for call in calls}
    action_payloads = {}
    for action_row in actions:
        if action_row['tool'] == 'browser.form.observe':
            continue
        if action_row['status'] != 'ok' or action_row['action_id'] in action_ids:
            _fail()
        envelope = connection.execute('SELECT * FROM action_envelopes WHERE action_id=?',
                                      (action_row['action_id'],)).fetchone()
        if envelope is None:
            _fail()
        try:
            action_payload = json.loads(envelope['envelope_json'])
            action = Action.model_validate(action_payload)
        except Exception:
            _fail()
        action_sha256 = digest(action.model_dump(mode='json'))
        if (action.run_id != run_id or action.action_id != action_row['action_id']
                or action.step_id != action_row['step_id'] or action.tool != action_row['tool']
                or canonical(action.arguments) != action_row['arguments_json']
                or envelope['payload_sha256'] != action_sha256):
            _fail()
        decision = connection.execute('SELECT * FROM decisions WHERE decision_id=?',
                                      (action_row['decision_id'],)).fetchone()
        if (decision is None or decision['decision_id'] in decision_ids
                or decision['run_id'] != run_id or decision['step_id'] != action.step_id
                or decision['call_id'] not in calls_by_id or decision['call_id'] in decision_call_ids
                or decision['selected_option'] != action.selected_option):
            _fail()
        call = calls_by_id[decision['call_id']]
        snapshot = connection.execute('SELECT * FROM state_snapshots WHERE snapshot_id=?',
                                      (decision['snapshot_id'],)).fetchone()
        if (call['step_id'] != action.step_id or snapshot is None
                or snapshot['run_id'] != run_id or snapshot['step_id'] != action.step_id):
            _fail()
        state = json.loads(snapshot['state_json'])
        if (state.get('phase') != 'DECIDE' or state.get('run_id') != run_id
                or state.get('step_id') != action.step_id or state.get('deployment_id') != deployment_id
                or state.get('runtime_id') != action.runtime_id
                or state.get('owner_lease_id') != action.owner_lease_id
                or digest(state) != snapshot['content_sha256']):
            _fail()
        decision_created = _timestamp(decision['created_at'])
        action_created = _timestamp(action_row['created_at'])
        action_completed = _timestamp(action_row['completed_at'])
        if not (run_started <= _timestamp(snapshot['created_at']) <= _timestamp(call['created_at'])
                <= decision_created <= action_created <= action_completed <= run_ended):
            _fail()
        decision_ids.add(decision['decision_id'])
        decision_call_ids.add(decision['call_id'])
        action_ids.add(action.action_id)
        action_hashes[action.action_id] = action_sha256
        action_times[action.action_id] = (decision_created, action_created, action_completed)
        action_payloads[action.action_id] = action
    if decision_call_ids != call_ids:
        _fail()

    receipt = by_tool['browser.form.receipt']
    readback = by_tool['browser.form.observe']
    state_after = by_tool['browser.form.state_after']
    receipt_action = action_payloads[receipt['action_id']]
    readback_envelope = connection.execute('SELECT * FROM action_envelopes WHERE action_id=?',
                                          (readback['action_id'],)).fetchone()
    if readback_envelope is None:
        _fail()
    readback_action = Action.model_validate_json(readback_envelope['envelope_json'])
    if (readback['status'] != 'ok' or readback['decision_id'] != receipt['decision_id']
            or readback_action.action_id != readback['action_id'] or readback_action.run_id != run_id
            or readback_action.step_id != readback['step_id'] or readback_action.tool != readback['tool']
            or canonical(readback_action.arguments) != readback['arguments_json']
            or digest(readback_action.model_dump(mode='json')) != readback_envelope['payload_sha256']
            or any(getattr(readback_action, key) != getattr(receipt_action, key) for key in (
                'task_id', 'step_id', 'runtime_id', 'owner_lease_id', 'state_version', 'selected_option',
                'verification', 'expected_effect'))
            or readback['actual_option'] != receipt['actual_option']
            or readback['actual_option'] != receipt_action.selected_option
            or receipt_action.arguments.get('stage') != 3
            or readback_action.arguments != receipt_action.arguments | {'stage': 4}
            or not (_timestamp(receipt['completed_at']) <= _timestamp(readback['created_at'])
                    <= _timestamp(readback['completed_at']) <= _timestamp(state_after['created_at']))):
        _fail()

    verifications = connection.execute('SELECT * FROM verifications WHERE run_id=?',
                                       (run_id,)).fetchall()
    expected_verifications = {
        (receipt['action_id'], 'independent_https_form_transport_readback'),
        (state_after['action_id'], 'declared_https_form_state_readback'),
    }
    if (len(verifications) != 2
            or {(row['action_id'], row['method']) for row in verifications} != expected_verifications
            or any(row['result'] != 'passed' or row['expected_json'] != row['actual_json']
                   for row in verifications)):
        _fail()

    jobs = connection.execute('SELECT * FROM desktop_tasks WHERE run_id=?', (run_id,)).fetchall()
    if (len(jobs) != 1 or jobs[0]['kind'] != 'browser_remote_form'
            or jobs[0]['status'] != 'succeeded'):
        _fail()
    job_started = _timestamp(jobs[0]['created_at'])
    job_ended = _timestamp(jobs[0]['updated_at'])
    if not job_started <= run_started <= run_ended <= job_ended:
        _fail()
    approvals = connection.execute('SELECT * FROM desktop_approvals WHERE job_id=? ORDER BY created_at,approval_id',
                                   (jobs[0]['job_id'],)).fetchall()
    if len(approvals) != 6 or any(row['status'] != 'consumed' for row in approvals):
        _fail()
    approved_action_ids = set()
    approval_window_sum = 0.0
    previous_completion = run_started
    for approval in approvals:
        try:
            envelope = json.loads(approval['envelope_json'])
            action = Action.model_validate(envelope['action'])
            created = _timestamp(approval['created_at'])
            updated = _timestamp(approval['updated_at'])
        except Exception:
            _fail()
        action_id = action.action_id
        if (action_id not in action_ids or action_id in approved_action_ids
                or approval['action_sha256'] != action_hashes[action_id]
                or digest(action.model_dump(mode='json')) != approval['action_sha256']
                or envelope.get('action_sha256') != approval['action_sha256']
                or envelope.get('job_id') != jobs[0]['job_id']
                or envelope.get('approval_id') != approval['approval_id']):
            _fail()
        decision_created, action_created, action_completed = action_times[action_id]
        if not (run_started <= previous_completion <= created <= updated <= action_created
                <= action_completed <= run_ended <= job_ended) or decision_created > created:
            _fail()
        previous_completion = action_completed
        elapsed_ms = (updated - created).total_seconds() * 1000.0
        if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
            _fail()
        approval_window_sum += elapsed_ms
        if not math.isfinite(approval_window_sum):
            _fail()
        approved_action_ids.add(action_id)
    if approved_action_ids != action_ids:
        _fail()

    proofs = connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=?',
                                (run_id, PROOF_KIND)).fetchall()
    if adapter_identity is None:
        if proofs:
            _fail()
    else:
        if (len(proofs) != 6 or not verify_adapter_registry(connection, adapter_identity)
                or any(not verify_adapter_inference(connection, call, adapter_identity)
                       for call in calls)):
            _fail()
        proof_call_ids = []
        for proof in proofs:
            try:
                payload = json.loads(proof['payload_json'])
            except (TypeError, json.JSONDecodeError):
                _fail()
            if (proof['action_id'] is not None or type(payload) is not dict
                    or payload.get('run_id') != run_id
                    or proof['step_id'] not in {call['step_id'] for call in calls}):
                _fail()
            proof_call_ids.append(payload.get('call_id'))
        if len(set(proof_call_ids)) != 6 or set(proof_call_ids) != call_ids:
            _fail()

    return {
        'run_id': run_id,
        'deployment_id': deployment_id,
        'call_count': 6,
        'action_count': len(actions),
        'consumed_approval_count': 6,
        'adapter_proof_count': 0 if adapter_identity is None else 6,
        'call_latency_sum_ms': call_latency_sum,
        'approval_window_sum_ms': approval_window_sum,
    }


def compare_adapter_runs(connection: sqlite3.Connection, base_run_id: str, adapter_run_id: str,
                         *, base_deployment_id: str, adapter_deployment_id: str) -> dict:
    """Compare joined six-call timings; not same-case, task-quality, or promotion proof."""
    try:
        if (type(base_run_id) is not str or not base_run_id
                or type(adapter_run_id) is not str or not adapter_run_id
                or base_run_id == adapter_run_id
                or type(base_deployment_id) is not str or not base_deployment_id
                or type(adapter_deployment_id) is not str or not adapter_deployment_id
                or base_deployment_id == adapter_deployment_id):
            _fail()
        adapter_row = connection.execute('SELECT deployment_snapshot_json FROM runs WHERE run_id=?',
                                         (adapter_run_id,)).fetchone()
        if adapter_row is None:
            _fail()
        adapter_identity = json.loads(adapter_row['deployment_snapshot_json'])
        binding = validate_adapter_identity(adapter_identity)
        if (adapter_identity['deployment_id'] != adapter_deployment_id
                or binding['base_deployment_id'] != base_deployment_id):
            _fail()
        base = _run_metrics(connection, base_run_id, base_deployment_id)
        adapter = _run_metrics(connection, adapter_run_id, adapter_deployment_id,
                                adapter_identity=adapter_identity)
        return {
            'schema_version': '1.0',
            'synthetic': True,
            'status': 'owned_adapter_pair_comparison_verified',
            'scope': 'two_run_timing_only',
            'base': base,
            'adapter': adapter,
            'adapter_minus_base': {
                'call_latency_sum_ms': adapter['call_latency_sum_ms'] - base['call_latency_sum_ms'],
                'approval_window_sum_ms': adapter['approval_window_sum_ms'] - base['approval_window_sum_ms'],
            },
            'timing_interpretation': {
                'call_latency_sum_ms': 'decision_call_wall_clock_not_inference_only',
                'approval_window_sum_ms': 'approval_window_not_human_wait_only',
            },
            'quality_superiority_verified': False,
            'training_ready': False,
            'promotion_authorized': False,
        }
    except (ValueError, TypeError, KeyError, AttributeError, IndexError,
            sqlite3.Error, json.JSONDecodeError):
        raise ValueError('owned_adapter_comparison_unavailable') from None

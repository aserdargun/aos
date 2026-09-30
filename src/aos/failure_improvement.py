"""Explicit post-failure, metadata-only human guidance; never training authority."""

import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import ErrorCode, Phase, State, TypedModel, canonical, digest
from .dataset_audit import SYNTHETIC_POLICIES, audit_snapshot
from .dataset_reviews import SOURCE_TABLES, source_fingerprint
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _read_private_child, _write_private_child
from .registries import ModelRegistry


Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')]
Count = Annotated[int, Field(ge=0, le=500)]
Correction = Literal['refresh_observation', 'inspect_before_retry',
                     'request_clarification', 'repair_environment']
CORRECTIONS = ('refresh_observation', 'inspect_before_retry',
               'request_clarification', 'repair_environment')
MAX_BYTES = 32768


class CallCounts(TypedModel):
    ok: Count
    error: Count
    timeout: Count
    cancelled: Count


class RoleSummary(TypedModel):
    deployment_id: Identifier | None
    real_model: bool
    calls: CallCounts


class RoleSummaries(TypedModel):
    system1: RoleSummary
    system2: RoleSummary


class ApprovalCounts(TypedModel):
    consumed: Count
    rejected: Count
    expired: Count
    revoked: Count


class ActionCounts(TypedModel):
    ok: Count
    error: Count
    denied: Count
    cancelled: Count


class FailureCandidate(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['failed_task_improvement']
    job_id: Identifier
    session_id: Identifier
    task_kind: Literal['hello', 'browser_form', 'vision_canvas', 'browser_local_navigation',
                       'browser_staging_workflow', 'browser_remote_entry', 'browser_remote_routes',
                       'browser_remote_static_assets', 'browser_remote_form']
    run_ref: Hash
    source_sha256: Hash
    scope_sha256: Hash
    outcome: Literal['failed', 'cancelled']
    failure_code: ErrorCode | None
    synthetic: bool
    model_roles: RoleSummaries
    approval_counts: ApprovalCounts
    action_counts: ActionCounts
    metadata_only: Literal[True]
    retrospective: Literal[True]
    training_ready: Literal[False]
    gold: Literal[False]
    execution_authorized: Literal[False]
    failure_attribution_verified: Literal[False]


class ReviewChoice(TypedModel):
    decision: Literal['accept', 'reject']
    correction_code: Correction | None

    @model_validator(mode='after')
    def correction_matches_decision(self):
        if (self.decision == 'accept') != (self.correction_code is not None):
            raise ValueError('failure_review_choice_invalid')
        return self


class ReviewOption(ReviewChoice):
    confirm_sha256: Hash


class ReviewSummary(ReviewChoice):
    receipt_sha256: Hash
    revoked: bool


class FailureImprovement(TypedModel):
    schema_version: Literal['1.0']
    job_id: Identifier
    available: Literal[True]
    candidate_sha256: Hash
    candidate: FailureCandidate
    saved: bool
    review: ReviewSummary | None
    review_options: list[ReviewOption] = Field(min_length=5, max_length=5)
    source_current: bool


def _check(condition):
    if not condition:
        raise ValueError('failure_improvement_source_unavailable')


def _identifier(value):
    _check(type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value) is not None)


def _hash(value):
    _check(type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None)


def _role(connection, identity, role, calls):
    counts = dict.fromkeys(('ok', 'error', 'timeout', 'cancelled'), 0)
    for call in calls:
        _check(call['status'] in counts)
        counts[call['status']] += 1
    if identity is None:
        _check(not calls)
        return {'deployment_id': None, 'real_model': False, 'calls': counts}
    _check(type(identity) is dict)
    if identity.get('real_model') is False:
        expected = 'fixture-decision-v1' if role == 'system1' else 'fixture-vision-v1'
        _check(identity == {'deployment_id': expected, 'kind': 'deterministic_fixture',
                            'real_model': False} and not calls)
    else:
        _check(identity.get('real_model') is True and isinstance(identity.get('pins'), dict))
        kinds = {'decider_native_worker': 'decider-', 'laya_candidate': 'laya-candidate-',
                 'bonsai_native_supervisor': 'bonsai-'}
        kind = identity.get('kind')
        _check(kind in ({'decider_native_worker', 'laya_candidate', 'owned_episode_adapter_runtime'}
                        if role == 'system1' else {'bonsai_native_supervisor'}))
        if kind == 'owned_episode_adapter_runtime':
            from .owned_adapter_identity import validate_adapter_identity, verify_adapter_registry

            validate_adapter_identity(identity)
            _check(verify_adapter_registry(connection, identity))
        else:
            _check(set(identity) == {'deployment_id', 'kind', 'real_model', 'pins'}
                   and identity['deployment_id'] == kinds[kind] + digest(identity['pins']))
        expected_model = ModelRegistry.experiment_values(identity)
        model = connection.execute('SELECT * FROM models WHERE model_id=?',
                                   (expected_model['model_id'],)).fetchone()
        deployment = connection.execute('SELECT * FROM deployments WHERE deployment_id=?',
                                        (identity['deployment_id'],)).fetchone()
        _check(model is not None and deployment is not None
               and all(model[key] == value for key, value in expected_model.items()
                       if key != 'enabled')
               and deployment['model_id'] == expected_model['model_id']
               and deployment['config_json'] == canonical(identity['pins'])
               and deployment['config_sha256'] == digest(identity['pins']))
        if kind != 'owned_episode_adapter_runtime':
            _check(deployment['adapter_id'] is None)
        for call in calls:
            _check(call['deployment_id'] == identity['deployment_id'])
            step = connection.execute('SELECT run_id FROM steps WHERE step_id=?',
                                      (call['step_id'],)).fetchone()
            _check(step is not None and step['run_id'] == call['run_id'])
            if kind == 'owned_episode_adapter_runtime' and call['status'] == 'ok':
                from .owned_adapter_identity import verify_adapter_inference

                _check(verify_adapter_inference(connection, call, identity))
    return {'deployment_id': identity['deployment_id'], 'real_model': identity['real_model'],
            'calls': counts}


def _derive(database, session_id, job_id):
    try:
        return _derive_snapshot(database, session_id, job_id)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RecursionError) as error:
        raise ValueError('failure_improvement_source_unavailable') from error


def _derive_snapshot(database, session_id, job_id):
    with audit_snapshot(database) as (connection, unused_identity):
        return derive_failure_connection(connection, session_id, job_id)


def derive_failure_connection(connection, session_id, job_id):
    from .desktop_tasks import Approval

    _identifier(session_id)
    _identifier(job_id)
    job = connection.execute('SELECT * FROM desktop_tasks WHERE job_id=? AND session_id=?',
                             (job_id, session_id)).fetchone()
    _check(job is not None and job['status'] in {'failed', 'cancelled'} and job['run_id'])
    run_id = job['run_id']
    for table in SOURCE_TABLES:
        _check(connection.execute(f'SELECT count(*) FROM {table} WHERE run_id=?',
                                  (run_id,)).fetchone()[0] <= 500)
    approvals = connection.execute('SELECT * FROM desktop_approvals WHERE job_id=? '
                                   'ORDER BY approval_id LIMIT 501', (job_id,)).fetchall()
    _check(len(approvals) <= 500)
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    parent = connection.execute('SELECT session_id,runtime_id,image_id FROM desktop_sessions '
                                'WHERE session_id=?', (session_id,)).fetchone()
    runtime = connection.execute('SELECT * FROM runtime_states WHERE run_id=?', (run_id,)).fetchone()
    _check(run is not None and parent is not None and runtime is not None
           and run['status'] == job['status'] and run['ended_at'] is not None)
    state = State.model_validate_json(runtime['state_json'])
    environment = json.loads(run['environment_json'])
    _check(state.run_id == run_id and state.task_id == run['task_id']
           and state.runtime_id == job['runtime_id'] and state.task_kind == job['kind']
           and state.state_version == runtime['state_version']
           and state.phase == (Phase.FAILED if job['status'] == 'failed' else Phase.CANCELLED)
           and type(environment) is dict and environment.get('runtime_id') == state.runtime_id
           and environment.get('parent_runtime_id', parent['runtime_id']) == parent['runtime_id']
           and (job['kind'] != 'hello' or state.runtime_id == parent['runtime_id']))
    _check(run['policy_version'] == job['kind'].replace('_', '-') + '-policy-v1')
    snapshots = connection.execute('SELECT * FROM state_snapshots WHERE run_id=? '
                                   'ORDER BY state_version,rowid', (run_id,)).fetchall()
    _check(bool(snapshots))
    versions = {}
    for snapshot in snapshots:
        captured = State.model_validate_json(snapshot['state_json'])
        step = connection.execute('SELECT run_id FROM steps WHERE step_id=?',
                                  (captured.step_id,)).fetchone()
        _check(captured.run_id == run_id and captured.task_id == state.task_id
               and captured.runtime_id == state.runtime_id and captured.task_kind == state.task_kind
               and captured.deployment_id == state.deployment_id
               and captured.supervisor_deployment_id == state.supervisor_deployment_id
               and captured.state_version == snapshot['state_version']
               and captured.step_id == snapshot['step_id']
               and digest(captured.model_dump(mode='json')) == snapshot['content_sha256']
               and captured.state_version not in versions
               and step is not None and step['run_id'] == run_id)
        versions[captured.state_version] = captured
    _check(versions[max(versions)] == state and sorted(versions) == list(range(len(versions)))
           and versions[0].phase == Phase.CREATED)
    step = connection.execute('SELECT state FROM steps WHERE step_id=?', (state.step_id,)).fetchone()
    _check(step is not None and step['state'] == state.phase.value)
    _check(any(captured.owner_lease_id == job['lease_id'] for captured in versions.values()))
    if state.phase == Phase.FAILED:
        _check(state.last_error is not None and state.owner_lease_id == job['lease_id'])
        failure_code = ErrorCode(state.last_error.code).value
    else:
        _check(state.owner == 'PAUSED')
        failure_code = None
    approval_counts = dict.fromkeys(('consumed', 'rejected', 'expired', 'revoked'), 0)
    for row in approvals:
        approval = Approval.model_validate_json(row['envelope_json'])
        action = approval.action
        _check(row['status'] in approval_counts and approval.approval_id == row['approval_id']
               and approval.job_id == job_id and approval.action_sha256 == row['action_sha256']
               and digest(action.model_dump(mode='json')) == row['action_sha256']
               and action.run_id == run_id and action.runtime_id == state.runtime_id
               and action.task_id == state.task_id and approval.expires_at == row['expires_at']
               and action.state_version in versions
               and action.step_id == versions[action.state_version].step_id
               and action.owner_lease_id == versions[action.state_version].owner_lease_id)
        approval_counts[row['status']] += 1
    action_counts = dict.fromkeys(('ok', 'error', 'denied', 'cancelled'), 0)
    for action in connection.execute('SELECT status FROM actions WHERE run_id=?', (run_id,)):
        _check(action['status'] in action_counts)
        action_counts[action['status']] += 1
    identities = json.loads(run['deployment_snapshot_json'])
    _check(type(identities) is dict)
    system1 = {key: value for key, value in identities.items() if key != 'supervisor'}
    system2 = identities.get('supervisor')
    _check(system1.get('deployment_id') == state.deployment_id
           and system1.get('real_model') is bool(job['real_model'])
           and state.supervisor_deployment_id == (system2.get('deployment_id')
                                                 if type(system2) is dict else None))
    calls = connection.execute('SELECT * FROM model_calls WHERE run_id=?', (run_id,)).fetchall()
    _check(all(call['role'] in {'system1', 'system2'} for call in calls))
    roles = {role: _role(connection, identity, role,
                        [call for call in calls if call['role'] == role])
             for role, identity in (('system1', system1), ('system2', system2))}
    source = digest({'version': 'failed-pilot-v1', 'run_content': source_fingerprint(connection, run_id),
                     'job': dict(job), 'parent': dict(parent),
                     'approvals': [dict(row) for row in approvals], 'identities': identities})
    result = {'schema_version': '1.0', 'kind': 'failed_task_improvement', 'job_id': job_id,
              'session_id': session_id, 'task_kind': state.task_kind,
              'run_ref': digest({'run_id': run_id}), 'source_sha256': source,
              'scope_sha256': digest({'parent': dict(parent), 'runtime_id': state.runtime_id,
                  'lease_id': job['lease_id'], 'generation': job['generation'],
                  'authorized_path': state.authorized_path}),
              'outcome': job['status'], 'failure_code': failure_code,
              'synthetic': run['policy_version'] in SYNTHETIC_POLICIES, 'model_roles': roles,
              'approval_counts': approval_counts, 'action_counts': action_counts,
              'metadata_only': True, 'retrospective': True, 'training_ready': False,
              'gold': False, 'execution_authorized': False, 'failure_attribution_verified': False}
    return FailureCandidate.model_validate_json(canonical(result)).model_dump(mode='json')


def _receipt(candidate_sha256, decision, correction_code):
    choice = ReviewChoice.model_validate({'decision': decision, 'correction_code': correction_code})
    return {'schema_version': '1.0', 'candidate_sha256': candidate_sha256,
            **choice.model_dump(), 'guidance_verified': False, 'training_ready': False,
            'gold': False, 'execution_authorized': False}


class FailureImprovementService:
    def __init__(self, database: Path, directory: Path, session_id: str):
        _identifier(session_id)
        self.database = Path(database)
        self.directory = Path(directory)
        self.session_id = session_id

    def _read(self, descriptor, filename):
        try:
            content = _read_private_child(descriptor, filename, MAX_BYTES)
        except FileNotFoundError:
            return None
        record = json.loads(content)
        _check(content == canonical(record).encode())
        return record

    def _put(self, descriptor, filename, record):
        content = canonical(record).encode()
        _check(len(content) <= MAX_BYTES)
        current = _directory(self.directory, create=False)
        try:
            before, linked = os.fstat(descriptor), os.fstat(current)
            _check((before.st_dev, before.st_ino) == (linked.st_dev, linked.st_ino))
            _write_private_child(descriptor, filename, content)
        finally:
            os.close(current)

    def _saved(self, descriptor, job_id, candidate_sha256):
        _identifier(job_id)
        _hash(candidate_sha256)
        saved = self._read(descriptor, candidate_sha256 + '.json')
        if saved is None:
            return None
        _check(type(saved) is dict and set(saved) == {'candidate', 'consent', 'consent_timing'}
               and saved['consent'] is True and saved['consent_timing'] == 'post_failure')
        candidate = FailureCandidate.model_validate_json(canonical(saved['candidate'])).model_dump(mode='json')
        _check(canonical(candidate) == canonical(saved['candidate'])
               and digest(candidate) == candidate_sha256 and candidate['job_id'] == job_id
               and candidate['session_id'] == self.session_id)
        return candidate

    def _envelope(self, candidate, *, source_current=True, descriptor=None):
        candidate_sha256 = digest(candidate)
        if descriptor is None:
            try:
                descriptor = _directory(self.directory, create=False)
            except FileNotFoundError:
                return self._envelope_empty(candidate, False, None, source_current)
            try:
                return self._envelope(candidate, source_current=source_current, descriptor=descriptor)
            finally:
                os.close(descriptor)
        saved = self._saved(descriptor, candidate['job_id'], candidate_sha256)
        receipt = self._read(descriptor, candidate_sha256 + '-review.json')
        revocation = self._read(descriptor, candidate_sha256 + '-revoke.json')
        summary = None
        if receipt is not None:
            _check(saved is not None and type(receipt) is dict
                   and canonical(receipt) == canonical(_receipt(candidate_sha256, receipt.get('decision'), receipt.get('correction_code'))))
            summary = {'receipt_sha256': digest(receipt), 'decision': receipt['decision'],
                       'correction_code': receipt['correction_code'], 'revoked': revocation is not None}
        if revocation is not None:
            _check(receipt is not None and canonical(revocation) == canonical({'schema_version': '1.0',
                   'candidate_sha256': candidate_sha256, 'receipt_sha256': digest(receipt), 'revoked': True}))
        return self._envelope_empty(candidate, saved is not None, summary, source_current)

    def _envelope_empty(self, candidate, saved, review, source_current):
        candidate_sha256 = digest(candidate)
        options = [{'decision': decision, 'correction_code': correction,
                    'confirm_sha256': digest(_receipt(candidate_sha256, decision, correction))}
                   for decision, correction in [*(('accept', code) for code in CORRECTIONS), ('reject', None)]]
        result = {'schema_version': '1.0', 'job_id': candidate['job_id'], 'available': True,
                  'candidate_sha256': candidate_sha256, 'candidate': candidate, 'saved': saved,
                  'review': review, 'review_options': options, 'source_current': source_current}
        return FailureImprovement.model_validate_json(canonical(result)).model_dump(mode='json')

    def preview(self, job_id):
        return self._envelope(_derive(self.database, self.session_id, job_id))

    def save(self, job_id, confirm_sha256, consent: bool):
        _check(consent is True)
        _hash(confirm_sha256)
        candidate = _derive(self.database, self.session_id, job_id)
        _check(digest(candidate) == confirm_sha256)
        descriptor = _directory(self.directory, create=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _check(_derive(self.database, self.session_id, job_id) == candidate)
            self._envelope(candidate, descriptor=descriptor)
            if self._saved(descriptor, job_id, confirm_sha256) is None:
                self._put(descriptor, confirm_sha256 + '.json', {'candidate': candidate,
                          'consent': True, 'consent_timing': 'post_failure'})
            return self._envelope(candidate, descriptor=descriptor)
        finally:
            os.close(descriptor)

    def review(self, job_id, candidate_sha256, decision, correction_code, confirm_sha256):
        _hash(candidate_sha256)
        _hash(confirm_sha256)
        receipt = _receipt(candidate_sha256, decision, correction_code)
        _check(digest(receipt) == confirm_sha256)
        descriptor = _directory(self.directory, create=False)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            candidate = self._saved(descriptor, job_id, candidate_sha256)
            _check(candidate is not None and _derive(self.database, self.session_id, job_id) == candidate)
            current = self._envelope(candidate, descriptor=descriptor)
            if current['review'] is None:
                self._put(descriptor, candidate_sha256 + '-review.json', receipt)
            else:
                _check(current['review']['receipt_sha256'] == confirm_sha256)
            return self._envelope(candidate, descriptor=descriptor)
        finally:
            os.close(descriptor)

    def revoke(self, job_id, candidate_sha256, receipt_sha256):
        _hash(candidate_sha256)
        _hash(receipt_sha256)
        descriptor = _directory(self.directory, create=False)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            candidate = self._saved(descriptor, job_id, candidate_sha256)
            _check(candidate is not None)
            current = self._envelope(candidate, descriptor=descriptor)
            _check(current['review'] is not None
                   and current['review']['receipt_sha256'] == receipt_sha256)
            if not current['review']['revoked']:
                self._put(descriptor, candidate_sha256 + '-revoke.json', {'schema_version': '1.0',
                          'candidate_sha256': candidate_sha256, 'receipt_sha256': receipt_sha256, 'revoked': True})
            try:
                source_current = _derive(self.database, self.session_id, job_id) == candidate
            except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
                source_current = False
            return self._envelope(candidate, descriptor=descriptor, source_current=source_current)
        finally:
            os.close(descriptor)

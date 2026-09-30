"""One-shot links from reviewed failure guidance to separately authorized fresh tasks."""

import fcntl
from datetime import datetime
import json
import os
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .contracts import Phase, State, TypedModel, canonical, digest, identifier, now
from .dataset_audit import audit_snapshot
from .failure_improvement import (Correction, Hash, Identifier, _check, _hash, _role,
                                  derive_failure_connection)
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _read_private_child, _write_private_child


TaskKind = State.model_fields['task_kind'].annotation
FLAGS = {'manual_approval_required': True, 'guidance_applied': False,
         'causality_verified': False, 'gold': False, 'training_ready': False}
ADMISSION = 'failure_followup_admission'
MARKER = 'failure.followup_admission'
MAX_BYTES = 32768


class FollowupTarget(TypedModel):
    session_id: Identifier
    parent_runtime_id: Identifier
    image_id: str = Field(min_length=1, max_length=128)
    lease_id: Identifier
    generation: int = Field(ge=0)
    task_kind: TaskKind
    configuration_sha256: Hash
    system1_deployment_id: Identifier
    system2_deployment_id: Identifier | None


class TaskContract(TypedModel):
    semantic_sha256: Hash
    binding_sha256: Hash


class FollowupFlags(TypedModel):
    manual_approval_required: Literal[True]
    guidance_applied: Literal[False]
    causality_verified: Literal[False]
    gold: Literal[False]
    training_ready: Literal[False]


class FailureFollowupPreview(FollowupFlags):
    schema_version: Literal['1.0']
    attempt_id: str = Field(pattern=r'^followup-[a-f0-9]{32}$')
    source_job_id: Identifier
    candidate_sha256: Hash
    receipt_sha256: Hash
    source_sha256: Hash
    target: FollowupTarget
    source_contract: TaskContract
    correction_code: Correction
    confirm_sha256: Hash


class FailureFollowupStart(TypedModel):
    schema_version: Literal['1.0']
    job_id: Identifier
    intent_sha256: Hash
    manual_approval_required: Literal[True]


class FollowupOutcome(TypedModel):
    status: Literal['verified', 'not_verified', 'unsupported']
    verification_count: int = Field(ge=0, le=500)
    scope: Literal['fixed_task_outcome', 'transport_readback', 'none']
    reason: Literal['independent_evidence_verified', 'run_not_settled_success',
                    'evidence_missing_or_changed', 'task_contract_unsupported']


class FailureFollowupReport(FollowupFlags):
    schema_version: Literal['1.0']
    job_id: Identifier
    source_job_id: Identifier
    intent_sha256: Hash
    historical_binding_verified: bool
    current_source_valid: bool
    current_review_valid: bool
    outcome: FollowupOutcome


def _typed(model, value):
    checked = model.model_validate_json(canonical(value)).model_dump(mode='json')
    _check(canonical(checked) == canonical(value))
    return checked


def _time(value):
    _check(type(value) is str and len(value) <= 64)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _check(parsed.tzinfo is not None)
    return parsed.timestamp()


def _table_row(connection, table, job_id):
    if connection.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                          ('table', table)).fetchone() is None:
        return None
    rows = connection.execute(f'SELECT * FROM {table} WHERE job_id=? LIMIT 2', (job_id,)).fetchall()
    _check(len(rows) <= 1)
    return rows[0] if rows else None


def task_contract_connection(connection, session_id, job_id):
    from .local_navigation_admission import SyntheticNavigationPin, SyntheticStagingPin
    from .web_application_binding import WebReadOnlyRoutePlan, WebTaskAdmissionDraft
    from .web_https_form_state_probe import WebHTTPSFormStatePlan
    from .web_https_form_transport import WebHTTPSFormPlan
    from .web_readonly_data import WebReadOnlyDataBundlePlan
    from .web_static_assets import WebStaticAssetPlan

    job = connection.execute('SELECT * FROM desktop_tasks WHERE session_id=? AND job_id=?',
                             (session_id, job_id)).fetchone()
    _check(job is not None and job['run_id'] is not None)
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (job['run_id'],)).fetchone()
    initial = connection.execute('SELECT * FROM state_snapshots WHERE run_id=? AND state_version=0 LIMIT 2',
                                 (job['run_id'],)).fetchall()
    _check(run is not None and len(initial) == 1)
    state = State.model_validate_json(initial[0]['state_json'])
    _check(state.phase == Phase.CREATED and state.state_version == 0
           and state.run_id == job['run_id'] and state.task_id == run['task_id']
           and state.runtime_id == job['runtime_id'] and state.task_kind == job['kind']
           and state.step_id == initial[0]['step_id']
           and digest(state.model_dump(mode='json')) == initial[0]['content_sha256'])
    semantic = {key: state.model_dump(mode='json')[key] for key in (
        'task_kind', 'original_goal', 'normalized_goal', 'authorized_path', 'authorized_content',
        'success_criteria', 'skill_invocation_sha256', 'max_attempts')}
    bindings = {}
    parent = connection.execute('SELECT runtime_id,image_id FROM desktop_sessions WHERE session_id=?',
                                (session_id,)).fetchone()
    _check(parent is not None)

    def bound(row):
        _check(row['run_id'] == state.run_id and row['browser_runtime_id'] == state.runtime_id)

    local = _table_row(connection, 'desktop_web_profile_bindings', job_id)
    if local is not None:
        _check(state.task_kind in {'browser_local_navigation', 'browser_staging_workflow'})
        bound(local)
        model = SyntheticNavigationPin if state.task_kind == 'browser_local_navigation' else SyntheticStagingPin
        pin = _typed(model, json.loads(local['pin_json']))
        _check(digest(pin) == local['pin_sha256'] and pin['profile_sha256'] == local['profile_sha256']
               and pin['task_key'] == local['task_key'] and pin['parent_runtime_id'] == parent['runtime_id']
               and pin['image_id'] == parent['image_id'])
        bindings['local_profile'] = digest(pin)
    tables = {'browser_remote_entry': 'desktop_remote_entry_bindings',
              'browser_remote_routes': 'desktop_remote_route_bindings',
              'browser_remote_static_assets': 'desktop_remote_static_asset_bindings',
              'browser_remote_form': 'desktop_remote_form_bindings'}
    for kind, table in tables.items():
        row = _table_row(connection, table, job_id)
        if kind != state.task_kind:
            _check(row is None)
            continue
        _check(row is not None)
        bound(row)
        draft = _typed(WebTaskAdmissionDraft, json.loads(row['draft_json']))
        _check(all(draft[key] == row[key] for key in ('profile_sha256', 'binding_sha256', 'runtime_sha256'))
               and draft['runtime']['runtime_id'] == state.runtime_id
               and draft['runtime']['parent_runtime_id'] == parent['runtime_id']
               and draft['runtime']['image_id'] == parent['image_id'])
        bindings['task'] = {'profile_sha256': draft['profile_sha256'], 'task_sha256': draft['task_sha256']}
        if kind == 'browser_remote_entry':
            _check(state.authorized_content == draft['binding_sha256'])
            semantic['authorized_content'] = digest(bindings['task'])
        else:
            plan = json.loads(row['plan_json'])
            model = (WebReadOnlyRoutePlan if kind == 'browser_remote_routes' else
                     WebHTTPSFormPlan if kind == 'browser_remote_form' else
                     WebReadOnlyDataBundlePlan if 'data_resources' in plan else WebStaticAssetPlan)
            plan = _typed(model, plan)
            _check(digest(plan) == row['plan_sha256'] == state.authorized_content
                   and plan['profile_sha256'] == draft['profile_sha256']
                   and plan['task_sha256'] == draft['task_sha256'])
            bindings['plan_sha256'] = row['plan_sha256']
    state_binding = _table_row(connection, 'desktop_remote_form_state_bindings', job_id)
    if state_binding is not None:
        _check(state.task_kind == 'browser_remote_form')
        bound(state_binding)
        plan = _typed(WebHTTPSFormStatePlan, json.loads(state_binding['state_plan_json']))
        _check(digest(plan) == state_binding['state_plan_sha256']
               and plan['form_plan_sha256'] == state_binding['form_plan_sha256'] == bindings['plan_sha256'])
        bindings['state_plan_sha256'] = state_binding['state_plan_sha256']
    cookie = _table_row(connection, 'desktop_remote_form_cookie_bindings', job_id)
    if cookie is not None:
        _check(state.task_kind == 'browser_remote_form')
        bound(cookie)
        _check(cookie['form_plan_sha256'] == bindings['plan_sha256'])
        _hash(cookie['cookie_sha256'])
        bindings['cookie_sha256'] = cookie['cookie_sha256']
    return {'semantic_sha256': digest(semantic), 'binding_sha256': digest(bindings)}


class FailureFollowupService:
    def __init__(self, failure_service):
        self.failures = failure_service
        self.directory = failure_service.directory.parent / 'failure-followups'

    def _source(self, connection, job_id, candidate_sha256, receipt_sha256):
        candidate = derive_failure_connection(connection, self.failures.session_id, job_id)
        _check(digest(candidate) == candidate_sha256)
        envelope = self.failures._envelope(candidate)
        review = envelope['review']
        _check(envelope['saved'] and review is not None and review['decision'] == 'accept'
               and not review['revoked'] and review['receipt_sha256'] == receipt_sha256)
        return candidate, review, task_contract_connection(connection, self.failures.session_id, job_id)

    def _authority(self, connection, target):
        target = _typed(FollowupTarget, target)
        _check(target['session_id'] == self.failures.session_id)
        parent = connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?',
                                    (target['session_id'],)).fetchone()
        _check(parent is not None and parent['runtime_id'] == target['parent_runtime_id']
               and parent['image_id'] == target['image_id'] and parent['lease_id'] == target['lease_id']
               and parent['generation'] == target['generation'] and parent['owner'] == 'AGENT'
               and parent['status'] == 'running')
        return target

    def _check_preview(self, connection, preview, target):
        checked = _typed(FailureFollowupPreview, preview)
        _check(checked['confirm_sha256'] == digest({key: value for key, value in checked.items()
                                                   if key != 'confirm_sha256'}))
        _check(canonical(checked['target']) == canonical(self._authority(connection, target)))
        candidate, review, contract = self._source(connection, checked['source_job_id'],
                                                    checked['candidate_sha256'], checked['receipt_sha256'])
        _check(candidate['task_kind'] == target['task_kind'] and candidate['source_sha256'] == checked['source_sha256']
               and review['correction_code'] == checked['correction_code'] and contract == checked['source_contract'])
        return checked

    def preview(self, source_job_id, candidate_sha256, receipt_sha256, target):
        with audit_snapshot(self.failures.database) as (connection, unused_identity):
            target = self._authority(connection, target)
            candidate, review, contract = self._source(connection, source_job_id, candidate_sha256, receipt_sha256)
            _check(candidate['task_kind'] == target['task_kind'])
            result = {'schema_version': '1.0', 'attempt_id': 'followup-' + uuid4().hex,
                      'source_job_id': source_job_id, 'candidate_sha256': candidate_sha256,
                      'receipt_sha256': receipt_sha256, 'source_sha256': candidate['source_sha256'],
                      'target': target, 'source_contract': contract, 'correction_code': review['correction_code'], **FLAGS}
            result['confirm_sha256'] = digest(result)
            return _typed(FailureFollowupPreview, result)

    def _read(self, descriptor, filename):
        content = _read_private_child(descriptor, filename, MAX_BYTES)
        record = json.loads(content)
        _check(content == canonical(record).encode())
        return record

    def _intent(self, intent_sha256):
        _hash(intent_sha256)
        descriptor = _directory(self.directory, create=False)
        try:
            intent = self._read(descriptor, 'intent-' + intent_sha256 + '.json')
            _check(type(intent) is dict and set(intent) == {'schema_version', 'preview', 'consent'}
                   and intent['schema_version'] == '1.0' and intent['consent'] is True
                   and digest(intent) == intent_sha256)
            preview = _typed(FailureFollowupPreview, intent['preview'])
            _check(preview['confirm_sha256'] == digest({key: value for key, value in preview.items()
                                                       if key != 'confirm_sha256'}))
            _check(preview['target']['session_id'] == self.failures.session_id
                   and canonical(self._read(descriptor, preview['attempt_id'] + '.json')) == canonical(intent))
            return intent
        finally:
            os.close(descriptor)

    def commit(self, preview, confirm_sha256, consent, target):
        _check(consent is True)
        with audit_snapshot(self.failures.database) as (connection, unused_identity):
            preview = self._check_preview(connection, preview, target)
            _check(confirm_sha256 == preview['confirm_sha256'])
        intent = {'schema_version': '1.0', 'preview': preview, 'consent': True}
        intent_sha256 = digest(intent)
        content = canonical(intent).encode()
        _check(len(content) <= MAX_BYTES)
        descriptor = _directory(self.directory, create=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current = _directory(self.directory, create=False)
            try:
                pinned, linked = os.fstat(descriptor), os.fstat(current)
                _check((pinned.st_dev, pinned.st_ino) == (linked.st_dev, linked.st_ino))
            finally:
                os.close(current)
            _write_private_child(descriptor, preview['attempt_id'] + '.json', content)
            _write_private_child(descriptor, 'intent-' + intent_sha256 + '.json', content)
        finally:
            os.close(descriptor)
        return {'intent_sha256': intent_sha256, 'intent': intent}

    def _event_payload(self, intent_sha256, preview, job_id):
        return {'schema_version': '1.0', 'intent_sha256': intent_sha256, 'job_id': job_id,
                'source_job_id': preview['source_job_id'], 'attempt_id': preview['attempt_id'],
                'candidate_sha256': preview['candidate_sha256'], 'receipt_sha256': preview['receipt_sha256'],
                'target': preview['target'], **FLAGS}

    def admit(self, store, intent_sha256, job_id, target):
        connection = store.connection
        _check(connection.in_transaction)
        intent = self._intent(intent_sha256)
        preview = self._check_preview(connection, intent['preview'], target)
        job = connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        _check(job is not None and job['run_id'] is None and job['status'] == 'queued'
               and job_id != preview['source_job_id'])
        self._job_authority(job, target)
        existing = connection.execute('SELECT 1 FROM desktop_events WHERE kind=? AND '
                                      '(json_extract(payload_json,\'$.intent_sha256\')=? OR '
                                      'json_extract(payload_json,\'$.job_id\')=?) LIMIT 1',
                                      (ADMISSION, intent_sha256, job_id)).fetchone()
        _check(existing is None)
        store.insert('desktop_events', event_id=identifier('event'), session_id=target['session_id'],
                     kind=ADMISSION, payload_json=canonical(self._event_payload(intent_sha256, preview, job_id)),
                     created_at=now())
        return {'intent_sha256': intent_sha256, 'source_job_id': preview['source_job_id']}

    def _job_authority(self, job, target):
        _check(all(job[key] == target[key] for key in ('session_id', 'lease_id', 'generation'))
               and job['kind'] == target['task_kind'])

    def _association(self, connection, intent_sha256, preview, job_id):
        job = connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        _check(job is not None and job_id != preview['source_job_id'])
        self._job_authority(job, preview['target'])
        events = connection.execute('SELECT * FROM desktop_events WHERE kind=? AND '
                                    '(json_extract(payload_json,\'$.intent_sha256\')=? OR '
                                    'json_extract(payload_json,\'$.job_id\')=?) LIMIT 3',
                                    (ADMISSION, intent_sha256, job_id)).fetchall()
        _check(len(events) == 1 and events[0]['session_id'] == self.failures.session_id
               and events[0]['payload_json'] == canonical(self._event_payload(intent_sha256, preview, job_id))
               and _time(job['created_at']) <= _time(events[0]['created_at']))
        return job, events[0]

    def _marker(self, intent_sha256, preview, job_id, state):
        return {**self._event_payload(intent_sha256, preview, job_id), 'run_id': state.run_id,
                'step_id': state.step_id, 'runtime_id': state.runtime_id,
                'created_state_sha256': digest(state.model_dump(mode='json')),
                'source_contract': preview['source_contract']}

    def created(self, store, intent_sha256, job_id, state, target):
        connection = store.connection
        _check(connection.in_transaction)
        intent = self._intent(intent_sha256)
        preview = self._check_preview(connection, intent['preview'], target)
        job, event = self._association(connection, intent_sha256, preview, job_id)
        _check(state.phase == Phase.CREATED and state.state_version == 0
               and state.run_id == job['run_id'] and state.runtime_id == job['runtime_id']
               and state.owner_lease_id == target['lease_id'] and state.owner == 'AGENT'
               and state.deployment_id == target['system1_deployment_id']
               and state.supervisor_deployment_id == target['system2_deployment_id']
               and task_contract_connection(connection, target['session_id'], job_id) == preview['source_contract'])
        run = connection.execute('SELECT started_at FROM runs WHERE run_id=?', (state.run_id,)).fetchone()
        _check(run is not None and _time(event['created_at']) <= _time(run['started_at']))
        for table in ('model_calls', 'actions'):
            _check(connection.execute(f'SELECT 1 FROM {table} WHERE run_id=? LIMIT 1',
                                      (state.run_id,)).fetchone() is None)
        _check(connection.execute('SELECT 1 FROM observations WHERE run_id=? AND kind=? LIMIT 1',
                                  (state.run_id, MARKER)).fetchone() is None)
        store.insert('observations', observation_id=identifier('observation'), run_id=state.run_id,
                     step_id=state.step_id, action_id=None, kind=MARKER,
                     payload_json=canonical(self._marker(intent_sha256, preview, job_id, state)), created_at=now())

    def _historical(self, connection, intent_sha256, preview, job_id):
        job, event = self._association(connection, intent_sha256, preview, job_id)
        if job['run_id'] is None:
            return job, False
        initial = connection.execute('SELECT * FROM state_snapshots WHERE run_id=? AND state_version=0 LIMIT 2',
                                     (job['run_id'],)).fetchall()
        _check(len(initial) == 1)
        state = State.model_validate_json(initial[0]['state_json'])
        target = preview['target']
        _check(state.phase == Phase.CREATED and state.run_id == job['run_id']
               and state.runtime_id == job['runtime_id'] and state.owner_lease_id == target['lease_id']
               and state.owner == 'AGENT'
               and (state.task_kind != 'hello' or state.runtime_id == target['parent_runtime_id'])
               and state.deployment_id == target['system1_deployment_id']
               and state.supervisor_deployment_id == target['system2_deployment_id']
               and task_contract_connection(connection, target['session_id'], job_id) == preview['source_contract'])
        run = connection.execute('SELECT * FROM runs WHERE run_id=?', (state.run_id,)).fetchone()
        _check(run is not None)
        environment = json.loads(run['environment_json'])
        parent = connection.execute('SELECT runtime_id,image_id FROM desktop_sessions WHERE session_id=?',
                                    (target['session_id'],)).fetchone()
        _check(type(environment) is dict and environment.get('runtime_id') == state.runtime_id
               and environment.get('parent_runtime_id', target['parent_runtime_id']) == target['parent_runtime_id']
               and parent is not None and parent['runtime_id'] == target['parent_runtime_id']
               and parent['image_id'] == target['image_id'])
        identities = json.loads(run['deployment_snapshot_json'])
        _check(type(identities) is dict)
        system1 = {key: value for key, value in identities.items() if key != 'supervisor'}
        system2 = identities.get('supervisor')
        _check(system1.get('deployment_id') == target['system1_deployment_id']
               and (system2.get('deployment_id') if type(system2) is dict else None)
               == target['system2_deployment_id'])
        calls = connection.execute('SELECT * FROM model_calls WHERE run_id=? LIMIT 501', (state.run_id,)).fetchall()
        _check(len(calls) <= 500 and all(call['role'] in {'system1', 'system2'} for call in calls))
        for role, identity in (('system1', system1), ('system2', system2)):
            _role(connection, identity, role, [call for call in calls if call['role'] == role])
        _check(bool(job['real_model']) == (system1['real_model'] and (
            job['kind'] != 'vision_canvas' or system2 is not None and system2['real_model'])))
        markers = connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=? LIMIT 2',
                                     (state.run_id, MARKER)).fetchall()
        if not markers:
            return job, False
        _check(len(markers) == 1 and markers[0]['action_id'] is None and markers[0]['step_id'] == state.step_id
               and markers[0]['payload_json'] == canonical(self._marker(intent_sha256, preview, job_id, state))
               and _time(event['created_at']) <= _time(run['started_at'])
               <= _time(initial[0]['created_at']) <= _time(markers[0]['created_at']))
        for table in ('model_calls', 'actions'):
            rows = connection.execute(f'SELECT created_at FROM {table} WHERE run_id=? LIMIT 501',
                                      (state.run_id,)).fetchall()
            _check(len(rows) <= 500 and all(_time(markers[0]['created_at']) <= _time(row['created_at'])
                                            for row in rows))
        return job, True

    def guard(self, connection, intent_sha256, job_id, target):
        intent = self._intent(intent_sha256)
        preview = self._check_preview(connection, intent['preview'], target)
        job, verified = self._historical(connection, intent_sha256, preview, job_id)
        _check(verified and job['status'] in {'running', 'waiting_approval'})

    def inspect(self, job_id):
        with audit_snapshot(self.failures.database) as (connection, unused_identity):
            return self.inspect_connection(connection, job_id)

    def inspect_connection(self, connection, job_id):
        from .failure_followup_outcome import audit_followup_outcome

        events = connection.execute('SELECT payload_json FROM desktop_events WHERE session_id=? AND kind=? '
                                    'AND json_extract(payload_json,\'$.job_id\')=? LIMIT 2',
                                    (self.failures.session_id, ADMISSION, job_id)).fetchall()
        _check(len(events) == 1)
        intent_sha256 = json.loads(events[0]['payload_json'])['intent_sha256']
        intent = self._intent(intent_sha256)
        preview = intent['preview']
        job, historical = self._historical(connection, intent_sha256, preview, job_id)
        source_valid = review_valid = False
        try:
            candidate = derive_failure_connection(connection, self.failures.session_id, preview['source_job_id'])
            source_valid = (digest(candidate) == preview['candidate_sha256']
                            and task_contract_connection(connection, self.failures.session_id,
                                                         preview['source_job_id']) == preview['source_contract'])
        except (ValueError, OSError, TypeError, KeyError, sqlite3.Error):
            pass
        try:
            descriptor = _directory(self.failures.directory, create=False)
            try:
                candidate = self.failures._saved(descriptor, preview['source_job_id'], preview['candidate_sha256'])
                _check(candidate is not None)
                review = self.failures._envelope(candidate, descriptor=descriptor)['review']
                review_valid = (review is not None and review['decision'] == 'accept' and not review['revoked']
                                and review['receipt_sha256'] == preview['receipt_sha256'])
            finally:
                os.close(descriptor)
        except (ValueError, OSError, TypeError, KeyError):
            pass
        outcome = (audit_followup_outcome(connection, job['run_id']) if historical else
                   {'status': 'not_verified', 'verification_count': 0, 'scope': 'none',
                    'reason': 'evidence_missing_or_changed'})
        return _typed(FailureFollowupReport, {'schema_version': '1.0', 'job_id': job_id,
            'source_job_id': preview['source_job_id'], 'intent_sha256': intent_sha256,
            'historical_binding_verified': historical, 'current_source_valid': source_valid,
            'current_review_valid': review_valid, 'outcome': outcome, **FLAGS})

"""One-task, consented S1 document context; reviewed evidence is never authority."""

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .contracts import Option, Phase, Prediction, State, TypedModel, canonical, digest, identifier, now
from .dataset_audit import audit_snapshot
from .decision import decision_request
from .failure_followup import FollowupOutcome, FollowupTarget, TaskKind, _time
from .failure_improvement import _role
from .knowledge import (EvidenceFlags, Hash, Identifier, KnowledgeScope, KnowledgeSearch,
                        SearchRequest, UTC_PATTERN, _utc, text_sha256, validate_knowledge_retrieval)
from .knowledge_answer import StoreIdentity, _identity
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _read_private_child, _write_private_child
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


VERSION = 'aos-task-knowledge-v1'
PREFIX = '\nUntrusted reviewed document context (' + VERSION + '):\n'
ADMISSION = 'task_knowledge_admission'
MARKER = 'task.knowledge_context'
DISPATCH = 'task.knowledge_dispatch'
MAX_BYTES = 131072
FLAGS = {'untrusted': True, 'execution_authorized': False, 'training_ready': False, 'gold': False,
         'manual_approval_required': True, 'causality_verified': False, 'scope_authorization_verified': False}


def _check(condition):
    if not condition:
        raise ValueError('task_knowledge_invalid')


def _typed(model, value):
    try:
        parsed = model.model_validate_json(canonical(value)).model_dump(mode='json')
        _check(canonical(parsed) == canonical(value))
        return parsed
    except Exception:
        raise ValueError('task_knowledge_invalid') from None


class TaskKnowledgeFlags(EvidenceFlags):
    manual_approval_required: Literal[True]
    causality_verified: Literal[False]
    scope_authorization_verified: Literal[False]

    @field_validator('manual_approval_required', 'causality_verified',
                     'scope_authorization_verified', mode='before')
    @classmethod
    def exact_flags(cls, value):
        if type(value) is not bool:
            raise ValueError('task_knowledge_exact_boolean_required')
        return value


class TaskKnowledgePreview(TaskKnowledgeFlags):
    schema_version: Literal['1.0']
    kind: Literal['task_knowledge_preview']
    preview_id: str = Field(pattern=r'^knowledge-preview-[a-f0-9]{32}$')
    expires_at: str = Field(pattern=UTC_PATTERN, json_schema_extra={'format': 'date-time'})
    scope: KnowledgeScope
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=4)
    context_chars: int = Field(ge=256, le=2048)
    retrieval: KnowledgeSearch
    target: FollowupTarget
    workspace_identity: WorkspaceIdentity
    store_identity: StoreIdentity
    context_version: Literal['aos-task-knowledge-v1']
    context_text: str = Field(min_length=1, max_length=8192)
    context_sha256: Hash
    confirm_sha256: Hash


class TaskKnowledgeIntent(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['task_knowledge_intent']
    preview: TaskKnowledgePreview
    inference_consent: Literal[True]
    trajectory_storage_consent: Literal[True]

    @field_validator('inference_consent', 'trajectory_storage_consent', mode='before')
    @classmethod
    def exact_consent(cls, value):
        if value is not True:
            raise ValueError('task_knowledge_consent_required')
        return value


class TaskKnowledgeCommit(TaskKnowledgeFlags):
    schema_version: Literal['1.0']
    kind: Literal['task_knowledge_commit']
    intent_sha256: Hash
    intent: TaskKnowledgeIntent


class TaskKnowledgeStart(TaskKnowledgeFlags):
    schema_version: Literal['1.0']
    kind: Literal['task_knowledge_start']
    job_id: Identifier
    intent_sha256: Hash


class TaskKnowledgeAdmission(TaskKnowledgeFlags):
    schema_version: Literal['1.0']
    kind: Literal['task_knowledge_admission']
    intent_sha256: Hash
    job_id: Identifier
    preview_sha256: Hash
    context_sha256: Hash
    target: FollowupTarget


class TaskKnowledgeModelProof(TypedModel):
    schema_version: Literal['1.0']
    intent_sha256: Hash
    admission_sha256: Hash
    job_id: Identifier
    context_sha256: Hash
    run_id: Identifier
    step_id: Identifier
    state_version: int = Field(ge=0)
    snapshot_id: Identifier
    state_sha256: Hash
    options: list[Option] = Field(min_length=2, max_length=10)
    options_sha256: Hash
    request_sha256: Hash
    call_id: Identifier
    deployment_id: Identifier
    decision_id: Identifier
    actual_request_sha256: Hash
    response_sha256: Hash
    response_canonical: str = Field(min_length=1)
    response: Prediction

    @model_validator(mode='after')
    def response_binding(self):
        try:
            self.response.validate_options(self.options)
        except Exception:
            raise ValueError('task_knowledge_invalid') from None
        _check(self.options_sha256 == digest([option.model_dump(mode='json') for option in self.options])
               and self.response_sha256 == digest(self.response.model_dump(mode='json'))
               and self.response_canonical == canonical(self.response.model_dump(mode='json'))
               and self.actual_request_sha256 == self.request_sha256)
        return self


class TaskKnowledgeReport(TaskKnowledgeFlags):
    schema_version: Literal['1.0']
    kind: Literal['task_knowledge_report']
    job_id: Identifier
    intent_sha256: Hash
    intent: TaskKnowledgeIntent
    admission_sha256: Hash
    admission: TaskKnowledgeAdmission
    historical_binding_verified: bool
    context_binding_verified: bool
    model_request_verified: bool
    knowledge_applied: bool
    current_source_valid: bool
    current_authority_valid: bool
    prepared_context_count: int = Field(ge=0, le=500)
    dispatched_context_count: int = Field(ge=0, le=500)
    successful_model_call_count: int = Field(ge=0, le=500)
    context_proofs: list[dict] = Field(max_length=500)
    dispatch_proofs: list[dict] = Field(max_length=500)
    model_proofs: list[TaskKnowledgeModelProof] = Field(max_length=500)
    outcome: FollowupOutcome

    @model_validator(mode='after')
    def proof_relationships(self):
        _check(self.knowledge_applied == (self.historical_binding_verified
                    and self.context_binding_verified and self.model_request_verified)
               and (not self.model_request_verified or self.historical_binding_verified and self.context_binding_verified)
               and self.model_request_verified == (self.successful_model_call_count > 0)
               and self.context_binding_verified == (self.prepared_context_count > 0)
               and self.prepared_context_count == len(self.context_proofs)
               and self.dispatched_context_count == len(self.dispatch_proofs)
               and self.successful_model_call_count == len(self.model_proofs)
               and self.successful_model_call_count <= self.dispatched_context_count <= self.prepared_context_count
               and self.intent_sha256 == digest(self.intent.model_dump(mode='json'))
               and self.admission_sha256 == digest(self.admission.model_dump(mode='json'))
               and self.admission.job_id == self.job_id
               and self.admission.intent_sha256 == self.intent_sha256
               and self.admission.preview_sha256 == self.intent.preview.confirm_sha256
               and self.admission.context_sha256 == self.intent.preview.context_sha256
               and self.admission.target == self.intent.preview.target)
        used_calls, used_decisions = set(), set()
        for proof in self.model_proofs:
            value = proof.model_dump(mode='json')
            marker = {key: item for key, item in value.items() if key not in (
                'deployment_id', 'decision_id', 'actual_request_sha256', 'response_sha256',
                'response_canonical', 'response')}
            matching = [dispatch for dispatch in self.dispatch_proofs
                        if dispatch.get('call_id') == proof.call_id]
            _check(proof.call_id not in used_calls and proof.decision_id not in used_decisions
                   and self.context_proofs.count(marker) == 1 and len(matching) == 1
                   and matching[0] == dict(marker, actual_request_sha256=proof.actual_request_sha256,
                                          actual_request=matching[0].get('actual_request'))
                   and digest(matching[0]['actual_request']) == proof.actual_request_sha256
                   and proof.deployment_id == self.admission.target.system1_deployment_id
                   and proof.intent_sha256 == self.intent_sha256
                   and proof.admission_sha256 == self.admission_sha256
                   and proof.context_sha256 == self.admission.context_sha256 and proof.job_id == self.job_id)
            used_calls.add(proof.call_id)
            used_decisions.add(proof.decision_id)
        return self


class TaskKnowledgePreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    scope: KnowledgeScope
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=4)
    context_chars: int = Field(ge=256, le=2048)
    task_kind: TaskKind
    lease_id: Identifier
    generation: int = Field(ge=0)


class TaskKnowledgeStartRequest(TypedModel):
    schema_version: Literal['1.0']
    preview: TaskKnowledgePreview
    confirm_sha256: Hash
    consent: Literal[True]
    lease_id: Identifier
    generation: int = Field(ge=0)

    @field_validator('consent', mode='before')
    @classmethod
    def exact_consent(cls, value):
        if value is not True:
            raise ValueError('task_knowledge_consent_required')
        return value


class TaskKnowledgeReportRequest(TypedModel):
    schema_version: Literal['1.0']
    job_id: Identifier


REQUESTS = {'preview': TaskKnowledgePreviewRequest, 'start': TaskKnowledgeStartRequest,
            'report': TaskKnowledgeReportRequest}
RESPONSES = {'preview': TaskKnowledgePreview, 'start': TaskKnowledgeStart, 'report': TaskKnowledgeReport}


def knowledge_context(retrieval):
    citations = [{'citation_id': 'c' + str(index), **hit}
                 for index, hit in enumerate(retrieval['hits'], 1)]
    return PREFIX + canonical({'context_version': VERSION, 'scope': retrieval['scope'],
                               'citations': citations, **FLAGS})


class TaskKnowledgeService:
    def __init__(self, scheduler, knowledgeStore, privateDirectory):
        self.scheduler = scheduler
        self.store = knowledgeStore
        self.directory = Path(privateDirectory).absolute()
        self.directory_identity = None
        self.parent_identity = None
        self.clock = lambda: datetime.now(timezone.utc)

    def _workspace(self):
        runtime = self.scheduler.controller.runtime
        descriptor = open_existing_workspace(self.scheduler.settings.workspace)
        try:
            current = workspace_identity(self.scheduler.settings.workspace, descriptor)
            _check(runtime.descriptor is not None
                   and current == workspace_identity(runtime.root, runtime.descriptor))
            from .desktop import DesktopRuntime
            if isinstance(runtime, DesktopRuntime):
                from .session_binding import runtime_binding
                runtime_binding(runtime, self.scheduler.controller.session_id,
                                self.scheduler.controller.state()['generation'])
            return current.model_dump(mode='json')
        finally:
            os.close(descriptor)

    def _target(self, kind, lease_id, generation, connection=None):
        scheduler = self.scheduler
        _check(type(lease_id) is str and type(generation) is int and generation >= 0
               and not scheduler.closed and not scheduler.restart_quiesced and not scheduler.paused
               and kind in scheduler.kinds()
               and getattr(scheduler, '_owned_skill_reuse', None) is None
               and getattr(scheduler, 'remote_form_owned_fixture', None) is None
               and getattr(scheduler, '_owned_candidate_execution', None) is None
               and getattr(scheduler, '_active_owned_candidate_execution', None) is None
               and getattr(scheduler, 'remote_form_skill_invocation', None) is None
               and getattr(scheduler, 'owned_episode_learning', None) is None
               and scheduler.engine.identity.get('kind') != 'owned_episode_adapter_runtime')
        target = _typed(FollowupTarget, scheduler.failure_followup_target(kind))
        _check(target['lease_id'] == lease_id and target['generation'] == generation
               and target['task_kind'] == kind
               and target['system1_deployment_id'] == scheduler.engine.identity['deployment_id'])
        connection = connection or scheduler.store.connection
        parent = connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?',
                                    (target['session_id'],)).fetchone()
        _check(parent is not None and parent['owner'] == 'AGENT' and parent['status'] == 'running'
               and all(parent[key] == target[key] for key in ('lease_id', 'generation', 'image_id'))
               and parent['runtime_id'] == target['parent_runtime_id'])
        return target

    def _preview(self, value, fresh=False):
        value = _typed(TaskKnowledgePreview, value)
        retrieval = value['retrieval']
        _check(retrieval['scope'] == value['scope'] and retrieval['query_sha256'] == text_sha256(value['query'])
               and retrieval['top_k'] == value['top_k'] and retrieval['context_chars'] == value['context_chars']
               and retrieval['hits'] and retrieval['used_context_chars'] <= value['context_chars']
               and retrieval['used_context_chars'] == sum(len(hit['text']) for hit in retrieval['hits'])
               and len(retrieval['hits']) <= value['top_k']
               and value['context_text'] == knowledge_context(retrieval)
               and value['context_sha256'] == text_sha256(value['context_text'])
               and value['confirm_sha256'] == digest({key: item for key, item in value.items()
                                                     if key != 'confirm_sha256'})
               and all(text_sha256(hit['text']) == hit['chunk_sha256'] for hit in retrieval['hits']))
        if fresh:
            _check(self.clock() < _utc(value['expires_at']) <= self.clock() + timedelta(seconds=300))
        return value

    def preview(self, scope, query, top_k, context_chars, task_kind, lease_id, generation):
        request = _typed(TaskKnowledgePreviewRequest, {'schema_version': '1.0', 'scope': scope,
            'query': query, 'top_k': top_k, 'context_chars': context_chars,
            'task_kind': task_kind, 'lease_id': lease_id, 'generation': generation})
        SearchRequest.model_validate({'schema_version': '1.0', 'scope': scope, 'query': query,
                                      'top_k': top_k, 'context_chars': context_chars})
        target = self._target(task_kind, lease_id, generation)
        workspace = self._workspace()
        retrieval = self.store.search(scope=request['scope'], query=query,
                                     top_k=top_k, context_chars=context_chars)
        _check(bool(retrieval['hits']))
        descriptor = _directory(self.store.root, create=False)
        try:
            store_identity = _identity(descriptor)
        finally:
            os.close(descriptor)
        context_text = knowledge_context(retrieval)
        value = {'schema_version': '1.0', 'kind': 'task_knowledge_preview',
            'preview_id': 'knowledge-preview-' + uuid4().hex,
            'expires_at': (self.clock() + timedelta(seconds=300)).isoformat(),
            'scope': request['scope'], 'query': query, 'top_k': top_k, 'context_chars': context_chars,
            'retrieval': retrieval, 'target': target, 'workspace_identity': workspace,
            'store_identity': store_identity, 'context_version': VERSION, 'context_text': context_text,
            'context_sha256': text_sha256(context_text), **FLAGS}
        value = self._preview(value | {'confirm_sha256': digest(value)}, fresh=True)
        self._current(value, fresh=True)
        return value

    def _current(self, preview, target=None, fresh=False, connection=None):
        preview = self._preview(preview, fresh=fresh)
        expected = self._target(preview['target']['task_kind'], preview['target']['lease_id'],
                                preview['target']['generation'], connection)
        _check(expected == preview['target'] and (target is None or target == expected)
               and self._workspace() == preview['workspace_identity'])
        validate_knowledge_retrieval(self.store, preview['retrieval'], preview['store_identity'])
        return preview

    def _open(self, create=False):
        parent = _directory(self.directory.parent, create=False)
        descriptor = None
        try:
            parent_identity = _identity(parent)
            _check(self.parent_identity is None or parent_identity == self.parent_identity)
            if create:
                try:
                    os.mkdir(self.directory.name, 0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            descriptor = _directory(self.directory, create=False)
            identity = _identity(descriptor)
            linked = os.stat(self.directory.name, dir_fd=parent, follow_symlinks=False)
            _check(identity == {'device': linked.st_dev, 'inode': linked.st_ino,
                                'owner': linked.st_uid, 'mode': linked.st_mode}
                   and (self.directory_identity is None or self.directory_identity == identity))
            self.parent_identity, self.directory_identity = parent_identity, identity
            return descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            os.close(parent)

    def _read(self, descriptor, filename):
        content = _read_private_child(descriptor, filename, MAX_BYTES)
        value = json.loads(content)
        _check(content == canonical(value).encode())
        return value

    def commit(self, preview, confirm_sha256, consent, lease_id, generation):
        _check(consent is True and type(confirm_sha256) is str)
        preview = self._preview(preview, fresh=True)
        _check(confirm_sha256 == preview['confirm_sha256']
               and lease_id == preview['target']['lease_id'] and generation == preview['target']['generation'])
        self._current(preview, fresh=True)
        retrieval = self.store.search(scope=preview['scope'], query=preview['query'],
                                     top_k=preview['top_k'], context_chars=preview['context_chars'])
        _check(retrieval == preview['retrieval'])
        intent = _typed(TaskKnowledgeIntent, {'schema_version': '1.0', 'kind': 'task_knowledge_intent',
            'preview': preview, 'inference_consent': True, 'trajectory_storage_consent': True})
        checksum = digest(intent)
        descriptor = self._open(create=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._current(preview, fresh=True)
            _write_private_child(descriptor, preview['preview_id'] + '.json', canonical(intent).encode())
            _write_private_child(descriptor, 'intent-' + checksum + '.json', canonical(intent).encode())
        finally:
            os.close(descriptor)
        _check(self._intent(checksum) == intent)
        return _typed(TaskKnowledgeCommit, {'schema_version': '1.0', 'kind': 'task_knowledge_commit',
                                           'intent_sha256': checksum, 'intent': intent, **FLAGS})

    def _intent(self, checksum):
        try:
            _check(type(checksum) is str and len(checksum) == 64 and all(character in '0123456789abcdef'
                                                                      for character in checksum))
            descriptor = self._open()
            try:
                intent = _typed(TaskKnowledgeIntent, self._read(descriptor, 'intent-' + checksum + '.json'))
                _check(digest(intent) == checksum)
                preview = self._preview(intent['preview'])
                _check(self._read(descriptor, preview['preview_id'] + '.json') == intent
                       and preview['target']['session_id'] == self.scheduler.controller.session_id)
            finally:
                os.close(descriptor)
            os.close(self._open())
            return intent
        except Exception:
            raise ValueError('task_knowledge_intent_invalid') from None

    def _admission(self, checksum, preview, job_id):
        return _typed(TaskKnowledgeAdmission, {'schema_version': '1.0', 'kind': ADMISSION,
            'intent_sha256': checksum, 'job_id': job_id, 'preview_sha256': preview['confirm_sha256'],
            'context_sha256': preview['context_sha256'], 'target': preview['target'], **FLAGS})

    def admit(self, store, intent_sha256, job_id, target):
        connection = store.connection
        _check(connection.in_transaction)
        intent = self._intent(intent_sha256)
        preview = self._current(intent['preview'], target, fresh=True, connection=connection)
        job = connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        _check(job is not None and job['run_id'] is None and job['status'] == 'queued'
               and job['kind'] == target['task_kind']
               and all(job[key] == target[key] for key in ('session_id', 'lease_id', 'generation')))
        _check(connection.execute('SELECT 1 FROM desktop_events WHERE kind=? AND '
            '(json_extract(payload_json,\'$.intent_sha256\')=? OR json_extract(payload_json,\'$.job_id\')=?)',
            (ADMISSION, intent_sha256, job_id)).fetchone() is None)
        admission = self._admission(intent_sha256, preview, job_id)
        descriptor = self._open()
        try:
            _write_private_child(descriptor, 'admission-' + job_id + '.json', canonical(admission).encode())
        finally:
            os.close(descriptor)
        store.insert('desktop_events', event_id=identifier('event'), session_id=target['session_id'],
                     kind=ADMISSION, payload_json=canonical(admission), created_at=now())
        return {'intent_sha256': intent_sha256}

    def _association(self, connection, checksum, job_id):
        intent = self._intent(checksum)
        preview = intent['preview']
        admission = self._admission(checksum, preview, job_id)
        descriptor = self._open()
        try:
            _check(self._read(descriptor, 'admission-' + job_id + '.json') == admission)
        finally:
            os.close(descriptor)
        rows = connection.execute('SELECT * FROM desktop_events WHERE kind=? AND '
            '(json_extract(payload_json,\'$.intent_sha256\')=? OR json_extract(payload_json,\'$.job_id\')=?) LIMIT 3',
            (ADMISSION, checksum, job_id)).fetchall()
        job = connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        target = preview['target']
        _check(len(rows) == 1 and rows[0]['payload_json'] == canonical(admission)
               and rows[0]['session_id'] == target['session_id'] and job is not None
               and job['kind'] == target['task_kind']
               and all(job[key] == target[key] for key in ('session_id', 'lease_id', 'generation'))
               and _time(job['created_at']) <= _time(rows[0]['created_at']))
        job = dict(job)
        job['system1_real_model'] = False
        _check(all(job_id not in getattr(self.scheduler, name, {}) for name in (
            '_failure_followup_jobs', '_failure_guidance_jobs', '_hello_guidance_reuse_jobs')))
        if job['run_id'] is not None:
            run = connection.execute('SELECT * FROM runs WHERE run_id=?', (job['run_id'],)).fetchone()
            snapshots = connection.execute('SELECT * FROM state_snapshots WHERE run_id=? AND state_version=0 LIMIT 2',
                                           (job['run_id'],)).fetchall()
            _check(run is not None and len(snapshots) == 1)
            state = State.model_validate_json(snapshots[0]['state_json'])
            _check(state.phase == Phase.CREATED and state.run_id == job['run_id']
                   and state.runtime_id == job['runtime_id'] and state.task_kind == target['task_kind']
                   and state.owner == 'AGENT' and state.owner_lease_id == target['lease_id']
                   and state.deployment_id == target['system1_deployment_id']
                   and state.supervisor_deployment_id == target['system2_deployment_id']
                   and digest(state.model_dump(mode='json')) == snapshots[0]['content_sha256']
                   and _time(rows[0]['created_at']) <= _time(run['started_at']))
            deployments = json.loads(run['deployment_snapshot_json'])
            _check(deployments.get('deployment_id') == target['system1_deployment_id']
                   and type(deployments.get('real_model')) is bool
                   and bool(job['real_model']) == (deployments['real_model'] and (
                       job['kind'] != 'vision_canvas' or deployments.get('supervisor', {}).get('real_model') is True)))
            job['system1_real_model'] = deployments['real_model']
            environment = json.loads(run['environment_json'])
            _check(environment.get('runtime_id') == state.runtime_id
                   and environment.get('parent_runtime_id', target['parent_runtime_id']) == target['parent_runtime_id'])
            calls = connection.execute('SELECT * FROM model_calls WHERE run_id=? AND role=? LIMIT 501',
                                       (state.run_id, 'system1')).fetchall()
            _check(len(calls) <= 500)
            _role(connection, {key: value for key, value in deployments.items() if key != 'supervisor'},
                  'system1', calls)
        return intent, admission, job

    def guard(self, connection, intent_sha256, job_id, target, require_context=False):
        intent, admission, job = self._association(connection, intent_sha256, job_id)
        self._current(intent['preview'], target, connection=connection)
        _check(job['status'] in {'queued', 'running', 'waiting_approval'})
        if require_context:
            proofs = self._application(connection, intent, admission, job)
            _check(proofs['context_binding_verified']
                   and (not job['system1_real_model'] or proofs['model_request_verified']))

    def _state_bound(self, connection, state, job, preview):
        target = preview['target']
        _check(state.phase == Phase.DECIDE and state.owner == 'AGENT'
               and state.run_id == job['run_id'] and state.runtime_id == job['runtime_id']
               and state.task_kind == target['task_kind'] and state.owner_lease_id == target['lease_id']
               and state.deployment_id == target['system1_deployment_id']
               and state.observation.endswith(preview['context_text']))
        rows = connection.execute('SELECT * FROM state_snapshots WHERE run_id=? AND state_version=? LIMIT 2',
                                  (state.run_id, state.state_version)).fetchall()
        _check(len(rows) == 1 and rows[0]['content_sha256'] == digest(state.model_dump(mode='json'))
               and State.model_validate_json(rows[0]['state_json']) == state)
        initial = connection.execute('SELECT state_json FROM state_snapshots WHERE run_id=? AND state_version=0',
                                     (state.run_id,)).fetchone()
        initial = State.model_validate_json(initial['state_json'])
        _check(all(getattr(state, key) == getattr(initial, key) for key in (
            'task_id', 'original_goal', 'normalized_goal', 'authorized_path', 'authorized_content',
            'success_criteria', 'skill_invocation_sha256', 'max_attempts')))
        return rows[0]

    def record(self, store, intent_sha256, job_id, state, options, call_id):
        connection = store.connection
        _check(connection.in_transaction)
        intent, admission, job = self._association(connection, intent_sha256, job_id)
        self._current(intent['preview'], connection=connection)
        snapshot = self._state_bound(connection, state, job, intent['preview'])
        _check(2 <= len(options) <= 10 and len({option.id for option in options}) == len(options)
               and (call_id is not None) == job['system1_real_model'])
        if call_id is not None:
            call = connection.execute('SELECT * FROM model_calls WHERE call_id=?', (call_id,)).fetchone()
            _check(call is not None and call['run_id'] == state.run_id and call['step_id'] == state.step_id
                   and call['role'] == 'system1' and call['deployment_id'] == state.deployment_id
                   and call['request_json'] == canonical(decision_request(state, options)))
        marker = {'schema_version': '1.0', 'intent_sha256': intent_sha256,
            'admission_sha256': digest(admission), 'job_id': job_id,
            'context_sha256': intent['preview']['context_sha256'], 'run_id': state.run_id,
            'step_id': state.step_id, 'state_version': state.state_version,
            'snapshot_id': snapshot['snapshot_id'], 'state_sha256': digest(state.model_dump(mode='json')),
            'options': [option.model_dump(mode='json') for option in options],
            'options_sha256': digest([option.model_dump(mode='json') for option in options]),
            'request_sha256': digest(decision_request(state, options)), 'call_id': call_id}
        _check(connection.execute('SELECT 1 FROM observations WHERE run_id=? AND kind=? AND '
            'json_extract(payload_json,\'$.snapshot_id\')=?', (state.run_id, MARKER, snapshot['snapshot_id'])).fetchone() is None)
        store.insert('observations', observation_id=identifier('observation'), run_id=state.run_id,
                     step_id=state.step_id, action_id=None, kind=MARKER,
                     payload_json=canonical(marker), created_at=now())
        return marker

    def dispatch(self, store, intent_sha256, job_id, marker, request):
        connection = store.connection
        intent, admission, job = self._association(connection, intent_sha256, job_id)
        self._current(intent['preview'], connection=connection)
        _check(marker['call_id'] is not None and marker['request_sha256'] == digest(request))
        state = store.state(job['run_id'])
        options = [Option.model_validate(option) for option in marker['options']]
        _check(self._state_bound(connection, state, job, intent['preview'])['snapshot_id'] == marker['snapshot_id']
               and request == decision_request(state, options))
        rows = connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=? AND '
            'json_extract(payload_json,\'$.snapshot_id\')=? LIMIT 2',
            (state.run_id, MARKER, marker['snapshot_id'])).fetchall()
        _check(len(rows) == 1 and rows[0]['payload_json'] == canonical(marker))
        proof = dict(marker, actual_request_sha256=digest(request), actual_request=request)
        _check(connection.execute('SELECT 1 FROM observations WHERE run_id=? AND kind=? AND '
            'json_extract(payload_json,\'$.call_id\')=?', (state.run_id, DISPATCH, marker['call_id'])).fetchone() is None)
        with connection:
            store.insert('observations', observation_id=identifier('observation'), run_id=state.run_id,
                         step_id=state.step_id, action_id=None, kind=DISPATCH,
                         payload_json=canonical(proof), created_at=now())

    def _application(self, connection, intent, admission, job):
        result = {'context_binding_verified': False, 'model_request_verified': False,
                  'prepared_context_count': 0, 'dispatched_context_count': 0,
                  'successful_model_call_count': 0, 'context_proofs': [], 'dispatch_proofs': [], 'model_proofs': []}
        if job['run_id'] is None:
            return result
        prepared = connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=? '
            'ORDER BY created_at,observation_id LIMIT 501', (job['run_id'], MARKER)).fetchall()
        dispatched = connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=? '
            'ORDER BY created_at,observation_id LIMIT 501', (job['run_id'], DISPATCH)).fetchall()
        _check(len(prepared) <= 500 and len(dispatched) <= 500)
        used_snapshots, used_calls = set(), set()
        for row in prepared:
            marker = json.loads(row['payload_json'])
            snapshot = connection.execute('SELECT * FROM state_snapshots WHERE snapshot_id=? AND run_id=?',
                (marker['snapshot_id'], job['run_id'])).fetchone()
            _check(snapshot is not None)
            state = State.model_validate_json(snapshot['state_json'])
            self._state_bound(connection, state, job, intent['preview'])
            options = [Option.model_validate(option) for option in marker['options']]
            expected = {'schema_version': '1.0', 'intent_sha256': digest(intent),
                'admission_sha256': digest(admission), 'job_id': job['job_id'],
                'context_sha256': intent['preview']['context_sha256'], 'run_id': state.run_id,
                'step_id': state.step_id, 'state_version': state.state_version,
                'snapshot_id': snapshot['snapshot_id'], 'state_sha256': digest(state.model_dump(mode='json')),
                'options': [option.model_dump(mode='json') for option in options],
                'options_sha256': digest([option.model_dump(mode='json') for option in options]),
                'request_sha256': digest(decision_request(state, options)), 'call_id': marker['call_id']}
            _check(row['payload_json'] == canonical(expected) and row['action_id'] is None
                   and row['step_id'] == state.step_id and snapshot['snapshot_id'] not in used_snapshots
                   and _time(snapshot['created_at']) <= _time(row['created_at'])
                   and (marker['call_id'] is not None) == job.get('system1_real_model', bool(job['real_model'])))
            used_snapshots.add(snapshot['snapshot_id'])
            matching = [proof for proof in dispatched
                        if json.loads(proof['payload_json']).get('call_id') == marker['call_id']]
            if marker['call_id'] is not None:
                _check(marker['call_id'] not in used_calls)
                used_calls.add(marker['call_id'])
                call = connection.execute('SELECT * FROM model_calls WHERE call_id=?', (marker['call_id'],)).fetchone()
                _check(call is not None and call['run_id'] == state.run_id and call['step_id'] == state.step_id
                       and call['role'] == 'system1' and call['deployment_id'] == state.deployment_id
                       and call['request_json'] == canonical(decision_request(state, options))
                       and _time(call['created_at']) <= _time(row['created_at']) and len(matching) <= 1)
                if matching:
                    proof = matching[0]
                    expected_dispatch = dict(expected, actual_request_sha256=expected['request_sha256'],
                                             actual_request=decision_request(state, options))
                    _check(proof['payload_json'] == canonical(expected_dispatch) and proof['action_id'] is None
                           and proof['step_id'] == state.step_id
                           and _time(row['created_at']) <= _time(proof['created_at']))
                    result['dispatch_proofs'].append(expected_dispatch)
                decisions = connection.execute('SELECT * FROM decisions WHERE run_id=? AND snapshot_id=? LIMIT 2',
                    (state.run_id, snapshot['snapshot_id'])).fetchall()
                if call['status'] == 'ok':
                    _check(len(matching) == 1 and len(decisions) == 1)
                    prediction = Prediction.model_validate_json(call['response_json'])
                    try:
                        prediction.validate_options(options)
                    except Exception:
                        raise ValueError('task_knowledge_invalid') from None
                    decision = decisions[0]
                    _check(decision['call_id'] == call['call_id'] and decision['step_id'] == state.step_id
                           and decision['options_json'] == canonical(expected['options'])
                           and decision['selected_option'] == prediction.selected_option
                           and decision['probabilities_json'] == canonical(prediction.probabilities)
                           and _time(matching[0]['created_at']) <= _time(decision['created_at']))
                    response = prediction.model_dump(mode='json')
                    result['model_proofs'].append(_typed(TaskKnowledgeModelProof, dict(expected,
                        deployment_id=call['deployment_id'], decision_id=decision['decision_id'],
                        actual_request_sha256=expected_dispatch['actual_request_sha256'],
                        response_sha256=digest(response), response_canonical=canonical(response), response=response)))
                    result['successful_model_call_count'] += 1
            else:
                _check(not matching)
            result['context_proofs'].append(expected)
        _check(len(dispatched) == len(result['dispatch_proofs']))
        decision_snapshots = connection.execute('SELECT snapshot_id FROM state_snapshots WHERE run_id=? '
            'AND json_extract(state_json,\'$.phase\')=? LIMIT 501', (job['run_id'], Phase.DECIDE.value)).fetchall()
        _check(len(decision_snapshots) <= 500
               and {snapshot['snapshot_id'] for snapshot in decision_snapshots} == used_snapshots)
        calls = connection.execute('SELECT call_id FROM model_calls WHERE run_id=? AND role=? LIMIT 501',
                                   (job['run_id'], 'system1')).fetchall()
        _check(len(calls) <= 500 and {call['call_id'] for call in calls} == used_calls)
        result.update(context_binding_verified=bool(prepared),
                      model_request_verified=result['successful_model_call_count'] > 0,
                      prepared_context_count=len(prepared), dispatched_context_count=len(dispatched))
        return result

    def report(self, job_id):
        with audit_snapshot(self.scheduler.settings.database) as (connection, unused_identity):
            return self.report_connection(connection, job_id)

    def report_connection(self, connection, job_id):
        rows = connection.execute('SELECT payload_json FROM desktop_events WHERE kind=? AND session_id=? '
            'AND json_extract(payload_json,\'$.job_id\')=? LIMIT 2',
            (ADMISSION, self.scheduler.controller.session_id, job_id)).fetchall()
        _check(len(rows) == 1)
        checksum = json.loads(rows[0]['payload_json'])['intent_sha256']
        intent, admission, job = self._association(connection, checksum, job_id)
        proofs = {'context_binding_verified': False, 'model_request_verified': False,
                  'prepared_context_count': 0, 'dispatched_context_count': 0,
                  'successful_model_call_count': 0, 'context_proofs': [], 'dispatch_proofs': [], 'model_proofs': []}
        historical = True
        try:
            proofs = self._application(connection, intent, admission, job)
        except (ValueError, TypeError, KeyError, IndexError):
            historical = False
        source_valid = authority_valid = False
        try:
            validate_knowledge_retrieval(self.store, intent['preview']['retrieval'],
                                       intent['preview']['store_identity'])
            source_valid = True
        except Exception:
            pass
        try:
            target = intent['preview']['target']
            authority_valid = (self._target(target['task_kind'], target['lease_id'], target['generation']) == target
                               and self._workspace() == intent['preview']['workspace_identity'])
        except Exception:
            pass
        from .failure_followup_outcome import audit_followup_outcome
        outcome = audit_followup_outcome(connection, job['run_id']) if job['run_id'] else {
            'status': 'not_verified', 'verification_count': 0, 'scope': 'none', 'reason': 'run_not_settled_success'}
        return _typed(TaskKnowledgeReport, {'schema_version': '1.0', 'kind': 'task_knowledge_report',
            'job_id': job_id, 'intent_sha256': checksum, 'intent': intent,
            'admission_sha256': digest(admission), 'admission': admission,
            'historical_binding_verified': historical, **proofs,
            'knowledge_applied': historical and proofs['context_binding_verified'] and proofs['model_request_verified'],
            'current_source_valid': source_valid, 'current_authority_valid': authority_valid,
            'outcome': outcome, **FLAGS})


class TaskKnowledgeContext:
    def __init__(self, scheduler, job_id):
        self.scheduler = scheduler
        self.job_id = job_id
        self.marker = None

    def _binding(self):
        binding = self.scheduler._task_knowledge_jobs.get(self.job_id)
        _check(binding is not None)
        return binding['intent_sha256']

    def before_call(self):
        checksum = self._binding()
        service = self.scheduler.task_knowledge
        preview = service._intent(checksum)['preview']
        service.guard(self.scheduler.store.connection, checksum, self.job_id,
                      self.scheduler.failure_followup_target(preview['target']['task_kind']))

    def apply(self, state, observation):
        self.before_call()
        preview = self.scheduler.task_knowledge._intent(self._binding())['preview']
        _check(state.phase in {Phase.OBSERVE, Phase.SUPERVISOR} and type(observation) is str
               and state.task_kind == preview['target']['task_kind']
               and state.owner == 'AGENT' and state.owner_lease_id == preview['target']['lease_id'])
        return observation + preview['context_text']

    def record(self, state, options, call_id):
        self.before_call()
        self.marker = self.scheduler.task_knowledge.record(self.scheduler.store, self._binding(),
                                                           self.job_id, state, options, call_id)

    def before_dispatch(self, request):
        self.before_call()
        _check(self.marker is not None)
        self.scheduler.task_knowledge.dispatch(self.scheduler.store, self._binding(),
                                              self.job_id, self.marker, request)

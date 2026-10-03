import asyncio
from typing import Literal
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .agent_contracts import (
    AgentAdmissionError, AgentCleanupProof, AgentHandle, AgentJobRequest,
    AgentObservation, AgentPrepared, AgentResultProof,
)
from .contracts import TypedModel, digest
from .scientist_intents import ScientistIntentBinding
from .scientist_lab import ScientistLabBudget, ScientistLabTask
from .scientist_lab_service import ScientistLabService
from .scientist_transport import ScientistAdmissionError


class ScientistAgentPayload(TypedModel):
    suite: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    track: Literal['anomaly', 'mode']
    program_version: str = Field(min_length=1, max_length=64)


class ScientistAgentHandlePayload(TypedModel):
    request: AgentJobRequest
    action_id: str = Field(pattern=r'^action-[a-f0-9]{32}$')
    envelope_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    original_binding: ScientistIntentBinding
    remote_run_id: str | None = Field(default=None,
        pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    stop_action_id: str | None = Field(default=None, pattern=r'^action-[a-f0-9]{32}$')
    stop_envelope_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def paired_stop_approval(self):
        if (self.stop_action_id is None) != (self.stop_envelope_sha256 is None):
            raise ValueError('STOP approval requires its exact envelope hash')
        return self


class ScientistAgentAdapter:
    """Opt-in bridge to an injected original Lab service; never grants approval.

    Read controls use the existing private original-task execution path because
    the public read loader deliberately permits controller-generation adoption.
    This adapter instead retains the original binding throughout the job.
    """

    _client_locks = WeakKeyDictionary()

    def __init__(self, service: ScientistLabService, *, cleanup_prover=None):
        if not isinstance(service, ScientistLabService) or service.store is not service.controller.store:
            raise AgentAdmissionError('Scientist adapter requires the existing original Lab service/store')
        self.service = service
        self.cleanup_prover = cleanup_prover
        self._controls = self._client_locks.setdefault(service.client, asyncio.Lock())

    @staticmethod
    def _request(request):
        request = AgentJobRequest.model_validate_json(request.model_dump_json(), strict=True)
        payload = ScientistAgentPayload.model_validate(request.payload, strict=True)
        budget = ScientistLabBudget.model_validate(request.budget.model_dump(), strict=True)
        if request.operation != 'scientist.lab.start.v1' or request.authority.owner != 'AGENT':
            raise AgentAdmissionError('Only an original AGENT Scientist Lab start is supported')
        return request, payload, budget

    def _current(self, request, binding=None):
        current = self.service.controller.state()
        authority = request.authority
        if (authority.principal != self.service.client.principal_id
                or current['status'] != 'running'
                or any(current[field] != getattr(authority, field)
                       for field in ('runtime_id', 'lease_id', 'owner', 'generation'))):
            raise AgentAdmissionError('Scientist original orchestration authority changed')
        if binding is not None and (
                current['session_id'] != binding.session_id
                or self.service.context_sha256 != binding.authorization_context_sha256
                or any(current[field] != getattr(binding, field)
                       for field in ('runtime_id', 'lease_id', 'owner', 'generation'))):
            raise AgentAdmissionError('Scientist original Lab binding changed; adoption is forbidden')

    def _load(self, handle):
        handle = AgentHandle.model_validate_json(handle.model_dump_json(), strict=True)
        payload = ScientistAgentHandlePayload.model_validate(handle.payload, strict=True)
        request, expected, budget = self._request(payload.request)
        if (request.job_id != handle.job_id or request.request_sha256() != handle.request_sha256
                or request.authority != handle.authority):
            raise AgentAdmissionError('Scientist handle differs from its original orchestration request')
        approval = self.service.approval(payload.action_id)
        task = ScientistLabTask.model_validate(approval['envelope']['task'], strict=True)
        if (approval['envelope_sha256'] != payload.envelope_sha256
                or task.request.external_run_id != handle.handle_id
                or task.request.external_action_id != payload.action_id
                or task.binding != payload.original_binding
                or task.authority_url != self.service.client.authority_url
                or task.principal_id != request.authority.principal
                or task.request.suite != expected.suite or task.request.track != expected.track
                or task.request.program_version != expected.program_version or task.request.budget != budget):
            raise AgentAdmissionError('Scientist immutable start action/envelope provenance differs')
        row = self.service.store.connection.execute(
            'SELECT task_json,lab_run_id FROM scientist_lab_jobs WHERE run_id=? AND session_id=?',
            (handle.handle_id, payload.original_binding.session_id),
        ).fetchone()
        if row is None or ScientistLabTask.model_validate_json(row['task_json'], strict=True) != task:
            raise AgentAdmissionError('Scientist durable original task differs')
        if payload.remote_run_id is not None and payload.remote_run_id != row['lab_run_id']:
            raise AgentAdmissionError('Scientist remote run differs from its durable original binding')
        bound = task.model_copy(update={'lab_run_id': row['lab_run_id']})
        self._current(request, payload.original_binding)
        self.service.journal.verify_authority(bound,
            self.service._action(bound, 'lab.status') if bound.lab_run_id else
            self.service._load_action(payload.action_id)[2])
        return payload, bound, approval

    def _uncertain(self):
        return (self.service.client.uncertain_action_id is not None
                or self.service.store.connection.execute(
                    "SELECT 1 FROM scientist_lab_actions WHERE state='intent' LIMIT 1",
                ).fetchone() is not None)

    @staticmethod
    def _handle(handle, payload, **updates):
        return handle.model_copy(update={'payload': payload.model_copy(update=updates).model_dump(mode='json')})

    def _stop_approval(self, payload, task):
        approval = self.service.approval(payload.stop_action_id)
        stop_task = ScientistLabTask.model_validate(approval['envelope']['task'], strict=True)
        action = approval['envelope']['action']
        if (approval['envelope_sha256'] != payload.stop_envelope_sha256 or stop_task != task
                or action['tool'] != 'lab.stop' or action['run_id'] != task.request.external_run_id
                or action['arguments'] != {'lab_run_id': task.lab_run_id}):
            raise AgentAdmissionError('Scientist STOP differs from its exact original-job approval')
        if approval['state'] in {'pending', 'approved'}:
            self.service.journal.verify_authority(stop_task, self.service._load_action(payload.stop_action_id)[2])
        return approval

    async def prepare(self, request):
        request, payload, budget = self._request(request)
        async with self._controls:
            with self.service.controller.lock:
                self._current(request)
                if self._uncertain():
                    raise AgentAdmissionError('Scientist service has an uncertain effect; no new proposal')
                approval = self.service.propose(suite=payload.suite, track=payload.track,
                    program_version=payload.program_version, budget=budget)
                task = ScientistLabTask.model_validate(approval['envelope']['task'], strict=True)
                self._current(request, task.binding)
                handle_payload = ScientistAgentHandlePayload(request=request, action_id=approval['action_id'],
                    envelope_sha256=approval['envelope_sha256'], original_binding=task.binding)
                handle = AgentHandle(job_id=request.job_id, request_sha256=request.request_sha256(),
                    authority=request.authority, handle_id=task.request.external_run_id,
                    payload=handle_payload.model_dump(mode='json'))
                return AgentPrepared(handle=handle, ready=False)

    async def dispatch(self, prepared):
        async with self._controls:
            with self.service.controller.lock:
                payload, task, approval = self._load(prepared.handle)
                if not prepared.ready or approval['state'] != 'approved' or self._uncertain():
                    raise AgentAdmissionError('Scientist start requires exact unconsumed human approval')
            result = await self.service.execute_async(payload.action_id)
            with self.service.controller.lock:
                self._current(payload.request, payload.original_binding)
                updated = self._handle(prepared.handle, payload, remote_run_id=result['run_id'])
                self._load(updated)
                return updated

    async def _read_async(self, handle, payload, task, tool):
        self._current(payload.request, payload.original_binding)
        result = await self.service._execute_async(task, self.service._action(task, tool))
        with self.service.controller.lock:
            self._current(payload.request, payload.original_binding)
            self._load(handle)
        return result

    @staticmethod
    def _state(status):
        return {'completed': 'succeeded', 'stopped': 'cancelled', 'failed': 'failed'}.get(status, 'running')

    async def observe(self, handle):
        async with self._controls:
            with self.service.controller.lock:
                payload, task, approval = self._load(handle)
                if self._uncertain() or approval['state'] == 'intent':
                    return AgentObservation(handle=handle, state='uncertain', detail='Unresolved Lab effect; no replay')
                if payload.stop_action_id:
                    approval = self._stop_approval(payload, task)
                if approval['state'] in {'pending', 'approved'} or (
                        approval['state'] == 'rejected' and payload.stop_action_id is None):
                    state = {'pending': 'waiting_approval', 'approved': 'ready', 'rejected': 'failed'}[approval['state']]
                    return AgentObservation(handle=handle, state=state)
                if approval['state'] not in {'acknowledged', 'rejected'} or task.lab_run_id is None:
                    return AgentObservation(handle=handle, state='uncertain', detail='No verified original remote binding')
                updated = self._handle(handle, payload, remote_run_id=task.lab_run_id)
            status = await self._read_async(updated, payload, task, 'lab.status')
            return AgentObservation(handle=updated, state=self._state(status['state']),
                detail='Independent Lab status is not GPU release or physical cleanup')

    async def request_cancel(self, handle):
        async with self._controls:
            with self.service.controller.lock:
                payload, task, _approval = self._load(handle)
                if self._uncertain():
                    return AgentObservation(handle=handle, state='uncertain', detail='Unresolved Lab effect; STOP not replayed')
                if task.lab_run_id is None:
                    raise AgentAdmissionError('Scientist STOP requires the verified original remote job')
                if payload.stop_action_id is None:
                    approval = self.service.propose_stop(handle.handle_id)
                    updated = self._handle(handle, payload, remote_run_id=task.lab_run_id,
                        stop_action_id=approval['action_id'], stop_envelope_sha256=approval['envelope_sha256'])
                    updated_payload = ScientistAgentHandlePayload.model_validate(updated.payload, strict=True)
                    self._stop_approval(updated_payload, task)
                    return AgentObservation(handle=updated, state='waiting_approval')
                approval = self._stop_approval(payload, task)
                if approval['state'] != 'approved':
                    if approval['state'] not in {'acknowledged', 'rejected'}:
                        return AgentObservation(handle=handle,
                            state='waiting_approval' if approval['state'] == 'pending' else 'uncertain')
                    execute_stop = False
                else:
                    execute_stop = True
            if not execute_stop:
                result = await self._read_async(handle, payload, task, 'lab.status')
                return AgentObservation(handle=handle, state=self._state(result['state']),
                    detail='Existing STOP approval is not repeated or replaced; actual job status only')
            result = await self.service.execute_async(payload.stop_action_id)
            with self.service.controller.lock:
                self._current(payload.request, payload.original_binding)
                self._load(handle)
            return AgentObservation(handle=handle, state=self._state(result['state']),
                detail='STOP acknowledgment does not verify GPU release')

    async def verify_result(self, handle):
        async with self._controls:
            with self.service.controller.lock:
                payload, task, approval = self._load(handle)
                if self._uncertain() or approval['state'] != 'acknowledged' or task.lab_run_id is None:
                    return AgentResultProof(handle=handle, verified=False, outcome='failed')
            try:
                report = await self._read_async(handle, payload, task, 'lab.report')
            except (AgentAdmissionError, ScientistAdmissionError):
                raise
            except ValueError:
                status = await self._read_async(handle, payload, task, 'lab.status')
                if status['state'] not in {'completed', 'stopped', 'failed'}:
                    raise AgentAdmissionError('Scientist result no longer has terminal status')
                return AgentResultProof(handle=handle, verified=False, outcome=self._state(status['state']))
            outcome = self._state(report['report']['status'])
            metadata = {'local_run_id': handle.handle_id, 'remote_run_id': report['run_id'],
                        'report_sha256': report['report_sha256'], 'outcome': outcome}
            return AgentResultProof(handle=handle, verified=True, outcome=outcome, evidence_sha256=digest(metadata))

    async def verify_cleanup(self, handle):
        async with self._controls:
            with self.service.controller.lock:
                payload, _task, _approval = self._load(handle)
                if self.cleanup_prover is None or self._uncertain():
                    return AgentCleanupProof(handle=handle, verified=False, scope='scientist_gpu')
            proof = await self.cleanup_prover(handle.model_copy(deep=True))
            with self.service.controller.lock:
                self._current(payload.request, payload.original_binding)
                self._load(handle)
            proof = AgentCleanupProof.model_validate_json(proof.model_dump_json(), strict=True)
            if proof.handle != handle or proof.scope != 'scientist_gpu':
                raise AgentAdmissionError('Trusted Scientist cleanup proof must bind the exact current handle and GPU scope')
            return proof

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from .agent_contracts import (
    AgentAdapter, AgentAdmissionError, AgentAuthority, AgentCleanupProof, AgentHandle,
    AgentJobRequest, AgentObservation, AgentPrepared, AgentRegistration, AgentResultProof,
)
from .agent_orchestration_store import AgentOrchestrationStore


def _deny_authority(request: AgentJobRequest) -> AgentAuthority:
    raise AgentAdmissionError('Current host authority provider is not configured')


class AgentOrchestrator:
    """Explicit host-driven delegation; no approvals, model inference or GPU allocation."""

    def __init__(self, store: AgentOrchestrationStore, *, adapters: Mapping[str, AgentAdapter],
                 current_authority: Callable[[AgentJobRequest], AgentAuthority] = _deny_authority):
        self.store = store
        self.adapters = MappingProxyType(dict(adapters))
        self.current_authority = current_authority
        self._busy = set()

    def register(self, registration: AgentRegistration) -> str:
        if registration.adapter_kind not in self.adapters:
            raise AgentAdmissionError('Adapter must be explicitly installed by the host')
        return self.store.register(registration)

    def submit(self, request: AgentJobRequest):
        request = AgentJobRequest.model_validate(request.model_dump(), strict=True)
        self._authorized(request)
        if request.authority.owner != 'AGENT':
            raise AgentAdmissionError('Only the existing Operator can delegate a start')
        return self.store.submit(request)

    def status(self, job_id: str):
        return self.store.status(job_id)

    def _authorized(self, request):
        supplied = self.current_authority(request.model_copy(deep=True))
        if not isinstance(supplied, AgentAuthority):
            raise AgentAdmissionError('Host authority provider must return typed current authority')
        current = AgentAuthority.model_validate(supplied.model_dump(), strict=True)
        if current != request.authority:
            raise AgentAdmissionError('Current principal, runtime, owner, lease or generation differs')

    def _adapter(self, status):
        registration = self.store.registration(status.request.agent_id, status.request.agent_version)
        adapter = self.adapters.get(registration.adapter_kind)
        if adapter is None:
            raise AgentAdmissionError('Registered adapter is unavailable')
        return adapter

    @contextmanager
    def _exclusive(self, job_id):
        if job_id in self._busy:
            raise AgentAdmissionError('Job already has an in-flight transition')
        self._busy.add(job_id)
        try:
            yield
        finally:
            self._busy.remove(job_id)

    def _change(self, job_id, **updates):
        with self.store.transaction():
            self.store.change(job_id, **updates)
        return self.status(job_id)

    def _bound(self, status, handle):
        handle = AgentHandle.model_validate(handle.model_dump(), strict=True)
        prior = status.handle or (status.prepared.handle if status.prepared else None)
        if (handle.job_id != status.request.job_id or handle.request_sha256 != status.request_sha256
                or handle.authority != status.request.authority
                or (prior is not None and handle.handle_id != prior.handle_id)):
            raise AgentAdmissionError('Adapter handle differs from immutable job binding')
        return handle

    def _uncertain(self, job_id, effect, error):
        self._change(job_id, state='uncertain', last_error=f'{effect}: {type(error).__name__}; no automatic replay')

    async def advance(self, job_id: str):
        with self._exclusive(job_id):
            status = self.status(job_id)
            self._authorized(status.request)
            if status.state in {'succeeded', 'failed', 'cancelled'}:
                return status
            adapter = self._adapter(status)
            if status.state == 'queued':
                status = self.store.reserve(job_id)
                try:
                    self._authorized(status.request)
                    prepared = await adapter.prepare(status.request.model_copy(deep=True))
                    prepared = AgentPrepared.model_validate(prepared.model_dump(), strict=True)
                    self._bound(status, prepared.handle)
                    self._authorized(status.request)
                    return self._change(job_id, prepared=prepared, handle=prepared.handle,
                                        state='prepared' if prepared.ready else 'waiting_approval')
                except BaseException as error:
                    self._uncertain(job_id, 'prepare', error)
                    raise
            if status.state == 'prepared':
                self._change(job_id, state='dispatch_intent')
                try:
                    self._authorized(status.request)
                    handle = await adapter.dispatch(status.prepared.model_copy(deep=True))
                    handle = self._bound(status, handle)
                    self._authorized(status.request)
                    return self._change(job_id, handle=handle, state='running', observation=None,
                                        result_proof=None, cleanup_proof=None, last_error=None)
                except BaseException as error:
                    self._uncertain(job_id, 'dispatch', error)
                    raise
            if status.state in {'waiting_approval', 'running', 'cancel_requested', 'verifying'}:
                return await self._observe(status, adapter)
            raise AgentAdmissionError('Uncertain or in-flight effects require read-only reconciliation, never replay')

    async def _observe(self, status, adapter):
        self._authorized(status.request)
        observation = await adapter.observe(status.handle.model_copy(deep=True))
        observation = AgentObservation.model_validate(observation.model_dump(), strict=True)
        handle = self._bound(status, observation.handle)
        self._authorized(status.request)
        terminal = observation.state in {'succeeded', 'failed', 'cancelled'}
        updates = dict(handle=handle, observation=observation, last_error=None,
                       result_proof=None, cleanup_proof=None)
        if terminal:
            updates['state'] = 'verifying'
        elif observation.state == 'uncertain':
            updates['state'] = 'uncertain'
        elif status.state == 'waiting_approval' and observation.state == 'ready':
            updates.update(state='prepared', prepared=AgentPrepared(handle=handle, ready=True))
        elif status.state == 'waiting_approval' and observation.state != 'waiting_approval':
            raise AgentAdmissionError('Unattempted preparation cannot adopt an active remote run')
        elif status.state == 'verifying':
            raise AgentAdmissionError('Terminal observation regressed before verification')
        status = self._change(status.request.job_id, **updates)
        if terminal:
            return await self._verify(status, adapter)
        return status

    async def _verify(self, status, adapter):
        self._authorized(status.request)
        result = await adapter.verify_result(status.handle.model_copy(deep=True))
        result = AgentResultProof.model_validate(result.model_dump(), strict=True)
        if result.handle != status.handle or result.outcome != status.observation.state:
            raise AgentAdmissionError('Independent result proof differs from current handle or terminal outcome')
        self._authorized(status.request)
        cleanup = await adapter.verify_cleanup(status.handle.model_copy(deep=True))
        cleanup = AgentCleanupProof.model_validate(cleanup.model_dump(), strict=True)
        registration = self.store.registration(status.request.agent_id, status.request.agent_version)
        if cleanup.handle != status.handle or cleanup.scope != registration.cleanup_scope:
            raise AgentAdmissionError('Cleanup proof differs from current handle or executor scope')
        self._authorized(status.request)
        complete = result.verified and cleanup.verified
        return self._change(status.request.job_id, result_proof=result, cleanup_proof=cleanup,
                            state=result.outcome if complete else 'verifying',
                            reservation_held=not complete,
                            last_error=None if complete else 'Result or cleanup is unproven; capacity remains reserved')

    async def request_cancel(self, job_id: str):
        with self._exclusive(job_id):
            status = self.status(job_id)
            self._authorized(status.request)
            if status.state in {'succeeded', 'failed', 'cancelled'}:
                return status
            if status.state == 'queued':
                return self._change(job_id, state='cancelled', last_error='Cancelled before preparation; no effect attempted')
            if status.state == 'cancel_requested':
                if status.observation is None or status.observation.state != 'ready':
                    raise AgentAdmissionError('Cancellation already requested; observe without repeating the effect')
            elif status.state not in {'prepared', 'waiting_approval', 'running'}:
                raise AgentAdmissionError('Cannot repeat or cancel an uncertain effect without reconciliation')
            adapter = self._adapter(status)
            self._change(job_id, state='cancel_intent')
            try:
                self._authorized(status.request)
                observation = await adapter.request_cancel(status.handle.model_copy(deep=True))
                observation = AgentObservation.model_validate(observation.model_dump(), strict=True)
                handle = self._bound(status, observation.handle)
                self._authorized(status.request)
                status = self._change(job_id, state='cancel_requested', handle=handle,
                                      observation=observation, last_error=None)
            except BaseException as error:
                self._uncertain(job_id, 'cancel', error)
                raise
            if observation.state in {'succeeded', 'failed', 'cancelled'}:
                status = self._change(job_id, state='verifying')
                return await self._verify(status, adapter)
            if observation.state == 'uncertain':
                return self._change(job_id, state='uncertain')
            return status

    async def reconcile(self, job_id: str):
        with self._exclusive(job_id):
            status = self.status(job_id)
            self._authorized(status.request)
            if status.state in {'succeeded', 'failed', 'cancelled'}:
                return status
            if status.state not in {'uncertain', 'verifying'} or status.handle is None:
                raise AgentAdmissionError('Reconciliation requires a durable handle; no guessed remote adoption')
            return await self._observe(status, self._adapter(status))

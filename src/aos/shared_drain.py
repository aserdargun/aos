import os
import re
from typing import Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest, identifier, now
from .lifecycle import ProcessIdentity, process_identity


class SharedDrainRequest(TypedModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    session_id: str = Field(pattern=r'^desktop-session-[a-f0-9]{32}$')
    runtime_id: str = Field(min_length=1, max_length=128)
    owner: Literal['AGENT']
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)


class SharedDrainObservation(TypedModel):
    version: Literal['1'] = '1'
    request: SharedDrainRequest
    admission_closed: bool
    local_controls_drained: bool
    blockers: list[str] = Field(max_length=128)
    cleanup_controls_closed: bool = False
    gpu_release_verified: Literal[False] = False
    remote_jobs_stopped_verified: Literal[False] = False
    native_gpu_excluded: Literal[False] = False

    @model_validator(mode='after')
    def consistent_local_observation(self):
        if self.local_controls_drained and (not self.admission_closed or self.blockers):
            raise ValueError('Local drainage requires closed admission and no observed blockers')
        return self


class SharedAdmissionDrain:
    def __init__(self, controller, scheduler, scientist_lab):
        if scheduler.controller is not controller or scientist_lab.controller is not controller:
            raise ValueError('Shared drain requires the same original desktop controller')
        self.controller = controller
        self.scheduler = scheduler
        self.scientist_lab = scientist_lab
        self._request = None
        self._latch_failures = []

    def _current(self, request):
        state = self.controller.state()
        if (state['status'] != 'running' or request.session_id != self.controller.session_id
                or request.runtime_id != self.controller.runtime.runtime_id
                or any(state[field] != getattr(request, field) for field in (
                    'session_id', 'runtime_id', 'owner', 'lease_id', 'generation'))):
            raise ValueError('Shared drain requires the exact current agent generation')

    def drain(self, request):
        request = SharedDrainRequest.model_validate(request.model_dump(), strict=True)
        with self.controller.lock:
            self._current(request)
            if self._request is not None and request != self._request:
                raise ValueError('Shared drain is already bound to another exact request')
            components = (('scheduler', self.scheduler), ('lab', self.scientist_lab))
            if self._request is None:
                self._request = request.model_copy(deep=True)
                for name, component in components:
                    try:
                        component.latch_shared_drain()
                    except Exception:
                        self._latch_failures.append(name + '.latch_unproven')
            blockers = list(self._latch_failures)
            closed = not self._latch_failures
            for name, component in components:
                try:
                    observation = component.shared_drain_status()
                    if (type(observation) is not dict or set(observation) != {'admission_closed', 'blockers'}
                            or type(observation['admission_closed']) is not bool
                            or type(observation['blockers']) is not list
                            or len(observation['blockers']) > 32
                            or any(type(value) is not str or not 1 <= len(value) <= 128
                                   for value in observation['blockers'])):
                        raise ValueError('Malformed component drainage observation')
                    closed = closed and observation['admission_closed']
                    blockers.extend(name + '.' + value for value in observation['blockers'])
                    if not observation['admission_closed']:
                        blockers.append(name + '.admission_open')
                except Exception:
                    closed = False
                    blockers.append(name + '.observation_unavailable')
            self._current(request)
            return SharedDrainObservation(request=request, admission_closed=closed,
                local_controls_drained=closed and not blockers, blockers=sorted(set(blockers)),
                cleanup_controls_closed=getattr(self.scientist_lab, 'cleanup_controls_closed', False) is True)

    def seal(self, request):
        with self.controller.lock:
            observation = self.drain(request)
            if not observation.local_controls_drained:
                raise ValueError('Shared control seal requires independently observed local drainage')
            self.scientist_lab.seal_shared_drain_controls()
            return self.persist_observation(request)

    def persist_observation(self, request):
        with self.controller.lock:
            observation = self.drain(request)
            receipt = SharedDrainReceipt(process=process_identity(os.getpid()), observation=observation)
            payload = canonical(receipt.model_dump(mode='json'))
            store = self.controller.store
            previous = store.connection.execute(
                "SELECT event_id,payload_json FROM desktop_events WHERE session_id=? "
                "AND kind='shared_admission_drain' ORDER BY rowid DESC LIMIT 1",
                (self.controller.session_id,)).fetchone()
            if previous is not None and previous['payload_json'] == payload:
                return self.read_receipt(previous['event_id'])
            event_id = identifier('event')
            self._current(request)
            with store.connection:
                store.insert('desktop_events', event_id=event_id, session_id=self.controller.session_id,
                             kind='shared_admission_drain', payload_json=payload, created_at=now())
            saved = self.read_receipt(event_id)
            self._current(request)
            return saved

    def read_receipt(self, event_id):
        with self.controller.lock:
            return read_shared_drain_receipt(self.controller.store.connection,
                session_id=self.controller.session_id, event_id=event_id)


class SharedDrainReceipt(TypedModel):
    version: Literal['1'] = '1'
    process: ProcessIdentity
    observation: SharedDrainObservation


class SavedSharedDrainObservation(TypedModel):
    receipt_id: str = Field(pattern=r'^event-[a-f0-9]{32}$')
    receipt_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    receipt: SharedDrainReceipt

    @model_validator(mode='after')
    def exact_receipt_hash(self):
        if self.receipt_sha256 != digest(self.receipt.model_dump(mode='json')):
            raise ValueError('Shared drain receipt hash differs')
        return self


def read_shared_drain_receipt(connection, *, session_id, event_id, expected_sha256=None):
    if (type(event_id) is not str or re.fullmatch(r'event-[a-f0-9]{32}', event_id) is None
            or type(session_id) is not str or re.fullmatch(r'desktop-session-[a-f0-9]{32}', session_id) is None):
        raise ValueError('Exact shared drain event and session identities required')
    row = connection.execute("SELECT payload_json FROM desktop_events WHERE event_id=? AND session_id=? "
        "AND kind='shared_admission_drain'", (event_id, session_id)).fetchone()
    if row is None or type(row[0]) is not str or len(row[0].encode()) > 65536:
        raise ValueError('Bounded shared drain receipt unavailable in this session')
    receipt = SharedDrainReceipt.model_validate_json(row[0], strict=True)
    if (receipt.observation.request.session_id != session_id
            or canonical(receipt.model_dump(mode='json')) != row[0]):
        raise ValueError('Shared drain receipt session or canonical bytes differ')
    fingerprint = digest(receipt.model_dump(mode='json'))
    if expected_sha256 is not None and expected_sha256 != fingerprint:
        raise ValueError('Shared drain receipt differs from its independently pinned hash')
    return SavedSharedDrainObservation(receipt_id=event_id, receipt_sha256=fingerprint, receipt=receipt)

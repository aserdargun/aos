"""Explicit inherited-channel readback; no listener or allocation authority."""

import array
from copy import deepcopy
from dataclasses import asdict
import math
import os
import re
import socket
import struct
import threading
import time
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, digest as intent_digest
from .scientist_admission_history import BootId, Hash, Identifier, SafeInteger, Seconds, ScientistAdmissionRecordV2
from .scientist_budget_witness import ScientistBudgetTarget, ScientistBudgetWitnessVerifier, ScientistOriginalBudgetWitness
from .scientist_evidence_transport import ScientistEvidenceCodec, _STRICT_VALIDATOR, _decode
from .scientist_intents import ScientistIntentBinding
from .scientist_protocol import Profile, ScientistTurnRequest, scientist_request_sha256
from .scientist_retained_host import ScientistRetainedHost
from .scientist_terminal import ScientistTerminalBudget, canonical, digest
from .scientist_transport import BROKER_UNIT, BrokerPeer, ScientistAdmissionError


SCHEMA = 'aos-scientist-retained-provider.v1'
FRAME_LIMIT = 128 * 1024
PROVIDER_SOURCE = 'lab/llm/aos_retained_provider.py'
PHYSICAL_SOURCE = 'lab/llm/aos_physical_readback.py'
NATIVE_SOURCE = 'lab/llm/native_runtime.py'


class ScientistRetainedProviderError(ScientistAdmissionError):
    pass


def _require(condition, message):
    if not condition:
        raise ScientistRetainedProviderError(message)


def _hash(value, length=64):
    return type(value) is str and re.fullmatch(r'[a-f0-9]{' + str(length) + '}', value) is not None


class _Evidence(TypedModel):
    schema_name: Literal['aos-scientist-terminal-evidence.v2'] = Field(alias='schema')
    version: Literal[2]
    target: ScientistBudgetTarget
    terminal_canonical: str = Field(min_length=2, max_length=FRAME_LIMIT)
    allocation_canonical: str | None
    drain_canonical: str | None
    no_admission_canonical: str | None
    result_canonical: str | None


class _Ticket(TypedModel):
    owner: Literal['aos']
    request_id: Identifier
    payload_sha256: Hash
    submitted_at: Seconds
    activation_seconds: int = Field(ge=1, le=600)
    inference_seconds: int = Field(ge=1, le=540)
    total_seconds: int = Field(ge=1, le=720)
    queue_deadline: Seconds
    owner_pid: int = Field(ge=1, le=2**31 - 1)
    owner_start_ticks: SafeInteger
    owner_boot_id: BootId
    owner_unit: str = Field(min_length=1, max_length=255)
    owner_invocation_id: Identifier
    sequence: SafeInteger
    state: Literal['done', 'expired', 'canceled']


class _Child(TypedModel):
    owner: Literal['aos']
    request_id: Identifier
    fencing_token: int = Field(ge=1, le=2**53 - 1)
    profile_id: Profile
    deployment_digest: Hash
    request_sha256: Hash
    unit: str = Field(pattern=r'^swapp-aos-gpu-turn-[a-f0-9]{32}\.service$')
    nonce: Hash
    launch_state: Literal['drained']
    created_boottime: Seconds
    total_seconds: int = Field(ge=1, le=720)
    invocation_id: Identifier
    main_pid: int = Field(ge=2, le=2**31 - 1)
    main_start_ticks: int = Field(ge=1, le=2**53 - 1)
    boot_id: BootId
    control_group: str = Field(min_length=1, max_length=512)
    observed_gpu_pids: list[int] = Field(max_length=4096)


class _Arbiter(TypedModel):
    active_owner: Literal['aos', 'lab'] | None
    active_request_id: str | None = Field(max_length=128)
    active_token: SafeInteger
    phase: Literal['activating', 'inference', 'quarantined'] | None


class _PhysicalSnapshot(TypedModel):
    schema_name: Literal['aos-scientist-physical-snapshot.v1'] = Field(alias='schema')
    version: Literal[1]
    evidence: _Evidence
    original_budget: ScientistTerminalBudget
    ticket: _Ticket | None
    child: _Child | None
    handoff_stage: Literal['start', 'go'] | None
    arbiter: _Arbiter


def _typed(model, value):
    ScientistEvidenceCodec._validate(_STRICT_VALIDATOR(model.model_json_schema()), value)
    return model.model_validate(value, strict=True)


def prepare_retained_provider_channel(channel):
    """Trusted inheritance setup must enable credentials on both ends before send."""
    _require(isinstance(channel, socket.socket) and channel.family == socket.AF_UNIX
             and channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET
             and channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0,
             'Retained provider requires a connected inherited Unix packet channel')
    for address in (channel.getsockname(), channel.getpeername()):
        _require(address in ('', b'') or type(address) is bytes
                 and re.fullmatch(b'\x00[0-9a-f]{5}', address) is not None,
                 'Retained provider cannot use a pathname or externally named endpoint')
    channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    channel.set_inheritable(False)


def _credentials(ancillary, flags):
    credentials, forbidden = [], False
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            handles = array.array('i')
            handles.frombytes(value[:len(value) - len(value) % handles.itemsize])
            for descriptor in handles:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            forbidden = True
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(value) == struct.calcsize('3i'):
            credentials.append(struct.unpack('3i', value))
        else:
            forbidden = True
    _require(not forbidden and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) and len(credentials) == 1,
             'Retained provider packet credentials are ambiguous')
    pid, uid, gid = credentials[0]
    _require(pid > 1 and uid == os.getuid() and gid >= 0, 'Retained provider packet sender differs')
    return pid, uid


class ScientistRetainedProviderClient:
    """Authenticated packets on one supplied channel; any ambiguity closes it.

    Authentication must independently resolve current systemd/PID generations.
    SO_PEERCRED cannot replace per-message SCM_CREDENTIALS after inheritance.
    """

    def __init__(self, channel, *, authenticator, expected_peer, timeout_seconds=3):
        _require(isinstance(expected_peer, BrokerPeer) and expected_peer.pid > 1
                 and expected_peer.uid == os.getuid()
                 and all(callable(getattr(authenticator, name, None)) for name in ('authenticate', 'still_current')),
                 'Retained provider requires explicit pinned broker authentication')
        _require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
                 and 0 < timeout_seconds <= 3, 'Retained provider deadline exceeds its bound')
        self._channel, self._authenticator = channel, authenticator
        self._expected_peer, self._timeout = deepcopy(expected_peer), timeout_seconds
        self._sequence, self._poisoned = 0, False
        self._lock = threading.Lock()
        try:
            prepare_retained_provider_channel(channel)
            self.verify_peer()
        except BaseException:
            self.close()
            raise

    @property
    def expected_peer(self):
        return deepcopy(self._expected_peer)

    @property
    def poisoned(self):
        return self._poisoned

    def close(self):
        self._poisoned = True
        if isinstance(self._channel, socket.socket):
            self._channel.close()

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        _require(remaining > 0, 'Retained provider deadline expired')
        return remaining

    def verify_peer(self, deadline=None):
        deadline = time.monotonic() + self._timeout if deadline is None else deadline
        self._remaining(deadline)
        expected = self.expected_peer
        _require(not self.poisoned and self._authenticator.authenticate(expected.pid, expected.uid) == expected
                 and self._authenticator.still_current(expected) is True,
                 'Retained provider broker generation changed')
        self._remaining(deadline)

    def exchange(self, *, op, target, profile_id, deployment_digest, expected_capability_sha256,
                 expected_evidence=None):
        if not self._lock.acquire(blocking=False):
            self.close()
            raise ScientistRetainedProviderError('Retained provider already has an outstanding request')
        try:
            _require(not self.poisoned and self._sequence < 2**53 - 1, 'Retained provider channel is closed or exhausted')
            deadline = time.monotonic() + self._timeout
            _typed(ScientistBudgetTarget, target)
            _require(op in ('read_budget', 'verify_physical')
                     and profile_id in ('aos.decider.turn.v1', 'aos.bonsai.recovery.v1', 'aos.bonsai.vision.v1')
                     and _hash(deployment_digest) and _hash(expected_capability_sha256), 'Retained provider request differs')
            if op == 'read_budget':
                _require(expected_evidence is None, 'Budget reads cannot take candidate evidence')
            else:
                _typed(_Evidence, expected_evidence)
                _require(expected_evidence['target'] == target, 'Physical evidence target differs')
                for name in ('terminal', 'allocation', 'drain', 'no_admission', 'result'):
                    encoded = expected_evidence[name + '_canonical']
                    if encoded is not None:
                        _decode(encoded.encode('utf-8'), FRAME_LIMIT)
            self._sequence += 1
            request = {'schema': SCHEMA, 'version': 1, 'sequence': self._sequence, 'op': op,
                       'target': deepcopy(target), 'profile_id': profile_id, 'deployment_digest': deployment_digest,
                       'expected_capability_sha256': expected_capability_sha256, 'expected_evidence': deepcopy(expected_evidence)}
            raw = canonical(request).encode('utf-8')
            _decode(raw, FRAME_LIMIT)
            prepare_retained_provider_channel(self._channel)
            self.verify_peer(deadline)
            self._channel.settimeout(self._remaining(deadline))
            _require(self._channel.send(raw) == len(raw), 'Retained provider send was incomplete')
            self.verify_peer(deadline)
            self._channel.settimeout(self._remaining(deadline))
            raw, ancillary, flags, _address = self._channel.recvmsg(
                FRAME_LIMIT + 1, socket.CMSG_SPACE(struct.calcsize('3i')) + socket.CMSG_SPACE(256 * 4),
                socket.MSG_CMSG_CLOEXEC)
            pid, uid = _credentials(ancillary, flags)
            _require((pid, uid) == (self._expected_peer.pid, self._expected_peer.uid)
                     and self._authenticator.authenticate(pid, uid) == self._expected_peer,
                     'Retained provider response sender generation differs')
            self.verify_peer(deadline)
            response = _decode(raw, FRAME_LIMIT)
            _require(set(response) == {'schema', 'version', 'sequence', 'ok', 'data', 'reason_code'}
                     and response['schema'] == SCHEMA and type(response['version']) is int and response['version'] == 1
                     and type(response['sequence']) is int and response['sequence'] == self._sequence
                     and type(response['ok']) is bool, 'Retained provider response correlation differs')
            _require(response['ok'] is True and response['reason_code'] is None and type(response['data']) is dict,
                     'Independent retained provider denied or returned a malformed result')
            data = response['data']
            if op == 'read_budget':
                witness = _typed(ScientistOriginalBudgetWitness, data)
                budget = _decode(witness.budget_canonical.encode('utf-8'), 16384)
                _typed(ScientistTerminalBudget, budget)
                _require(all(data[key] == request[key] for key in ('target', 'profile_id', 'deployment_digest'))
                         and digest(budget) == witness.budget_sha256, 'Provider budget witness binding differs')
            else:
                _typed(_PhysicalSnapshot, data)
                _require(data['evidence'] == expected_evidence, 'Provider physical snapshot evidence differs')
            self.verify_peer(deadline)
            return deepcopy(data)
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise ScientistRetainedProviderError('Retained provider exchange failed; channel closed without retry') from error
            raise
        finally:
            self._lock.release()


def _deny_current(operation, original, binding):
    raise ScientistRetainedProviderError('Independent current provider source and target authority is not configured')


class ScientistRetainedProviderAdapter:
    """Bind authenticated independent readback to exact durable original ACKs.

    verify_current(operation, original, current_binding) must independently check
    current target/source/config rights, including pinned provider and physical
    observer execution in this canonical broker, including native_runtime.py
    and its reviewed dependency/config closure, not just the minimum source set.
    Packet authentication and a
    schema tag alone cannot prove this. Control and resolution authorization
    remain separate host gates. The remote physical operation performs actual
    independent observation; this adapter does not assert an AOS GPU UUID.
    """

    def __init__(self, host, client, *, capability_control_id, capability_response_sha256,
                 reconcile_control_id, reconcile_response_sha256, verify_current=_deny_current):
        _require(isinstance(host, ScientistRetainedHost) and isinstance(client, ScientistRetainedProviderClient)
                 and host.history.store is host.store and host.history.record_version == '2.0',
                 'Retained provider requires the same original retained host and history2.0')
        _require(_hash(capability_control_id, 32) and _hash(reconcile_control_id, 32)
                 and capability_control_id != reconcile_control_id and _hash(capability_response_sha256)
                 and _hash(reconcile_response_sha256) and callable(verify_current), 'Retained provider ACK selection differs')
        self._host, self._client, self._verify_current = host, client, verify_current
        self._store, self._history = host.store, host.history
        self._binding = host.binding.model_copy(deep=True)
        self._capability_id, self._reconcile_id = capability_control_id, reconcile_control_id
        self._capability_sha, self._reconcile_sha = capability_response_sha256, reconcile_response_sha256
        self._last_fence = None

    def _authority(self, operation, original):
        _require(self._verify_current is not _deny_current and isinstance(original, ScientistAdmissionRecordV2)
                 and self._host.store is self._store and self._host.history is self._history
                 and self._host.binding == self._binding, 'Retained provider authority or original store binding differs')
        connection = self._store.connection
        transaction = connection.in_transaction
        retained, _checksum = self._history.read(original.request_id)
        _require(retained == original and original.session_id == self._binding.session_id,
                 'Retained provider original admission changed')
        self._host._journal()._current()
        row = connection.execute('SELECT binding_json FROM scientist_turn_intents WHERE request_id=?',
                                 (original.request_id,)).fetchone()
        _require(row is not None, 'Retained provider original intent is missing')
        binding = ScientistIntentBinding.model_validate_json(row['binding_json'], strict=True)
        _require(binding.runtime_id == self._binding.runtime_id and binding.session_id == original.session_id
                 and intent_digest(binding.model_dump(mode='json')) == original.intent_binding_sha256
                 and original.admission_binding.server_generation.model_dump(mode='json')
                 == {**asdict(self._client.expected_peer), 'unit': BROKER_UNIT},
                 'Retained provider original runtime or broker generation differs')
        self._client.verify_peer()
        _require(self._verify_current(operation, original.model_copy(deep=True), self._binding.model_copy(deep=True)) is None,
                 'Retained provider current authority must complete or raise')
        _require(connection.in_transaction is transaction, 'Retained provider callback changed transaction ownership')
        self._host._journal()._current()
        self._client.verify_peer()

    @staticmethod
    def _bindings(original, request=None):
        binding = original.admission_binding
        if request is not None:
            _require(request.request_id == original.request_id and scientist_request_sha256(request) == original.request_sha256
                     and request.profile_id == binding.profile_id
                     and request.deployment_digest == binding.profile_pin.deployment_digest,
                     'Provider request differs from original admission')
        return {'target': {'request_id': original.request_id, 'request_sha256': original.request_sha256,
                          'original_peer_generation_sha256': digest(binding.caller_generation.model_dump(mode='json'))},
                'profile_id': binding.profile_id, 'deployment_digest': binding.profile_pin.deployment_digest}

    def _ack(self, original, *, reconcile=False):
        control_id, fingerprint = (self._reconcile_id, self._reconcile_sha) if reconcile else (self._capability_id, self._capability_sha)
        inspected = self._host.inspect(control_id, **({'capability_control_id': self._capability_id} if reconcile else {}))
        response, request = inspected['response'], inspected['request']
        operation = 'reconcile' if reconcile else 'capability'
        _require(not inspected['pending'] and response is not None and response['ok'] is True
                 and inspected['control_id'] == response['control_id'] == control_id
                 and request['op'] == response['op'] == operation
                 and inspected['response_sha256'] == fingerprint == digest(response)
                 and inspected['admission_record_sha256'] == self._history.read(original.request_id)[1]
                 and all(request[key] == value for key, value in self._bindings(original).items()),
                 'Provider selected ACK is not the exact durable original response')
        return deepcopy(response)

    def _capability(self, original):
        response = self._ack(original)
        capability = response['data']['capability']
        _require(digest(capability) == response['capability_sha256'], 'Provider original capability hash differs')
        return digest(capability)

    def _reconcile(self, original):
        capability = self._capability(original)
        response = self._ack(original, reconcile=True)
        _require(response['capability_sha256'] == capability, 'Provider reconcile ACK capability differs')
        return response['data'], capability

    def _witness(self, original, value):
        witness = _typed(ScientistOriginalBudgetWitness, value)
        binding = original.admission_binding
        _require(all(value[key] == expected for key, expected in self._bindings(original).items())
                 and witness.profile_config_sha256 == binding.profile_pin.config_sha256
                 and witness.response_schema_sha256 == binding.profile_pin.response_schema_sha256
                 and witness.original_admission_binding_sha256 == original.admission_binding_sha256
                 and witness.original_cleanup_authorization_sha256 is None, 'Provider witness original admission differs')
        budget = _decode(witness.budget_canonical.encode('utf-8'), 16384)
        typed = _typed(ScientistTerminalBudget, budget)
        _require(digest(budget) == witness.budget_sha256 and typed.boot_id == binding.server_generation.boot_id,
                 'Provider original budget preimage or boot differs')
        return deepcopy(value)

    def create_budget_verifier(self, *, schema_sha256):
        return ScientistBudgetWitnessVerifier(self._history, schema_sha256=schema_sha256,
            verify_source=self.verify_source, read_source=self.read_source)

    def verify_source(self, request, original, witness):
        """Candidate consistency only; use create_budget_verifier to require read_source."""
        self._authority('read_budget', original)
        self._bindings(original, request)
        self._capability(original)
        self._witness(original, witness.model_dump(mode='json', by_alias=True))
        self._authority('read_budget', original)

    def read_source(self, request, original):
        self._authority('read_budget', original)
        capability = self._capability(original)
        value = self._client.exchange(op='read_budget', **self._bindings(original, request), expected_capability_sha256=capability)
        result = self._witness(original, value)
        self._authority('read_budget', original)
        _require(self._capability(original) == capability, 'Provider capability ACK changed during budget readback')
        return result

    def verify_resolver(self, original, terminal):
        self._authority('verify_physical', original)
        retained, _capability = self._reconcile(original)
        _require(canonical(terminal.model_dump(mode='json', by_alias=True)) == retained['evidence']['terminal_canonical'],
                 'Provider resolver terminal differs from exact retained ACK')
        self._authority('verify_physical', original)

    def _physical(self, original, terminal, allocation, drain, no_admission, snapshot):
        _typed(_PhysicalSnapshot, snapshot)
        _require(snapshot['original_budget'] == terminal.original_budget.model_dump(mode='json'),
                 'Provider physical original budget differs')
        arbiter = snapshot['arbiter']
        active = arbiter['active_owner'] is not None
        _require(active == (arbiter['active_request_id'] is not None) == (arbiter['phase'] is not None)
                 and (arbiter['active_owner'], arbiter['active_request_id']) != ('aos', original.request_id)
                 and (self._last_fence is None or arbiter['active_token'] >= self._last_fence),
                 'Provider original target is active/quarantined or current fence regressed')
        child, ticket = snapshot['child'], snapshot['ticket']
        if allocation is None:
            _require(no_admission is not None and terminal.release_outcome == 'never_admitted'
                     and child is None and drain is None and snapshot['handoff_stage'] is None
                     and (ticket is None or ticket['state'] in ('canceled', 'expired')),
                     'Provider no-admission snapshot contains an allocation or child')
        else:
            _require(child is not None and ticket is not None and drain is not None
                     and no_admission is None and ticket['state'] in ('done', 'expired')
                     and snapshot['handoff_stage'] == drain['handoff_stage']
                     and child['fencing_token'] == allocation['lease']['fencing_token']
                     and arbiter['active_token'] >= child['fencing_token']
                     and (not active or arbiter['active_token'] > child['fencing_token']),
                     'Provider allocation, ticket or current arbiter differs')
            row = self._store.connection.execute('SELECT request_json FROM scientist_turn_intents WHERE request_id=?',
                                                  (original.request_id,)).fetchone()
            request = ScientistTurnRequest.model_validate_json(row['request_json'], strict=True)
            _require(child['request_id'] == original.request_id and child['profile_id'] == terminal.profile_id
                     and child['deployment_digest'] == terminal.deployment_digest
                     and child['request_sha256'] == digest(request.payload)
                     and all(child[key] == value for key, value in drain['child_intent'].items())
                     and {'unit': child['unit'], 'invocation_id': child['invocation_id'], 'pid': child['main_pid'],
                          'start_ticks': child['main_start_ticks'], 'boot_id': child['boot_id'],
                          'control_group': child['control_group']} == drain['child_generation']
                     and child['observed_gpu_pids'] == drain['observed_gpu_pids']
                     and all(type(pid) is int and 1 <= pid <= 2**31 - 1 for pid in child['observed_gpu_pids'])
                     and child['observed_gpu_pids'] == sorted(set(child['observed_gpu_pids'])),
                     'Provider child nonce, generation, payload or original GPU PID provenance differs')
        if ticket is not None:
            principal = original.admission_binding.caller_generation
            _require(ticket['request_id'] == original.request_id and ticket['payload_sha256'] == original.request_sha256
                     and all(ticket['owner_' + key] == getattr(principal, key) for key in
                             ('pid', 'start_ticks', 'boot_id', 'unit', 'invocation_id'))
                     and all(ticket[key] == snapshot['original_budget'][key] for key in
                             ('activation_seconds', 'inference_seconds', 'total_seconds', 'queue_deadline')),
                     'Provider original ticket ownership or budget differs')
        return arbiter['active_token']

    def verify_physical(self, original, terminal, allocation, drain, no_admission):
        self._authority('verify_physical', original)
        retained, capability = self._reconcile(original)
        evidence = retained['evidence']
        for name, value in (('terminal', terminal.model_dump(mode='json', by_alias=True)),
                            ('allocation', allocation), ('drain', drain), ('no_admission', no_admission)):
            _require((None if value is None else canonical(value)) == evidence[name + '_canonical'],
                     'Provider physical arguments differ from complete retained canonical preimages')
        snapshot = self._client.exchange(op='verify_physical', **self._bindings(original),
            expected_capability_sha256=capability, expected_evidence=evidence)
        fence = self._physical(original, terminal, allocation, drain, no_admission, snapshot)
        self._authority('verify_physical', original)
        _require(self._reconcile(original) == (retained, capability), 'Provider ACK closure changed during physical readback')
        self._last_fence = fence

"""Source-derived bootstrap candidate, not a jointly reviewed full wire schema."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import math
import os
import threading
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .contracts import TypedModel
from .scientist_admission_history import (
    BootId, Hash, Identifier, Seconds, ScientistAdmissionBindingV2,
    ScientistAdmissionCaptureV2, ScientistCallerGeneration, ScientistControlSchemaPin,
    ScientistInferSchemaPin, ScientistProfilePinV2, ScientistSourceFingerprints,
)
from .scientist_async import ScientistHostBridge
from .scientist_evidence_client import ScientistEvidenceClient, _deny_intent
from .scientist_evidence_transport import (
    REQUEST_LIMIT, RESPONSE_LIMIT, ScientistEvidenceCodec, _STRICT_VALIDATOR, _decode,
)
from .scientist_intents import ScientistIntentBinding
from .scientist_protocol import Profile, ScientistTurnRequest, scientist_request_frame
from .scientist_terminal import TERMINAL_DESCRIPTOR_SHA256, canonical, digest
from .scientist_transport import BROKER_UNIT, BrokerPeer, ScientistAdmissionError
from .storage import TrajectoryStore


BOOTSTRAP_SCHEMA = 'aos-scientist-control.v1'
CONTROL_DESCRIPTOR_SHA256 = '110838e17f1ea899067d9c9ec61769b6fe26a607ecac986f83786b9f8943068d'
INFER_DESCRIPTOR_SHA256 = 'a5aada91670f3741d822148ebc5bf9a2d4775362bda66bc4825def0960338d14'
HISTORY_DESCRIPTOR_SHA256 = 'fbd009fe2b09c2a4da7cc00d9862bcabb9d9b86f30c0f558ae56145656690c9f'
BOOTSTRAP_PROPOSAL = {
    'status': 'source-derived-local-candidate',
    'scientist_commit': '737b80b67167287f84476f6999fbf1e854a160ca',
    'source': 'lab/llm/aos_gpu_control.py:CONTROL_CONTRACT,decode_request,LabAOSControl._mint',
    'descriptor_sha256': CONTROL_DESCRIPTOR_SHA256,
    'full_wire_schema_reviewed': False,
}


def _require(condition, message):
    if not condition:
        raise ScientistAdmissionError(message)


def _deny_bootstrap(*arguments):
    raise ScientistAdmissionError('Trusted bootstrap authority and expected binding are not configured')


class _BootstrapRequest(TypedModel):
    schema_name: Literal['aos-scientist-control.v1'] = Field(alias='schema')
    version: Literal[1]
    op: Literal['capability']
    control_id: Identifier
    expected_capability_sha256: None
    profile_id: Profile
    deployment_digest: Hash
    target: None


class _BootstrapCapability(ScientistProfilePinV2):
    admission_binding: ScientistAdmissionBindingV2
    admission_binding_sha256: Hash
    server_generation_sha256: Hash
    caller_generation: ScientistCallerGeneration
    caller_generation_sha256: Hash
    policy_sha256: Hash
    source_fingerprints: ScientistSourceFingerprints
    profile_id: Profile
    infer_schema: ScientistInferSchemaPin
    control_schema: ScientistControlSchemaPin
    history_schema_sha256: Hash | None
    operations: list[Literal['cancel', 'capability', 'reconcile', 'status']] = Field(min_length=4, max_length=4)
    request_bytes: Literal[8192]
    response_bytes: Literal[131072]
    frame_seconds: Literal[5.0]
    call_seconds: Literal[10.0]
    boot_id: BootId
    issued_boottime: Seconds
    expires_boottime: Seconds


class _BootstrapData(TypedModel):
    capability: _BootstrapCapability
    admission: Literal['enabled']
    reason_code: Literal['enabled']


class _BootstrapResponse(TypedModel):
    schema_name: Literal['aos-scientist-control.v1'] = Field(alias='schema')
    version: Literal[1]
    control_id: Identifier
    op: Literal['capability']
    ok: Literal[True]
    capability_sha256: Hash
    data: _BootstrapData
    error: None


class ScientistBootstrapCodec(ScientistEvidenceCodec):
    """Closed bootstrap-only candidate; the pinned hash is a descriptor hash.

    No base codec initialization or retained-target schema is reused. All three
    transport entry points are overridden; no target or other operation fits.
    """

    def __init__(self, *, control_descriptor_sha256):
        _require(control_descriptor_sha256 == CONTROL_DESCRIPTOR_SHA256,
                 'Bootstrap requires the explicit observed control-v1 descriptor pin')
        self._request_validator = _STRICT_VALIDATOR(_BootstrapRequest.model_json_schema())
        self._response_validator = _STRICT_VALIDATOR(_BootstrapResponse.model_json_schema())

    def encode_request(self, value):
        try:
            raw = canonical(value).encode('utf-8')
        except (TypeError, ValueError, UnicodeError, RecursionError) as error:
            raise ScientistAdmissionError('Bootstrap request cannot be encoded') from error
        self.decode_request(raw)
        return raw

    def decode_request(self, raw):
        value = _decode(raw, REQUEST_LIMIT - 1)
        self._validate(self._request_validator, value)
        return value

    def decode_response(self, raw, original_request_bytes):
        request = self.decode_request(original_request_bytes)
        response = _decode(raw, RESPONSE_LIMIT - 1)
        self._validate(self._response_validator, response)
        capability = response['data']['capability']
        binding = capability['admission_binding']
        _require(response['control_id'] == request['control_id']
                 and response['capability_sha256'] == digest(capability)
                 and capability['admission_binding_sha256'] == digest(binding)
                 and capability['server_generation_sha256'] == digest(binding['server_generation'])
                 and capability['caller_generation_sha256'] == digest(binding['caller_generation'])
                 and capability['caller_generation'] == binding['caller_generation'],
                 'Bootstrap capability hashes or request/generation binding differ')
        _require(all(capability[key] == binding[key] for key in
                     ('policy_sha256', 'source_fingerprints', 'profile_id', 'infer_schema', 'control_schema'))
                 and all(capability[key] == value for key, value in binding['profile_pin'].items())
                 and capability['profile_id'] == request['profile_id']
                 and capability['deployment_digest'] == request['deployment_digest'],
                 'Bootstrap policy/source/profile/output binding differs')
        _require(capability['infer_schema']['sha256'] == INFER_DESCRIPTOR_SHA256
                 and capability['control_schema']['sha256'] == CONTROL_DESCRIPTOR_SHA256
                 and binding['terminal_schema']['sha256'] == TERMINAL_DESCRIPTOR_SHA256
                 and capability['history_schema_sha256'] in (None, HISTORY_DESCRIPTOR_SHA256)
                 and capability['operations'] == ['cancel', 'capability', 'reconcile', 'status']
                 and capability['boot_id'] == binding['server_generation']['boot_id']
                 == binding['caller_generation']['boot_id']
                 and capability['expires_boottime'] == capability['issued_boottime'] + 60,
                 'Bootstrap descriptor, limits, boot or freshness interval differs')
        return response


class ScientistBootstrapCapture:
    """Prefetch outside SQL, then consume once in the original intent transaction.

    verify_current(request, intent_binding, infer_peer) authorizes bootstrap and
    checks the current caller process/service and original desktop authority.
    verify_capture(request, intent_binding, infer_peer, capture) independently
    checks expected caller generation, server, policy, source fingerprints and
    complete profile/output pins. ACK self-consistency cannot supply this trust.
    persist_intent uses the EvidenceClient before-send durable-writer signature;
    the host must supply storage independent of the original intent transaction.
    No network or transaction commit occurs in __call__. This does not implement
    producer activation, deployed capability confirmation, or durable recovery.
    """

    def __init__(self, store, socket_path, *, codec, verify_current=_deny_bootstrap,
                 verify_capture=_deny_bootstrap, persist_intent=_deny_intent,
                 authenticator=None, timeout_seconds=10, clock=None):
        if not isinstance(store, TrajectoryStore) or not isinstance(codec, ScientistBootstrapCodec):
            raise TypeError('Bootstrap capture requires the original store and explicit bootstrap codec')
        if not all(callable(callback) for callback in (verify_current, verify_capture, persist_intent)):
            raise TypeError('Bootstrap callbacks must be explicit trusted callables')
        self.store = store
        self._codec = codec
        self._verify_current, self._verify_capture = verify_current, verify_capture
        self._persist = persist_intent
        self._clock = clock if clock is not None else lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
        if not callable(self._clock):
            raise TypeError('Bootstrap clock must be callable')
        self._client = ScientistEvidenceClient(socket_path, codec=codec, authenticator=authenticator,
            authorize=self._authorize, persist_intent=self._persist_outside, timeout_seconds=timeout_seconds)
        self._lock = threading.Lock()
        self._context = None
        self._prepared = None
        self._attempted = set()
        self._blocked = False

    @staticmethod
    def _freeze(request, binding, peer):
        request = ScientistTurnRequest.model_validate_json(scientist_request_frame(request)[:-1], strict=True)
        binding = ScientistIntentBinding.model_validate(binding.model_dump(mode='json'), strict=True)
        _require(binding.owner == 'AGENT' and isinstance(peer, BrokerPeer)
                 and peer.uid == os.getuid() and peer.pid > 1,
                 'Bootstrap requires the actual current inference peer and AGENT intent')
        return request, binding, deepcopy(peer)

    def _current(self, context, *, in_transaction):
        _require(self.store.connection.in_transaction is in_transaction,
                 'Bootstrap callback transaction boundary differs')
        _require(self._verify_current(*deepcopy(context)) is None,
                 'Bootstrap current authority must complete or raise')
        _require(self.store.connection.in_transaction is in_transaction,
                 'Bootstrap current authority changed transaction ownership')

    def _authorize(self, control, peer):
        _require(not self.store.connection.in_transaction,
                 'Bootstrap preparation lost its outside-transaction boundary')
        _require(self._context is not None and peer == self._context[2]
                 and control['profile_id'] == self._context[0].profile_id
                 and control['deployment_digest'] == self._context[0].deployment_digest,
                 'Bootstrap socket peer or profile differs from original inference')
        self._current(self._context, in_transaction=False)

    def _persist_outside(self, *arguments):
        _require(not self.store.connection.in_transaction,
                 'Bootstrap durable intent requires an outside-transaction boundary')
        _require(self._persist(*arguments) is None,
                 'Bootstrap durable intent must complete or raise')
        _require(not self.store.connection.in_transaction,
                 'Bootstrap durable writer changed original transaction ownership')

    def _verified_capture(self, context, response, *, in_transaction, guard=None):
        request, binding, peer = context
        capability = response['data']['capability']
        stable = capability['admission_binding']
        _require(stable['server_generation'] == {**asdict(peer), 'unit': BROKER_UNIT}
                 and stable['caller_generation']['pid'] == os.getpid()
                 and stable['caller_generation']['uid'] == os.getuid()
                 and stable['profile_id'] == request.profile_id
                 and stable['profile_pin']['deployment_digest'] == request.deployment_digest,
                 'Bootstrap ACK differs from original inference peer or actual caller process')
        capture = ScientistAdmissionCaptureV2.model_validate({
            'admission_binding': stable, 'capability_sha256': response['capability_sha256'],
            'capability_freshness': {key: capability[key] for key in
                                     ('boot_id', 'issued_boottime', 'expires_boottime')}}, strict=True)
        self._current(context, in_transaction=in_transaction)
        if guard is not None:
            guard()
        _require(self._verify_capture(*deepcopy(context), capture.model_copy(deep=True)) is None,
                 'Bootstrap expected binding verifier must complete or raise')
        if guard is not None:
            guard()
        self._current(context, in_transaction=in_transaction)
        if guard is not None:
            guard()
        observed = self._clock()
        _require(type(observed) in (int, float) and math.isfinite(observed)
                 and capability['issued_boottime'] <= observed < capability['expires_boottime'],
                 'Bootstrap capability expired or is not yet fresh')
        return capture

    def _begin_prepare(self, request, binding, peer):
        _require(not self.store.connection.in_transaction,
                 'Bootstrap network preparation cannot run inside the original SQL transaction')
        _require(self._verify_current is not _deny_bootstrap and self._verify_capture is not _deny_bootstrap
                 and self._persist is not _deny_intent,
                 'Bootstrap requires explicit current/expected authority and durable intent persistence')
        context = self._freeze(request, binding, peer)
        _require(not self._blocked and self._prepared is None
                 and context[0].request_id not in self._attempted and len(self._attempted) < 256,
                 'Bootstrap replay, pending capture or uncertain preparation prevents another request')
        self._current(context, in_transaction=False)
        self._context = context
        self._attempted.add(context[0].request_id)
        control = {'schema': BOOTSTRAP_SCHEMA, 'version': 1, 'op': 'capability',
                   'control_id': uuid4().hex, 'profile_id': context[0].profile_id,
                   'deployment_digest': context[0].deployment_digest,
                   'target': None, 'expected_capability_sha256': None}
        return context, control

    def _complete_prepare(self, context, control, response, *, guard=None):
        self._verified_capture(context, response, in_transaction=False, guard=guard)
        _require(not self.store.connection.in_transaction,
                 'Bootstrap preparation callbacks entered the original transaction')
        self._prepared = (context, self._codec.encode_request(control), canonical(response).encode('utf-8'))

    def prepare(self, request, binding, peer):
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Bootstrap preparation or consumption is already active')
        try:
            context, control = self._begin_prepare(request, binding, peer)
            response = self._client.exchange(control)
            self._complete_prepare(context, control, response)
        except BaseException:
            if self._context is not None:
                self._blocked = True
            raise
        finally:
            self._context = None
            self._lock.release()

    async def prepare_async(self, request, binding, expected_infer_peer, *, cancel_event=None, deadline=None):
        """Stage before infer connect; socket work is off-loop, all SQL stays here.

        Per-call bridge/client wrappers never replace shared callback fields.
        Cancellation retains the preparation lock until its worker is terminal.
        The expected peer must come from independent trusted pre-connect setup.
        """
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Bootstrap preparation or consumption is already active')
        cancelled = threading.Event() if cancel_event is None else cancel_event
        worker = None
        try:
            _require(isinstance(cancelled, threading.Event), 'Bootstrap cancellation requires a thread event')
            now = time.monotonic()
            _require(deadline is None or type(deadline) in (int, float) and math.isfinite(deadline),
                     'Bootstrap deadline must be finite')
            control_deadline = min(now + self._client.timeout_seconds, deadline if deadline is not None else float('inf'))
            verification_deadline = control_deadline if deadline is None else deadline
            _require(not cancelled.is_set() and control_deadline > now, 'Bootstrap preparation was cancelled or expired')
            context, control = self._begin_prepare(request, binding, expected_infer_peer)
            bridge = ScientistHostBridge(asyncio.get_running_loop(), cancelled, control_deadline)
            authorize, persist = self._authorize, self._persist_outside
            remaining = control_deadline - time.monotonic()
            _require(not cancelled.is_set() and remaining > 0, 'Bootstrap control preparation was cancelled or expired')
            client = ScientistEvidenceClient(self._client.socket_path, codec=self._codec,
                authenticator=self._client.authenticator, timeout_seconds=remaining,
                authorize=lambda *arguments: bridge.invoke(authorize, *arguments),
                persist_intent=lambda *arguments: bridge.invoke(persist, *arguments))
            worker = asyncio.create_task(asyncio.to_thread(client.exchange, control, cancel_event=cancelled))
            await asyncio.wait({worker})
            response = worker.result()
            _require(not cancelled.is_set() and time.monotonic() < control_deadline,
                     'Bootstrap control response lost its authority or deadline')
            def active():
                _require(not cancelled.is_set() and time.monotonic() < verification_deadline,
                         'Bootstrap preparation lost its authority or deadline')
            active()
            self._complete_prepare(context, control, response, guard=active)
            active()
        except BaseException:
            if isinstance(cancelled, threading.Event):
                cancelled.set()
            if worker is not None:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if not worker.cancelled():
                    worker.exception()
            if self._context is not None:
                self._blocked = True
                self._prepared = None
            raise
        finally:
            self._context = None
            self._lock.release()

    def __call__(self, request, binding, peer):
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Bootstrap preparation or consumption is already active')
        try:
            _require(self.store.connection.in_transaction and self._prepared is not None,
                     'Bootstrap capture requires a prefetched ACK and original intent transaction')
            prepared, self._prepared = self._prepared, None
            context = self._freeze(request, binding, peer)
            _require(context == prepared[0], 'Bootstrap prepared ACK differs from the exact original intent or peer')
            response = self._codec.decode_response(prepared[2], prepared[1])
            capture = self._verified_capture(context, response, in_transaction=True)
            _require(self.store.connection.in_transaction,
                     'Bootstrap capture callbacks changed original transaction ownership')
            return capture
        finally:
            self._lock.release()

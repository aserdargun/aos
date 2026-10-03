from dataclasses import asdict
import json
from pathlib import PurePosixPath
import time
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest, now
from .scientist_intents import ScientistIntentBinding
from .scientist_protocol import (
    Profile, ScientistTurnRequest, _reject_constant, _unique_object,
    scientist_request_frame, scientist_request_sha256,
)
from .scientist_transport import BROKER_UNIT, BrokerPeer, ScientistAdmissionError


Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Identifier = Annotated[str, Field(pattern=r'^[a-f0-9]{32}$')]
BootId = Annotated[str, Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')]
SafeInteger = Annotated[int, Field(ge=0, le=2**53 - 1)]
Seconds = Annotated[int | float, Field(ge=0, le=2**53 - 1)]


class ScientistServerGeneration(TypedModel):
    uid: SafeInteger
    pid: int = Field(ge=1, le=2**31 - 1)
    start_ticks: SafeInteger
    boot_id: BootId
    unit: str = Field(min_length=1, max_length=255, pattern=r'^[a-zA-Z0-9_.@-]+\.service$')
    invocation_id: Identifier
    control_group: str = Field(min_length=1, max_length=4096, pattern=r'^/')

    @model_validator(mode='after')
    def valid_cgroup(self):
        if '..' in PurePosixPath(self.control_group).parts:
            raise ValueError('Scientist generation cgroup cannot escape its path')
        return self


class ScientistCallerGeneration(ScientistServerGeneration):
    parent_pid: int = Field(ge=1, le=2**31 - 1)
    parent_start_ticks: SafeInteger


class ScientistSourceFingerprints(TypedModel):
    scientist: Hash
    aos: Hash


class ScientistProfilePin(TypedModel):
    deployment_digest: Hash
    manifest_sha256: Hash
    config_sha256: Hash
    response_schema_sha256: Hash


class ScientistOutputContractPin(TypedModel):
    name: Literal['aos-scientist-profile-output.v2']
    version: Literal[2]
    bundle_sha256: Hash

    @model_validator(mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not dict or type(value.get('version')) is not int:
            raise ValueError('Scientist output contract version requires an integer lexeme')
        return value


class ScientistProfilePinV2(ScientistProfilePin):
    output_contract: ScientistOutputContractPin


class ScientistSchemaPin(TypedModel):
    version: Literal[1]
    sha256: Hash

    @model_validator(mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not dict or type(value.get('version')) is not int:
            raise ValueError('Scientist schema version requires an integer lexeme')
        return value


class ScientistInferSchemaPin(ScientistSchemaPin):
    name: Literal['aos-scientist-runtime.v1']


class ScientistControlSchemaPin(ScientistSchemaPin):
    name: Literal['aos-scientist-control.v1']


class ScientistTerminalSchemaPin(ScientistSchemaPin):
    name: Literal['aos-scientist-terminal.v1']


class ScientistAdmissionBinding(TypedModel):
    server_generation: ScientistServerGeneration
    caller_generation: ScientistCallerGeneration
    policy_sha256: Hash
    source_fingerprints: ScientistSourceFingerprints
    profile_id: Profile
    profile_pin: ScientistProfilePin | ScientistProfilePinV2
    infer_schema: ScientistInferSchemaPin
    control_schema: ScientistControlSchemaPin
    terminal_schema: ScientistTerminalSchemaPin


class ScientistAdmissionBindingV2(ScientistAdmissionBinding):
    profile_pin: ScientistProfilePinV2


class ScientistCapabilityFreshness(TypedModel):
    boot_id: BootId
    issued_boottime: Seconds
    expires_boottime: Seconds

    @model_validator(mode='after')
    def ordered(self):
        if not self.issued_boottime < self.expires_boottime:
            raise ValueError('Scientist capability freshness interval is invalid')
        return self


class ScientistAdmissionCapture(TypedModel):
    admission_binding: ScientistAdmissionBinding
    capability_sha256: Hash
    capability_freshness: ScientistCapabilityFreshness


class ScientistAdmissionCaptureV2(ScientistAdmissionCapture):
    admission_binding: ScientistAdmissionBindingV2


class ScientistAdmissionRecord(ScientistAdmissionCapture):
    schema_version: Literal['1.0']
    request_id: Identifier
    request_sha256: Hash
    session_id: str = Field(min_length=1, max_length=128)
    intent_binding_sha256: Hash
    admission_binding_sha256: Hash
    captured_boottime: Seconds

    @model_validator(mode='after')
    def original_identity(self):
        binding = self.admission_binding
        freshness = self.capability_freshness
        if (digest(binding.model_dump(mode='json')) != self.admission_binding_sha256
                or binding.server_generation.boot_id != binding.caller_generation.boot_id
                or freshness.boot_id != binding.server_generation.boot_id
                or not freshness.issued_boottime <= self.captured_boottime < freshness.expires_boottime):
            raise ValueError('Scientist original binding hash, boot or capture freshness differs')
        return self


class ScientistAdmissionRecordV2(ScientistAdmissionRecord):
    schema_version: Literal['2.0']
    admission_binding: ScientistAdmissionBindingV2


def _deny_capture(request, binding, peer):
    raise ScientistAdmissionError('Trusted original Scientist admission capture is not configured')


def _deny_current(record, request, binding, peer):
    raise ScientistAdmissionError('Current Scientist admission identity verifier is not configured')


def _boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _decode(raw, bound=32768):
    if type(raw) is not str or not 0 < len(raw.encode('utf-8')) <= bound:
        raise ScientistAdmissionError('Scientist admission history exceeds its canonical bound')
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


class ScientistAdmissionHistory:
    def __init__(self, store, *, capture=_deny_capture, verify_current=_deny_current, clock=_boottime,
                 record_version='1.0'):
        if type(record_version) is not str or record_version not in ('1.0', '2.0'):
            raise ValueError('Scientist admission record version must be explicitly supported')
        self.store = store
        self.capture = capture
        self.verify_current = verify_current
        self.clock = clock
        self._record_version = record_version

    @property
    def record_version(self):
        return self._record_version

    def _context(self, record, request, binding, peer):
        stable = record.admission_binding
        if (binding.owner != 'AGENT' or record.request_id != request.request_id
                or record.request_sha256 != scientist_request_sha256(request)
                or record.session_id != binding.session_id
                or record.intent_binding_sha256 != digest(binding.model_dump(mode='json'))
                or stable.profile_id != request.profile_id
                or stable.profile_pin.deployment_digest != request.deployment_digest
                or canonical(stable.server_generation.model_dump(mode='json'))
                != canonical({**asdict(peer), 'unit': BROKER_UNIT})):
            raise ScientistAdmissionError('Scientist admission identity differs from the original intent or peer')

    def _verify(self, record, request, binding, peer):
        connection = self.store.connection
        if not connection.in_transaction:
            raise ScientistAdmissionError('Scientist admission verification requires the intent transaction')
        if (record.schema_version != self.record_version
                or record.schema_version == '1.0'
                and isinstance(record.admission_binding.profile_pin, ScientistProfilePinV2)):
            raise ScientistAdmissionError('Scientist historical admission version cannot authorize current work')
        self._context(record, request, binding, peer)
        if self.verify_current(record.model_copy(deep=True), request.model_copy(deep=True),
                               binding.model_copy(deep=True), peer) is not None:
            raise ScientistAdmissionError('Scientist admission verifier must complete or raise')
        if not connection.in_transaction:
            raise ScientistAdmissionError('Scientist admission verifier changed transaction ownership')

    def check_capture_freshness(self, record):
        observed = self.clock()
        if (type(observed) not in (int, float)
                or not record.capability_freshness.issued_boottime <= observed
                < record.capability_freshness.expires_boottime):
            raise ScientistAdmissionError('Scientist original capability expired during admission capture')

    def capture_intent(self, request, binding, peer):
        connection = self.store.connection
        if not connection.in_transaction:
            raise ScientistAdmissionError('Scientist admission capture requires the intent transaction')
        if connection.execute('SELECT 1 FROM scientist_admission_history WHERE request_id=?',
                              (request.request_id,)).fetchone() is not None:
            raise ScientistAdmissionError('Scientist original admission identity cannot be recaptured')
        captured = self.capture(request.model_copy(deep=True), binding.model_copy(deep=True), peer)
        if isinstance(captured, ScientistAdmissionCapture):
            captured = captured.model_dump(mode='json')
        capture_model, record_model = {
            '1.0': (ScientistAdmissionCapture, ScientistAdmissionRecord),
            '2.0': (ScientistAdmissionCaptureV2, ScientistAdmissionRecordV2),
        }[self.record_version]
        captured = capture_model.model_validate(captured, strict=True)
        if self.record_version == '1.0' and isinstance(captured.admission_binding.profile_pin, ScientistProfilePinV2):
            raise ScientistAdmissionError('Scientist output-pinned capture requires explicit record version 2.0')
        record = record_model.model_validate({**captured.model_dump(mode='json'),
            'schema_version': self.record_version, 'request_id': request.request_id,
            'request_sha256': scientist_request_sha256(request), 'session_id': binding.session_id,
            'intent_binding_sha256': digest(binding.model_dump(mode='json')),
            'admission_binding_sha256': digest(captured.admission_binding.model_dump(mode='json')),
            'captured_boottime': self.clock()}, strict=True)
        self._verify(record, request, binding, peer)
        raw = canonical(record.model_dump(mode='json'))
        _decode(raw)
        connection.execute('INSERT INTO scientist_admission_history VALUES(?,?,?,?,?,?,?,?)',
            (record.request_id, record.request_sha256, record.session_id, record.intent_binding_sha256,
             record.admission_binding_sha256, digest(record.model_dump(mode='json')), raw, now()))
        self._verify(record, request, binding, peer)
        self.check_capture_freshness(record)
        return record

    def read(self, request_id):
        try:
            row = self.store.connection.execute('SELECT * FROM scientist_admission_history WHERE request_id=?',
                                                (request_id,)).fetchone()
            if row is None:
                raise ScientistAdmissionError('Scientist original admission identity is missing')
            value = _decode(row['record_json'])
            if type(value) is not dict:
                raise ScientistAdmissionError('Scientist admission record must be an object')
            record_model = {'1.0': ScientistAdmissionRecord, '2.0': ScientistAdmissionRecordV2}.get(
                value.get('schema_version'))
            if record_model is None:
                raise ScientistAdmissionError('Scientist admission record version is unsupported')
            record = record_model.model_validate(value, strict=True)
            value = record.model_dump(mode='json')
            if (canonical(value) != row['record_json'] or digest(value) != row['record_sha256']
                    or any(getattr(record, field) != row[field] for field in
                           ('request_id', 'request_sha256', 'session_id', 'intent_binding_sha256',
                            'admission_binding_sha256'))):
                raise ScientistAdmissionError('Scientist admission history canonical identity differs')
            intent = self.store.connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                                   (request_id,)).fetchone()
            if intent is None or intent['request_sha256'] != record.request_sha256:
                raise ScientistAdmissionError('Scientist admission history lost its original intent')
            request = ScientistTurnRequest.model_validate(_decode(intent['request_json'], 128 * 1024), strict=True)
            binding = ScientistIntentBinding.model_validate(_decode(intent['binding_json']), strict=True)
            peer = BrokerPeer(**_decode(intent['broker_peer_json']))
            if (scientist_request_frame(request)[:-1].decode() != intent['request_json']
                    or binding.session_id != intent['session_id']):
                raise ScientistAdmissionError('Scientist original request or session differs')
            self._context(record, request, binding, peer)
            return record, row['record_sha256']
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ScientistAdmissionError('Scientist admission history is malformed') from error

    def verify_intent(self, request, binding, peer):
        record, checksum = self.read(request.request_id)
        self._verify(record, request, binding, peer)
        return record

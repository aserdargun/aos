import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .contracts import TypedModel
from .scientist_admission_history import (
    BootId, Hash, Identifier, SafeInteger, Seconds, ScientistAdmissionBindingV2,
    ScientistAdmissionHistory, ScientistAdmissionRecordV2, ScientistCallerGeneration,
)
from .scientist_protocol import (
    Profile, ScientistTurnReceipt, ScientistTurnRequest, _reject_constant, _unique_object,
    scientist_request_frame, scientist_request_sha256,
)
from .scientist_transport import ScientistAdmissionError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


TERMINAL_DESCRIPTOR_SHA256 = digest({'schema': 'aos-scientist-terminal.v1', 'fields': [
    'schema', 'request_id', 'request_sha256', 'original_principal', 'profile_id',
    'deployment_digest', 'profile_config_sha256', 'response_schema_sha256',
    'admission_binding', 'admission_binding_sha256', 'original_budget',
    'allocation_binding_sha256', 'child_generation', 'drain_evidence_sha256',
    'no_admission_evidence_sha256', 'release_outcome', 'terminal_state', 'reason_code',
    'result_sha256', 'recorded_boot_id', 'recorded_boottime', 'receipt_sha256',
]})


class ScientistTerminalChild(TypedModel):
    unit: str = Field(pattern=r'^swapp-aos-gpu-turn-[a-f0-9]{32}\.service$')
    invocation_id: Identifier
    pid: int = Field(ge=2, le=2**31 - 1)
    start_ticks: SafeInteger
    boot_id: BootId
    control_group: str = Field(min_length=1, max_length=512)

    @model_validator(mode='after')
    def exact_cgroup(self):
        parts = self.control_group.split('/')
        if parts[0] or parts[-1] != self.unit or any(part in ('', '.', '..') for part in parts[1:]):
            raise ValueError('Terminal child cgroup differs from its exact unit')
        return self


class ScientistTerminalBudget(TypedModel):
    activation_seconds: int = Field(ge=1, le=600)
    inference_seconds: int = Field(ge=1, le=540)
    total_seconds: int = Field(ge=1, le=720)
    queue_seconds: int = Field(ge=1, le=2**53 - 1)
    max_output_tokens: int = Field(ge=1, le=512)
    context_tokens: int = Field(ge=256, le=16384)
    activation_deadline: Seconds | None
    inference_deadline: Seconds | None
    total_deadline: Seconds | None
    envelope_deadline: Seconds | None
    queue_deadline: Seconds | None
    admitted_boottime: Seconds | None
    boot_id: BootId

    @model_validator(mode='after')
    def original_limits(self):
        if (self.total_seconds < self.activation_seconds + self.inference_seconds
                or self.queue_seconds < self.total_seconds + 30):
            raise ValueError('Terminal original budgets do not cover the bounded turn and drain')
        return self


class ScientistTerminalReceipt(TypedModel):
    schema_name: Literal['aos-scientist-terminal.v1'] = Field(alias='schema')
    request_id: Identifier
    request_sha256: Hash
    original_principal: ScientistCallerGeneration
    profile_id: Profile
    deployment_digest: Hash
    profile_config_sha256: Hash
    response_schema_sha256: Hash
    admission_binding: ScientistAdmissionBindingV2 | None
    admission_binding_sha256: Hash | None
    original_budget: ScientistTerminalBudget | None
    allocation_binding_sha256: Hash | None
    child_generation: ScientistTerminalChild | None
    drain_evidence_sha256: Hash | None
    no_admission_evidence_sha256: Hash | None
    release_outcome: Literal['released', 'recovered_released', 'never_admitted']
    terminal_state: Literal['completed', 'canceled', 'expired', 'failed']
    reason_code: Literal['success', 'caller_cancel', 'generation_lost', 'queue_timeout',
                         'turn_timeout', 'recovered_after_crash', 'execution_failed']
    result_sha256: Hash | None
    recorded_boot_id: BootId
    recorded_boottime: Seconds
    receipt_sha256: Hash


def terminal_schema_sha256():
    return digest({'$schema': 'https://json-schema.org/draft/2020-12/schema',
                   **ScientistTerminalReceipt.model_json_schema()})


def _deny_proof(record, terminal):
    raise ScientistAdmissionError('Trusted allocation/fencing and physical release evidence is not configured')


def _deny_resolver(record, terminal):
    raise ScientistAdmissionError('Current authenticated terminal resolver is not configured')


def _deny_result(request, receipt):
    raise ScientistAdmissionError('Original pinned profile result validator is not configured')


def _decode(raw, limit):
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        raise ScientistAdmissionError('Terminal evidence exceeds its exact byte bound')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if type(value) is not dict or canonical(value).encode('utf-8') != raw:
        raise ScientistAdmissionError('Terminal evidence is not an exact canonical object')
    return value


class ScientistTerminalVerifier:
    """Verify evidence only; never release a lease, resolve a journal or publish output.

    expected_budget must be an independently retained original Scientist budget,
    not copied from the terminal being checked. verify_resolver authenticates the
    current evidence source and its exact retained-target authority. verify_proof
    independently verifies allocation/fencing ownership and the hashed physical
    drain or no-allocation evidence, including the original child and late-start
    fence. A control response or a matching hash alone cannot implement either
    callback. Neither callback may perform inference or renew original budgets.
    """

    def __init__(self, history, *, descriptor_sha256, schema_sha256, expected_budget,
                 validate_result=_deny_result, verify_proof=_deny_proof, verify_resolver=_deny_resolver):
        if not isinstance(history, ScientistAdmissionHistory) or history.record_version != '2.0':
            raise ScientistAdmissionError('Terminal evidence requires explicit original admission version 2.0')
        if descriptor_sha256 != TERMINAL_DESCRIPTOR_SHA256 or schema_sha256 != terminal_schema_sha256():
            raise ScientistAdmissionError('Terminal descriptor and full local schema pins must both match')
        if not all(callable(callback) for callback in (validate_result, verify_proof, verify_resolver)):
            raise TypeError('Terminal verification callbacks must be explicit trusted callables')
        if isinstance(expected_budget, ScientistTerminalBudget):
            expected_budget = expected_budget.model_dump(mode='json')
        self.expected_budget = ScientistTerminalBudget.model_validate(expected_budget, strict=True)
        self.history = history
        self.validate_result = validate_result
        self.verify_proof = verify_proof
        self.verify_resolver = verify_resolver

    def _original(self, request, terminal, original):
        binding = original.admission_binding
        budget = terminal.original_budget
        if (not isinstance(original, ScientistAdmissionRecordV2)
                or terminal.request_id != request.request_id or original.request_id != request.request_id
                or terminal.request_sha256 != scientist_request_sha256(request)
                or terminal.request_sha256 != original.request_sha256
                or terminal.profile_id != request.profile_id or terminal.profile_id != binding.profile_id
                or terminal.deployment_digest != request.deployment_digest
                or terminal.deployment_digest != binding.profile_pin.deployment_digest
                or terminal.profile_config_sha256 != binding.profile_pin.config_sha256
                or terminal.response_schema_sha256 != binding.profile_pin.response_schema_sha256
                or binding.terminal_schema.sha256 != TERMINAL_DESCRIPTOR_SHA256
                or terminal.admission_binding is None
                or canonical(terminal.admission_binding.model_dump(mode='json'))
                != canonical(binding.model_dump(mode='json'))
                or terminal.admission_binding_sha256 != original.admission_binding_sha256
                or digest(binding.model_dump(mode='json')) != original.admission_binding_sha256
                or canonical(terminal.original_principal.model_dump(mode='json'))
                != canonical(binding.caller_generation.model_dump(mode='json'))
                or budget is None
                or canonical(budget.model_dump(mode='json')) != canonical(self.expected_budget.model_dump(mode='json'))):
            raise ScientistAdmissionError('Terminal evidence differs from the exact original admission or budget')
        if (terminal.recorded_boot_id != binding.server_generation.boot_id
                or budget.boot_id != terminal.recorded_boot_id
                or budget.admitted_boottime is None or budget.envelope_deadline is None
                or not original.captured_boottime <= budget.admitted_boottime
                <= terminal.recorded_boottime
                or budget.envelope_deadline <= budget.admitted_boottime
                or any(deadline is not None and not budget.admitted_boottime < deadline <= budget.envelope_deadline
                       for deadline in (budget.activation_deadline, budget.inference_deadline,
                                        budget.total_deadline, budget.queue_deadline))
                or budget.total_deadline is not None
                and any(deadline is not None and deadline > budget.total_deadline
                        for deadline in (budget.activation_deadline, budget.inference_deadline))):
            raise ScientistAdmissionError('Terminal original boot or assigned deadline binding differs')

    def _outcome(self, terminal, result_bytes):
        state, outcome, reason = terminal.terminal_state, terminal.release_outcome, terminal.reason_code
        allowed = {('completed', 'released', 'success'),
                   ('canceled', 'released', 'caller_cancel'),
                   ('canceled', 'recovered_released', 'caller_cancel'),
                   ('expired', 'released', 'turn_timeout'),
                   ('expired', 'recovered_released', 'recovered_after_crash'),
                   ('failed', 'released', 'execution_failed'),
                   ('canceled', 'never_admitted', 'caller_cancel'),
                   ('expired', 'never_admitted', 'generation_lost'),
                   ('expired', 'never_admitted', 'queue_timeout'),
                   ('expired', 'never_admitted', 'turn_timeout')}
        if (state, outcome, reason) not in allowed:
            raise ScientistAdmissionError('Terminal release outcome is not an admitted evidence variant')
        budget = terminal.original_budget
        phases = (budget.activation_deadline, budget.inference_deadline, budget.total_deadline)
        if outcome == 'never_admitted':
            if (terminal.no_admission_evidence_sha256 is None or any(value is not None for value in
                    (terminal.allocation_binding_sha256, terminal.drain_evidence_sha256,
                     terminal.child_generation, *phases))):
                raise ScientistAdmissionError('Never-admitted evidence conflicts with allocation or launch')
        elif (terminal.allocation_binding_sha256 is None or terminal.drain_evidence_sha256 is None
                or terminal.no_admission_evidence_sha256 is not None or terminal.child_generation is None
                or terminal.child_generation.boot_id != terminal.recorded_boot_id
                or any(value is None for value in phases)):
            raise ScientistAdmissionError('Released evidence lacks its original bound child and physical drain')
        if (state == 'completed') != (terminal.result_sha256 is not None and result_bytes is not None):
            raise ScientistAdmissionError('Terminal result presence differs from completed outcome')
        if state != 'completed' and (terminal.result_sha256 is not None or result_bytes is not None):
            raise ScientistAdmissionError('Noncompleted terminal cannot publish a result')

    def verify(self, request, terminal_bytes, *, result_bytes=None):
        try:
            request = ScientistTurnRequest.model_validate_json(scientist_request_frame(request)[:-1], strict=True)
            value = _decode(terminal_bytes, 128 * 1024)
            terminal = ScientistTerminalReceipt.model_validate(value, strict=True)
            if (canonical(terminal.model_dump(mode='json', by_alias=True)).encode('utf-8') != terminal_bytes
                    or digest({key: entry for key, entry in value.items() if key != 'receipt_sha256'})
                    != terminal.receipt_sha256):
                raise ScientistAdmissionError('Terminal canonical receipt hash differs')
            original, checksum = self.history.read(request.request_id)
            self._original(request, terminal, original)
            self._outcome(terminal, result_bytes)
            if result_bytes is not None:
                result = _decode(result_bytes, 96 * 1024)
                child = terminal.child_generation
                generation = {'unit': child.unit, 'invocation_id': child.invocation_id,
                              'main_pid': child.pid, 'control_group': child.control_group}
                if (set(result) != {'response', 'usage', 'generation'}
                        or digest(result) != terminal.result_sha256
                        or canonical(result['generation']) != canonical(generation)):
                    raise ScientistAdmissionError('Terminal result hash or independently bound child differs')
                receipt = ScientistTurnReceipt.model_validate({'version': 1, 'request_id': request.request_id,
                    'profile_id': request.profile_id, 'deployment_digest': request.deployment_digest,
                    **result}, strict=True)
                if self.validate_result(request.model_copy(deep=True), receipt) is not None:
                    raise ScientistAdmissionError('Terminal profile validator must complete or raise')
            for callback in (self.verify_resolver, self.verify_proof, self.verify_resolver):
                if callback(original.model_copy(deep=True), terminal.model_copy(deep=True)) is not None:
                    raise ScientistAdmissionError('Terminal trusted verifier must complete or raise')
            if self.history.read(request.request_id)[1] != checksum:
                raise ScientistAdmissionError('Original admission changed during terminal verification')
            return terminal
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ScientistAdmissionError('Terminal evidence is malformed or unbound') from error

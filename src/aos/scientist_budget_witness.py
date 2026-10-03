from typing import Literal

from pydantic import Field, model_validator

from .contracts import TypedModel
from .scientist_admission_history import Hash, Identifier, ScientistAdmissionHistory, ScientistAdmissionRecordV2
from .scientist_protocol import Profile, ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
from .scientist_terminal import ScientistTerminalBudget, _decode, canonical, digest
from .scientist_transport import ScientistAdmissionError


class ScientistBudgetTarget(TypedModel):
    request_id: Identifier
    request_sha256: Hash
    original_peer_generation_sha256: Hash


class ScientistOriginalBudgetWitness(TypedModel):
    schema_name: Literal['aos-scientist-original-budget-witness.v1'] = Field(alias='schema')
    version: Literal[1]
    target: ScientistBudgetTarget
    profile_id: Profile
    deployment_digest: Hash
    profile_config_sha256: Hash
    response_schema_sha256: Hash
    original_admission_binding_sha256: Hash | None
    original_cleanup_authorization_sha256: Hash | None
    allocation_binding_sha256: Hash | None
    budget_canonical: str = Field(min_length=1, max_length=16384)
    budget_sha256: Hash

    @model_validator(mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not dict or type(value.get('version')) is not int:
            raise ValueError('Budget witness version requires an integer lexeme')
        return value


def budget_witness_schema():
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            **ScientistOriginalBudgetWitness.model_json_schema()}


def _deny_source(request, original, witness):
    raise ScientistAdmissionError('Independent retained Scientist budget source is not configured')


class ScientistBudgetWitnessVerifier:
    """Read-only binding of a trusted retained witness, never a socket or GPU release.

    verify_source must authenticate the current source and independently attest
    its retained intent/allocation/readiness provenance, not a terminal receipt.
    It must return None or raise, without changing admission history or authority.
    """

    def __init__(self, history, *, schema_sha256, verify_source=_deny_source,
                 read_source=None):
        if not isinstance(history, ScientistAdmissionHistory) or history.record_version != '2.0':
            raise ScientistAdmissionError('Budget witness requires original admission version 2.0')
        if schema_sha256 != digest(budget_witness_schema()):
            raise ScientistAdmissionError('Budget witness full local schema pin differs')
        if not callable(verify_source):
            raise TypeError('Budget source verifier must be a trusted callable')
        if read_source is not None and not callable(read_source):
            raise TypeError('Independent budget source reader must be a trusted callable')
        self.history = history
        self.verify_source = verify_source
        self.read_source = read_source

    def verify(self, request, witness_bytes):
        try:
            request = ScientistTurnRequest.model_validate_json(scientist_request_frame(request)[:-1], strict=True)
            original, checksum = self.history.read(request.request_id)
            witness = ScientistOriginalBudgetWitness.model_validate(_decode(witness_bytes, 32768), strict=True)
            binding = original.admission_binding
            if (not isinstance(original, ScientistAdmissionRecordV2)
                    or witness.target.request_id != request.request_id
                    or witness.target.request_sha256 != scientist_request_sha256(request)
                    or witness.target.request_sha256 != original.request_sha256
                    or witness.target.original_peer_generation_sha256
                    != digest(binding.caller_generation.model_dump(mode='json'))
                    or witness.profile_id != request.profile_id or witness.profile_id != binding.profile_id
                    or witness.deployment_digest != request.deployment_digest
                    or witness.deployment_digest != binding.profile_pin.deployment_digest
                    or witness.profile_config_sha256 != binding.profile_pin.config_sha256
                    or witness.response_schema_sha256 != binding.profile_pin.response_schema_sha256
                    or witness.original_admission_binding_sha256 != original.admission_binding_sha256
                    or witness.original_cleanup_authorization_sha256 is not None):
                raise ScientistAdmissionError('Budget witness differs from exact original admission')
            value = _decode(witness.budget_canonical.encode('utf-8'), 16384)
            budget = ScientistTerminalBudget.model_validate(value, strict=True)
            if (digest(value) != witness.budget_sha256
                    or canonical(budget.model_dump(mode='json')) != witness.budget_canonical
                    or budget.boot_id != binding.server_generation.boot_id
                    or budget.admitted_boottime is None or budget.envelope_deadline is None
                    or budget.admitted_boottime < original.captured_boottime
                    or budget.envelope_deadline <= budget.admitted_boottime
                    or any(deadline is not None and not budget.admitted_boottime < deadline <= budget.envelope_deadline
                           for deadline in (budget.activation_deadline, budget.inference_deadline,
                                            budget.total_deadline, budget.queue_deadline))
                    or budget.total_deadline is not None and any(
                        deadline is not None and deadline > budget.total_deadline
                        for deadline in (budget.activation_deadline, budget.inference_deadline))):
                raise ScientistAdmissionError('Budget witness assigned budget or boot differs')
            phases = (budget.activation_deadline, budget.inference_deadline, budget.total_deadline)
            if ((witness.allocation_binding_sha256 is None and any(value is not None for value in phases))
                    or (witness.allocation_binding_sha256 is not None and any(value is None for value in phases))):
                raise ScientistAdmissionError('Budget witness allocation and assigned phases differ')
            for _attempt in range(2):
                if self.verify_source(request.model_copy(deep=True), original.model_copy(deep=True),
                                      witness.model_copy(deep=True)) is not None:
                    raise ScientistAdmissionError('Trusted budget source verifier must complete or raise')
                if self.read_source is not None:
                    retained = self.read_source(request.model_copy(deep=True), original.model_copy(deep=True))
                    if type(retained) is not dict:
                        raise ScientistAdmissionError('Independent budget source must return the complete retained witness')
                    encoded = canonical(retained).encode('utf-8')
                    observed = ScientistOriginalBudgetWitness.model_validate(_decode(encoded, 32768), strict=True)
                    if canonical(observed.model_dump(mode='json', by_alias=True)) != canonical(
                            witness.model_dump(mode='json', by_alias=True)):
                        raise ScientistAdmissionError('Independent retained budget source differs from candidate witness')
                    if self.verify_source(request.model_copy(deep=True), original.model_copy(deep=True),
                                          witness.model_copy(deep=True)) is not None:
                        raise ScientistAdmissionError('Trusted budget source verifier must complete or raise')
                if self.history.read(request.request_id)[1] != checksum:
                    raise ScientistAdmissionError('Original admission changed during budget verification')
            return budget
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ScientistAdmissionError('Budget witness is malformed or unbound') from error


class ScientistRetainedTerminalVerifier:
    """Compose independent retained budget with release proof, without resolving work."""

    def __init__(self, budget_verifier, *, evidence_schema_bytes, evidence_schema_sha256,
                 descriptor_sha256, terminal_schema_sha256, validate_result, verify_resolver,
                 verify_physical):
        if not isinstance(budget_verifier, ScientistBudgetWitnessVerifier):
            raise TypeError('Retained terminal requires its independent budget verifier')
        self.budget_verifier = budget_verifier
        self.options = {'evidence_schema_bytes': evidence_schema_bytes,
                        'evidence_schema_sha256': evidence_schema_sha256,
                        'descriptor_sha256': descriptor_sha256,
                        'terminal_schema_sha256': terminal_schema_sha256,
                        'validate_result': validate_result, 'verify_resolver': verify_resolver,
                        'verify_physical': verify_physical}

    def verify_response(self, request, *, codec, response_bytes, control_request_bytes):
        from .scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec

        if not isinstance(codec, ScientistRetainedEvidenceCodec):
            raise TypeError('Retained terminal response requires an explicitly pinned version3 codec')
        control = codec.decode_request(control_request_bytes)
        if control['op'] != 'reconcile':
            raise ScientistAdmissionError('Capability discovery is not retained terminal evidence')
        response = codec.decode_response(response_bytes, control_request_bytes)
        if response['ok'] is not True:
            raise ScientistAdmissionError('Failed control response cannot verify a terminal outcome')
        data = response['data']
        return self.verify(request,
            witness_bytes=canonical(data['original_budget_witness']).encode('utf-8'),
            evidence_bytes=canonical(data['evidence']).encode('utf-8'))

    def verify(self, request, *, witness_bytes, evidence_bytes):
        from .scientist_release_proof import ScientistReleaseProofVerifier
        from .scientist_evidence_transport import RESPONSE_LIMIT

        budget = self.budget_verifier.verify(request, witness_bytes)
        witness = ScientistOriginalBudgetWitness.model_validate(_decode(witness_bytes, 32768), strict=True)
        try:
            evidence = _decode(evidence_bytes, RESPONSE_LIMIT)
            terminal = _decode(evidence['terminal_canonical'].encode('utf-8'), 131072)
            if terminal['allocation_binding_sha256'] != witness.allocation_binding_sha256:
                raise ScientistAdmissionError('Release allocation differs from independently retained witness')
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise ScientistAdmissionError('Retained terminal evidence is malformed') from error
        verifier = ScientistReleaseProofVerifier(self.budget_verifier.history,
                                                 expected_budget=budget, **self.options)
        terminal = verifier.verify(request, evidence_bytes)
        retained = self.budget_verifier.verify(request, witness_bytes)
        if canonical(retained.model_dump(mode='json')) != canonical(budget.model_dump(mode='json')):
            raise ScientistAdmissionError('Retained budget changed during release verification')
        return terminal

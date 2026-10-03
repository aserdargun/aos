import hashlib
import http.client
import json
import math
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel
from .scientist_lab import ScientistLabAction, ScientistLabClient, ScientistLabPolicy, ScientistLabTask, _object
from .scientist_transport import ScientistAdmissionError


Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
CPU_SCHEMA_SHA256 = '44c142fa51200e846971df8e57d6c83b0fe18bf1694eb8c4835471cc521b750e'


def cpu_capability_sha256(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class ScientistCpuCapability(TypedModel):
    schema_version: Literal['scientist.lab-cpu-capability.v1'] = Field(alias='schema')
    owner_id: str = Field(min_length=1, max_length=128)
    origin: Literal['aos']
    suite_id: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$')
    track: Literal['mode']
    program_version: str = Field(min_length=1, max_length=64)
    provider: Literal['mode-grid']
    source_kind: Literal['synthetic']
    suite_manifest_sha256: Hash
    suite_entry_sha256: Hash
    provider_config_sha256: Hash
    snapshot_sha256: Hash
    aos_cpu_study_sha256: Hash
    max_experiments: int = Field(ge=1, le=35)
    max_wall_seconds: int = Field(ge=1, le=14400)
    model_tokens: Literal[0]
    allowed_purpose: Literal['research']
    allocation_authority: Literal[False]
    gpu_release_verified: Literal[False]
    native_inference_authorized: Literal[False]
    launch_authorized: Literal[False]

    @field_validator('model_tokens', mode='before')
    @classmethod
    def integer_zero(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError('CPU tokens must be integer zero')
        return value

    @field_validator('allocation_authority', 'gpu_release_verified',
                     'native_inference_authorized', 'launch_authorized', mode='before')
    @classmethod
    def boolean_false(cls, value):
        if value is not False:
            raise ValueError('CPU capability carries no execution or GPU authority')
        return value


class ScientistCpuReviewedGrant(TypedModel):
    """Trusted host review configuration; source pins are not disk or live-source observations."""

    profile: Literal['scientist-cpu-mode-grid.v1']
    authority_url: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=128)
    authorization_context_sha256: Hash
    scientist_source_manifest_sha256: Hash
    aos_source_manifest_sha256: Hash
    capability_schema_sha256: Literal[CPU_SCHEMA_SHA256]
    capability: ScientistCpuCapability
    capability_sha256: Hash

    @model_validator(mode='after')
    def exact_reviewed_capability(self):
        if (self.capability.owner_id != self.principal_id
                or self.capability.program_version != 'mode-grid.v1'
                or cpu_capability_sha256(self.capability.model_dump(mode='json', by_alias=True))
                != self.capability_sha256):
            raise ValueError('Reviewed CPU program or canonical capability hash differs')
        return self


class ScientistCpuCapabilityVerifier:
    """Fresh owner-bound CPU description check, not source attestation or launch/GPU authority."""

    def __init__(self, client: ScientistLabClient, grant: ScientistCpuReviewedGrant):
        if not isinstance(client, ScientistLabClient) or not isinstance(grant, ScientistCpuReviewedGrant):
            raise ScientistAdmissionError('CPU verifier requires explicit typed client and reviewed grant')
        self.grant = ScientistCpuReviewedGrant.model_validate(grant.model_dump(by_alias=True), strict=True)
        self.client = client
        self._binding = self._transport_binding()
        self._lock = threading.Lock()
        self._check_client()

    def _transport_binding(self):
        return (self.client.authority_url, self.client.host, self.client.port, self.client.principal_id,
                self.client.allowed_suites, self.client.timeout_seconds, self.client.token_file)

    def _check_client(self):
        parsed = urlsplit(self.grant.authority_url)
        if (self._transport_binding() != self._binding or self.client._closed
                or self.client.authority_url != self.grant.authority_url
                or self.client.principal_id != self.grant.principal_id
                or self.client.host != parsed.hostname or self.client.port != parsed.port
                or self.grant.capability.suite_id not in self.client.allowed_suites):
            raise ScientistAdmissionError('Reviewed CPU authority or transport binding differs')

    def _check_task(self, task, action):
        self._check_client()
        ScientistLabPolicy.check(task, action, authority_url=self.grant.authority_url,
            allowed_suites=frozenset({self.grant.capability.suite_id}), principal_id=self.grant.principal_id)
        request, capability = task.request, self.grant.capability
        if (not math.isfinite(action.deadline) or action.deadline <= time.time()
                or task.binding.authorization_context_sha256 != self.grant.authorization_context_sha256
                or request.track != capability.track or request.program_version != capability.program_version
                or request.budget.model_tokens != 0
                or request.budget.experiments > capability.max_experiments
                or request.budget.wall_seconds > capability.max_wall_seconds):
            raise ScientistAdmissionError('Reviewed CPU task context, version, budget or deadline differs')

    def __call__(self, task: ScientistLabTask, action: ScientistLabAction) -> None:
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('CPU capability readback is already active')
        try:
            task_snapshot = ScientistLabTask.model_validate(task.model_dump(), strict=True)
            action_snapshot = ScientistLabAction.model_validate(action.model_dump(), strict=True)
            self._check_task(task_snapshot, action_snapshot)
            deadline = min(time.monotonic() + min(self.client.timeout_seconds,
                           action_snapshot.deadline - time.time()),
                           getattr(self.client, '_control_deadline', float('inf')))
            if time.monotonic() >= deadline:
                raise ScientistAdmissionError('CPU capability deadline expired before dispatch')
            raw = self.client._request('GET', '/v1/aos-cpu-capability/' + task_snapshot.request.suite,
                                       b'', deadline, bound=16384)
            if time.monotonic() >= deadline or len(raw) > 16384:
                raise ScientistAdmissionError('CPU capability readback exceeded its deadline or bound')
            capability = ScientistCpuCapability.model_validate(_object(raw), strict=True)
            if (capability != self.grant.capability
                    or cpu_capability_sha256(capability.model_dump(mode='json', by_alias=True))
                    != self.grant.capability_sha256):
                raise ScientistAdmissionError('Fresh CPU capability differs from exact reviewed grant')
            self._check_task(task_snapshot, action_snapshot)
            if task.model_dump() != task_snapshot.model_dump() or action.model_dump() != action_snapshot.model_dump():
                raise ScientistAdmissionError('CPU task or action changed during readback')
        except (OSError, ValueError, TypeError, OverflowError, http.client.HTTPException):
            raise ScientistAdmissionError('CPU capability verification failed closed') from None
        finally:
            self._lock.release()

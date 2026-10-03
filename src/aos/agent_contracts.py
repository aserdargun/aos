from typing import Annotated, Literal, Protocol

from pydantic import Field, JsonValue, model_validator

from .contracts import TypedModel, digest


AgentName = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]
AgentSHA = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class AgentAuthority(TypedModel):
    principal: AgentName
    runtime_id: AgentName
    lease_id: AgentName
    owner: Literal['AGENT', 'HUMAN']
    generation: int = Field(ge=0)


class AgentBudget(TypedModel):
    wall_seconds: int = Field(ge=1, le=86400, description='Reserved wall-clock seconds, not measured or billed usage')
    model_tokens: int = Field(ge=0, le=10000000, description='Reserved model tokens, not a runtime GPU grant')
    experiments: int = Field(ge=0, le=10000, description='Reserved experiment count; zero for non-experiment operations')


class AgentRegistration(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    agent_id: AgentName
    version: AgentName
    adapter_kind: AgentName
    capabilities: list[AgentName] = Field(min_length=1, max_length=32)
    max_active_instances: int = Field(ge=1, le=64)
    per_job_budget: AgentBudget
    aggregate_budget: AgentBudget
    cleanup_scope: Literal['local_process', 'scientist_gpu']

    @model_validator(mode='after')
    def consistent_limits(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError('Capabilities must be unique')
        for unit in ('wall_seconds', 'model_tokens', 'experiments'):
            if getattr(self.per_job_budget, unit) > getattr(self.aggregate_budget, unit):
                raise ValueError('Per-job budget exceeds aggregate reservation limit')
        return self


class AgentJobRequest(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    job_id: AgentName
    agent_id: AgentName
    agent_version: AgentName
    operation: AgentName
    authority: AgentAuthority
    budget: AgentBudget
    dependencies: list[AgentName] = Field(default_factory=list, max_length=64)
    payload: dict[str, JsonValue]
    delegated_by: Literal['supervisor'] = 'supervisor'
    executed_by: Literal['operator'] = 'operator'

    @model_validator(mode='after')
    def distinct_dependencies(self):
        if self.job_id in self.dependencies or len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError('Dependencies must be distinct prior jobs')
        return self

    def request_sha256(self) -> str:
        return digest(self.model_dump(mode='json'))


class AgentHandle(TypedModel):
    job_id: AgentName
    request_sha256: AgentSHA
    authority: AgentAuthority
    handle_id: AgentName
    payload: dict[str, JsonValue]


class AgentPrepared(TypedModel):
    handle: AgentHandle
    ready: bool


class AgentObservation(TypedModel):
    handle: AgentHandle
    state: Literal['waiting_approval', 'ready', 'running', 'succeeded', 'failed', 'cancelled', 'uncertain']
    detail: str = Field(default='', max_length=2048)


class AgentResultProof(TypedModel):
    handle: AgentHandle
    verified: bool
    outcome: Literal['succeeded', 'failed', 'cancelled']
    evidence_sha256: AgentSHA | None = None

    @model_validator(mode='after')
    def evidence_required(self):
        if self.verified and self.evidence_sha256 is None:
            raise ValueError('Verified result requires independent evidence hash')
        return self


class AgentCleanupProof(TypedModel):
    handle: AgentHandle
    verified: bool
    scope: Literal['local_process', 'scientist_gpu']
    evidence_sha256: AgentSHA | None = None

    @model_validator(mode='after')
    def evidence_required(self):
        if self.verified and self.evidence_sha256 is None:
            raise ValueError('Verified cleanup requires independent evidence hash')
        return self


AgentJobState = Literal[
    'queued', 'prepare_intent', 'waiting_approval', 'prepared', 'dispatch_intent',
    'running', 'cancel_intent', 'cancel_requested', 'verifying', 'uncertain',
    'succeeded', 'failed', 'cancelled',
]


class AgentJobStatus(TypedModel):
    request: AgentJobRequest
    request_sha256: AgentSHA
    registration_sha256: AgentSHA
    state: AgentJobState
    prepared: AgentPrepared | None
    handle: AgentHandle | None
    observation: AgentObservation | None
    result_proof: AgentResultProof | None
    cleanup_proof: AgentCleanupProof | None
    reservation_held: bool
    revision: int = Field(ge=0)
    last_error: str | None


class AgentAdmissionError(ValueError):
    pass


class AgentAdapter(Protocol):
    async def prepare(self, request: AgentJobRequest) -> AgentPrepared: ...
    async def dispatch(self, prepared: AgentPrepared) -> AgentHandle: ...
    async def observe(self, handle: AgentHandle) -> AgentObservation: ...
    async def request_cancel(self, handle: AgentHandle) -> AgentObservation: ...
    async def verify_result(self, handle: AgentHandle) -> AgentResultProof: ...
    async def verify_cleanup(self, handle: AgentHandle) -> AgentCleanupProof: ...

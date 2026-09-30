import hashlib
import json
import math
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


HELLO_PATH = "/workspace/hello.txt"
HELLO_CONTENT = "Hello from the local agent.\n"
HELLO_GOAL = "Create /workspace/hello.txt containing exactly Hello from the local agent. followed by a newline, then independently read and verify it."
BROWSER_GOAL = 'Enter exactly "Hello from the local agent." in the Message field, then click Save locally once. Verify the local receipt. No network or other pages are authorized.'
VISION_GOAL = "Select the visible SAVE button once in this synthetic local canvas. Do not select CANCEL. Only the supplied symbolic visual targets are available. Verify the independent application outcome."
LOCAL_NAVIGATION_GOAL = "Open only the synthetic local Start page, follow its observed Details link once, and independently verify the Details page. No other URL or network is authorized."
LOCAL_NAVIGATION_SCOPE = "aos://synthetic/navigation"
STAGING_WORKFLOW_GOAL = "Open the only authorized synthetic local App page, follow its observed Draft link, fill the exact Message, submit the form once, and independently verify the local receipt. No other URL or network is authorized."
STAGING_WORKFLOW_SCOPE = "aos://synthetic/staging"
REMOTE_ENTRY_SCOPE = "aos://web/remote-entry"
REMOTE_ROUTES_SCOPE = "aos://web/readonly-routes"
REMOTE_STATIC_ASSETS_SCOPE = "aos://web/static-assets"
REMOTE_FORM_SCOPE = "aos://web/https-form"
STAGING_WORKFLOW_MESSAGE = "Hello from the local agent."
MISSING_FILE_SUMMARY = "The authorized /workspace/hello.txt is absent. Reading before creation failed with ELEMENT_MISSING; the read had no side effects."
QUESTION = "What is the next action?"
REPO_ROOT = Path(__file__).resolve().parents[2]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ErrorCode(StrEnum):
    TOOL_FAILURE = "TOOL_FAILURE"
    MODEL_FAILURE = "MODEL_FAILURE"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    TIMEOUT = "TIMEOUT"
    UI_CHANGED = "UI_CHANGED"
    ELEMENT_MISSING = "ELEMENT_MISSING"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    APP_CRASH = "APP_CRASH"
    RUNTIME_CRASH = "RUNTIME_CRASH"
    STUCK = "STUCK"
    UNSAFE_ACTION = "UNSAFE_ACTION"


class AOSFault(Exception):
    def __init__(self, code: ErrorCode, detail: str, retryable: bool = False):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def payload(self) -> dict:
        return {"code": self.code.value, "detail": self.detail,
                "retryable": self.retryable, "evidence_refs": []}


class TypedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class Failure(TypedModel):
    code: str
    detail: str
    retryable: bool
    evidence_refs: list[str]


class Phase(StrEnum):
    CREATED = "CREATED"
    OBSERVE = "OBSERVE"
    DECIDE = "DECIDE"
    POLICY = "POLICY"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    WAITING_HUMAN = "WAITING_HUMAN"
    SUPERVISOR = "SUPERVISOR"
    REPLAN = "REPLAN"


TRANSITIONS = {
    Phase.CREATED: {Phase.OBSERVE},
    Phase.OBSERVE: {Phase.DECIDE, Phase.SUPERVISOR},
    Phase.DECIDE: {Phase.POLICY},
    Phase.POLICY: {Phase.EXECUTE, Phase.WAITING_HUMAN},
    Phase.EXECUTE: {Phase.VERIFY, Phase.SUPERVISOR},
    Phase.SUPERVISOR: {Phase.REPLAN, Phase.WAITING_HUMAN, Phase.DECIDE},
    Phase.REPLAN: {Phase.OBSERVE},
    Phase.VERIFY: {Phase.SUCCEEDED, Phase.OBSERVE},
    Phase.PAUSED: {Phase.OBSERVE},
    Phase.WAITING_HUMAN: {Phase.OBSERVE},
}


class State(TypedModel):
    task_kind: Literal["hello", "browser_form", "vision_canvas", "browser_local_navigation", "browser_staging_workflow", "browser_remote_entry", "browser_remote_routes", "browser_remote_static_assets", "browser_remote_form"] = "hello"
    capture_id: str | None = None
    scene_sha256: str | None = None
    task_id: str
    run_id: str
    step_id: str
    runtime_id: str
    deployment_id: str
    state_version: int = Field(default=0, ge=0)
    phase: Phase = Phase.CREATED
    owner: Literal["AGENT", "HUMAN", "PAUSED"] = "AGENT"
    owner_lease_id: str
    original_goal: str = "Merhaba dosyasını oluştur ve içeriğini doğrula."
    normalized_goal: str = HELLO_GOAL
    success_criteria: tuple[str, ...] = ("Exact hello content followed by a newline",)
    authorized_path: str = HELLO_PATH
    authorized_content: str = HELLO_CONTENT
    skill_invocation_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    observation: str = "Not yet observed"
    max_attempts: int = 3
    last_error: Failure | None = None
    supervisor_deployment_id: str | None = None
    recovery_attempts: int = Field(default=0, ge=0, le=2)
    recovery_plan: tuple[str, ...] = ()
    plan_version: int = Field(default=0, ge=0)

    def advance(self, phase: Phase, **changes) -> "State":
        terminal = self.phase in {Phase.SUCCEEDED, Phase.FAILED, Phase.CANCELLED}
        allowed = TRANSITIONS.get(self.phase, set()) | {Phase.FAILED, Phase.CANCELLED, Phase.PAUSED}
        if terminal or phase not in allowed:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, "Invalid state transition")
        return State.model_validate({**self.model_dump(), **changes, "phase": phase,
                                     "state_version": self.state_version + 1})


class Option(TypedModel):
    id: str
    label: str


class Prediction(TypedModel):
    selected_option: str
    probabilities: dict[str, float]

    def validate_options(self, options: list[Option]) -> None:
        option_ids = [option.id for option in options]
        probabilities = self.probabilities
        if (not 2 <= len(option_ids) <= 10 or len(set(option_ids)) != len(option_ids)
                or set(probabilities) != set(option_ids) or self.selected_option not in option_ids
                or any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > 1e-6):
            raise AOSFault(ErrorCode.INVALID_OUTPUT, "Invalid finite-option probability distribution")


class Action(TypedModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    run_id: str
    step_id: str
    action_id: str
    runtime_id: str
    state_version: int
    owner_lease_id: str
    tool: Literal["filesystem.write", "filesystem.read", "process.sha256", "browser.fill", "browser.submit", "browser.verify", "browser.fixture.open", "browser.fixture.follow", "browser.fixture.snapshot", "browser.staging.open", "browser.staging.follow", "browser.staging.fill", "browser.staging.submit", "browser.staging.snapshot", "browser.remote.open", "browser.remote.route", "browser.remote.observe", "browser.static.open", "browser.static.observe", "browser.form.open", "browser.form.fill", "browser.form.submit", "browser.form.receipt", "browser.form.observe", "browser.form.state_before", "browser.form.state_after", "vision.click", "vision.verify"]
    arguments: dict[str, str | int]
    expected_effect: str
    verification: str = "independent_read_equals"
    deadline: float
    idempotency_key: str
    selected_option: str


class Settings(TypedModel):
    workspace: Path
    database: Path
    model_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    execute_min: float = Field(default=0.88, ge=0.88, le=1)
    supervisor_timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    max_supervisor_recoveries: int = Field(default=2, ge=0, le=2)

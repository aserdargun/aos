import hashlib
import json
from typing import Literal

from pydantic import Field

from .contracts import TypedModel


MAX_FRAME_BYTES = 128 * 1024
Profile = Literal['aos.decider.turn.v1', 'aos.bonsai.recovery.v1', 'aos.bonsai.vision.v1']
RunState = Literal['queued', 'running', 'stop_requested', 'completed', 'stopped', 'failed']


class ScientistTurnRequest(TypedModel):
    version: Literal[1] = 1
    op: Literal['infer'] = 'infer'
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    profile_id: Profile
    deployment_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    payload: dict[str, object]


class ScientistWorkerGeneration(TypedModel):
    unit: str = Field(pattern=r'^swapp-aos-gpu-turn-[a-f0-9]{32}\.service$')
    invocation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    main_pid: int = Field(gt=1)
    control_group: str = Field(min_length=1, max_length=512)


class ScientistTurnReceipt(TypedModel):
    version: Literal[1]
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    profile_id: Profile
    deployment_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    generation: ScientistWorkerGeneration
    response: dict[str, object]
    usage: dict[str, object]


def scientist_request_frame(request: ScientistTurnRequest) -> bytes:
    if type(request.version) is not int or request.version != 1:
        raise ValueError('Scientist request protocol version must be integer 1')
    value = ScientistTurnRequest.model_validate(request.model_dump(), strict=True)
    encoded = json.dumps(value.model_dump(), ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError('Scientist request exceeds the broker frame bound')
    return encoded


def scientist_request_sha256(request: ScientistTurnRequest) -> str:
    return hashlib.sha256(scientist_request_frame(request)[:-1]).hexdigest()


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Scientist receipt contains duplicate JSON fields')
        result[name] = value
    return result


def _reject_constant(value):
    raise ValueError('Scientist JSON contains a nonfinite constant')


def scientist_receipt_frame(frame: bytes, request: ScientistTurnRequest) -> ScientistTurnReceipt:
    if (type(frame) is not bytes or not frame.endswith(b'\n')
            or len(frame) > MAX_FRAME_BYTES or b'\n' in frame[:-1]):
        raise ValueError('Scientist receipt must be one bounded newline frame')
    value = json.loads(frame[:-1].decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant)
    if not isinstance(value, dict) or type(value.get('version')) is not int:
        raise ValueError('Scientist receipt protocol version must be an integer')
    receipt = ScientistTurnReceipt.model_validate(value, strict=True)
    if (receipt.request_id != request.request_id or receipt.profile_id != request.profile_id
            or receipt.deployment_digest != request.deployment_digest):
        raise ValueError('Scientist receipt belongs to another request or deployment')
    generation = receipt.generation
    if (not generation.control_group.startswith('/')
            or generation.control_group.rsplit('/', 1)[-1] != generation.unit):
        raise ValueError('Scientist worker cgroup does not match its unit')
    return receipt


class ScientistRunStatus(TypedModel):
    run_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    origin: Literal['aos']
    purpose: Literal['baseline', 'research', 'mode-grid'] | None = None
    state: RunState
    created_at: str
    updated_at: str
    stop_requested: bool
    report_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class ScientistReport(TypedModel):
    run_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    report_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    report: dict[str, object]
    verified_at: str


def verify_scientist_report(raw: bytes, *, status: ScientistRunStatus,
                            expected_run_id: str) -> ScientistReport:
    if type(raw) is not bytes or len(raw) > 256 * 1024:
        raise ValueError('Scientist report exceeds its bounded readback size')
    current = ScientistRunStatus.model_validate(status.model_dump(), strict=True)
    if current.purpose == 'baseline':
        raise ValueError('Scientist research report purpose cannot adopt a baseline operation')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant)
    report = ScientistReport.model_validate(value, strict=True)
    if (current.run_id != expected_run_id or report.run_id != expected_run_id
            or current.state not in {'completed', 'stopped', 'failed'}
            or report.report.get('run_id') != expected_run_id
            or report.report.get('status') != current.state):
        raise ValueError('Scientist report identity or terminal state differs from readback')
    encoded = json.dumps(report.report, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(',', ':')).encode('utf-8')
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != report.report_sha256 or actual != current.report_sha256:
        raise ValueError('Scientist report hash differs from independent status readback')
    return report

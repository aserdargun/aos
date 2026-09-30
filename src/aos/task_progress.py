from datetime import datetime, timezone
import json
import math
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .contracts import ErrorCode, Phase, State, TypedModel, digest


class ModelCallProgress(TypedModel):
    role: Literal['system1', 'system2']
    status: Literal['ok', 'error', 'timeout', 'cancelled']
    latency_ms: float = Field(ge=0)


class ModelCallRoleTotals(TypedModel):
    ok: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode='after')
    def successful_calls_cannot_exceed_total(self):
        if self.ok > self.total:
            raise ValueError('model_call_success_count_exceeds_total')
        return self


class ModelCallTotals(TypedModel):
    system1: ModelCallRoleTotals
    system2: ModelCallRoleTotals


class ModelCallRoleLatency(TypedModel):
    samples: int = Field(ge=0, le=256)
    p50_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    p95_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def samples_and_percentiles_agree(self):
        if (self.samples == 0) != (self.p50_ms is None and self.p95_ms is None):
            raise ValueError('model_call_latency_samples_mismatch')
        if (self.p50_ms is None) != (self.p95_ms is None):
            raise ValueError('model_call_latency_percentiles_incomplete')
        if self.p50_ms is not None and self.p95_ms < self.p50_ms:
            raise ValueError('model_call_latency_percentiles_out_of_order')
        return self


class ModelCallLatency(TypedModel):
    system1: ModelCallRoleLatency
    system2: ModelCallRoleLatency


class TaskProgress(TypedModel):
    phase: Phase | None = None
    state_version: int | None = Field(default=None, ge=0)
    elapsed_ms: float | None = Field(default=None, ge=0)
    failure_code: ErrorCode | None = None
    model_calls: list[ModelCallProgress] = Field(default_factory=list, max_length=10)
    model_call_totals: ModelCallTotals | None = None
    model_call_latency: ModelCallLatency | None = None


def model_call_latency(store, run_id: str) -> ModelCallLatency | None:
    samples = {'system1': [], 'system2': []}
    for call in store.connection.execute(
            "SELECT role,latency_ms FROM model_calls WHERE run_id=? AND status='ok' "
            "AND role IN ('system1','system2') AND typeof(latency_ms) IN ('integer','real') "
            'AND latency_ms BETWEEN 0 AND 1.7976931348623157e308 '
            'ORDER BY rowid ASC LIMIT 257', (run_id,)):
        samples[call['role']].append(float(call['latency_ms']))
        if sum(map(len, samples.values())) > 256:
            return None

    def role_latency(values):
        if not values:
            return {'samples': 0, 'p50_ms': None, 'p95_ms': None}
        ordered = sorted(values)
        return {'samples': len(values),
                'p50_ms': ordered[(len(values) * 50 + 99) // 100 - 1],
                'p95_ms': ordered[(len(values) * 95 + 99) // 100 - 1]}

    return ModelCallLatency.model_validate({role: role_latency(values)
                                            for role, values in samples.items()})


def elapsed_time(created, updated, status, current):
    try:
        started = datetime.fromisoformat(created)
        finished = current if status in {'queued', 'running', 'waiting_approval'} else datetime.fromisoformat(updated)
        if started.tzinfo is None or finished.tzinfo is None:
            return None
        elapsed = (finished - started).total_seconds() * 1000
        return elapsed if math.isfinite(elapsed) and elapsed >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def task_progress(store, job_id, session_id, *, current=None):
    current = current or datetime.now(timezone.utc)
    row = store.connection.execute(
        'SELECT desktop_tasks.*, desktop_sessions.runtime_id AS parent_runtime_id, '
        'runs.task_id AS task_id, runs.status AS run_status, runs.environment_json, runtime_states.state_version AS persisted_version, '
        'runtime_states.state_json FROM desktop_tasks '
        'JOIN desktop_sessions USING(session_id) LEFT JOIN runs USING(run_id) '
        'LEFT JOIN runtime_states USING(run_id) WHERE job_id=? AND session_id=?', (job_id, session_id)).fetchone()
    if row is None:
        return TaskProgress().model_dump(mode='json')
    job_status = row['status']
    run_status = row['run_status']
    elapsed = elapsed_time(row['created_at'], row['updated_at'], row['status'], current)
    empty = TaskProgress(elapsed_ms=elapsed)
    try:
        state = State.model_validate_json(row['state_json'])
        environment = json.loads(row['environment_json'])
        if (state.run_id != row['run_id'] or state.runtime_id != row['runtime_id']
                or state.task_id != row['task_id'] or state.task_kind != row['kind']
                or row['kind'] == 'hello' and state.runtime_id != row['parent_runtime_id']
                or state.state_version != row['persisted_version']
                or not isinstance(environment, dict) or environment.get('runtime_id') != state.runtime_id
                or environment.get('parent_runtime_id', row['parent_runtime_id']) != row['parent_runtime_id']):
            return empty.model_dump(mode='json')
        snapshot = store.connection.execute(
            'SELECT state_version,state_json,content_sha256,step_id FROM state_snapshots '
            'WHERE run_id=? ORDER BY state_version DESC,rowid DESC LIMIT 1', (state.run_id,)).fetchone()
        if (snapshot is None or snapshot['state_version'] != state.state_version
                or snapshot['step_id'] != state.step_id
                or snapshot['content_sha256'] != digest(state.model_dump(mode='json'))
                or State.model_validate_json(snapshot['state_json']) != state):
            return empty.model_dump(mode='json')
        step = store.connection.execute('SELECT run_id,state FROM steps WHERE step_id=?', (state.step_id,)).fetchone()
        if step is None or step['run_id'] != state.run_id or step['state'] != state.phase.value:
            return empty.model_dump(mode='json')
    except (ValidationError, ValueError, TypeError, KeyError):
        return empty.model_dump(mode='json')
    calls = []
    for call in store.connection.execute(
            'SELECT role,status,latency_ms FROM model_calls WHERE run_id=? AND latency_ms IS NOT NULL '
            'ORDER BY rowid DESC LIMIT 10', (state.run_id,)):
        try:
            calls.append(ModelCallProgress.model_validate(dict(call)))
        except ValidationError:
            continue
    totals = {'system1': {'ok': 0, 'total': 0}, 'system2': {'ok': 0, 'total': 0}}
    for row in store.connection.execute(
            "SELECT role,status,count(*) AS count FROM model_calls WHERE run_id=? "
            "AND role IN ('system1','system2') "
            "AND status IN ('ok','error','timeout','cancelled') "
            "AND typeof(latency_ms) IN ('integer','real') "
            'AND latency_ms BETWEEN 0 AND 1.7976931348623157e308 '
            'GROUP BY role,status', (state.run_id,)):
        totals[row['role']]['total'] += row['count']
        if row['status'] == 'ok':
            totals[row['role']]['ok'] += row['count']
    failure_code = None
    if (job_status == 'failed' and run_status == 'failed'
            and state.phase == Phase.FAILED and state.last_error is not None):
        try:
            failure_code = ErrorCode(state.last_error.code)
        except ValueError:
            pass
    return TaskProgress(phase=state.phase, state_version=state.state_version,
                        elapsed_ms=elapsed, model_calls=calls,
                        failure_code=failure_code,
                        model_call_totals=ModelCallTotals.model_validate(totals),
                        model_call_latency=model_call_latency(store, state.run_id)).model_dump(mode='json')

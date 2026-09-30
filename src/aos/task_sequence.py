import asyncio
import hashlib
from contextlib import suppress
from typing import Literal

from pydantic import Field, model_validator

from .contracts import AOSFault, ErrorCode, Phase, TypedModel, canonical, digest, identifier, now
from .task_plan import plan_payload


class SequencePlan(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    kinds: list[Literal['hello', 'browser_form', 'vision_canvas']] = Field(min_length=2, max_length=3, json_schema_extra={'uniqueItems': True})
    requires_action_approval: Literal[True] = True

    @model_validator(mode='after')
    def unique_tasks(self):
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError('Repeated task kinds are not allowed')
        return self


class SequenceStart(TypedModel):
    plan: SequencePlan
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)


class SequenceStatus(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    sequence_id: str = Field(pattern='^sequence-[a-f0-9]{32}$')
    plan: SequencePlan
    plan_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    status: Literal['running', 'succeeded', 'failed', 'cancelled']
    job_ids: list[str] = Field(max_length=3)
    completed: int = Field(ge=0, le=3)
    reason: Literal['started', 'step_started', 'step_verified', 'all_verified', 'step_not_verified', 'control_change', 'sequence_error']
    automatic_replay_allowed: Literal[False] = False

    @model_validator(mode='after')
    def consistent(self):
        if (self.plan_sha256 != digest(self.plan.model_dump()) or self.completed > len(self.job_ids)
                or len(self.job_ids) > len(self.plan.kinds) or len(self.job_ids) != len(set(self.job_ids))
                or (self.status == 'succeeded' and self.completed != len(self.plan.kinds))):
            raise ValueError('Invalid sequence progress')
        return self


class TaskSequences:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.report: SequenceStatus | None = None
        self.task: asyncio.Task | None = None

    @property
    def reserved(self):
        return self.task is not None and not self.task.done()

    def publish(self, **changes):
        report = SequenceStatus.model_validate({**self.report.model_dump(), **changes})
        with self.scheduler.store.connection:
            self.scheduler.store.insert('desktop_events', event_id=identifier('event'),
                                        session_id=self.scheduler.controller.session_id, kind='task_sequence',
                                        payload_json=canonical(report.model_dump()), created_at=now())
        self.report = report

    def status(self):
        return {'reserved': self.reserved, 'sequence': self.report.model_dump() if self.report else None}

    def start(self, request: SequenceStart):
        request = SequenceStart.model_validate(request.model_dump())
        scheduler = self.scheduler
        desktop = scheduler.controller.state()
        if (scheduler.closed or scheduler.restart_quiesced or scheduler.reserved
                or any(kind not in scheduler.kinds() for kind in request.plan.kinds)
                or desktop['owner'] != 'AGENT' or desktop['status'] != 'running'
                or desktop['lease_id'] != request.lease_id or desktop['generation'] != request.generation):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Sequence requires idle current ownership and available fixed tasks')
        report = SequenceStatus(sequence_id=identifier('sequence'), plan=request.plan,
                                plan_sha256=digest(request.plan.model_dump()), status='running', job_ids=[],
                                completed=0, reason='started')
        identity = self.identity()
        previous = self.report
        self.report = report
        try:
            self.publish()
        except BaseException:
            self.report = previous
            raise
        self.task = asyncio.create_task(self.run(request, desktop['runtime_id'], identity))
        return self.report.model_dump()

    def identity(self):
        scheduler = self.scheduler
        return digest({'engine': scheduler.engine.identity,
                       'supervisor': scheduler.vision_supervisor.identity if scheduler.vision_supervisor else None,
                       'browser_manifest': hashlib.sha256(scheduler.browser_manifest.read_bytes()).hexdigest()
                       if scheduler.browser_manifest else None})

    def verified(self, job_id):
        connection = self.scheduler.store.connection
        job = connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if not job or job['status'] != 'succeeded' or not job['run_id']:
            return False
        run = connection.execute('SELECT status,outcome FROM runs WHERE run_id=?', (job['run_id'],)).fetchone()
        checks = connection.execute('SELECT result,method,expected_json,actual_json FROM verifications WHERE run_id=?', (job['run_id'],)).fetchall()
        uncertain = connection.execute("SELECT 1 FROM actions WHERE run_id=? AND status IN ('intent','running','uncertain')", (job['run_id'],)).fetchone()
        criteria = plan_payload(job['kind'])
        return (run['status'] == 'succeeded' and run['outcome'] == 'passed'
                and self.scheduler.store.state(job['run_id']).phase == Phase.SUCCEEDED and not uncertain and bool(checks)
                and all(row['result'] == 'passed' and row['expected_json'] == row['actual_json']
                        and row['method'] == criteria['verification_method'] for row in checks)
                and any(row['actual_json'] == criteria['expected_json'] for row in checks))

    async def run(self, request, runtime_id, identity):
        scheduler = self.scheduler
        try:
            for kind in request.plan.kinds:
                await asyncio.sleep(0)
                if self.identity() != identity or scheduler.controller.state()['runtime_id'] != runtime_id:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Sequence deployment or runtime changed')
                job_id = scheduler._start(request.lease_id, request.generation, kind)['job_id']
                self.publish(job_ids=[*self.report.job_ids, job_id], reason='step_started')
                try:
                    await asyncio.shield(scheduler.task)
                except asyncio.CancelledError:
                    if self.report.status == 'cancelled':
                        raise
                if not self.verified(job_id):
                    self.publish(status='failed', reason='step_not_verified')
                    return
                self.publish(completed=self.report.completed + 1, reason='step_verified')
            self.publish(status='succeeded', reason='all_verified')
        except asyncio.CancelledError:
            if self.report.status == 'running':
                self.publish(status='cancelled', reason='control_change')
            raise
        except Exception:
            await scheduler._cancel_job('stop', 'sequence_failure')
            self.publish(status='failed', reason='sequence_error')

    async def cancel(self):
        if self.reserved:
            try:
                self.publish(status='cancelled', reason='control_change')
            except Exception:
                await self.scheduler._cancel_job('stop', 'sequence_audit_failure')
                raise
            finally:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task

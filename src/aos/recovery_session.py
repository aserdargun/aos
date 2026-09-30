import argparse
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Literal

from pydantic import Field, model_validator

from .contracts import HELLO_CONTENT, HELLO_GOAL, HELLO_PATH, State, TypedModel, canonical, digest, now
from .dataset_audit import audit_snapshot
from .lifecycle import read_journal
from .recovery import JobStatus, RunStatus
from .recovery_lifecycle import LifecycleInspection, inspect_lifecycle
from .session_binding import SessionRuntimeBinding


class SessionJobInspection(TypedModel):
    job_ref: str = Field(pattern='^[a-f0-9]{64}$')
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    status: JobStatus
    run_status: RunStatus
    unresolved_actions: int = Field(ge=0)
    pending_approvals: int = Field(ge=0)


class SessionInspection(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_desktop_session'] = 'read_only_desktop_session'
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    session_ref: str = Field(pattern='^[a-f0-9]{64}$')
    binding_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    session_status: Literal['running', 'paused', 'stopped']
    session_owner: Literal['AGENT', 'HUMAN', 'PAUSED']
    generation: int = Field(ge=0)
    job_count: int = Field(ge=0, le=10000)
    unresolved_inputs: int = Field(ge=0)
    lifecycle: LifecycleInspection
    job: SessionJobInspection | None = None
    inspected_at: str
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    cleanup_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False
    lease_restored: Literal[False] = False
    approval_restored: Literal[False] = False

    @model_validator(mode='after')
    def bound_stage(self):
        if self.lifecycle.recorded_stage not in {'started', 'removed'}:
            raise ValueError('session_report_stage_invalid')
        return self


def inspect_job(connection, binding: SessionRuntimeBinding, session, job_id: str, deployment_sha256: str):
    job = connection.execute('SELECT * FROM desktop_tasks WHERE job_id=? AND session_id=?',
                             (job_id, binding.session_id)).fetchone()
    if (job is None or job['kind'] != 'hello' or job['run_id'] is None
            or job['runtime_id'] != binding.birth.runtime_id
            or not binding.generation <= job['generation'] <= session['generation']):
        raise ValueError('session_job_binding_invalid')
    row = connection.execute('''SELECT runs.*,runtime_states.state_json,runtime_states.state_version,
        tasks.workspace_scope_json,tasks.normalized_goal,tasks.success_criteria_json FROM runs
        JOIN runtime_states USING(run_id) JOIN tasks USING(task_id) WHERE runs.run_id=?''', (job['run_id'],)).fetchone()
    if row is None:
        raise ValueError('session_run_missing')
    state = State.model_validate_json(row['state_json'])
    environment = json.loads(row['environment_json'])
    deployment = json.loads(row['deployment_snapshot_json'])
    if (state.run_id != job['run_id'] or state.task_id != row['task_id'] or state.state_version != row['state_version']
            or state.runtime_id != binding.birth.runtime_id or state.task_kind != 'hello'
            or row['policy_version'] != 'hello-policy-v1' or state.authorized_path != HELLO_PATH
            or state.authorized_content != HELLO_CONTENT or state.normalized_goal != HELLO_GOAL
            or row['normalized_goal'] != HELLO_GOAL or state.success_criteria != ('Exact hello content followed by a newline',)
            or json.loads(row['success_criteria_json']) != list(state.success_criteria)
            or json.loads(row['workspace_scope_json']) != [HELLO_PATH]
            or state.supervisor_deployment_id is not None or not isinstance(environment, dict)
            or any(environment.get(key) != value for key, value in {
                'kind': 'docker_xfce', 'runtime_id': binding.birth.runtime_id,
                'container_id': binding.container_id, 'image_id': binding.birth.image_id,
                'workspace_identity': binding.birth.workspace.model_dump(), 'lifecycle_ref': digest(binding.birth.model_dump()),
                'mount': '/workspace'}.items())
            or environment.get('running') is not True or environment.get('desktop') is not True
            or environment.get('real_execution') is not True or environment.get('network') is not False
            or environment.get('recovery_probe') is not False or not isinstance(deployment, dict)
            or digest(deployment) != deployment_sha256 or deployment.get('deployment_id') != state.deployment_id
            or deployment.get('supervisor')):
        raise ValueError('session_run_binding_invalid')
    snapshot = connection.execute('''SELECT state_json,content_sha256 FROM state_snapshots
        WHERE run_id=? AND step_id=? AND state_version=? ORDER BY rowid DESC LIMIT 1''',
                                  (state.run_id, state.step_id, state.state_version)).fetchone()
    if (snapshot is None or State.model_validate_json(snapshot['state_json']) != state
            or snapshot['content_sha256'] != digest(state.model_dump(mode='json'))):
        raise ValueError('session_state_binding_invalid')
    return SessionJobInspection(job_ref=digest({'job_id': job_id}), run_ref=digest({'run_id': state.run_id}),
                                deployment_sha256=deployment_sha256, status=job['status'], run_status=row['status'],
                                unresolved_actions=connection.execute("SELECT count(*) FROM actions WHERE run_id=? AND status IN ('intent','running','uncertain')", (state.run_id,)).fetchone()[0],
                                pending_approvals=connection.execute("SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status IN ('pending','approved')", (job_id,)).fetchone()[0])


def inspect_session(database: Path, journal: Path, workspace: Path, session_id: str, image_id: str,
                    source_sha256: str, job_id: str | None = None, deployment_sha256: str | None = None) -> SessionInspection:
    if (not re.fullmatch('desktop-session-[a-f0-9]{32}', session_id)
            or (job_id is None) != (deployment_sha256 is None)
            or job_id is not None and (not re.fullmatch('job-[a-f0-9]{32}', job_id)
                                       or not re.fullmatch('[a-f0-9]{64}', deployment_sha256))):
        raise ValueError('session_inspection_input_invalid')
    events, journal_hash = read_journal(journal)
    birth, latest = events[0].birth, events[-1]
    if latest.stage not in {'started', 'removed'} or birth.image_id != image_id or birth.source_sha256 != source_sha256:
        raise ValueError('session_journal_binding_invalid')
    with audit_snapshot(database) as (connection, identity):
        session = connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?', (session_id,)).fetchone()
        if session is None or session['runtime_id'] != birth.runtime_id or session['image_id'] != image_id:
            raise ValueError('session_runtime_binding_invalid')
        rows = connection.execute("SELECT payload_json FROM desktop_events WHERE session_id=? AND kind='runtime_binding' ORDER BY rowid DESC LIMIT 10001", (session_id,)).fetchall()
        if not rows or len(rows) > 10000:
            raise ValueError('session_binding_missing_or_limit')
        bindings = [SessionRuntimeBinding.model_validate_json(row[0]) for row in rows]
        binding = bindings[0]
        if (binding.session_id != session_id or binding.birth != birth or binding.container_id != latest.container_id
                or binding.generation > session['generation']
                or bindings[-1].generation != 0
                or len({item.birth.runtime_id for item in bindings}) != len(bindings)
                or any(item.session_id != session_id for item in bindings)
                or any(earlier.generation >= later.generation for later, earlier in zip(bindings, bindings[1:]))):
            raise ValueError('session_birth_binding_invalid')
        count = connection.execute('SELECT count(*) FROM desktop_tasks WHERE session_id=?', (session_id,)).fetchone()[0]
        if count > 10000:
            raise ValueError('session_job_limit')
        job = inspect_job(connection, binding, session, job_id, deployment_sha256) if job_id is not None else None
        inputs = connection.execute("SELECT count(*) FROM desktop_inputs WHERE session_id=? AND status IN ('queued','running','uncertain')", (session_id,)).fetchone()[0]
    lifecycle = inspect_lifecycle(journal, workspace, image_id, source_sha256)
    if lifecycle.journal_sha256 != journal_hash:
        raise ValueError('session_journal_changed')
    with audit_snapshot(database) as (connection, refreshed):
        if refreshed['sha256'] != identity['sha256']:
            raise ValueError('session_database_changed')
    if read_journal(journal)[1] != journal_hash:
        raise ValueError('session_journal_changed')
    return SessionInspection(snapshot_sha256=identity['sha256'], session_ref=digest({'session_id': session_id}),
                             binding_sha256=digest(binding.model_dump()), session_status=session['status'],
                             session_owner=session['owner'], generation=session['generation'], job_count=count,
                             unresolved_inputs=inputs, lifecycle=lifecycle, job=job, inspected_at=now())


def main():
    parser = argparse.ArgumentParser(description='UI session/runtime/lifecycle bağını salt okunur incele; continuation veya yetki yok')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--job-id')
    parser.add_argument('--deployment-sha256')
    arguments = parser.parse_args()
    try:
        print(canonical(inspect_session(**vars(arguments)).model_dump()))
    except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError, subprocess.TimeoutExpired):
        parser.exit(1, 'Session incelenemedi; kalıcı exact session/runtime/lifecycle bağı ve sabit pinler gerekli.\n')


if __name__ == '__main__':
    main()

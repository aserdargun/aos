import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Literal

from pydantic import Field, model_validator

from .contracts import HELLO_CONTENT, HELLO_GOAL, HELLO_PATH, State, TypedModel, canonical, digest, now
from .dataset_audit import audit_snapshot
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


class CheckpointReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_hello_checkpoint'] = 'read_only_hello_checkpoint'
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    state_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    captured_at: str
    inspected_at: str
    unresolved_actions: int = Field(ge=0)
    workspace_binding_verified: Literal[True] = True
    deployment_snapshot_matches: Literal[True] = True
    live_deployment_verified: Literal[False] = False
    effect: Literal['exact_content', 'absent', 'different_content', 'unreadable']
    criterion_observed: bool
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False
    action_causality_verified: Literal[False] = False
    uncertain_effect_resolved: Literal[False] = False

    @model_validator(mode='after')
    def observed_criterion(self):
        if self.criterion_observed != (self.effect == 'exact_content'):
            raise ValueError('checkpoint_criterion_mismatch')
        return self


def inspect_effect(descriptor: int) -> str:
    try:
        target = os.open('hello.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        return 'absent'
    except OSError:
        return 'unreadable'
    try:
        before = os.fstat(target)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4096:
            return 'unreadable'
        content = os.read(target, 4097)
        after = os.fstat(target)
        linked = os.stat('hello.txt', dir_fd=descriptor, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
        if any(getattr(before, field) != getattr(metadata, field) for metadata in (after, linked) for field in fields):
            return 'unreadable'
        return 'exact_content' if content == HELLO_CONTENT.encode() else 'different_content'
    except OSError:
        return 'unreadable'
    finally:
        os.close(target)


def inspect_checkpoint(database: Path, workspace: Path, run_id: str, deployment_sha256: str,
                       *, workspace_descriptor: int | None = None) -> CheckpointReport:
    if (not isinstance(run_id, str) or not 1 <= len(run_id) <= 128
            or not isinstance(deployment_sha256, str) or not re.fullmatch('[a-f0-9]{64}', deployment_sha256)):
        raise ValueError('checkpoint_input_invalid')
    descriptor = open_existing_workspace(workspace) if workspace_descriptor is None else os.dup(workspace_descriptor)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current_binding = workspace_identity(workspace, descriptor)
        if current_binding.owner_uid != os.getuid():
            raise ValueError('workspace_owner_mismatch')
        with audit_snapshot(database) as (connection, identity):
            row = connection.execute('''SELECT runs.*,runtime_states.state_json,runtime_states.state_version,
                tasks.workspace_scope_json,tasks.normalized_goal,tasks.success_criteria_json FROM runs
                JOIN runtime_states USING(run_id) JOIN tasks USING(task_id) WHERE runs.run_id=?''', (run_id,)).fetchone()
            if row is None:
                raise ValueError('checkpoint_run_missing')
            state = State.model_validate_json(row['state_json'])
            environment = json.loads(row['environment_json'])
            deployment = json.loads(row['deployment_snapshot_json'])
            if (state.task_kind != 'hello' or state.run_id != run_id or state.task_id != row['task_id']
                    or state.state_version != row['state_version'] or row['policy_version'] != 'hello-policy-v1'
                    or state.authorized_path != HELLO_PATH or state.authorized_content != HELLO_CONTENT
                    or state.normalized_goal != HELLO_GOAL or row['normalized_goal'] != HELLO_GOAL
                    or state.success_criteria != ('Exact hello content followed by a newline',)
                    or json.loads(row['success_criteria_json']) != list(state.success_criteria)
                    or json.loads(row['workspace_scope_json']) != [HELLO_PATH]):
                raise ValueError('checkpoint_scope_mismatch')
            if (not isinstance(environment, dict) or environment.get('kind') != 'descriptor_workspace'
                    or environment.get('mount') != '/workspace' or environment.get('network') is not False
                    or environment.get('desktop') is not False or environment.get('running') is not True):
                raise ValueError('checkpoint_runtime_unsupported')
            recorded_binding = WorkspaceIdentity.model_validate(environment.get('workspace_identity'))
            expected_runtime = 'workspace-' + hashlib.sha256(str(workspace.absolute()).encode()).hexdigest()[:24]
            if (recorded_binding != current_binding or state.runtime_id != expected_runtime
                    or environment.get('runtime_id') != expected_runtime):
                raise ValueError('checkpoint_workspace_mismatch')
            if (not isinstance(deployment, dict) or not isinstance(deployment.get('supervisor', {}), dict)
                    or digest(deployment) != deployment_sha256
                    or deployment.get('deployment_id') != state.deployment_id
                    or (deployment.get('supervisor') or {}).get('deployment_id') != state.supervisor_deployment_id):
                raise ValueError('checkpoint_deployment_mismatch')
            snapshot = connection.execute('''SELECT state_json,content_sha256 FROM state_snapshots
                WHERE run_id=? AND step_id=? AND state_version=? ORDER BY rowid DESC LIMIT 1''',
                (run_id, state.step_id, state.state_version)).fetchone()
            state_hash = digest(state.model_dump(mode='json'))
            if (snapshot is None or snapshot['content_sha256'] != state_hash
                    or State.model_validate_json(snapshot['state_json']) != state):
                raise ValueError('checkpoint_state_mismatch')
            unresolved = connection.execute("SELECT count(*) FROM actions WHERE run_id=? AND status IN ('intent','running','uncertain')",
                                            (run_id,)).fetchone()[0]
            effect = inspect_effect(descriptor)
            check = open_existing_workspace(workspace)
            try:
                if workspace_identity(workspace, check) != current_binding:
                    raise ValueError('checkpoint_workspace_changed')
            finally:
                os.close(check)
            return CheckpointReport(snapshot_sha256=identity['sha256'], run_ref=digest({'run_id': run_id}),
                                    state_sha256=state_hash, deployment_sha256=deployment_sha256,
                                    workspace_sha256=digest(current_binding.model_dump()), captured_at=identity['captured_at'],
                                    inspected_at=now(), unresolved_actions=unresolved, effect=effect,
                                    criterion_observed=effect == 'exact_content')
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description='Hello checkpoint ve dosya etkisini salt okunur denetler; devam veya tekrar yapmaz')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--deployment-sha256', required=True)
    arguments = parser.parse_args()
    try:
        report = inspect_checkpoint(arguments.database, arguments.workspace, arguments.run_id, arguments.deployment_sha256)
        print(canonical(report.model_dump()))
    except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError):
        parser.exit(1, 'Checkpoint denetlenemedi; kaynak, boşta workspace ve sabit deployment bağını kontrol edin.\n')


if __name__ == '__main__':
    main()

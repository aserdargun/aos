import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess
import time
from typing import Literal

from pydantic import Field, model_validator

from .contracts import HELLO_CONTENT, HELLO_GOAL, HELLO_PATH, State, TypedModel, canonical, digest, now
from .dataset_audit import audit_snapshot
from .desktop import DOCKER
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


INSPECT_FORMAT = '{' + ','.join('"' + key + '":{{json ' + expression + '}}' for key, expression in (
    ('id', '.Id'), ('image', '.Image'), ('runtime', '(index .Config.Labels "com.aos.runtime")'),
    ('status', '.State.Status'), ('running', '.State.Running'), ('started_at', '.State.StartedAt'),
    ('finished_at', '.State.FinishedAt'), ('restart_count', '.RestartCount'), ('user', '.Config.User'),
    ('network', '.HostConfig.NetworkMode'), ('readonly', '.HostConfig.ReadonlyRootfs'),
    ('privileged', '.HostConfig.Privileged'), ('cap_drop', '.HostConfig.CapDrop'),
    ('security_opt', '.HostConfig.SecurityOpt'), ('mounts', '.Mounts'), ('ports', '.HostConfig.PortBindings'),
)) + '}'


class RuntimeInspection(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_desktop_runtime'] = 'read_only_desktop_runtime'
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    runtime_ref: str = Field(pattern='^[a-f0-9]{64}$')
    container_ref: str = Field(pattern='^[a-f0-9]{64}$')
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    image_id: str = Field(pattern='^sha256:[a-f0-9]{64}$')
    disposition: Literal['workspace_busy', 'container_missing', 'running_candidate', 'stopped_container']
    observation_sha256: str | None = Field(pattern='^[a-f0-9]{64}$')
    unresolved_actions: int = Field(ge=0)
    inspected_at: str
    orphan_confirmed: Literal[False] = False
    process_liveness_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    cleanup_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False

    @model_validator(mode='after')
    def observation_binding(self):
        if (self.disposition == 'workspace_busy') != (self.observation_sha256 is None):
            raise ValueError('runtime_report_observation_mismatch')
        return self


def docker_read(arguments: list[str]) -> bytes:
    with subprocess.Popen([*DOCKER, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin'}) as process:
        content = bytearray()
        total = 0
        deadline = time.monotonic() + 10
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError('runtime_inspection_timeout')
                    for key, events in selector.select(remaining):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > 65536:
                            raise ValueError('runtime_inspection_output_limit')
                        if key.fileobj is process.stdout:
                            content.extend(chunk)
            if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                raise ValueError('runtime_inspection_unavailable')
            return bytes(content)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def container_observation(container_id: str) -> dict | None:
    if not re.fullmatch('[a-f0-9]{64}', container_id):
        raise ValueError('runtime_container_id_invalid')
    listed = docker_read(['container', 'ls', '--all', '--no-trunc', '--filter', 'id=' + container_id,
                          '--format', '{{.ID}}']).decode().splitlines()
    if not listed:
        return None
    if listed != [container_id]:
        raise ValueError('runtime_container_listing_ambiguous')
    value = json.loads(docker_read(['container', 'inspect', '--format', INSPECT_FORMAT, container_id]))
    if not isinstance(value, dict):
        raise ValueError('runtime_container_inspection_invalid')
    return value


def bound_source(connection: sqlite3.Connection, run_id: str, deployment_sha256: str, image_id: str):
    row = connection.execute('''SELECT runs.*,runtime_states.state_json,runtime_states.state_version,
        tasks.workspace_scope_json,tasks.normalized_goal,tasks.success_criteria_json FROM runs
        JOIN runtime_states USING(run_id) JOIN tasks USING(task_id) WHERE runs.run_id=?''', (run_id,)).fetchone()
    if row is None:
        raise ValueError('runtime_run_missing')
    state = State.model_validate_json(row['state_json'])
    environment = json.loads(row['environment_json'])
    deployment = json.loads(row['deployment_snapshot_json'])
    if (state.run_id != run_id or state.task_id != row['task_id'] or state.state_version != row['state_version']
            or state.task_kind != 'hello' or row['policy_version'] != 'hello-policy-v1'
            or state.authorized_path != HELLO_PATH or state.authorized_content != HELLO_CONTENT
            or state.normalized_goal != HELLO_GOAL or row['normalized_goal'] != HELLO_GOAL
            or state.success_criteria != ('Exact hello content followed by a newline',)
            or json.loads(row['success_criteria_json']) != list(state.success_criteria)
            or json.loads(row['workspace_scope_json']) != [HELLO_PATH]
            or state.supervisor_deployment_id is not None or not isinstance(environment, dict)
            or environment.get('kind') != 'docker_xfce' or environment.get('runtime_id') != state.runtime_id
            or environment.get('image_id') != image_id or environment.get('running') is not True
            or environment.get('desktop') is not True or environment.get('real_execution') is not True
            or environment.get('network') is not False or environment.get('mount') != '/workspace'
            or environment.get('recovery_probe') is not False
            or not re.fullmatch('desktop-[a-f0-9]{32}', state.runtime_id)
            or not re.fullmatch('[a-f0-9]{64}', environment.get('container_id', ''))
            or not isinstance(deployment, dict) or digest(deployment) != deployment_sha256
            or deployment.get('deployment_id') != state.deployment_id or deployment.get('supervisor')
            or connection.execute('SELECT 1 FROM desktop_tasks WHERE run_id=?', (run_id,)).fetchone()):
        raise ValueError('runtime_source_binding_invalid')
    snapshot = connection.execute('''SELECT state_json,content_sha256 FROM state_snapshots
        WHERE run_id=? AND step_id=? AND state_version=? ORDER BY rowid DESC LIMIT 1''',
                                  (run_id, state.step_id, state.state_version)).fetchone()
    if (snapshot is None or State.model_validate_json(snapshot['state_json']) != state
            or snapshot['content_sha256'] != digest(state.model_dump(mode='json'))):
        raise ValueError('runtime_state_binding_invalid')
    binding = WorkspaceIdentity.model_validate(environment.get('workspace_identity'))
    unresolved = connection.execute("SELECT count(*) FROM actions WHERE run_id=? AND status IN ('intent','running','uncertain')", (run_id,)).fetchone()[0]
    return state.runtime_id, environment['container_id'], binding, unresolved


def validate_container(value: dict, container_id: str, runtime_id: str, image_id: str, workspace: Path) -> None:
    mounts = value.get('mounts')
    if not isinstance(mounts, list):
        raise ValueError('runtime_mounts_missing')
    binds = [mount for mount in mounts if mount.get('Type') == 'bind']
    others = [mount for mount in mounts if mount.get('Type') != 'bind']
    if (value.get('id') != container_id or value.get('image') != image_id or value.get('runtime') != runtime_id
            or value.get('user') != f'{os.getuid()}:{os.getgid()}' or value.get('network') != 'none'
            or value.get('readonly') is not True or value.get('privileged') is not False
            or value.get('cap_drop') != ['ALL'] or value.get('security_opt') != ['no-new-privileges']
            or value.get('ports') not in ({}, None) or len(binds) != 1
            or binds[0].get('Source') != str(workspace.absolute()) or binds[0].get('Destination') != '/workspace'
            or binds[0].get('RW') is not True or binds[0].get('Propagation') != 'rprivate'
            or any(mount.get('Type') != 'tmpfs' or mount.get('Destination') not in {'/tmp', '/run', '/home/agent'} for mount in others)
            or value.get('status') not in {'running', 'created', 'exited'}
            or type(value.get('running')) is not bool or value['running'] != (value['status'] == 'running')
            or not isinstance(value.get('started_at'), str) or not isinstance(value.get('finished_at'), str)
            or type(value.get('restart_count')) is not int or value['restart_count'] < 0):
        raise ValueError('runtime_container_binding_invalid')


def inspect_runtime(database: Path, workspace: Path, run_id: str, deployment_sha256: str, image_id: str) -> RuntimeInspection:
    if (not isinstance(run_id, str) or not 1 <= len(run_id) <= 128
            or not re.fullmatch('[a-f0-9]{64}', deployment_sha256) or not re.fullmatch('sha256:[a-f0-9]{64}', image_id)):
        raise ValueError('runtime_inspection_input_invalid')
    with audit_snapshot(database) as (connection, identity):
        runtime_id, container_id, binding, unresolved = bound_source(connection, run_id, deployment_sha256, image_id)
    descriptor = open_existing_workspace(workspace)
    try:
        if workspace_identity(workspace, descriptor) != binding or binding.owner_uid != os.getuid():
            raise ValueError('runtime_workspace_binding_invalid')
        observation_hash = None
        disposition = 'workspace_busy'
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            first = container_observation(container_id)
            if first is not None:
                validate_container(first, container_id, runtime_id, image_id, workspace)
            second = container_observation(container_id)
            if first != second:
                raise ValueError('runtime_observation_changed')
            observation_hash = digest({'container': first})
            disposition = 'container_missing' if first is None else 'running_candidate' if first['running'] else 'stopped_container'
        with audit_snapshot(database) as (connection, refreshed):
            if refreshed['sha256'] != identity['sha256']:
                raise ValueError('runtime_source_changed')
        check = open_existing_workspace(workspace)
        try:
            if workspace_identity(workspace, check) != binding:
                raise ValueError('runtime_workspace_changed')
        finally:
            os.close(check)
        return RuntimeInspection(snapshot_sha256=identity['sha256'], run_ref=digest({'run_id': run_id}),
                                 runtime_ref=digest({'runtime_id': runtime_id}), container_ref=digest({'container_id': container_id}),
                                 workspace_sha256=digest(binding.model_dump()), deployment_sha256=deployment_sha256, image_id=image_id,
                                 disposition=disposition, observation_sha256=observation_hash, unresolved_actions=unresolved, inspected_at=now())
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description='Bağlı Docker hello runtime kaydını salt okunur incele; sahiplenme, silme veya replay yok')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--deployment-sha256', required=True)
    parser.add_argument('--image-id', required=True)
    arguments = parser.parse_args()
    try:
        print(canonical(inspect_runtime(arguments.database, arguments.workspace, arguments.run_id,
                                        arguments.deployment_sha256, arguments.image_id).model_dump()))
    except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError, subprocess.TimeoutExpired):
        parser.exit(1, 'Runtime incelenemedi; bağlı kaynak/workspace, sabit image/deployment ve erişilebilir yerel Docker gerekli.\n')


if __name__ == '__main__':
    main()

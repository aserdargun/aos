import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, canonical, digest, now
from .lifecycle import LifecycleBirth, observe_process, read_journal
from .recovery_runtime import INSPECT_FORMAT, docker_read, validate_container
from .workspace_identity import open_existing_workspace, workspace_identity


LIFECYCLE_FORMAT = INSPECT_FORMAT[:-1] + ',"birth_ref":{{json (index .Config.Labels "com.aos.lifecycle")}}}'


class LifecycleInspection(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_desktop_lifecycle'] = 'read_only_desktop_lifecycle'
    journal_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    birth_ref: str = Field(pattern='^[a-f0-9]{64}$')
    runtime_ref: str = Field(pattern='^[a-f0-9]{64}$')
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    image_id: str = Field(pattern='^sha256:[a-f0-9]{64}$')
    source_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    recorded_stage: Literal['intent', 'created', 'started', 'removed']
    owner_observation: Literal['same_process', 'not_observed', 'different_process', 'different_boot', 'incomparable', 'unavailable']
    workspace_busy: bool
    container_observation: Literal['not_queried', 'missing', 'created', 'running', 'exited']
    observation_sha256: str | None = Field(pattern='^[a-f0-9]{64}$')
    inspected_at: str
    orphan_confirmed: Literal[False] = False
    model_deployment_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    cleanup_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False


def observe_container(birth: LifecycleBirth, container_id: str | None, workspace: Path) -> dict | None:
    selector = 'id=' + container_id if container_id is not None else 'name=^/' + birth.container_name + '$'
    rows = docker_read(['container', 'ls', '--all', '--no-trunc', '--filter', selector, '--format', '{{.ID}}']).decode().splitlines()
    if not rows:
        return None
    if len(rows) != 1 or not re.fullmatch('[a-f0-9]{64}', rows[0]) or container_id is not None and rows != [container_id]:
        raise ValueError('lifecycle_container_ambiguous')
    value = json.loads(docker_read(['container', 'inspect', '--format', LIFECYCLE_FORMAT, rows[0]]))
    if not isinstance(value, dict):
        raise ValueError('lifecycle_container_invalid')
    validate_container(value, rows[0], birth.runtime_id, birth.image_id, workspace)
    if value.get('birth_ref') != digest(birth.model_dump()):
        raise ValueError('lifecycle_container_birth_mismatch')
    return value


def inspect_lifecycle(journal: Path, workspace: Path, image_id: str, source_sha256: str) -> LifecycleInspection:
    if not re.fullmatch('sha256:[a-f0-9]{64}', image_id) or not re.fullmatch('[a-f0-9]{64}', source_sha256):
        raise ValueError('lifecycle_pins_invalid')
    events, journal_hash = read_journal(journal)
    birth, latest = events[0].birth, events[-1]
    if birth.image_id != image_id or birth.source_sha256 != source_sha256:
        raise ValueError('lifecycle_pins_mismatch')
    descriptor = open_existing_workspace(workspace)
    try:
        if (workspace_identity(workspace, descriptor) != birth.workspace or birth.workspace.owner_uid != os.getuid()
                or birth.container_name != 'aos-desktop-' + hashlib.sha256(str(workspace.absolute()).encode()).hexdigest()[:20]):
            raise ValueError('lifecycle_workspace_mismatch')
        owner = observe_process(birth.process)
        busy = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            busy = True
        observation = 'not_queried'
        observation_hash = None
        if not busy and owner in {'not_observed', 'different_process', 'different_boot'}:
            first = observe_container(birth, latest.container_id, workspace)
            if first != observe_container(birth, latest.container_id, workspace):
                raise ValueError('lifecycle_container_changed')
            if latest.stage == 'removed' and first is not None:
                raise ValueError('lifecycle_removed_container_present')
            observation = 'missing' if first is None else first['status']
            observation_hash = digest({'container': first})
        if observe_process(birth.process) != owner or read_journal(journal)[1] != journal_hash:
            raise ValueError('lifecycle_evidence_changed')
        check = open_existing_workspace(workspace)
        try:
            if workspace_identity(workspace, check) != birth.workspace:
                raise ValueError('lifecycle_workspace_changed')
        finally:
            os.close(check)
        return LifecycleInspection(journal_sha256=journal_hash, birth_ref=digest(birth.model_dump()),
                                   runtime_ref=digest({'runtime_id': birth.runtime_id}), workspace_sha256=digest(birth.workspace.model_dump()),
                                   image_id=image_id, source_sha256=source_sha256, recorded_stage=latest.stage,
                                   owner_observation=owner, workspace_busy=busy, container_observation=observation,
                                   observation_sha256=observation_hash, inspected_at=now())
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description='Kalıcı Docker doğum kaydı ve süreç kimliğini salt okunur incele; adopt/cleanup/replay yok')
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--image-id', required=True)
    parser.add_argument('--source-sha256', required=True)
    arguments = parser.parse_args()
    try:
        print(canonical(inspect_lifecycle(arguments.journal, arguments.workspace, arguments.image_id, arguments.source_sha256).model_dump()))
    except (ValueError, OSError, TypeError, AttributeError, subprocess.TimeoutExpired):
        parser.exit(1, 'Lifecycle incelenemedi; private bağlı kayıt, workspace ve sabit image/source gerekli.\n')


if __name__ == '__main__':
    main()

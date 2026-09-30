import fcntl
import hashlib
import os
from pathlib import Path
import select
import stat
from typing import Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest, now
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


class ProcessIdentity(TypedModel):
    boot_id: str = Field(pattern='^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    pid: int = Field(ge=1, le=2147483647)
    start_ticks: int = Field(ge=1)
    pid_namespace: int = Field(ge=1)
    uid: int = Field(ge=1)


def process_identity(pid: int) -> ProcessIdentity:
    if type(pid) is not int or pid < 1:
        raise ValueError('lifecycle_pid_invalid')
    root = Path('/proc') / str(pid)
    with (root / 'stat').open('rb') as source:
        content = source.read(8193)
    if len(content) > 8192 or b') ' not in content:
        raise ValueError('lifecycle_process_stat_invalid')
    fields = content.rsplit(b') ', 1)[1].split()
    if len(fields) < 20:
        raise ValueError('lifecycle_process_stat_invalid')
    return ProcessIdentity(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), pid=pid,
                           start_ticks=int(fields[19]), pid_namespace=(root / 'ns/pid').stat().st_ino, uid=root.stat().st_uid)


def observe_process(expected: ProcessIdentity) -> str:
    try:
        current = process_identity(os.getpid())
    except (OSError, ValueError, AttributeError):
        return 'unavailable'
    try:
        if current.boot_id != expected.boot_id:
            return 'different_boot'
        if current.pid_namespace != expected.pid_namespace or current.uid != expected.uid:
            return 'incomparable'
        descriptor = os.pidfd_open(expected.pid, 0)
        try:
            if select.select([descriptor], [], [], 0)[0]:
                return 'not_observed'
            observed = process_identity(expected.pid)
            if select.select([descriptor], [], [], 0)[0]:
                return 'not_observed'
            return 'same_process' if observed == expected else 'different_process'
        finally:
            os.close(descriptor)
    except (ProcessLookupError, FileNotFoundError):
        return 'not_observed'
    except (OSError, ValueError, AttributeError):
        return 'unavailable'


class LifecycleBirth(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    runtime_id: str = Field(pattern='^desktop-[a-f0-9]{32}$')
    container_name: str = Field(pattern='^aos-desktop-[a-f0-9]{20}$')
    image_id: str = Field(pattern='^sha256:[a-f0-9]{64}$')
    source_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    workspace: WorkspaceIdentity
    process: ProcessIdentity

    @model_validator(mode='after')
    def matching_owner(self):
        if self.workspace.owner_uid != self.process.uid:
            raise ValueError('lifecycle_owner_mismatch')
        return self


class LifecycleEvent(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    birth: LifecycleBirth
    sequence: int = Field(ge=0, le=3)
    previous_sha256: str | None = Field(pattern='^[a-f0-9]{64}$')
    stage: Literal['intent', 'created', 'started', 'removed']
    container_id: str | None = Field(pattern='^[a-f0-9]{64}$')
    recorded_at: str

    @model_validator(mode='after')
    def valid_stage(self):
        if ((self.stage == 'intent') != (self.sequence == 0)
                or (self.sequence == 0) != (self.previous_sha256 is None)
                or (self.stage == 'intent') != (self.container_id is None)
                or self.stage == 'created' and self.sequence != 1
                or self.stage == 'started' and self.sequence != 2
                or self.stage == 'removed' and self.sequence not in {2, 3}):
            raise ValueError('lifecycle_event_invalid')
        return self


def private_directory(path: Path) -> int:
    descriptor = open_existing_workspace(path)
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise ValueError('lifecycle_private_directory_required')
    return descriptor


def read_journal(path: Path) -> tuple[list[LifecycleEvent], str]:
    parent = private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1 or before.st_size > 16384):
                raise ValueError('lifecycle_private_file_required')
            content = os.read(descriptor, 16385)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
            if (len(content) != before.st_size or not content.endswith(b'\n')
                    or any(getattr(before, field) != getattr(metadata, field) for metadata in (after, linked) for field in fields)):
                raise ValueError('lifecycle_journal_changed_or_partial')
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    lines = content.splitlines()
    if not 1 <= len(lines) <= 4:
        raise ValueError('lifecycle_journal_bound')
    events = [LifecycleEvent.model_validate_json(line) for line in lines]
    for index, event in enumerate(events):
        if event.sequence != index or event.birth != events[0].birth:
            raise ValueError('lifecycle_sequence_invalid')
        if index:
            previous = events[index - 1]
            if (event.previous_sha256 != digest(previous.model_dump())
                    or event.stage not in {'intent': {'created'}, 'created': {'started', 'removed'}, 'started': {'removed'}}.get(previous.stage, set())
                    or index > 1 and event.container_id != previous.container_id):
                raise ValueError('lifecycle_chain_invalid')
    if path.name != events[0].birth.runtime_id + '.jsonl':
        raise ValueError('lifecycle_filename_invalid')
    return events, hashlib.sha256(content).hexdigest()


class LifecycleJournal:
    def __init__(self, workspace: Path, workspace_descriptor: int, runtime_id: str, image_id: str, source_sha256: str):
        self.descriptor = None
        self.last = None
        self.failed = False
        check = open_existing_workspace(workspace)
        try:
            binding = workspace_identity(workspace, check)
            if binding != workspace_identity(workspace, workspace_descriptor):
                raise ValueError('lifecycle_workspace_changed')
        finally:
            os.close(check)
        self.birth = LifecycleBirth(runtime_id=runtime_id,
                                    container_name='aos-desktop-' + hashlib.sha256(str(workspace.absolute()).encode()).hexdigest()[:20],
                                    image_id=image_id, source_sha256=source_sha256, workspace=binding,
                                    process=process_identity(os.getpid()))
        directory = workspace.parent / '.aos-lifecycle'
        parent = open_existing_workspace(workspace.parent)
        try:
            try:
                os.mkdir('.aos-lifecycle', mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            os.fsync(parent)
        finally:
            os.close(parent)
        self.path = directory / (runtime_id + '.jsonl')
        parent = private_directory(directory)
        try:
            self.descriptor = os.open(self.path.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.record('intent', None)
            os.fsync(parent)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(parent)

    def record(self, stage: str, container_id: str | None) -> None:
        if self.descriptor is None or self.failed:
            raise ValueError('lifecycle_writer_unavailable')
        event = LifecycleEvent(birth=self.birth, sequence=0 if self.last is None else self.last.sequence + 1,
                               previous_sha256=None if self.last is None else digest(self.last.model_dump()),
                               stage=stage, container_id=container_id, recorded_at=now())
        if self.last and (stage not in {'intent': {'created'}, 'created': {'started', 'removed'}, 'started': {'removed'}}.get(self.last.stage, set())
                          or self.last.container_id is not None and container_id != self.last.container_id):
            raise ValueError('lifecycle_writer_transition_invalid')
        payload = (canonical(event.model_dump()) + '\n').encode()
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(self.descriptor, payload[offset:])
                if written <= 0:
                    raise OSError('lifecycle_short_write')
                offset += written
            os.fsync(self.descriptor)
        except BaseException:
            self.failed = True
            raise
        self.last = event

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

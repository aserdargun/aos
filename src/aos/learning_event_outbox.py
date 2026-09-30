"""Manual, synthetic-only durable checkpoints of audited learning metadata."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import SYNTHETIC_POLICIES, audit_snapshot
from .learning_events import DERIVATION_VERSION, MAX_REVIEW_EVENTS, RUN_ID, project_learning_events
from .lifecycle import private_directory
from .workspace_identity import open_existing_workspace


MAX_CHECKPOINT_BYTES = 256 * 1024
ROLES = ('system1', 'system2')
FILE_FIELDS = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
TEMPORARY_NAME = re.compile(r'\.checkpoint-[a-f0-9]{32}\Z')


def _key(run_id: str, role: str) -> str:
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None or role not in ROLES:
        raise ValueError('invalid_learning_checkpoint_selection')
    return digest({'derivation_version': DERIVATION_VERSION, 'role': role, 'run_id': run_id})


def _directory(root: Path, *, create: bool) -> int:
    root = Path(root)
    if root.name in ('', '.', '..'):
        raise ValueError('invalid_learning_checkpoint_directory')
    if create:
        parent = open_existing_workspace(root.parent)
        try:
            try:
                os.mkdir(root.name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        finally:
            os.close(parent)
    return private_directory(root)


def _validate(record: dict, *, key: str, role: str) -> None:
    if not validator('learning_event_outbox').is_valid(record):
        raise ValueError('invalid_learning_checkpoint')
    unsigned = {name: value for name, value in record.items() if name != 'checkpoint_sha256'}
    if (record['key_sha256'] != key or record['role'] != role
            or record['event_count'] != len(record['events'])
            or record['events_sha256'] != digest(record['events'])
            or record['checkpoint_sha256'] != digest(unsigned)):
        raise ValueError('learning_checkpoint_integrity_failure')
    if len({event['event_id'] for event in record['events']}) != len(record['events']):
        raise ValueError('duplicate_learning_checkpoint_event')
    for event in record['events']:
        if (not validator('learning_event_v2').is_valid(event)
                or event['role'] != role or event['derivation_version'] != DERIVATION_VERSION
                or 'synthetic_task_excluded' not in event['blockers']):
            raise ValueError('invalid_learning_checkpoint_event')


def _project(database: Path, run_id: str, role: str) -> dict:
    key = _key(run_id, role)
    with audit_snapshot(database) as (snapshot, identity):
        run = snapshot.execute('SELECT policy_version FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            raise ValueError('learning_checkpoint_run_missing')
        if run['policy_version'] not in SYNTHETIC_POLICIES:
            raise ValueError('learning_checkpoint_requires_synthetic_policy')
        calls = snapshot.execute('SELECT count(*) FROM model_calls WHERE run_id=?', (run_id,)).fetchone()[0]
        if calls > MAX_REVIEW_EVENTS:
            raise ValueError('learning_checkpoint_event_limit')
        events = [event for event in project_learning_events(snapshot, run_id) if event['role'] == role]
        record = {'schema_version': '1.0', 'mode': 'manual_synthetic_metadata_checkpoint',
                  'key_sha256': key, 'derivation_version': DERIVATION_VERSION,
                  'run_ref': digest({'run_id': run_id}), 'role': role,
                  'source_snapshot_sha256': identity['sha256'], 'source_policy': run['policy_version'],
                  'event_count': len(events), 'events_sha256': digest(events), 'events': events,
                  'synthetic': True, 'metadata_only': True,
                  'collection_authorized': False, 'training_ready': False}
        record['checkpoint_sha256'] = digest(record)
        _validate(record, key=key, role=role)
        return record


def _read(directory: int, key: str, role: str, *, link_count: int = 1) -> dict:
    filename = key + '.json'
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != link_count
                or before.st_size == 0 or before.st_size > MAX_CHECKPOINT_BYTES):
            raise ValueError('invalid_private_learning_checkpoint')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            payload = source.read(MAX_CHECKPOINT_BYTES + 1)
        after = os.fstat(descriptor)
        linked = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if (len(payload) != before.st_size or len(payload) > MAX_CHECKPOINT_BYTES
                or any(getattr(before, field) != getattr(current, field)
                       for field in FILE_FIELDS for current in (after, linked))):
            raise ValueError('learning_checkpoint_file_changed')
        record = json.loads(payload)
        if payload != canonical(record).encode():
            raise ValueError('learning_checkpoint_noncanonical')
        _validate(record, key=key, role=role)
        return record
    finally:
        os.close(descriptor)


def _recover_published_link(directory: int, key: str, role: str, expected: dict) -> None:
    filename = key + '.json'
    try:
        published = os.stat(filename, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if published.st_nlink != 2:
        return
    matches = []
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries, 1):
            if count > 1024:
                raise ValueError('learning_checkpoint_recovery_directory_limit')
            if TEMPORARY_NAME.fullmatch(entry.name) is None:
                continue
            temporary = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            if (stat.S_ISREG(temporary.st_mode) and temporary.st_uid == os.getuid()
                    and stat.S_IMODE(temporary.st_mode) == 0o600
                    and temporary.st_dev == published.st_dev and temporary.st_ino == published.st_ino
                    and temporary.st_nlink == 2):
                matches.append(entry.name)
    if len(matches) != 1:
        raise ValueError('learning_checkpoint_recovery_ambiguous')
    if _read(directory, key, role, link_count=2) != expected:
        raise ValueError('learning_checkpoint_source_changed')
    os.unlink(matches[0], dir_fd=directory)
    os.fsync(directory)


def load_learning_checkpoint(database: Path, run_id: str, role: str, root: Path) -> dict:
    """Inspect one exact persisted checkpoint against the current audited source."""
    expected = _project(database, run_id, role)
    directory = _directory(root, create=False)
    try:
        recorded = _read(directory, expected['key_sha256'], role)
        if recorded != expected:
            raise ValueError('learning_checkpoint_source_changed')
        return recorded
    finally:
        os.close(directory)


def derive_learning_checkpoint(database: Path, run_id: str, role: str, root: Path) -> dict:
    """Explicitly persist one role's bounded metadata; never enqueue training work."""
    expected = _project(database, run_id, role)
    payload = canonical(expected).encode()
    if len(payload) > MAX_CHECKPOINT_BYTES:
        raise ValueError('learning_checkpoint_size_limit')
    directory = _directory(root, create=True)
    key = expected['key_sha256']
    temporary = '.checkpoint-' + uuid4().hex
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        _recover_published_link(directory, key, role, expected)
        try:
            recorded = _read(directory, key, role)
        except FileNotFoundError:
            recorded = None
        if recorded is not None:
            if recorded != expected:
                raise ValueError('learning_checkpoint_key_conflict')
            return recorded
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, key + '.json', src_dir_fd=directory,
                    dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            pass
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
        recorded = _read(directory, key, role)
        if recorded != expected:
            raise ValueError('learning_checkpoint_key_conflict')
        return recorded
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Explicit synthetic learning metadata checkpoint')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--role', choices=ROLES, required=True)
    parser.add_argument('--outbox-dir', type=Path, required=True)
    parser.add_argument('--inspect', action='store_true')
    arguments, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.exit(2, 'Learning checkpoint unavailable: invalid arguments.\n')
    try:
        operation = load_learning_checkpoint if arguments.inspect else derive_learning_checkpoint
        result = operation(arguments.database, arguments.run_id, arguments.role, arguments.outbox_dir)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Learning checkpoint unavailable: missing, changed or unsafe source.\n')
    print(canonical(result))


if __name__ == '__main__':
    main()

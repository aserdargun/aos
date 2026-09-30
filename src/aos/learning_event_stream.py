"""Opt-in, synthetic-only incremental metadata derivation into an independent store."""

import argparse
from contextlib import closing
import fcntl
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time

from .contracts import REPO_ROOT, canonical, digest
from .dataset import validator
from .dataset_audit import SYNTHETIC_POLICIES, audit_snapshot, schema_signature
from .learning_event_outbox import _directory
from .learning_events import MAX_REVIEW_EVENTS, RUN_ID, project_learning_events


DERIVATION_VERSION = 'learning-stream-v1'
DOWNSTREAM_DERIVATION_VERSION = 'learning-stream-v2'
MIGRATION = REPO_ROOT / 'database/learning_migrations/0001_incremental_metadata.sql'
DOWNSTREAM_MIGRATION = REPO_ROOT / 'database/learning_migrations/0002_downstream_metadata.sql'
STORE_NAME = 'learning-stream.sqlite'
MAX_ENTRIES = 2000
MAX_WATCH_SECONDS = 300
MAX_POLLS = 1200
ROLES = ('system1', 'system2')


def _private_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        raise ValueError('private_learning_file_required')
    return metadata


def _source_ref(path: Path) -> str:
    metadata = _private_file(path)
    return digest({'path': str(path.absolute()), 'device': metadata.st_dev, 'inode': metadata.st_ino})


@lru_cache(maxsize=1)
def _schema_signatures() -> tuple[str, str]:
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.executescript(MIGRATION.read_text())
        initial = schema_signature(connection)
        connection.executescript(DOWNSTREAM_MIGRATION.read_text())
        return initial, schema_signature(connection)


def _open_store(root: Path) -> tuple[sqlite3.Connection, int]:
    directory = _directory(root, create=True)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        path = Path(root) / STORE_NAME
        try:
            metadata = _private_file(path)
            created = False
        except FileNotFoundError:
            descriptor = os.open(STORE_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            os.close(descriptor)
            os.fsync(directory)
            metadata = _private_file(path)
            created = True
        if metadata.st_size > 32 * 1024 * 1024:
            raise ValueError('learning_stream_size_limit')
        journal = path.with_name(path.name + '-journal')
        if journal.exists() or journal.is_symlink():
            _private_file(journal)
        connection = sqlite3.connect(path, timeout=2)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=2000')
            if created:
                connection.executescript(MIGRATION.read_text())
                connection.executescript(DOWNSTREAM_MIGRATION.read_text())
                os.fsync(directory)
            elif (schema_signature(connection) == _schema_signatures()[0]
                  and [tuple(row) for row in connection.execute(
                      'SELECT version,name FROM learning_migrations ORDER BY version')]
                  == [(1, 'incremental_metadata')]):
                if ([tuple(row) for row in connection.execute('PRAGMA integrity_check')] != [('ok',)]
                        or connection.execute('PRAGMA foreign_key_check').fetchone()):
                    raise ValueError('learning_stream_schema_or_integrity_failure')
                for row in connection.execute('SELECT * FROM entries'):
                    try:
                        stored = json.loads(row['entry_json'])
                    except (TypeError, ValueError):
                        raise ValueError('learning_stream_schema_or_integrity_failure') from None
                    if (not validator('learning_event_stream_entry').is_valid(stored)
                            or stored['derivation_version'] != DERIVATION_VERSION
                            or row['entry_json'] != canonical(stored)
                            or row['content_sha256'] != digest(stored)
                            or stored['entry_id'] != row['entry_id']
                            or stored['source']['run_id'] != row['run_id']
                            or stored['role'] != row['role'] or stored['kind'] != row['kind']):
                        raise ValueError('learning_stream_schema_or_integrity_failure')
                connection.executescript(DOWNSTREAM_MIGRATION.read_text())
                os.fsync(directory)
            if (schema_signature(connection) != _schema_signatures()[1]
                    or [tuple(row) for row in connection.execute(
                        'SELECT version,name FROM learning_migrations ORDER BY version')]
                    != [(1, 'incremental_metadata'), (2, 'downstream_metadata')]
                    or [tuple(row) for row in connection.execute('PRAGMA integrity_check')] != [('ok',)]
                    or connection.execute('PRAGMA foreign_key_check').fetchone()):
                raise ValueError('learning_stream_schema_or_integrity_failure')
            return connection, directory
        except BaseException:
            connection.close()
            raise
    except BaseException:
        os.close(directory)
        raise


def _row(connection: sqlite3.Connection, table: str, column: str, identifier: str, run_id: str, step_id: str) -> dict:
    row = connection.execute(f'SELECT * FROM {table} WHERE {column}=? AND run_id=? AND step_id=?',
                             (identifier, run_id, step_id)).fetchone()
    if row is None:
        raise ValueError('learning_stream_source_missing')
    return dict(row)


def _source_sha256(connection: sqlite3.Connection, event: dict, kind: str, verification_id: str | None) -> str:
    source = event['source']
    run_id, step_id = source['run_id'], source['step_id']
    call = _row(connection, 'model_calls', 'call_id', source['call_id'], run_id, step_id)
    identity = {'call': call}
    if source['decision_id'] is not None:
        decision = _row(connection, 'decisions', 'decision_id', source['decision_id'], run_id, step_id)
        identity['decision'] = decision
        identity['state_snapshot'] = _row(connection, 'state_snapshots', 'snapshot_id',
                                          decision['snapshot_id'], run_id, step_id)
    if source['escalation_id'] is not None:
        escalation = _row(connection, 'supervisor_escalations', 'escalation_id',
                          source['escalation_id'], run_id, step_id)
        identity['escalation'] = {key: value for key, value in escalation.items() if key != 'outcome'}
    if kind in {'verification_linked', 'downstream_verification_linked'}:
        if verification_id is None:
            raise ValueError('learning_stream_verification_missing')
        verification = _row(connection, 'verifications', 'verification_id', verification_id, run_id, step_id)
        identity['verification'] = verification
        identity['action'] = _row(connection, 'actions', 'action_id', verification['action_id'], run_id, step_id)
        evidence = json.loads(verification['evidence_refs_json'])
        identity['evidence'] = []
        for reference in evidence:
            observation = connection.execute('SELECT * FROM observations WHERE observation_id=? AND run_id=? AND step_id=?',
                                             (reference, run_id, step_id)).fetchone()
            artifact = connection.execute('SELECT * FROM artifacts WHERE artifact_id=? AND run_id=? AND step_id=?',
                                          (reference, run_id, step_id)).fetchone()
            if (observation is None) == (artifact is None):
                raise ValueError('learning_stream_evidence_invalid')
            identity['evidence'].append(dict(observation if observation is not None else artifact))
    if kind == 'downstream_verification_linked':
        scene = _row(connection, 'observations', 'observation_id',
                     source['scene_observation_id'], run_id, step_id)
        if scene['kind'] != 'vision.scene' or scene['action_id'] is not None:
            raise ValueError('learning_stream_scene_source_invalid')
        identity['scene_observation'] = scene
    return digest(identity)


def _candidates(connection: sqlite3.Connection, run_id: str, roles: tuple[str, ...], snapshot_sha256: str) -> dict[str, dict]:
    result = {}
    for event in project_learning_events(connection, run_id):
        if event['role'] not in roles:
            continue
        source = event['source']
        base = {key: source[key] for key in ('run_id', 'step_id', 'call_id', 'deployment_id',
                                            'deployment_sha256', 'decision_id', 'escalation_id')}
        linked = ([('verification_linked', value) for value in source['verification_ids']]
                  if event['role'] == 'system1' else
                  [('downstream_verification_linked', value)
                   for value in source['downstream_verification_ids']]
                  if source['scene_observation_id'] is not None else [])
        for kind, verification_id in [('call_observed', None), *linked]:
            entry_source = {**base, 'verification_id': verification_id}
            version = DERIVATION_VERSION
            if kind == 'downstream_verification_linked':
                entry_source['scene_observation_id'] = source['scene_observation_id']
                version = DOWNSTREAM_DERIVATION_VERSION
            entry_id = digest({'version': version, 'kind': kind, 'role': event['role'],
                               'run_id': run_id, 'call_id': source['call_id'],
                               'verification_id': verification_id,
                               **({'scene_observation_id': source['scene_observation_id']}
                                  if kind == 'downstream_verification_linked' else {})})
            entry = {'schema_version': '1.0', 'derivation_version': version,
                     'entry_id': entry_id, 'kind': kind, 'role': event['role'],
                     'model_kind': event['model_kind'], 'source': entry_source,
                     'source_sha256': _source_sha256(connection, event, kind, verification_id),
                     'source_snapshot_sha256': snapshot_sha256, 'synthetic': True,
                     'metadata_only': True, 'collection_authorized': False, 'training_ready': False}
            if not validator('learning_event_stream_entry').is_valid(entry) or entry_id in result:
                raise ValueError('invalid_learning_stream_entry')
            result[entry_id] = entry
    if len(result) > MAX_ENTRIES:
        raise ValueError('learning_stream_entry_limit')
    return result


def poll_learning_stream(database: Path, run_id: str, roles: tuple[str, ...], outbox_dir: Path) -> dict:
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not roles or len(set(roles)) != len(roles) or any(role not in ROLES for role in roles)):
        raise ValueError('invalid_learning_stream_selection')
    database = Path(database)
    before = _source_ref(database)
    with audit_snapshot(database) as (snapshot, identity):
        run = snapshot.execute('SELECT task_id,policy_version,deployment_snapshot_json,status FROM runs WHERE run_id=?',
                               (run_id,)).fetchone()
        if run is None or run['policy_version'] not in SYNTHETIC_POLICIES:
            raise ValueError('learning_stream_requires_synthetic_run')
        if snapshot.execute('SELECT count(*) FROM model_calls WHERE run_id=?', (run_id,)).fetchone()[0] > MAX_REVIEW_EVENTS:
            raise ValueError('learning_stream_call_limit')
        candidates = _candidates(snapshot, run_id, roles, identity['sha256'])
        status = run['status']
        run_identity_sha256 = digest({'run_id': run_id, 'task_id': run['task_id'],
                                      'policy_version': run['policy_version'],
                                      'deployment_snapshot_json': run['deployment_snapshot_json']})
        schema_sha256 = identity['schema_sha256']
        snapshot_sha256 = identity['sha256']
    if _source_ref(database) != before:
        raise ValueError('learning_stream_source_replaced')
    outbox, directory = _open_store(outbox_dir)
    try:
        outbox.execute('BEGIN IMMEDIATE')
        try:
            binding = outbox.execute('SELECT source_ref,source_schema_sha256 FROM source_binding WHERE singleton=1').fetchone()
            if binding is None:
                if outbox.execute('SELECT 1 FROM entries LIMIT 1').fetchone() is not None:
                    raise ValueError('learning_stream_binding_missing')
                outbox.execute('INSERT INTO source_binding VALUES(1,?,?)', (before, schema_sha256))
            elif binding['source_ref'] != before or binding['source_schema_sha256'] != schema_sha256:
                raise ValueError('learning_stream_source_changed')
            run_binding = outbox.execute('SELECT run_identity_sha256 FROM run_bindings WHERE run_id=?',
                                         (run_id,)).fetchone()
            if run_binding is None:
                if outbox.execute('SELECT 1 FROM entries WHERE run_id=? LIMIT 1', (run_id,)).fetchone() is not None:
                    raise ValueError('learning_stream_run_binding_missing')
                outbox.execute('INSERT INTO run_bindings VALUES(?,?)', (run_id, run_identity_sha256))
            elif run_binding['run_identity_sha256'] != run_identity_sha256:
                raise ValueError('learning_stream_run_identity_changed')
            existing = outbox.execute('SELECT * FROM entries WHERE run_id=? AND role IN ('
                                      + ','.join('?' for _ in roles) + ') ORDER BY entry_id', (run_id, *roles)).fetchall()
            for row in existing:
                stored = json.loads(row['entry_json'])
                current = candidates.get(row['entry_id'])
                if (not validator('learning_event_stream_entry').is_valid(stored)
                        or row['entry_json'] != canonical(stored)
                        or row['content_sha256'] != digest(stored)
                        or stored['entry_id'] != row['entry_id'] or stored['role'] != row['role']
                        or stored['kind'] != row['kind'] or stored['source']['run_id'] != row['run_id']
                        or current is None or stored['source_sha256'] != current['source_sha256']
                        or {key: value for key, value in stored.items() if key != 'source_snapshot_sha256'}
                        != {key: value for key, value in current.items() if key != 'source_snapshot_sha256'}):
                    raise ValueError('learning_stream_source_regressed_or_changed')
            known = {row['entry_id'] for row in existing}
            added = [candidates[key] for key in sorted(candidates) if key not in known]
            if len(existing) + len(added) > MAX_ENTRIES:
                raise ValueError('learning_stream_entry_limit')
            for entry in added:
                outbox.execute('INSERT INTO entries VALUES(?,?,?,?,?,?)',
                               (entry['entry_id'], run_id, entry['role'], entry['kind'], canonical(entry), digest(entry)))
            outbox.commit()
        except BaseException:
            outbox.rollback()
            raise
        totals = {role: outbox.execute('SELECT count(*) FROM entries WHERE run_id=? AND role=?',
                                       (run_id, role)).fetchone()[0] for role in roles}
        report = {'schema_version': '1.0', 'mode': 'opt_in_synthetic_metadata_poll',
                  'run_ref': digest({'run_id': run_id}), 'source_ref': before,
                  'source_snapshot_sha256': snapshot_sha256, 'run_status': status,
                  'roles': list(roles), 'new_entries': len(added), 'total_entries': sum(totals.values()),
                  'entries_by_role': totals, 'synthetic': True, 'metadata_only': True,
                  'collection_authorized': False, 'training_ready': False}
        if not validator('learning_event_stream_report').is_valid(report):
            raise ValueError('invalid_learning_stream_report')
        return report
    finally:
        outbox.close()
        os.close(directory)


def watch_learning_stream(database: Path, run_id: str, roles: tuple[str, ...], outbox_dir: Path,
                          watch_seconds: float = 0, interval_seconds: float = 1) -> dict:
    if (not math.isfinite(watch_seconds) or not 0 <= watch_seconds <= MAX_WATCH_SECONDS
            or not math.isfinite(interval_seconds) or not 0.25 <= interval_seconds <= 30):
        raise ValueError('invalid_learning_stream_budget')
    deadline = time.monotonic() + watch_seconds
    polls = 0
    added = 0
    while True:
        report = poll_learning_stream(database, run_id, roles, outbox_dir)
        polls += 1
        added += report['new_entries']
        if polls >= MAX_POLLS or time.monotonic() >= deadline:
            watched = {**report, 'mode': 'opt_in_synthetic_metadata_watch', 'polls': polls, 'new_entries': added}
            if not validator('learning_event_stream_report').is_valid(watched):
                raise ValueError('invalid_learning_stream_report')
            return watched
        time.sleep(min(interval_seconds, max(0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Explicit bounded synthetic-only learning metadata polling')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--roles', choices=('system1', 'system2', 'both'), required=True)
    parser.add_argument('--outbox-dir', type=Path, required=True)
    parser.add_argument('--watch-seconds', type=float, default=0)
    parser.add_argument('--interval-seconds', type=float, default=1)
    arguments, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.exit(2, 'Learning stream unavailable: invalid arguments.\n')
    roles = ROLES if arguments.roles == 'both' else (arguments.roles,)
    try:
        report = watch_learning_stream(arguments.database, arguments.run_id, roles, arguments.outbox_dir,
                                       arguments.watch_seconds, arguments.interval_seconds)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Learning stream unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

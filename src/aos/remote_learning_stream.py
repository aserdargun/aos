"""Explicit, run-bound incremental remote model metadata polling."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .contracts import REPO_ROOT, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot, schema_signature
from .desktop_tasks import Approval
from .learning_event_outbox import _directory
from .learning_event_stream import _private_file, _source_ref
from .learning_events import MAX_REVIEW_EVENTS, project_learning_events
from .remote_learning_consent import RemoteLearningConsents, _bound_run_snapshot
from .remote_route_evidence import CHECKSUM, _decode
from .web_application import WebApplicationProfiles


MIGRATION = REPO_ROOT / 'database/remote_learning_migrations/0001_metadata.sql'
STORE_NAME = 'remote-learning-stream.sqlite'
VERSION = 'remote-learning-stream-v1'
MAX_ENTRIES = 500
MAX_WATCH_SECONDS = 300
MAX_POLLS = 1200
TERMINAL = {'succeeded', 'failed', 'cancelled'}


@lru_cache(maxsize=1)
def _expected_schema() -> str:
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.executescript(MIGRATION.read_text())
        return schema_signature(connection)


def _open_store(root: Path, *, create: bool = True) -> tuple[sqlite3.Connection, int]:
    directory = _directory(root, create=create)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        path = Path(root) / STORE_NAME
        if create:
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
        else:
            metadata = _private_file(path)
            created = False
        if metadata.st_size > 16 * 1024 * 1024:
            raise ValueError('remote_learning_stream_size_limit')
        journal = path.with_name(path.name + '-journal')
        if journal.exists() or journal.is_symlink():
            _private_file(journal)
        connection = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True, timeout=2)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=2000')
            if created:
                connection.executescript(MIGRATION.read_text())
                os.fsync(directory)
            if (schema_signature(connection) != _expected_schema()
                    or [tuple(row) for row in connection.execute(
                        'SELECT version,name FROM remote_learning_migrations ORDER BY version')]
                    != [(1, 'metadata')]
                    or [tuple(row) for row in connection.execute('PRAGMA integrity_check')] != [('ok',)]
                    or connection.execute('PRAGMA foreign_key_check').fetchone()):
                raise ValueError('remote_learning_stream_schema_or_integrity_failure')
            return connection, directory
        except BaseException:
            connection.close()
            raise
    except BaseException:
        os.close(directory)
        raise


def _store_for_consent(root: Path, consent_sha256: str) -> Path:
    if not isinstance(consent_sha256, str) or CHECKSUM.fullmatch(consent_sha256) is None:
        raise ValueError('invalid_remote_learning_consent_selection')
    root = Path(root)
    directory = _directory(root, create=True)
    try:
        try:
            os.stat(STORE_NAME, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return root / consent_sha256
    finally:
        os.close(directory)
    legacy, legacy_directory = _open_store(root, create=False)
    try:
        binding = legacy.execute('SELECT consent_sha256 FROM source_binding WHERE singleton=1').fetchone()
        if binding is None and legacy.execute('SELECT 1 FROM entries LIMIT 1').fetchone() is not None:
            raise ValueError('remote_learning_legacy_binding_missing')
        if binding is not None and binding['consent_sha256'] == consent_sha256:
            if (root / consent_sha256).exists() or (root / consent_sha256).is_symlink():
                raise ValueError('remote_learning_duplicate_store')
            return root
    finally:
        legacy.close()
        os.close(legacy_directory)
    return root / consent_sha256


def _candidate_actions(snapshot: sqlite3.Connection, run_id: str, binding,
                       plan) -> dict[tuple[str, str], tuple[int, sqlite3.Row, sqlite3.Row, sqlite3.Row, dict]]:
    actions = snapshot.execute('''SELECT * FROM actions WHERE run_id=?
        AND tool='browser.remote.route' ORDER BY created_at,action_id''', (run_id,)).fetchall()
    if len(actions) > len(plan.routes):
        raise ValueError('remote_learning_route_count_exceeded')
    result = {}
    route_indices = set()
    for action in actions:
        arguments = _decode(action['arguments_json'],
                            {'profile_sha256', 'binding_sha256', 'plan_sha256',
                             'route_index', 'url'})
        index = arguments['route_index']
        if (type(index) is not int or index < 0 or index >= len(plan.routes)
                or index in route_indices
                or arguments != {'profile_sha256': binding['profile_sha256'],
                                 'binding_sha256': binding['binding_sha256'],
                                 'plan_sha256': binding['plan_sha256'],
                                 'route_index': index, 'url': plan.routes[index]}):
            raise ValueError('remote_learning_route_action_changed')
        route_indices.add(index)
        if action['status'] != 'ok':
            continue
        if action['actual_option'] != 'open_entry':
            raise ValueError('remote_learning_route_option_changed')
        decision = snapshot.execute('''SELECT * FROM decisions WHERE run_id=?
            AND step_id=? AND decision_id=?''',
            (run_id, action['step_id'], action['decision_id'])).fetchone()
        if (decision is None or decision['selected_option'] != 'open_entry'
                or decision['policy_result'] != 'allow'):
            raise ValueError('remote_learning_route_decision_changed')
        approvals = snapshot.execute('''SELECT * FROM desktop_approvals
            WHERE job_id=? AND status='consumed' ''', (binding['job_id'],)).fetchall()
        matches = []
        for row in approvals:
            approval = Approval.model_validate_json(row['envelope_json'])
            if approval.action.action_id == action['action_id']:
                matches.append((row, approval))
        if len(matches) != 1:
            raise ValueError('remote_learning_route_approval_missing')
        approval_row, approval = matches[0]
        if (approval.job_id != binding['job_id'] or approval.action.run_id != run_id
                or approval.action.step_id != action['step_id']
                or approval.action.runtime_id != binding['browser_runtime_id']
                or approval.action.tool != 'browser.remote.route'
                or approval.action.arguments != arguments
                or approval.action_sha256 != approval_row['action_sha256']
                or digest(approval.action.model_dump(mode='json')) != approval_row['action_sha256']):
            raise ValueError('remote_learning_route_approval_changed')
        interventions = snapshot.execute('''SELECT * FROM human_interventions
            WHERE run_id=? AND step_id=? AND kind='approve' ''',
            (run_id, action['step_id'])).fetchall()
        matched = [row for row in interventions
                   if row['actor'] == 'local_authenticated_user'
                   and json.loads(row['payload_json']) == {
                       'approval_id': approval.approval_id,
                       'action_sha256': approval.action_sha256}]
        if len(matched) != 1:
            raise ValueError('remote_learning_human_approval_missing')
        key = (action['step_id'], action['decision_id'])
        if key in result:
            raise ValueError('remote_learning_route_decision_ambiguous')
        result[key] = (index, action, decision, approval_row, dict(matched[0]))
    return result


def _candidates(snapshot: sqlite3.Connection, run_id: str, consent, binding,
                plan, snapshot_sha256: str, consent_sha256: str) -> dict[str, dict]:
    routes = _candidate_actions(snapshot, run_id, binding, plan)
    candidates = {}
    for event in project_learning_events(snapshot, run_id):
        role = event['role']
        source = event['source']
        if role not in consent.roles:
            continue
        route = routes.get((source['step_id'], source['decision_id'])) if role == 'system1' else None
        if (role == 'system1' and route is None) or (
                role == 'system2' and source['escalation_id'] is None):
            continue
        call = snapshot.execute('''SELECT * FROM model_calls
            WHERE run_id=? AND step_id=? AND call_id=?''',
            (run_id, source['step_id'], source['call_id'])).fetchone()
        if call is None:
            raise ValueError('remote_learning_model_call_missing')
        source_rows = {'call': dict(call)}
        index = None
        if route is not None:
            index, action, decision, approval, intervention = route
            source_rows.update({'action': dict(action), 'decision': dict(decision),
                                'approval': dict(approval), 'intervention': intervention})
        if role == 'system2':
            escalation = snapshot.execute('''SELECT * FROM supervisor_escalations
                WHERE run_id=? AND step_id=? AND escalation_id=?''',
                (run_id, source['step_id'], source['escalation_id'])).fetchone()
            if escalation is None:
                raise ValueError('remote_learning_escalation_missing')
            source_rows['escalation'] = {key: value for key, value in dict(escalation).items()
                                         if key != 'outcome'}
        entry_id = digest({'version': VERSION, 'run_id': run_id,
                           'consent_sha256': consent_sha256,
                           'source_event_id': event['event_id']})
        entry = {'schema_version': '1.0', 'derivation_version': VERSION,
                 'entry_id': entry_id, 'source_event_id': event['event_id'],
                 'role': role, 'model_kind': event['model_kind'],
                 'run_ref': digest({'run_id': run_id}),
                 'profile_sha256': consent.profile_sha256,
                 'plan_sha256': consent.plan_sha256,
                 'consent_sha256': consent_sha256,
                 'route_index': index, 'source_sha256': digest(source_rows),
                 'source_snapshot_sha256': snapshot_sha256,
                 'status': 'unreviewed_metadata_candidate',
                 'metadata_only': True, 'transport_readback_verified': False,
                 'site_outcome_verified': False, 'external_rights_verified': False,
                 'redaction_reviewed': False, 'training_ready': False}
        if not validator('remote_learning_stream_entry').is_valid(entry) or entry_id in candidates:
            raise ValueError('remote_learning_entry_invalid')
        candidates[entry_id] = entry
    if len(candidates) > MAX_ENTRIES:
        raise ValueError('remote_learning_entry_limit')
    return candidates


def poll_remote_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                consent_sha256: str, outbox_dir: Path) -> dict:
    with RemoteLearningConsents(consents).active(consent_sha256) as consent:
        return _poll_authorized_remote_learning_stream(
            database, profiles=profiles,
            consent_sha256=consent_sha256, outbox_dir=outbox_dir, consent=consent)


def _poll_authorized_remote_learning_stream(database: Path, *, profiles: Path,
                                            consent_sha256: str, outbox_dir: Path, consent) -> dict:
    if consent.scope != 'remote_route_model_metadata_only':
        raise ValueError('remote_learning_scope_mismatch')
    profile_store = WebApplicationProfiles(profiles)
    profile = profile_store.get(consent.profile_sha256)
    if (profile.learning.data_rights_ref != consent.data_rights_ref
            or profile.learning.retention_days != consent.retention_days
            or any(getattr(profile.learning, role) != 'requested' for role in consent.roles)):
        raise ValueError('remote_learning_consent_profile_changed')
    database = Path(database)
    source_ref = _source_ref(database)
    with audit_snapshot(database) as (snapshot, identity):
        binding, draft, plan = _bound_run_snapshot(
            snapshot, consent.run_id, profile_store,
            consent.profile_sha256, consent.plan_sha256)
        if (binding['binding_sha256'] != consent.binding_sha256
                or draft.task.task_key != consent.task_key):
            raise ValueError('remote_learning_consent_binding_changed')
        run = snapshot.execute('''SELECT task_id,policy_version,deployment_snapshot_json,status
            FROM runs WHERE run_id=?''', (consent.run_id,)).fetchone()
        if (run is None or run['policy_version'] != 'browser-remote-routes-policy-v1'
                or snapshot.execute('SELECT count(*) FROM model_calls WHERE run_id=?',
                                    (consent.run_id,)).fetchone()[0] > MAX_REVIEW_EVENTS):
            raise ValueError('remote_learning_run_invalid')
        candidates = _candidates(snapshot, consent.run_id, consent, binding, plan,
                                 identity['sha256'], consent_sha256)
        run_identity_sha256 = digest({'run_id': consent.run_id, 'task_id': run['task_id'],
                                      'policy_version': run['policy_version'],
                                      'deployment_snapshot_json': run['deployment_snapshot_json']})
        status = run['status']
    if (_source_ref(database) != source_ref
            or datetime.fromisoformat(consent.expires_at) <= datetime.now(timezone.utc)):
        raise ValueError('remote_learning_source_or_consent_changed')
    outbox, directory = _open_store(_store_for_consent(outbox_dir, consent_sha256))
    try:
        if datetime.fromisoformat(consent.expires_at) <= datetime.now(timezone.utc):
            raise ValueError('remote_learning_consent_expired_during_poll')
        outbox.execute('BEGIN IMMEDIATE')
        try:
            binding = outbox.execute('SELECT * FROM source_binding WHERE singleton=1').fetchone()
            expected = (source_ref, identity['schema_sha256'], consent.run_id,
                        consent.profile_sha256, consent.plan_sha256,
                        consent.binding_sha256, consent_sha256, run_identity_sha256)
            if binding is None:
                if outbox.execute('SELECT 1 FROM entries LIMIT 1').fetchone() is not None:
                    raise ValueError('remote_learning_binding_missing')
                outbox.execute('INSERT INTO source_binding VALUES(1,?,?,?,?,?,?,?,?)', expected)
            elif tuple(binding)[1:] != expected:
                raise ValueError('remote_learning_source_binding_changed')
            existing = outbox.execute('SELECT * FROM entries ORDER BY entry_id').fetchall()
            for row in existing:
                stored = json.loads(row['entry_json'])
                current = candidates.get(row['entry_id'])
                if (not validator('remote_learning_stream_entry').is_valid(stored)
                        or row['entry_json'] != canonical(stored)
                        or row['content_sha256'] != digest(stored)
                        or row['entry_id'] != stored['entry_id']
                        or row['role'] != stored['role']
                        or row['source_event_id'] != stored['source_event_id']
                        or current is None
                        or {key: value for key, value in stored.items()
                            if key != 'source_snapshot_sha256'}
                        != {key: value for key, value in current.items()
                            if key != 'source_snapshot_sha256'}):
                    raise ValueError('remote_learning_source_regressed_or_changed')
            known = {row['entry_id'] for row in existing}
            added = [candidates[key] for key in sorted(candidates) if key not in known]
            if len(existing) + len(added) > MAX_ENTRIES:
                raise ValueError('remote_learning_entry_limit')
            for entry in added:
                outbox.execute('INSERT INTO entries VALUES(?,?,?,?,?)',
                               (entry['entry_id'], entry['role'], entry['source_event_id'],
                                canonical(entry), digest(entry)))
            outbox.commit()
        except BaseException:
            outbox.rollback()
            raise
        counts = {role: outbox.execute('SELECT count(*) FROM entries WHERE role=?',
                                       (role,)).fetchone()[0] for role in ('system1', 'system2')}
        report = {'schema_version': '1.0', 'mode': 'opt_in_remote_metadata_poll',
                  'run_ref': digest({'run_id': consent.run_id}),
                  'profile_sha256': consent.profile_sha256,
                  'plan_sha256': consent.plan_sha256,
                  'consent_sha256': consent_sha256,
                  'source_snapshot_sha256': identity['sha256'],
                  'run_status': status, 'roles': consent.roles,
                  'new_entries': len(added), 'total_entries': sum(counts.values()),
                  'entries_by_role': counts, 'polls': 1,
                  'metadata_only': True, 'collection_granted_locally': True,
                  'site_outcome_verified': False, 'training_ready': False}
        validator('remote_learning_stream_report').validate(report)
        return report
    finally:
        outbox.close()
        os.close(directory)


def watch_remote_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                 consent_sha256: str, outbox_dir: Path,
                                 watch_seconds: float = 0, interval_seconds: float = 1) -> dict:
    if (not math.isfinite(watch_seconds) or not 0 <= watch_seconds <= MAX_WATCH_SECONDS
            or not math.isfinite(interval_seconds) or not 0.25 <= interval_seconds <= 30):
        raise ValueError('remote_learning_watch_budget_invalid')
    deadline = time.monotonic() + watch_seconds
    polls = 0
    added = 0
    while True:
        report = poll_remote_learning_stream(
            database, profiles=profiles, consents=consents,
            consent_sha256=consent_sha256, outbox_dir=outbox_dir)
        polls += 1
        added += report['new_entries']
        if polls >= MAX_POLLS or time.monotonic() >= deadline or report['run_status'] in TERMINAL:
            watched = {**report, 'mode': 'opt_in_remote_metadata_watch',
                       'polls': polls, 'new_entries': added}
            validator('remote_learning_stream_report').validate(watched)
            return watched
        time.sleep(min(interval_seconds, max(0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Explicit bounded remote model metadata polling')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--consents', type=Path, required=True)
    parser.add_argument('--consent-sha256', required=True)
    parser.add_argument('--outbox-dir', type=Path, required=True)
    parser.add_argument('--watch-seconds', type=float, default=0)
    parser.add_argument('--interval-seconds', type=float, default=1)
    arguments = parser.parse_args(argv)
    try:
        report = watch_remote_learning_stream(
            arguments.database, profiles=arguments.profiles,
            consents=arguments.consents, consent_sha256=arguments.consent_sha256,
            outbox_dir=arguments.outbox_dir, watch_seconds=arguments.watch_seconds,
            interval_seconds=arguments.interval_seconds)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote learning stream unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

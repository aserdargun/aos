"""Explicit, run-bound static-bundle model metadata polling."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .desktop_tasks import Approval
from .learning_event_stream import _source_ref
from .learning_events import MAX_REVIEW_EVENTS, project_learning_events
from .remote_learning_consent import (RemoteLearningConsents, _bound_readonly_data_run_snapshot,
                                      _bound_static_run_snapshot)
from .remote_learning_stream import (MAX_ENTRIES, MAX_POLLS, MAX_WATCH_SECONDS,
                                     TERMINAL, _open_store, _store_for_consent)
from .remote_route_evidence import _decode
from .web_application import WebApplicationProfiles


VERSION = 'remote-static-learning-stream-v1'
ENTRY_SCHEMA = 'remote_static_learning_stream_entry'
REPORT_SCHEMA = 'remote_static_learning_stream_report'
JSON_VERSION = 'remote-json-learning-stream-v1'
JSON_ENTRY_SCHEMA = 'remote_readonly_data_stream_entry'
JSON_REPORT_SCHEMA = 'remote_readonly_data_stream_report'


def _approved_open(snapshot: sqlite3.Connection, run_id: str, binding, draft,
                   plan_sha256: str):
    actions = snapshot.execute('''SELECT * FROM actions WHERE run_id=?
        AND tool='browser.static.open' ORDER BY created_at,action_id''', (run_id,)).fetchall()
    if len(actions) > 1:
        raise ValueError('static_learning_open_count_changed')
    if not actions:
        return None
    action = actions[0]
    arguments = _decode(action['arguments_json'], {'profile_sha256', 'binding_sha256',
                                                    'plan_sha256', 'entry_url'})
    expected = {'profile_sha256': binding['profile_sha256'],
                'binding_sha256': binding['binding_sha256'],
                'plan_sha256': plan_sha256, 'entry_url': draft.task.entry_url}
    if arguments != expected:
        raise ValueError('static_learning_open_arguments_changed')
    if action['status'] != 'ok':
        return None
    if action['actual_option'] != 'open_entry':
        raise ValueError('static_learning_open_option_changed')
    decision = snapshot.execute('''SELECT * FROM decisions WHERE run_id=? AND step_id=?
        AND decision_id=?''', (run_id, action['step_id'], action['decision_id'])).fetchone()
    if (decision is None or decision['selected_option'] != 'open_entry'
            or decision['policy_result'] != 'allow'):
        raise ValueError('static_learning_open_decision_changed')
    approvals = snapshot.execute('''SELECT * FROM desktop_approvals
        WHERE job_id=? AND status='consumed' ''', (binding['job_id'],)).fetchall()
    matches = []
    for row in approvals:
        approval = Approval.model_validate_json(row['envelope_json'])
        if approval.action.action_id == action['action_id']:
            matches.append((row, approval))
    if len(matches) != 1:
        raise ValueError('static_learning_open_approval_missing')
    approval_row, approval = matches[0]
    if (approval.job_id != binding['job_id'] or approval.action.run_id != run_id
            or approval.action.step_id != action['step_id']
            or approval.action.runtime_id != binding['browser_runtime_id']
            or approval.action.tool != 'browser.static.open'
            or approval.action.arguments != expected
            or approval.action.idempotency_key != action['idempotency_key']
            or approval.action.selected_option != decision['selected_option']
            or approval.action_sha256 != approval_row['action_sha256']
            or digest(approval.action.model_dump(mode='json')) != approval_row['action_sha256']):
        raise ValueError('static_learning_open_approval_changed')
    interventions = snapshot.execute('''SELECT * FROM human_interventions
        WHERE run_id=? AND step_id=? AND kind='approve' ''',
        (run_id, action['step_id'])).fetchall()
    matched = [row for row in interventions
               if row['actor'] == 'local_authenticated_user'
               and json.loads(row['payload_json']) == {
                   'approval_id': approval.approval_id,
                   'action_sha256': approval.action_sha256}]
    if len(matched) != 1:
        raise ValueError('static_learning_human_approval_missing')
    return action, decision, approval_row, matched[0]


def _candidates(snapshot: sqlite3.Connection, run_id: str, consent, binding, draft, plan,
                snapshot_sha256: str, consent_sha256: str,
                *, data_bundle: bool = False) -> dict[str, dict]:
    approved = _approved_open(snapshot, run_id, binding, draft, consent.plan_sha256)
    if approved is None:
        return {}
    action, decision, approval, intervention = approved
    candidates = {}
    for event in project_learning_events(snapshot, run_id):
        role = event['role']
        source = event['source']
        if (role not in consent.roles or source['step_id'] != action['step_id']
                or (role == 'system1' and source['decision_id'] != decision['decision_id'])
                or (role == 'system2' and source['escalation_id'] is None)):
            continue
        call = snapshot.execute('''SELECT * FROM model_calls
            WHERE run_id=? AND step_id=? AND call_id=?''',
            (run_id, source['step_id'], source['call_id'])).fetchone()
        if call is None:
            raise ValueError('static_learning_model_call_missing')
        source_rows = {'call': dict(call), 'action': dict(action),
                       'decision': dict(decision), 'approval': dict(approval),
                       'intervention': dict(intervention)}
        if role == 'system2':
            escalation = snapshot.execute('''SELECT * FROM supervisor_escalations
                WHERE run_id=? AND step_id=? AND escalation_id=?''',
                (run_id, source['step_id'], source['escalation_id'])).fetchone()
            if escalation is None:
                raise ValueError('static_learning_escalation_missing')
            source_rows['escalation'] = {key: value for key, value in dict(escalation).items()
                                         if key != 'outcome'}
        version = JSON_VERSION if data_bundle else VERSION
        entry_schema = JSON_ENTRY_SCHEMA if data_bundle else ENTRY_SCHEMA
        entry_id = digest({'version': version, 'run_id': run_id,
                           'consent_sha256': consent_sha256,
                           'source_event_id': event['event_id']})
        entry = {'schema_version': '1.0', 'derivation_version': version,
                 'entry_id': entry_id, 'source_event_id': event['event_id'],
                 'role': role, 'model_kind': event['model_kind'],
                 'run_ref': digest({'run_id': run_id}),
                 'profile_sha256': consent.profile_sha256,
                 'plan_sha256': consent.plan_sha256,
                 'consent_sha256': consent_sha256,
                 'asset_count': len(plan.assets),
                 **({'data_count': len(plan.data_resources)} if data_bundle else {}),
                 'source_sha256': digest(source_rows),
                 'source_snapshot_sha256': snapshot_sha256,
                 'status': 'unreviewed_metadata_candidate',
                 'metadata_only': True, 'transport_readback_verified': False,
                 'site_outcome_verified': False, 'external_rights_verified': False,
                 'redaction_reviewed': False, 'training_ready': False}
        if not validator(entry_schema).is_valid(entry) or entry_id in candidates:
            raise ValueError('static_learning_entry_invalid')
        candidates[entry_id] = entry
    if len(candidates) > MAX_ENTRIES:
        raise ValueError('static_learning_entry_limit')
    return candidates


def poll_remote_static_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                       consent_sha256: str, outbox_dir: Path) -> dict:
    return _poll_bundle_learning_stream(
        database, profiles=profiles, consents=consents,
        consent_sha256=consent_sha256, outbox_dir=outbox_dir, data_bundle=False)


def poll_remote_readonly_data_stream(database: Path, *, profiles: Path, consents: Path,
                                     consent_sha256: str, outbox_dir: Path) -> dict:
    return _poll_bundle_learning_stream(
        database, profiles=profiles, consents=consents,
        consent_sha256=consent_sha256, outbox_dir=outbox_dir, data_bundle=True)


def _poll_bundle_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                 consent_sha256: str, outbox_dir: Path,
                                 data_bundle: bool) -> dict:
    with RemoteLearningConsents(consents).active(consent_sha256) as consent:
        scope = ('remote_json_model_metadata_only' if data_bundle else
                 'remote_static_model_metadata_only')
        if consent.scope != scope:
            raise ValueError('static_learning_scope_mismatch')
        profile_store = WebApplicationProfiles(profiles)
        profile = profile_store.get(consent.profile_sha256)
        if (profile.learning.data_rights_ref != consent.data_rights_ref
                or profile.learning.retention_days != consent.retention_days
                or any(getattr(profile.learning, role) != 'requested' for role in consent.roles)):
            raise ValueError('static_learning_consent_profile_changed')
        database = Path(database)
        source_ref = _source_ref(database)
        with audit_snapshot(database) as (snapshot, identity):
            bound_run = (_bound_readonly_data_run_snapshot if data_bundle else
                         _bound_static_run_snapshot)
            binding, draft, plan = bound_run(
                snapshot, consent.run_id, profile_store,
                consent.profile_sha256, consent.plan_sha256)
            if (binding['binding_sha256'] != consent.binding_sha256
                    or draft.task.task_key != consent.task_key):
                raise ValueError('static_learning_consent_binding_changed')
            run = snapshot.execute('''SELECT task_id,policy_version,deployment_snapshot_json,status
                FROM runs WHERE run_id=?''', (consent.run_id,)).fetchone()
            if (run is None or run['policy_version'] != 'browser-remote-static-assets-policy-v1'
                    or snapshot.execute('SELECT count(*) FROM model_calls WHERE run_id=?',
                                        (consent.run_id,)).fetchone()[0] > MAX_REVIEW_EVENTS):
                raise ValueError('static_learning_run_invalid')
            candidates = _candidates(snapshot, consent.run_id, consent, binding, draft, plan,
                                     identity['sha256'], consent_sha256,
                                     data_bundle=data_bundle)
            run_identity_sha256 = digest({
                'run_id': consent.run_id, 'task_id': run['task_id'],
                'policy_version': run['policy_version'],
                'deployment_snapshot_json': run['deployment_snapshot_json']})
            status = run['status']
            asset_count = len(plan.assets)
            data_count = len(plan.data_resources) if data_bundle else 0
        if (_source_ref(database) != source_ref
                or datetime.fromisoformat(consent.expires_at) <= datetime.now(timezone.utc)):
            raise ValueError('static_learning_source_or_consent_changed')
        outbox, directory = _open_store(_store_for_consent(outbox_dir, consent_sha256))
        try:
            if datetime.fromisoformat(consent.expires_at) <= datetime.now(timezone.utc):
                raise ValueError('static_learning_consent_expired_during_poll')
            outbox.execute('BEGIN IMMEDIATE')
            try:
                binding_row = outbox.execute(
                    'SELECT * FROM source_binding WHERE singleton=1').fetchone()
                expected = (source_ref, identity['schema_sha256'], consent.run_id,
                            consent.profile_sha256, consent.plan_sha256,
                            consent.binding_sha256, consent_sha256, run_identity_sha256)
                if binding_row is None:
                    if outbox.execute('SELECT 1 FROM entries LIMIT 1').fetchone() is not None:
                        raise ValueError('static_learning_outbox_binding_missing')
                    outbox.execute('INSERT INTO source_binding VALUES(1,?,?,?,?,?,?,?,?)', expected)
                elif tuple(binding_row)[1:] != expected:
                    raise ValueError('static_learning_outbox_source_changed')
                existing = outbox.execute('SELECT * FROM entries ORDER BY entry_id').fetchall()
                for row in existing:
                    stored = json.loads(row['entry_json'])
                    current = candidates.get(row['entry_id'])
                    entry_schema = JSON_ENTRY_SCHEMA if data_bundle else ENTRY_SCHEMA
                    if (not validator(entry_schema).is_valid(stored)
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
                        raise ValueError('static_learning_source_regressed_or_changed')
                known = {row['entry_id'] for row in existing}
                added = [candidates[key] for key in sorted(candidates) if key not in known]
                if len(existing) + len(added) > MAX_ENTRIES:
                    raise ValueError('static_learning_entry_limit')
                for entry in added:
                    outbox.execute('INSERT INTO entries VALUES(?,?,?,?,?)',
                                   (entry['entry_id'], entry['role'], entry['source_event_id'],
                                    canonical(entry), digest(entry)))
                outbox.commit()
            except BaseException:
                outbox.rollback()
                raise
            counts = {role: outbox.execute('SELECT count(*) FROM entries WHERE role=?',
                                           (role,)).fetchone()[0]
                      for role in ('system1', 'system2')}
            report = {'schema_version': '1.0',
                      'mode': ('opt_in_remote_json_metadata_poll' if data_bundle else
                               'opt_in_remote_static_metadata_poll'),
                      'run_ref': digest({'run_id': consent.run_id}),
                      'profile_sha256': consent.profile_sha256,
                      'plan_sha256': consent.plan_sha256,
                      'consent_sha256': consent_sha256,
                      'source_snapshot_sha256': identity['sha256'],
                      'run_status': status, 'asset_count': asset_count,
                      **({'data_count': data_count} if data_bundle else {}),
                      'roles': consent.roles, 'new_entries': len(added),
                      'total_entries': sum(counts.values()),
                      'entries_by_role': counts, 'polls': 1,
                      'metadata_only': True, 'collection_granted_locally': True,
                      'site_outcome_verified': False, 'training_ready': False}
            validator(JSON_REPORT_SCHEMA if data_bundle else REPORT_SCHEMA).validate(report)
            return report
        finally:
            outbox.close()
            os.close(directory)


def watch_remote_static_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                        consent_sha256: str, outbox_dir: Path,
                                        watch_seconds: float = 0,
                                        interval_seconds: float = 1) -> dict:
    return _watch_bundle_learning_stream(
        database, profiles=profiles, consents=consents,
        consent_sha256=consent_sha256, outbox_dir=outbox_dir,
        watch_seconds=watch_seconds, interval_seconds=interval_seconds,
        data_bundle=False)


def watch_remote_readonly_data_stream(database: Path, *, profiles: Path, consents: Path,
                                      consent_sha256: str, outbox_dir: Path,
                                      watch_seconds: float = 0,
                                      interval_seconds: float = 1) -> dict:
    return _watch_bundle_learning_stream(
        database, profiles=profiles, consents=consents,
        consent_sha256=consent_sha256, outbox_dir=outbox_dir,
        watch_seconds=watch_seconds, interval_seconds=interval_seconds,
        data_bundle=True)


def _watch_bundle_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                  consent_sha256: str, outbox_dir: Path,
                                  watch_seconds: float, interval_seconds: float,
                                  data_bundle: bool) -> dict:
    if (not math.isfinite(watch_seconds) or not 0 <= watch_seconds <= MAX_WATCH_SECONDS
            or not math.isfinite(interval_seconds) or not 0.25 <= interval_seconds <= 30):
        raise ValueError('static_learning_watch_budget_invalid')
    deadline = time.monotonic() + watch_seconds
    polls = 0
    added = 0
    while True:
        poll = (poll_remote_readonly_data_stream if data_bundle else
                poll_remote_static_learning_stream)
        report = poll(
            database, profiles=profiles, consents=consents,
            consent_sha256=consent_sha256, outbox_dir=outbox_dir)
        polls += 1
        added += report['new_entries']
        if polls >= MAX_POLLS or time.monotonic() >= deadline or report['run_status'] in TERMINAL:
            watched = {**report,
                       'mode': ('opt_in_remote_json_metadata_watch' if data_bundle else
                                'opt_in_remote_static_metadata_watch'),
                       'polls': polls, 'new_entries': added}
            validator(JSON_REPORT_SCHEMA if data_bundle else REPORT_SCHEMA).validate(watched)
            return watched
        time.sleep(min(interval_seconds, max(0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Explicit bounded static-bundle metadata polling',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--consents', type=Path, required=True)
    parser.add_argument('--consent-sha256', required=True)
    parser.add_argument('--outbox-dir', type=Path, required=True)
    parser.add_argument('--watch-seconds', type=float, default=0)
    parser.add_argument('--interval-seconds', type=float, default=1)
    arguments = parser.parse_args(argv)
    try:
        report = watch_remote_static_learning_stream(
            arguments.database, profiles=arguments.profiles,
            consents=arguments.consents, consent_sha256=arguments.consent_sha256,
            outbox_dir=arguments.outbox_dir, watch_seconds=arguments.watch_seconds,
            interval_seconds=arguments.interval_seconds)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote static learning stream unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

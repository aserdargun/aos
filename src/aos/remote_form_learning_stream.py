"""Explicit, consent-bound metadata polling for approved HTTPS form stages."""

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
from .remote_form_learning_source import STAGES, STATE_STAGES
from .remote_learning_consent import RemoteLearningConsents, _bound_form_run_snapshot
from .remote_learning_stream import (MAX_ENTRIES, MAX_POLLS, MAX_WATCH_SECONDS,
                                     TERMINAL, _open_store, _store_for_consent)
from .web_application import WebApplicationProfiles
from .web_https_form_transport import form_stage_arguments
from .web_https_form_state_probe import WebHTTPSFormStatePlan, form_state_action_arguments


VERSION = 'remote-form-learning-stream-v1'
STATE_VERSION = 'remote-form-state-learning-stream-v1'
ENTRY_SCHEMA = 'remote_form_learning_stream_entry'
REPORT_SCHEMA = 'remote_form_learning_stream_report'
STATE_ENTRY_SCHEMA = 'remote_form_state_learning_stream_entry'
STATE_REPORT_SCHEMA = 'remote_form_state_learning_stream_report'


def _canonical_object(value: str) -> dict:
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or canonical(decoded) != value:
        raise ValueError('form_learning_noncanonical_source')
    return decoded


def _approved_stages(snapshot: sqlite3.Connection, run_id: str, binding, plan,
                     state_plan: WebHTTPSFormStatePlan | None = None) -> dict:
    actions = snapshot.execute('''SELECT * FROM actions WHERE run_id=?
        ORDER BY created_at,action_id''', (run_id,)).fetchall()
    stages = STATE_STAGES if state_plan is not None else STAGES
    allowed = {tool for tool, _choice in stages} | {'browser.form.observe'}
    if (len(actions) > len(stages) + 1 or any(action['tool'] not in allowed for action in actions)
            or len({action['tool'] for action in actions}) != len(actions)):
        raise ValueError('form_learning_action_scope_changed')
    approved = {}
    for stage, (tool, choice) in enumerate(stages):
        matching = [action for action in actions if action['tool'] == tool]
        if not matching:
            continue
        action = matching[0]
        arguments = _canonical_object(action['arguments_json'])
        if 'field_name' in arguments:
            field_names = arguments['field_name']
        elif 'field_names' in arguments and isinstance(arguments['field_names'], str):
            field_names = tuple(arguments['field_names'].split(','))
        else:
            field_names = 'placeholder'
        form_stage = {'browser.form.open': 0, 'browser.form.fill': 1,
                      'browser.form.submit': 2, 'browser.form.receipt': 3}
        expected = (form_state_action_arguments(
            state_plan, binding['binding_sha256'],
            'before' if tool.endswith('before') else 'after')
            if tool.startswith('browser.form.state_') else
            form_stage_arguments(plan, binding['binding_sha256'], field_names,
                                 form_stage[tool]))
        if arguments != expected:
            raise ValueError('form_learning_action_changed')
        if action['status'] != 'ok':
            continue
        if action['actual_option'] != choice:
            raise ValueError('form_learning_option_changed')
        decision = snapshot.execute('''SELECT * FROM decisions WHERE run_id=?
            AND step_id=? AND decision_id=?''',
            (run_id, action['step_id'], action['decision_id'])).fetchone()
        if (decision is None or decision['selected_option'] != choice
                or decision['policy_result'] != 'allow'):
            raise ValueError('form_learning_decision_changed')
        rows = snapshot.execute('''SELECT * FROM desktop_approvals
            WHERE job_id=? AND status='consumed' ''', (binding['job_id'],)).fetchall()
        matches = [(row, Approval.model_validate_json(row['envelope_json']))
                   for row in rows if Approval.model_validate_json(
                       row['envelope_json']).action.action_id == action['action_id']]
        if len(matches) != 1:
            raise ValueError('form_learning_approval_missing')
        approval_row, approval = matches[0]
        if (approval.model_dump_json() != approval_row['envelope_json']
                or approval_row['approval_id'] != approval.approval_id
                or approval.job_id != binding['job_id']
                or approval.action.run_id != run_id
                or approval.action.step_id != action['step_id']
                or approval.action.runtime_id != binding['browser_runtime_id']
                or approval.action.tool != tool
                or approval.action.arguments != arguments
                or approval.action.idempotency_key != action['idempotency_key']
                or approval.action.selected_option != choice
                or approval.action_sha256 != approval_row['action_sha256']
                or digest(approval.action.model_dump(mode='json')) != approval_row['action_sha256']):
            raise ValueError('form_learning_approval_changed')
        interventions = snapshot.execute('''SELECT * FROM human_interventions
            WHERE run_id=? AND step_id=? AND kind='approve' ''',
            (run_id, action['step_id'])).fetchall()
        matches = [row for row in interventions
                   if row['actor'] == 'local_authenticated_user'
                   and _canonical_object(row['payload_json']) == {
                       'approval_id': approval.approval_id,
                       'action_sha256': approval.action_sha256}]
        if len(matches) != 1:
            raise ValueError('form_learning_human_approval_missing')
        approved[(action['step_id'], decision['decision_id'])] = (
            stage, action, decision, approval_row, matches[0])
    if any(stage not in {item[0] for item in approved.values()}
           for stage in range(len(approved))):
        raise ValueError('form_learning_noncontiguous_stages')
    if state_plan is not None:
        ordered = sorted(approved.values(), key=lambda item: item[0])
        versions = [Approval.model_validate_json(item[3]['envelope_json']).action.state_version
                    for item in ordered]
        if versions != sorted(set(versions)):
            raise ValueError('form_learning_state_order_changed')
    return approved


def _candidates(snapshot: sqlite3.Connection, run_id: str, consent, binding, plan,
                snapshot_sha256: str, consent_sha256: str,
                state_plan: WebHTTPSFormStatePlan | None = None) -> tuple[dict, int]:
    approved = _approved_stages(snapshot, run_id, binding, plan, state_plan)
    state_mode = state_plan is not None
    version = STATE_VERSION if state_mode else VERSION
    entry_schema = STATE_ENTRY_SCHEMA if state_mode else ENTRY_SCHEMA
    candidates = {}
    for event in project_learning_events(snapshot, run_id):
        role = event['role']
        source = event['source']
        selected = (approved.get((source['step_id'], source['decision_id']))
                    if role == 'system1' else next(
                        (stage for (step_id, _decision_id), stage in approved.items()
                         if step_id == source['step_id']), None))
        if (role not in consent.roles or selected is None
                or role == 'system2' and source['escalation_id'] is None):
            continue
        stage, action, decision, approval, intervention = selected
        call = snapshot.execute('''SELECT * FROM model_calls
            WHERE run_id=? AND step_id=? AND call_id=?''',
            (run_id, source['step_id'], source['call_id'])).fetchone()
        if call is None:
            raise ValueError('form_learning_model_call_missing')
        source_rows = {'call': dict(call), 'action': dict(action),
                       'decision': dict(decision), 'approval': dict(approval),
                       'intervention': dict(intervention)}
        if role == 'system2':
            escalation = snapshot.execute('''SELECT * FROM supervisor_escalations
                WHERE run_id=? AND step_id=? AND escalation_id=?''',
                (run_id, source['step_id'], source['escalation_id'])).fetchone()
            if escalation is None:
                raise ValueError('form_learning_escalation_missing')
            source_rows['escalation'] = {key: value for key, value in dict(escalation).items()
                                         if key != 'outcome'}
        entry_id = digest({'version': version, 'run_id': run_id,
                           'consent_sha256': consent_sha256,
                           'source_event_id': event['event_id']})
        entry = {'schema_version': '1.0', 'derivation_version': version,
                 'entry_id': entry_id, 'source_event_id': event['event_id'],
                 'role': role, 'model_kind': event['model_kind'],
                 'run_ref': digest({'run_id': run_id}),
                 'profile_sha256': consent.profile_sha256,
                 'plan_sha256': consent.plan_sha256,
                 'consent_sha256': consent_sha256, 'form_stage': stage,
                 'source_sha256': digest(source_rows),
                 'source_snapshot_sha256': snapshot_sha256,
                 'status': 'unreviewed_metadata_candidate',
                 'metadata_only': True, 'transport_readback_verified': False,
                 'site_outcome_verified': False, 'external_rights_verified': False,
                 'redaction_reviewed': False, 'training_ready': False}
        if state_mode:
            entry['state_plan_sha256'] = consent.state_plan_sha256
        if not validator(entry_schema).is_valid(entry) or entry_id in candidates:
            raise ValueError('form_learning_entry_invalid')
        candidates[entry_id] = entry
    if len(candidates) > MAX_ENTRIES:
        raise ValueError('form_learning_entry_limit')
    return candidates, len(approved)


def poll_remote_form_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                     consent_sha256: str, outbox_dir: Path) -> dict:
    with RemoteLearningConsents(consents).active(consent_sha256) as consent:
        if consent.scope not in {'remote_form_model_metadata_only',
                                 'remote_form_state_model_metadata_only'}:
            raise ValueError('form_learning_scope_mismatch')
        state_mode = consent.scope == 'remote_form_state_model_metadata_only'
        entry_schema = STATE_ENTRY_SCHEMA if state_mode else ENTRY_SCHEMA
        report_schema = STATE_REPORT_SCHEMA if state_mode else REPORT_SCHEMA
        profile_store = WebApplicationProfiles(profiles)
        profile = profile_store.get(consent.profile_sha256)
        if (profile.learning.data_rights_ref != consent.data_rights_ref
                or profile.learning.retention_days != consent.retention_days
                or any(getattr(profile.learning, role) != 'requested' for role in consent.roles)):
            raise ValueError('form_learning_consent_profile_changed')
        database = Path(database)
        source_ref = _source_ref(database)
        with audit_snapshot(database) as (snapshot, identity):
            binding, draft, plan = _bound_form_run_snapshot(
                snapshot, consent.run_id, profile_store,
                consent.profile_sha256, consent.plan_sha256,
                state_plan_sha256=consent.state_plan_sha256)
            state_plan = None
            if state_mode:
                state_binding = snapshot.execute('''SELECT state_plan_json
                    FROM desktop_remote_form_state_bindings WHERE job_id=? AND run_id=?''',
                    (binding['job_id'], consent.run_id)).fetchone()
                if state_binding is None:
                    raise ValueError('form_learning_state_binding_missing')
                state_plan = WebHTTPSFormStatePlan.model_validate_json(
                    state_binding['state_plan_json'])
            if (binding['binding_sha256'] != consent.binding_sha256
                    or draft.task.task_key != consent.task_key):
                raise ValueError('form_learning_consent_binding_changed')
            run = snapshot.execute('''SELECT task_id,policy_version,deployment_snapshot_json,status
                FROM runs WHERE run_id=?''', (consent.run_id,)).fetchone()
            if (run is None or run['policy_version'] != 'browser-remote-form-policy-v1'
                    or snapshot.execute('SELECT count(*) FROM model_calls WHERE run_id=?',
                                        (consent.run_id,)).fetchone()[0] > MAX_REVIEW_EVENTS):
                raise ValueError('form_learning_run_invalid')
            candidates, approved_count = _candidates(
                snapshot, consent.run_id, consent, binding, plan,
                identity['sha256'], consent_sha256, state_plan)
            run_identity_sha256 = digest({
                'run_id': consent.run_id, 'task_id': run['task_id'],
                'policy_version': run['policy_version'],
                'deployment_snapshot_json': run['deployment_snapshot_json']})
            status = run['status']
        if (_source_ref(database) != source_ref
                or datetime.fromisoformat(consent.expires_at) <= datetime.now(timezone.utc)):
            raise ValueError('form_learning_source_or_consent_changed')
        outbox, directory = _open_store(_store_for_consent(outbox_dir, consent_sha256))
        try:
            if datetime.fromisoformat(consent.expires_at) <= datetime.now(timezone.utc):
                raise ValueError('form_learning_consent_expired_during_poll')
            outbox.execute('BEGIN IMMEDIATE')
            try:
                binding_row = outbox.execute(
                    'SELECT * FROM source_binding WHERE singleton=1').fetchone()
                expected = (source_ref, identity['schema_sha256'], consent.run_id,
                            consent.profile_sha256, consent.plan_sha256,
                            consent.binding_sha256, consent_sha256, run_identity_sha256)
                if binding_row is None:
                    if outbox.execute('SELECT 1 FROM entries LIMIT 1').fetchone() is not None:
                        raise ValueError('form_learning_outbox_binding_missing')
                    outbox.execute('INSERT INTO source_binding VALUES(1,?,?,?,?,?,?,?,?)', expected)
                elif tuple(binding_row)[1:] != expected:
                    raise ValueError('form_learning_outbox_source_changed')
                existing = outbox.execute('SELECT * FROM entries ORDER BY entry_id').fetchall()
                for row in existing:
                    stored = json.loads(row['entry_json'])
                    current = candidates.get(row['entry_id'])
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
                        raise ValueError('form_learning_source_regressed_or_changed')
                known = {row['entry_id'] for row in existing}
                added = [candidates[key] for key in sorted(candidates) if key not in known]
                if len(existing) + len(added) > MAX_ENTRIES:
                    raise ValueError('form_learning_entry_limit')
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
            report = {'schema_version': '1.0', 'mode': 'opt_in_remote_form_metadata_poll',
                      'run_ref': digest({'run_id': consent.run_id}),
                      'profile_sha256': consent.profile_sha256,
                      'plan_sha256': consent.plan_sha256,
                      'consent_sha256': consent_sha256,
                      'source_snapshot_sha256': identity['sha256'],
                      'run_status': status, 'approved_stage_count': approved_count,
                      'roles': consent.roles, 'new_entries': len(added),
                      'total_entries': sum(counts.values()), 'entries_by_role': counts,
                      'polls': 1, 'metadata_only': True,
                      'collection_granted_locally': True,
                      'site_outcome_verified': False, 'training_ready': False}
            if state_mode:
                report['mode'] = 'opt_in_remote_form_state_metadata_poll'
                report['state_plan_sha256'] = consent.state_plan_sha256
            validator(report_schema).validate(report)
            return report
        finally:
            outbox.close()
            os.close(directory)


def watch_remote_form_learning_stream(database: Path, *, profiles: Path, consents: Path,
                                      consent_sha256: str, outbox_dir: Path,
                                      watch_seconds: float = 0,
                                      interval_seconds: float = 1) -> dict:
    if (not math.isfinite(watch_seconds) or not 0 <= watch_seconds <= MAX_WATCH_SECONDS
            or not math.isfinite(interval_seconds) or not 0.25 <= interval_seconds <= 30):
        raise ValueError('form_learning_watch_budget_invalid')
    deadline = time.monotonic() + watch_seconds
    polls = 0
    added = 0
    while True:
        report = poll_remote_form_learning_stream(
            database, profiles=profiles, consents=consents,
            consent_sha256=consent_sha256, outbox_dir=outbox_dir)
        polls += 1
        added += report['new_entries']
        if polls >= MAX_POLLS or time.monotonic() >= deadline or report['run_status'] in TERMINAL:
            state_mode = 'state_plan_sha256' in report
            watched = {**report, 'mode': ('opt_in_remote_form_state_metadata_watch'
                                           if state_mode else 'opt_in_remote_form_metadata_watch'),
                       'polls': polls, 'new_entries': added}
            validator(STATE_REPORT_SCHEMA if state_mode else REPORT_SCHEMA).validate(watched)
            return watched
        time.sleep(min(interval_seconds, max(0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Explicit bounded HTTPS form metadata polling',
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
        report = watch_remote_form_learning_stream(
            arguments.database, profiles=arguments.profiles,
            consents=arguments.consents, consent_sha256=arguments.consent_sha256,
            outbox_dir=arguments.outbox_dir, watch_seconds=arguments.watch_seconds,
            interval_seconds=arguments.interval_seconds)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote form learning stream unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

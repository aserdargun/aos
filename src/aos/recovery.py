import argparse
from collections import defaultdict
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, canonical, digest
from .dataset_audit import audit_snapshot


UNSETTLED_JOBS = {'queued', 'running', 'waiting_approval', 'paused', 'waiting_human'}
UNSETTLED_RUNS = {'created', 'running', 'paused', 'waiting_human'}
MAX_RECORDS = 10000
RunStatus = Literal['created', 'running', 'paused', 'waiting_human', 'succeeded', 'failed', 'cancelled']
JobStatus = Literal['queued', 'running', 'waiting_approval', 'paused', 'waiting_human', 'succeeded', 'failed', 'cancelled']
Reason = Literal['job_unsettled', 'run_unsettled', 'action_effect_unresolved', 'approval_not_terminal',
                 'job_run_status_mismatch', 'job_run_missing']


class PendingCounts(TypedModel):
    action_intent: int = Field(default=0, ge=0)
    action_running: int = Field(default=0, ge=0)
    action_uncertain: int = Field(default=0, ge=0)
    approval_pending: int = Field(default=0, ge=0)
    approval_approved: int = Field(default=0, ge=0)


class RecoveryJob(TypedModel):
    job_ref: str = Field(pattern='^[a-f0-9]{64}$')
    session_ref: str = Field(pattern='^[a-f0-9]{64}$')
    run_ref: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    kind: Literal['hello', 'browser_form', 'vision_canvas', 'browser_local_navigation', 'browser_staging_workflow', 'browser_remote_entry', 'browser_remote_routes', 'browser_remote_form']
    status: JobStatus
    run_status: RunStatus | None
    pending: PendingCounts
    reasons: list[Reason]


class RecoveryTotals(PendingCounts):
    sessions: int = Field(ge=0)
    sessions_not_stopped: int = Field(ge=0)
    jobs: int = Field(ge=0)
    jobs_requiring_inspection: int = Field(ge=0)
    runs: int = Field(ge=0)
    unsettled_runs: int = Field(ge=0)
    unsettled_runs_without_jobs: int = Field(ge=0)
    input_queued: int = Field(ge=0)
    input_running: int = Field(ge=0)
    input_uncertain: int = Field(ge=0)


class RecoveryReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_recovery_inventory'] = 'read_only_recovery_inventory'
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    captured_at: str
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    live_state_verified: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False
    requires_inspection: bool
    totals: RecoveryTotals
    job_limit: int = Field(ge=1, le=100)
    jobs_truncated: bool
    jobs: list[RecoveryJob] = Field(max_length=100)


def inspect_recovery(connection: sqlite3.Connection, identity: dict, limit: int = 30) -> RecoveryReport:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_recovery_limit')
    for table in ('desktop_sessions', 'desktop_tasks', 'runs'):
        if connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] > MAX_RECORDS:
            raise ValueError('recovery_record_limit')
    actions = defaultdict(lambda: defaultdict(int))
    approvals = defaultdict(lambda: defaultdict(int))
    for row in connection.execute("SELECT run_id,status,count(*) FROM actions WHERE status IN ('intent','running','uncertain') GROUP BY run_id,status"):
        actions[row[0]]['action_' + row[1]] = row[2]
    for row in connection.execute("SELECT job_id,status,count(*) FROM desktop_approvals WHERE status IN ('pending','approved') GROUP BY job_id,status"):
        approvals[row[0]]['approval_' + row[1]] = row[2]
    jobs = []
    for row in connection.execute('''SELECT job.job_id,job.session_id,job.run_id,job.kind,job.status,run.status AS run_status
            FROM desktop_tasks job LEFT JOIN runs run ON run.run_id=job.run_id ORDER BY job.created_at DESC,job.job_id'''):
        pending = PendingCounts(**actions[row['run_id']], **approvals[row['job_id']])
        conditions = {
            'job_unsettled': row['status'] in UNSETTLED_JOBS,
            'run_unsettled': row['run_status'] in UNSETTLED_RUNS,
            'action_effect_unresolved': bool(pending.action_intent + pending.action_running + pending.action_uncertain),
            'approval_not_terminal': bool(pending.approval_pending + pending.approval_approved),
            'job_run_status_mismatch': row['run_status'] is not None and (
                row['status'] in {'succeeded', 'failed'} and row['status'] != row['run_status']
                or row['run_status'] == 'succeeded' and row['status'] != 'succeeded'),
            'job_run_missing': row['run_id'] is None and row['status'] not in {'queued', 'cancelled', 'failed'},
        }
        jobs.append(RecoveryJob(job_ref=digest({'job_id': row['job_id']}), session_ref=digest({'session_id': row['session_id']}),
                                run_ref=digest({'run_id': row['run_id']}) if row['run_id'] is not None else None,
                                kind=row['kind'], status=row['status'], run_status=row['run_status'], pending=pending,
                                reasons=sorted(reason for reason, present in conditions.items() if present)))
    jobs.sort(key=lambda job: not bool(job.reasons))
    runs = dict(connection.execute('SELECT status,count(*) FROM runs GROUP BY status'))
    sessions = dict(connection.execute('SELECT status,count(*) FROM desktop_sessions GROUP BY status'))
    inputs = dict(connection.execute('SELECT status,count(*) FROM desktop_inputs GROUP BY status'))
    pending_totals = {key: sum(counts.get(key, 0) for counts in collection.values())
                      for collection, keys in ((actions, ('action_intent', 'action_running', 'action_uncertain')),
                                               (approvals, ('approval_pending', 'approval_approved')))
                      for key in keys}
    orphaned = connection.execute("""SELECT count(*) FROM runs WHERE status IN ('created','running','paused','waiting_human')
        AND NOT EXISTS (SELECT 1 FROM desktop_tasks WHERE desktop_tasks.run_id=runs.run_id)""").fetchone()[0]
    totals = RecoveryTotals(**pending_totals, sessions=sum(sessions.values()), sessions_not_stopped=sum(
        count for status, count in sessions.items() if status != 'stopped'), jobs=len(jobs),
        jobs_requiring_inspection=sum(bool(job.reasons) for job in jobs), runs=sum(runs.values()),
        unsettled_runs=sum(count for status, count in runs.items() if status in UNSETTLED_RUNS), unsettled_runs_without_jobs=orphaned,
        input_queued=inputs.get('queued', 0), input_running=inputs.get('running', 0), input_uncertain=inputs.get('uncertain', 0))
    attention = bool(totals.jobs_requiring_inspection or totals.unsettled_runs or sum(pending_totals.values())
                     or totals.input_queued or totals.input_running or totals.input_uncertain)
    return RecoveryReport(snapshot_sha256=identity['sha256'], captured_at=identity['captured_at'], totals=totals,
                          requires_inspection=attention, job_limit=limit, jobs_truncated=len(jobs) > limit, jobs=jobs[:limit])


def recovery_inventory(database: Path, limit: int = 30) -> RecoveryReport:
    with audit_snapshot(database) as (connection, identity):
        return inspect_recovery(connection, identity, limit)


def main():
    parser = argparse.ArgumentParser(description='Salt okunur kurtarma envanteri; devam/tekrar veya yetki üretmez')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=30)
    arguments = parser.parse_args()
    try:
        print(canonical(recovery_inventory(arguments.database, arguments.limit).model_dump()))
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(1, 'Kurtarma envanteri okunamadı; desteklenen mevcut DB ve bütünlüğünü kontrol edin.\n')


if __name__ == '__main__':
    main()

import argparse
import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Literal

from pydantic import Field

from .computer import SafetyPolicy, WorkspaceRuntime
from .contracts import AOSFault, Action, ErrorCode, HELLO_CONTENT, HELLO_PATH, Phase, Settings, TypedModel, canonical, digest, identifier, now
from .decision import DeciderEngine, DecisionEngine, FixtureDecisionEngine
from .operator import Operator
from .recovery_effect import inspect_write_effect
from .recovery_reconcile import WriteReconciliation
from .recovery_resume import ResumeApproval, terminal_approval
from .storage import TrajectoryStore


class VerificationAdmission(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['reconciled_hello_verification'] = 'reconciled_hello_verification'
    admission_id: str = Field(pattern='^resume-[a-f0-9]{32}$')
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    write_action_ref: str = Field(pattern='^[a-f0-9]{64}$')
    reconciliation_ref: str = Field(pattern='^[a-f0-9]{64}$')
    reconciliation_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    write_receipt_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    source_snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    source_state_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    source_state_version: int = Field(ge=0)
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    fresh_lease_ref: str = Field(pattern='^[a-f0-9]{64}$')
    allowed_tool: Literal['filesystem.read'] = 'filesystem.read'
    maximum_read_actions: Literal[2] = 2
    requires_action_approval: Literal[True] = True
    write_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False


def verification_source(store: TrajectoryStore, runtime: WorkspaceRuntime, run_id: str, action_id: str,
                        reconciliation_id: str, deployment_sha256: str):
    connection = store.connection
    database = Path(connection.execute('PRAGMA database_list').fetchone()[2])
    report = inspect_write_effect(database, runtime.root, run_id, action_id, deployment_sha256,
                                  workspace_descriptor=runtime.descriptor)
    rows = connection.execute("SELECT * FROM human_interventions WHERE run_id=? AND kind='correction' LIMIT 2", (run_id,)).fetchall()
    if len(rows) != 1 or rows[0]['intervention_id'] != reconciliation_id:
        raise ValueError('verification_reconciliation_missing')
    correction = WriteReconciliation.model_validate_json(rows[0]['payload_json'])
    action = connection.execute('SELECT * FROM actions WHERE action_id=? AND run_id=?', (action_id, run_id)).fetchone()
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    state = store.state(run_id)
    if (report.result != 'recorded_effect_matches' or report.action_status != 'ok'
            or correction.reconciliation_id != reconciliation_id or correction.actor != rows[0]['actor']
            or correction.recorded_at != rows[0]['created_at'] or rows[0]['step_id'] != state.step_id
            or correction.request.run_ref != report.checkpoint.run_ref or correction.request.action_ref != report.action_ref
            or correction.request.receipt_sha256 != report.receipt_sha256
            or correction.request.workspace_sha256 != report.checkpoint.workspace_sha256
            or correction.request.deployment_sha256 != deployment_sha256
            or action['completed_at'] != correction.recorded_at or action['error_code'] is not None
            or json.loads(action['result_json']) != {'bytes_written': 28} or action['step_id'] != state.step_id
            or run['training_eligible'] != 0 or run['outcome'] != 'unknown'
            or state.supervisor_deployment_id is not None
            or json.loads(run['environment_json']).get('recovery_probe') is not False
            or any(connection.execute(f'SELECT 1 FROM {table} WHERE run_id=? LIMIT 1', (run_id,)).fetchone()
                   for table in ('desktop_tasks', 'verifications', 'supervisor_escalations', 'trajectory_labels'))):
        raise ValueError('verification_source_invalid')
    return report, correction


async def verify_reconciled_hello(settings: Settings, store: TrajectoryStore, runtime: WorkspaceRuntime,
                                  engine: DecisionEngine, run_id: str, action_id: str, reconciliation_id: str,
                                  approve: Callable[[Action], Awaitable[bool]], *,
                                  actor: Literal['local_terminal', 'acceptance_test'] = 'local_terminal') -> dict:
    database = Path(store.connection.execute('PRAGMA database_list').fetchone()[2])
    if (store.lock is None or store.connection.in_transaction or type(runtime) is not WorkspaceRuntime
            or runtime.descriptor is None or runtime.root != settings.workspace.absolute()
            or database != settings.database.absolute() or not callable(approve)
            or actor not in {'local_terminal', 'acceptance_test'}):
        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Verification requires owned existing workspace, writer and approval gate')
    deployment_sha256 = digest(engine.identity)
    with store.connection:
        store.connection.execute('BEGIN IMMEDIATE')
        report, correction = verification_source(store, runtime, run_id, action_id, reconciliation_id, deployment_sha256)
        state = store.state(run_id)
        admissions = store.connection.execute("""SELECT 1 FROM human_interventions WHERE run_id=? AND kind='resume'
            AND json_extract(payload_json,'$.mode')='reconciled_hello_verification' LIMIT 1""", (run_id,)).fetchone()
        if (state.phase != Phase.PAUSED or state.owner != 'PAUSED'
                or store.connection.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] != 'paused'
                or correction.request.state_sha256 != report.checkpoint.state_sha256 or admissions
                or store.connection.execute('SELECT count(*) FROM actions WHERE run_id=?', (run_id,)).fetchone()[0] != 1):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only an unused reconciled single-write checkpoint can be verified')
        fresh_lease = identifier('lease')
        admission = VerificationAdmission(admission_id=identifier('resume'), run_ref=report.checkpoint.run_ref,
                                          write_action_ref=report.action_ref,
                                          reconciliation_ref=digest({'reconciliation_id': reconciliation_id}),
                                          reconciliation_sha256=digest(correction.model_dump()),
                                          write_receipt_sha256=report.receipt_sha256,
                                          source_snapshot_sha256=report.checkpoint.snapshot_sha256,
                                          source_state_sha256=report.checkpoint.state_sha256,
                                          source_state_version=state.state_version,
                                          workspace_sha256=report.checkpoint.workspace_sha256,
                                          deployment_sha256=deployment_sha256, fresh_lease_ref=digest({'lease_id': fresh_lease}))
        store.insert('human_interventions', intervention_id=admission.admission_id, run_id=run_id,
                     step_id=state.step_id, actor=actor, kind='resume', payload_json=canonical(admission.model_dump()), created_at=now())
    reads: list[Action] = []
    approvals: dict[str, ResumeApproval] = {}

    def guard() -> None:
        try:
            if (runtime.descriptor is None or runtime.root != settings.workspace.absolute()
                    or digest(engine.identity) != deployment_sha256):
                raise ValueError('verification_runtime_changed')
            refreshed, recorded = verification_source(store, runtime, run_id, action_id, reconciliation_id, deployment_sha256)
            current = store.state(run_id)
            saved = store.connection.execute("SELECT payload_json,actor,kind FROM human_interventions WHERE intervention_id=? AND run_id=?",
                                             (admission.admission_id, run_id)).fetchone()
            if (recorded != correction or refreshed.receipt_sha256 != admission.write_receipt_sha256
                    or refreshed.checkpoint.workspace_sha256 != admission.workspace_sha256
                    or current.owner != 'AGENT' or current.owner_lease_id != fresh_lease or current.phase != Phase.EXECUTE
                    or store.connection.execute('SELECT status FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] != 'running'
                    or saved is None or tuple(saved) != (canonical(admission.model_dump()), actor, 'resume')):
                raise ValueError('verification_binding_changed')
            for approval_id, approval in approvals.items():
                saved_approval = store.connection.execute('''SELECT payload_json,actor,kind FROM human_interventions
                    WHERE intervention_id=? AND run_id=? AND step_id=?''', (approval_id, run_id, state.step_id)).fetchone()
                if saved_approval is None or tuple(saved_approval) != (canonical(approval.model_dump()), actor, 'approve'):
                    raise ValueError('verification_approval_history_changed')
            recorded_reads = store.connection.execute('SELECT * FROM actions WHERE run_id=? AND action_id!=?', (run_id, action_id)).fetchall()
            if len(recorded_reads) != len(reads):
                raise ValueError('verification_action_history_changed')
            expected = {read.action_id: read for read in reads}
            for row in recorded_reads:
                read = expected.get(row['action_id'])
                envelope = store.connection.execute('SELECT envelope_json,payload_sha256 FROM action_envelopes WHERE action_id=?', (row['action_id'],)).fetchone()
                if (read is None or row['status'] != 'ok' or row['actual_option'] != 'read_file'
                        or row['tool'] != 'filesystem.read' or row['step_id'] != state.step_id
                        or json.loads(row['arguments_json']) != {'path': HELLO_PATH}
                        or json.loads(row['result_json']) != {'content': HELLO_CONTENT}
                        or envelope is None or Action.model_validate_json(envelope[0]) != read
                        or envelope[1] != digest(read.model_dump(mode='json'))):
                    raise ValueError('verification_read_history_invalid')
        except (ValueError, OSError, TypeError):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Verification source, read history or ownership changed') from None

    async def gate(action: Action) -> None:
        guard()
        if (len(reads) >= 2 or action.tool != 'filesystem.read' or action.arguments != {'path': HELLO_PATH}
                or any(read.action_id == action.action_id for read in reads)
                or action.selected_option != 'read_file' or action.run_id != run_id
                or action.owner_lease_id != fresh_lease):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Verification permits only two fresh approved reads')
        offered = Action.model_validate_json(action.model_dump_json())
        action_sha256 = digest(action.model_dump(mode='json'))
        try:
            approved = await asyncio.wait_for(approve(offered), min(60, max(0, action.deadline - time.time())))
        except TimeoutError:
            approved = False
        decision = 'expired' if time.time() >= action.deadline else 'approved' if approved is True else 'rejected'
        if digest(offered.model_dump(mode='json')) != action_sha256:
            decision = 'rejected'
        approval = ResumeApproval(admission_id=admission.admission_id, action_sha256=action_sha256,
                                  fresh_lease_ref=admission.fresh_lease_ref, state_version=action.state_version,
                                  expires_at=action.deadline, decision=decision)
        approval_id = identifier('intervention')
        with store.connection:
            store.insert('human_interventions', intervention_id=approval_id, run_id=run_id,
                         step_id=state.step_id, actor=actor, kind='approve' if decision == 'approved' else 'reject',
                         payload_json=canonical(approval.model_dump()), created_at=now())
        if decision != 'approved':
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'A fresh exact-read approval is required')
        approvals[approval_id] = approval
        guard()
        SafetyPolicy.check(action, store.state(run_id), runtime.runtime_id)
        reads.append(action)

    operator = Operator(settings, store, runtime, engine)
    try:
        result = await operator.hello(owner_lease_id=fresh_lease, resume_state=state, execution_gate=gate,
                                      verification_only=True, verification_guard=guard)
        return {**result, 'verification_admission_id': admission.admission_id, 'write_replayed': False,
                'automatic_replay_allowed': False}
    finally:
        current = store.state(run_id)
        if current.owner_lease_id == fresh_lease:
            if current.phase not in {Phase.SUCCEEDED, Phase.FAILED, Phase.CANCELLED, Phase.WAITING_HUMAN}:
                operator.interrupt(current)
            else:
                revoked = current.model_copy(update={'owner': 'PAUSED', 'owner_lease_id': identifier('revoked'),
                                                     'state_version': current.state_version + 1})
                store.save_state(current, revoked)


def main():
    parser = argparse.ArgumentParser(description='Uzlaştırılmış hello yazmasını iki taze onaylı okuma ile doğrula; yazmayı tekrarlama')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--action-id', required=True)
    parser.add_argument('--reconciliation-id', required=True)
    parser.add_argument('--engine', choices=['fixture', 'decider'], default='decider')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--model-python', type=Path)
    arguments = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(1, 'Son doğrulama için yerel terminal ve iki taze okuma onayı gerekli.\n')
    if arguments.engine == 'decider' and (arguments.manifest is None or arguments.model_python is None):
        parser.error('Gerçek motor için --manifest ve --model-python gerekli.')
    settings = Settings(database=arguments.database, workspace=arguments.workspace)
    runtime = WorkspaceRuntime(settings.workspace)
    store = None
    try:
        engine = FixtureDecisionEngine() if arguments.engine == 'fixture' else DeciderEngine(arguments.manifest, arguments.model_python)
        inspect_write_effect(settings.database, settings.workspace, arguments.run_id, arguments.action_id, digest(engine.identity))
        runtime.start_existing()
        store = TrajectoryStore(settings.database)
        result = asyncio.run(verify_reconciled_hello(settings, store, runtime, engine, arguments.run_id, arguments.action_id,
                                                    arguments.reconciliation_id, terminal_approval))
        print(canonical(result))
        if result['status'] != 'succeeded':
            raise SystemExit(1)
    except (AOSFault, OSError, ValueError, sqlite3.Error, TypeError):
        parser.exit(1, 'Son doğrulama reddedildi; bağlı uzlaştırma, mevcut dosya ve taze onaylar gerekli.\n')
    finally:
        runtime.stop()
        if store is not None:
            store.close()


if __name__ == '__main__':
    main()

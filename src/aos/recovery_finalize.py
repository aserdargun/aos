import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Literal

from pydantic import Field, model_validator

from .computer import WorkspaceRuntime
from .contracts import AOSFault, Action, HELLO_CONTENT, HELLO_PATH, Phase, State, TypedModel, canonical, digest, identifier, now
from .dataset_audit import audit_snapshot
from .recovery_effect import inspect_write_effect
from .recovery_reconcile import WriteReconciliation
from .recovery_resume import ResumeApproval
from .recovery_verify import VerificationAdmission
from .storage import TrajectoryStore
from .workspace_identity import open_existing_workspace, workspace_identity


class FinalizationReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_hello_finalization'] = 'read_only_hello_finalization'
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    action_ref: str = Field(pattern='^[a-f0-9]{64}$')
    admission_ref: str = Field(pattern='^[a-f0-9]{64}$')
    state_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    read_actions: int = Field(ge=0, le=2)
    verification_ref: str | None = Field(pattern='^[a-f0-9]{64}$')
    evidence_sha256: str | None = Field(pattern='^[a-f0-9]{64}$')
    disposition: Literal['incomplete', 'file_not_matching', 'requires_startup', 'ready_to_finalize', 'already_finalized', 'blocked']
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False

    @model_validator(mode='after')
    def complete_evidence(self):
        complete = self.evidence_sha256 is not None
        if complete != (self.verification_ref is not None) or complete and self.read_actions != 2:
            raise ValueError('finalization_evidence_inconsistent')
        if self.disposition in {'requires_startup', 'ready_to_finalize', 'already_finalized'} and not complete:
            raise ValueError('finalization_evidence_required')
        return self


class FinalizationRequest(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['finalize_verified_hello'] = 'finalize_verified_hello'
    source: FinalizationReport
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False

    @model_validator(mode='after')
    def ready_source(self):
        if self.source.disposition != 'ready_to_finalize':
            raise ValueError('finalization_source_not_ready')
        return self


class FinalizationReceipt(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['finalized_verified_hello'] = 'finalized_verified_hello'
    finalization_id: str = Field(pattern='^finalization-[a-f0-9]{32}$')
    request: FinalizationRequest
    request_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    finalized_state_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    actor: Literal['local_terminal', 'acceptance_test']
    recorded_at: str
    run_success_declared: Literal[True] = True
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False

    @model_validator(mode='after')
    def bound_request(self):
        if self.request_sha256 != digest(self.request.model_dump()):
            raise ValueError('finalization_request_mismatch')
        return self


def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('finalization_timestamp_requires_timezone')
    return parsed.timestamp()


def historical_state(connection: sqlite3.Connection, current: State, version: int) -> State:
    rows = connection.execute('''SELECT * FROM state_snapshots WHERE run_id=? AND step_id=? AND state_version=?''',
                              (current.run_id, current.step_id, version)).fetchall()
    if len(rows) != 1:
        raise ValueError('finalization_state_ambiguous')
    state = State.model_validate_json(rows[0]['state_json'])
    fields = ('run_id', 'task_id', 'step_id', 'runtime_id', 'deployment_id', 'task_kind', 'authorized_path',
              'authorized_content', 'normalized_goal', 'success_criteria', 'supervisor_deployment_id')
    if (state.state_version != version or version > current.state_version
            or digest(state.model_dump(mode='json')) != rows[0]['content_sha256']
            or any(getattr(state, field) != getattr(current, field) for field in fields)):
        raise ValueError('finalization_state_binding_invalid')
    return state


def verification_evidence(connection: sqlite3.Connection, effect, run_id: str, action_id: str, admission_id: str):
    current = State.model_validate_json(connection.execute('SELECT state_json FROM runtime_states WHERE run_id=?', (run_id,)).fetchone()[0])
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if (current.supervisor_deployment_id is not None or run['training_eligible'] != 0
            or json.loads(run['environment_json']).get('recovery_probe') is not False
            or any(connection.execute(f'SELECT 1 FROM {table} WHERE run_id=? LIMIT 1', (run_id,)).fetchone()
                   for table in ('desktop_tasks', 'supervisor_escalations', 'trajectory_labels'))):
        raise ValueError('finalization_scope_invalid')
    steps = connection.execute('SELECT * FROM steps WHERE run_id=?', (run_id,)).fetchall()
    if len(steps) != 1 or steps[0]['step_id'] != current.step_id or steps[0]['state'] != current.phase.value:
        raise ValueError('finalization_step_invalid')
    admissions = connection.execute("""SELECT * FROM human_interventions WHERE run_id=? AND kind='resume'
        AND json_extract(payload_json,'$.mode')='reconciled_hello_verification' LIMIT 2""", (run_id,)).fetchall()
    corrections = connection.execute("""SELECT * FROM human_interventions WHERE run_id=? AND kind='correction'
        AND json_extract(payload_json,'$.mode')='recorded_hello_write_result' LIMIT 2""", (run_id,)).fetchall()
    if len(admissions) != 1 or len(corrections) != 1:
        raise ValueError('finalization_source_ambiguous')
    admission_row, correction_row = admissions[0], corrections[0]
    admission = VerificationAdmission.model_validate_json(admission_row['payload_json'])
    correction = WriteReconciliation.model_validate_json(correction_row['payload_json'])
    source = historical_state(connection, current, admission.source_state_version)
    write = connection.execute('SELECT * FROM actions WHERE run_id=? AND action_id=?', (run_id, action_id)).fetchone()
    if (admission.admission_id != admission_id or admission_row['intervention_id'] != admission_id
            or admission_row['step_id'] != current.step_id or admission_row['actor'] not in {'local_terminal', 'acceptance_test'}
            or admission.run_ref != effect.checkpoint.run_ref or admission.write_action_ref != effect.action_ref
            or admission.workspace_sha256 != effect.checkpoint.workspace_sha256
            or admission.deployment_sha256 != effect.checkpoint.deployment_sha256
            or admission.write_receipt_sha256 != effect.receipt_sha256 or effect.action_status != 'ok'
            or correction.reconciliation_id != correction_row['intervention_id']
            or correction.actor != correction_row['actor'] or correction.recorded_at != correction_row['created_at']
            or correction_row['step_id'] != current.step_id
            or admission.reconciliation_ref != digest({'reconciliation_id': correction.reconciliation_id})
            or admission.reconciliation_sha256 != digest(correction.model_dump())
            or correction.request.run_ref != admission.run_ref or correction.request.action_ref != admission.write_action_ref
            or correction.request.receipt_sha256 != admission.write_receipt_sha256
            or correction.request.workspace_sha256 != admission.workspace_sha256
            or correction.request.deployment_sha256 != admission.deployment_sha256
            or correction.request.state_sha256 != admission.source_state_sha256
            or digest(source.model_dump(mode='json')) != admission.source_state_sha256
            or source.phase != Phase.PAUSED or source.owner != 'PAUSED'
            or write['error_code'] is not None or json.loads(write['result_json'] or 'null') != {'bytes_written': 28}
            or write['completed_at'] != correction.recorded_at
            or timestamp(correction.recorded_at) > timestamp(admission_row['created_at'])):
        raise ValueError('finalization_source_binding_invalid')
    reads = connection.execute('SELECT * FROM actions WHERE run_id=? AND action_id!=? ORDER BY rowid LIMIT 3', (run_id, action_id)).fetchall()
    approvals = connection.execute("""SELECT * FROM human_interventions WHERE run_id=?
        AND json_extract(payload_json,'$.admission_id')=? AND kind IN ('approve','reject') ORDER BY rowid LIMIT 3""",
                                   (run_id, admission_id)).fetchall()
    if len(reads) > 2 or len(approvals) > 2 or len(approvals) < len(reads):
        raise ValueError('finalization_read_count_invalid')
    bound = []
    completed = True
    previous_completed = timestamp(admission_row['created_at'])
    for index, row in enumerate(reads):
        envelope = connection.execute('SELECT * FROM action_envelopes WHERE action_id=?', (row['action_id'],)).fetchone()
        if envelope is None:
            raise ValueError('finalization_read_envelope_missing')
        action = Action.model_validate_json(envelope['envelope_json'])
        execution = historical_state(connection, current, action.state_version)
        decision = connection.execute('SELECT * FROM decisions WHERE decision_id=?', (row['decision_id'],)).fetchone()
        approval_row = approvals[index]
        approval = ResumeApproval.model_validate_json(approval_row['payload_json'])
        action_hash = digest(action.model_dump(mode='json'))
        deciding = historical_state(connection, current, action.state_version - 2)
        decision_snapshot = connection.execute('SELECT * FROM state_snapshots WHERE snapshot_id=?',
                                               (decision['snapshot_id'],)).fetchone() if decision else None
        if (action.action_id != row['action_id'] or action.run_id != run_id or action.task_id != current.task_id
                or action.step_id != current.step_id or row['step_id'] != current.step_id
                or action.runtime_id != current.runtime_id or action.state_version <= source.state_version
                or action.tool != 'filesystem.read' or row['tool'] != action.tool
                or action.verification != 'independent_read_equals'
                or action.arguments != {'path': HELLO_PATH} or json.loads(row['arguments_json']) != action.arguments
                or action.selected_option != 'read_file' or row['actual_option'] not in {None, 'read_file'}
                or action.idempotency_key != row['idempotency_key'] or action_hash != envelope['payload_sha256']
                or execution.phase != Phase.EXECUTE or execution.owner != 'AGENT'
                or execution.owner_lease_id != action.owner_lease_id
                or digest({'lease_id': action.owner_lease_id}) != admission.fresh_lease_ref
                or decision is None or decision['run_id'] != run_id or decision['step_id'] != current.step_id
                or decision['policy_result'] != 'allow' or decision['selected_option'] != 'read_file'
                or decision_snapshot is None or decision_snapshot['content_sha256'] != digest(deciding.model_dump(mode='json'))
                or deciding.phase != Phase.DECIDE or deciding.owner != 'AGENT' or deciding.owner_lease_id != action.owner_lease_id
                or deciding.state_version <= source.state_version
                or not timestamp(admission_row['created_at']) <= timestamp(decision['created_at']) <= timestamp(approval_row['created_at'])
                or index > 0 and decision['decision_id'] != reads[0]['decision_id']
                or approval.admission_id != admission_id or approval.action_sha256 != action_hash
                or approval.fresh_lease_ref != admission.fresh_lease_ref or approval.state_version != action.state_version
                or approval.expires_at != action.deadline or approval.decision != 'approved'
                or approval_row['kind'] != 'approve' or approval_row['actor'] != admission_row['actor']
                or approval_row['step_id'] != current.step_id
                or not previous_completed <= timestamp(approval_row['created_at']) <= timestamp(row['created_at']) <= action.deadline):
            raise ValueError('finalization_read_binding_invalid')
        if row['status'] == 'ok':
            if (row['actual_option'] != 'read_file' or row['error_code'] is not None or row['completed_at'] is None
                    or json.loads(row['result_json'] or 'null') != {'content': HELLO_CONTENT}
                    or timestamp(row['completed_at']) < timestamp(row['created_at'])):
                raise ValueError('finalization_read_result_invalid')
            previous_completed = timestamp(row['completed_at'])
        else:
            completed = False
        bound.append({'action': dict(row), 'envelope': dict(envelope), 'approval': dict(approval_row),
                      'decision': dict(decision), 'execution': execution.model_dump(mode='json')})
    verifications = connection.execute('SELECT * FROM verifications WHERE run_id=? LIMIT 2', (run_id,)).fetchall()
    if not verifications:
        return current, run, len(reads), None, None
    if len(verifications) != 1 or len(reads) != 2 or len(approvals) != 2 or not completed:
        raise ValueError('finalization_verification_incomplete')
    verification = verifications[0]
    references = json.loads(verification['evidence_refs_json'])
    if not isinstance(references, list) or len(references) != 1 or not isinstance(references[0], str):
        raise ValueError('finalization_observation_reference_invalid')
    observation = connection.execute('SELECT * FROM observations WHERE observation_id=?', (references[0],)).fetchone()
    execution_version = bound[0]['execution']['state_version']
    verifying = historical_state(connection, current, execution_version + 1)
    verifying_row = connection.execute('SELECT created_at FROM state_snapshots WHERE run_id=? AND state_version=?',
                                      (run_id, verifying.state_version)).fetchone()
    if (bound[1]['execution']['state_version'] != execution_version
            or verifying.phase != Phase.VERIFY or verifying.owner != 'AGENT'
            or digest({'lease_id': verifying.owner_lease_id}) != admission.fresh_lease_ref
            or verification['step_id'] != current.step_id or verification['action_id'] != reads[0]['action_id']
            or verification['method'] != 'independent_read_equals' or verification['verifier'] != 'aos-exact-bytes-v1'
            or verification['result'] != 'passed' or verification['criterion'] != current.success_criteria[0]
            or json.loads(verification['expected_json']) != HELLO_CONTENT or json.loads(verification['actual_json']) != HELLO_CONTENT
            or observation is None or observation['run_id'] != run_id or observation['step_id'] != current.step_id
            or observation['action_id'] != reads[1]['action_id'] or observation['kind'] != 'filesystem.read'
            or json.loads(observation['payload_json']) != {'content': HELLO_CONTENT}
            or not previous_completed <= timestamp(verifying_row['created_at']) <= timestamp(observation['created_at']) <= timestamp(verification['created_at'])):
        raise ValueError('finalization_independent_evidence_invalid')
    evidence = {'admission': dict(admission_row), 'correction': dict(correction_row), 'reads': bound,
                'verification': dict(verification), 'observation': dict(observation), 'verifying': verifying.model_dump(mode='json')}
    if run['status'] == 'succeeded':
        if (run['ended_at'] is None or steps[0]['ended_at'] is None
                or timestamp(run['ended_at']) < timestamp(verification['created_at'])
                or timestamp(steps[0]['ended_at']) < timestamp(verification['created_at'])):
            raise ValueError('finalization_finished_metadata_invalid')
    if current.phase in {Phase.VERIFY, Phase.SUCCEEDED}:
        if current.state_version < verifying.state_version or current.phase == Phase.VERIFY and current != verifying:
            raise ValueError('finalization_current_verification_invalid')
    return current, run, 2, digest({'verification_id': verification['verification_id']}), digest(evidence)


def inspect_finalization(database: Path, workspace: Path, run_id: str, action_id: str, admission_id: str,
                         deployment_sha256: str, *, workspace_descriptor: int | None = None) -> FinalizationReport:
    descriptor = open_existing_workspace(workspace) if workspace_descriptor is None else os.dup(workspace_descriptor)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        effect = inspect_write_effect(database, workspace, run_id, action_id, deployment_sha256, workspace_descriptor=descriptor)
        with audit_snapshot(database) as (connection, identity):
            if identity['sha256'] != effect.checkpoint.snapshot_sha256:
                raise ValueError('finalization_snapshot_changed')
            current, run, count, verification_ref, evidence_hash = verification_evidence(connection, effect, run_id, action_id, admission_id)
            disposition = 'blocked'
            if evidence_hash is None:
                disposition = 'incomplete'
            elif effect.result != 'recorded_effect_matches':
                disposition = 'file_not_matching'
            elif (run['status'] == 'succeeded' and run['outcome'] == 'passed' and current.phase == Phase.SUCCEEDED
                  and current.owner in {'AGENT', 'PAUSED'} and run['ended_at']):
                disposition = 'already_finalized'
            elif run['status'] == 'running' and run['outcome'] == 'unknown' and current.owner == 'AGENT' and current.phase in {Phase.VERIFY, Phase.SUCCEEDED}:
                disposition = 'requires_startup'
            elif (run['status'] == 'paused' and run['outcome'] == 'unknown' and run['ended_at'] is None
                  and current.phase == Phase.PAUSED and current.owner == 'PAUSED'):
                previous = historical_state(connection, current, current.state_version - 1)
                admission = VerificationAdmission.model_validate_json(connection.execute(
                    'SELECT payload_json FROM human_interventions WHERE intervention_id=?', (admission_id,)).fetchone()[0])
                if (previous.phase in {Phase.VERIFY, Phase.SUCCEEDED} and previous.owner == 'AGENT'
                        and digest({'lease_id': previous.owner_lease_id}) == admission.fresh_lease_ref):
                    disposition = 'ready_to_finalize'
            report = FinalizationReport(snapshot_sha256=identity['sha256'], run_ref=effect.checkpoint.run_ref,
                                        action_ref=effect.action_ref, admission_ref=digest({'admission_id': admission_id}),
                                        state_sha256=effect.checkpoint.state_sha256, workspace_sha256=effect.checkpoint.workspace_sha256,
                                        deployment_sha256=deployment_sha256, read_actions=count, verification_ref=verification_ref,
                                        evidence_sha256=evidence_hash, disposition=disposition)
            audits = connection.execute("""SELECT * FROM human_interventions WHERE run_id=? AND kind='correction'
                AND json_extract(payload_json,'$.mode')='finalized_verified_hello' LIMIT 2""", (run_id,)).fetchall()
            if not audits and current.phase == Phase.SUCCEEDED and current.owner == 'PAUSED':
                previous = historical_state(connection, current, current.state_version - 1)
                if previous.phase != Phase.SUCCEEDED or previous.owner != 'AGENT':
                    raise ValueError('finalization_audit_missing')
            if audits:
                if len(audits) != 1:
                    raise ValueError('finalization_audit_ambiguous')
                audit = FinalizationReceipt.model_validate_json(audits[0]['payload_json'])
                source = audit.request.source
                previous = historical_state(connection, current, current.state_version - 1)
                if (audit.finalization_id != audits[0]['intervention_id'] or audit.actor != audits[0]['actor']
                        or audit.recorded_at != audits[0]['created_at'] or audits[0]['step_id'] != current.step_id
                        or audit.finalized_state_sha256 != report.state_sha256
                        or source.state_sha256 != digest(previous.model_dump(mode='json'))
                        or previous.phase != Phase.PAUSED or previous.owner != 'PAUSED'
                        or current.owner != 'PAUSED' or current.phase != Phase.SUCCEEDED
                        or run['status'] != 'succeeded' or run['outcome'] != 'passed' or run['ended_at'] != audit.recorded_at
                        or connection.execute('SELECT ended_at FROM steps WHERE step_id=?', (current.step_id,)).fetchone()[0] != audit.recorded_at
                        or timestamp(audit.recorded_at) >= audit.request.expires_at
                        or any(getattr(source, field) != getattr(report, field) for field in
                               ('run_ref', 'action_ref', 'admission_ref', 'workspace_sha256', 'deployment_sha256', 'evidence_sha256', 'verification_ref'))):
                    raise ValueError('finalization_audit_binding_invalid')
            check = open_existing_workspace(workspace)
            try:
                if digest(workspace_identity(workspace, check).model_dump()) != report.workspace_sha256:
                    raise ValueError('finalization_workspace_changed')
            finally:
                os.close(check)
            return report
    finally:
        os.close(descriptor)


def finalization_request(store: TrajectoryStore, runtime: WorkspaceRuntime, run_id: str, action_id: str,
                         admission_id: str, deployment_sha256: str, expires_at: float) -> FinalizationRequest:
    if store.lock is None or type(runtime) is not WorkspaceRuntime or runtime.descriptor is None:
        raise ValueError('finalization_owned_runtime_required')
    database = Path(store.connection.execute('PRAGMA database_list').fetchone()[2])
    report = inspect_finalization(database, runtime.root, run_id, action_id, admission_id, deployment_sha256,
                                  workspace_descriptor=runtime.descriptor)
    return FinalizationRequest(source=report, expires_at=expires_at)


async def finalize_hello(store: TrajectoryStore, runtime: WorkspaceRuntime, run_id: str, action_id: str,
                         admission_id: str, deployment_sha256: str,
                         approve: Callable[[FinalizationRequest], Awaitable[bool]], *,
                         actor: Literal['local_terminal', 'acceptance_test'] = 'local_terminal') -> FinalizationReceipt:
    if not callable(approve) or actor not in {'local_terminal', 'acceptance_test'} or store.connection.in_transaction:
        raise ValueError('finalization_approval_required')
    request = finalization_request(store, runtime, run_id, action_id, admission_id, deployment_sha256, time.time() + 60)
    confirmation = request.model_copy(deep=True)
    try:
        accepted = await asyncio.wait_for(approve(confirmation), max(0, request.expires_at - time.time()))
    except TimeoutError:
        raise ValueError('finalization_approval_expired') from None
    if accepted is not True or confirmation != request or time.time() >= request.expires_at:
        raise ValueError('finalization_not_approved')
    connection = store.connection
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        refreshed = finalization_request(store, runtime, run_id, action_id, admission_id, deployment_sha256, request.expires_at)
        if refreshed != request or time.time() >= request.expires_at:
            raise ValueError('finalization_evidence_changed')
        previous = store.state(run_id)
        current = State.model_validate({**previous.model_dump(), 'phase': Phase.SUCCEEDED, 'owner': 'PAUSED',
                                        'owner_lease_id': identifier('revoked'), 'state_version': previous.state_version + 1})
        receipt = FinalizationReceipt(finalization_id=identifier('finalization'), request=request,
                                      request_sha256=digest(request.model_dump()), actor=actor, recorded_at=now(),
                                      finalized_state_sha256=digest(current.model_dump(mode='json')))
        changed = connection.execute('''UPDATE runtime_states SET state_version=?,state_json=?
            WHERE run_id=? AND state_version=? AND state_json=?''',
                                     (current.state_version, current.model_dump_json(), run_id, previous.state_version, previous.model_dump_json())).rowcount
        finished = connection.execute("""UPDATE runs SET status='succeeded',outcome='passed',ended_at=?
            WHERE run_id=? AND status='paused' AND outcome='unknown' AND training_eligible=0""", (receipt.recorded_at, run_id)).rowcount
        stepped = connection.execute("UPDATE steps SET state='SUCCEEDED',ended_at=? WHERE run_id=? AND step_id=? AND state='PAUSED'",
                                     (receipt.recorded_at, run_id, current.step_id)).rowcount
        if changed != 1 or finished != 1 or stepped != 1:
            raise ValueError('finalization_state_changed')
        store.snapshot(current)
        store.insert('human_interventions', intervention_id=receipt.finalization_id, run_id=run_id, step_id=current.step_id,
                     actor=actor, kind='correction', payload_json=canonical(receipt.model_dump()), created_at=receipt.recorded_at)
        if time.time() >= request.expires_at:
            raise ValueError('finalization_approval_expired')
    return receipt


async def terminal_confirmation(request: FinalizationRequest) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    request_hash = digest(request.model_dump())
    print(canonical(request.model_dump()), flush=True)
    print(f'Yalnız kalıcı doğrulamanın state/run sonucu tamamlanacak; yeni eylem yok.\nFINALIZE {request_hash}', flush=True)
    loop = asyncio.get_running_loop()
    response = loop.create_future()
    content = bytearray()

    def read_line():
        chunk = os.read(sys.stdin.fileno(), 257)
        content.extend(chunk)
        if not chunk or b'\n' in content or len(content) > 256:
            loop.remove_reader(sys.stdin.fileno())
            if not response.done():
                response.set_result(bool(chunk) and b'\n' in content and len(content) <= 256
                                    and bytes(content).strip() == f'FINALIZE {request_hash}'.encode())

    loop.add_reader(sys.stdin.fileno(), read_line)
    try:
        return await asyncio.wait_for(response, max(0, request.expires_at - time.time()))
    except TimeoutError:
        return False
    finally:
        loop.remove_reader(sys.stdin.fileno())


def main():
    parser = argparse.ArgumentParser(description='Kalıcı hello doğrulamasını salt okunur sınıflandır veya açık onayla atomik finalleştir')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--action-id', required=True)
    parser.add_argument('--admission-id', required=True)
    parser.add_argument('--deployment-sha256', required=True)
    parser.add_argument('--finalize', action='store_true')
    arguments = parser.parse_args()
    if arguments.finalize and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        parser.exit(1, 'Finalizasyon için etkileşimli yerel terminal ve exact-request onayı gerekli.\n')
    runtime = WorkspaceRuntime(arguments.workspace)
    store = None
    try:
        report = inspect_finalization(arguments.database, arguments.workspace, arguments.run_id, arguments.action_id,
                                      arguments.admission_id, arguments.deployment_sha256)
        if not arguments.finalize:
            print(canonical(report.model_dump()))
            return
        if report.disposition not in {'requires_startup', 'ready_to_finalize'}:
            raise ValueError('finalization_complete_evidence_required')
        runtime.start_existing()
        store = TrajectoryStore(arguments.database)
        receipt = asyncio.run(finalize_hello(store, runtime, arguments.run_id, arguments.action_id, arguments.admission_id,
                                             arguments.deployment_sha256, terminal_confirmation))
        print(canonical(receipt.model_dump()))
    except (AOSFault, OSError, ValueError, sqlite3.Error, TypeError, AttributeError):
        parser.exit(1, 'Finalizasyon reddedildi; bağlı tam kanıt, boşta workspace ve taze onay gerekli.\n')
    finally:
        runtime.stop()
        if store is not None:
            store.close()


if __name__ == '__main__':
    main()

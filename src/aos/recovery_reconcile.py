import argparse
import asyncio
from collections.abc import Awaitable, Callable
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Literal

from pydantic import Field, model_validator

from .computer import WorkspaceRuntime
from .contracts import AOSFault, Phase, TypedModel, canonical, digest, identifier, now
from .recovery_effect import inspect_write_effect
from .storage import TrajectoryStore


class WriteReconciliationRequest(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['record_hello_write_result'] = 'record_hello_write_result'
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    action_ref: str = Field(pattern='^[a-f0-9]{64}$')
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    state_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    receipt_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    previous_status: Literal['uncertain'] = 'uncertain'
    recorded_status: Literal['ok'] = 'ok'
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False


class WriteReconciliation(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['recorded_hello_write_result'] = 'recorded_hello_write_result'
    reconciliation_id: str = Field(pattern='^reconciliation-[a-f0-9]{32}$')
    request: WriteReconciliationRequest
    request_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    actor: Literal['local_terminal', 'acceptance_test']
    recorded_at: str
    action_result_recorded: Literal[True] = True
    run_success_declared: Literal[False] = False
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False

    @model_validator(mode='after')
    def bound_request(self):
        if self.request_sha256 != digest(self.request.model_dump()):
            raise ValueError('reconciliation_request_mismatch')
        return self


def reconciliation_request(store: TrajectoryStore, runtime: WorkspaceRuntime, run_id: str, action_id: str,
                           deployment_sha256: str, expires_at: float) -> WriteReconciliationRequest:
    if store.lock is None or type(runtime) is not WorkspaceRuntime or runtime.descriptor is None:
        raise ValueError('reconciliation_owned_runtime_required')
    database = Path(store.connection.execute('PRAGMA database_list').fetchone()[2])
    report = inspect_write_effect(database, runtime.root, run_id, action_id, deployment_sha256,
                                  workspace_descriptor=runtime.descriptor)
    state = store.state(run_id)
    connection = store.connection
    run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
    action = connection.execute('SELECT * FROM actions WHERE action_id=? AND run_id=?', (action_id, run_id)).fetchone()
    if (report.result != 'recorded_effect_matches' or report.action_status != 'uncertain'
            or state.phase != Phase.PAUSED or state.owner != 'PAUSED' or run['status'] != 'paused'
            or run['outcome'] != 'unknown' or run['training_eligible'] != 0
            or state.supervisor_deployment_id is not None
            or json.loads(run['environment_json']).get('recovery_probe') is not False
            or action['step_id'] != state.step_id or action['result_json'] is not None
            or action['completed_at'] is not None or action['error_code'] != 'RUNTIME_CRASH'
            or connection.execute('SELECT count(*) FROM actions WHERE run_id=?', (run_id,)).fetchone()[0] != 1
            or any(connection.execute(f'SELECT 1 FROM {table} WHERE run_id=? LIMIT 1', (run_id,)).fetchone()
                   for table in ('desktop_tasks', 'verifications', 'supervisor_escalations', 'trajectory_labels'))):
        raise ValueError('reconciliation_checkpoint_not_eligible')
    return WriteReconciliationRequest(run_ref=report.checkpoint.run_ref, action_ref=report.action_ref,
                                      snapshot_sha256=report.checkpoint.snapshot_sha256,
                                      state_sha256=report.checkpoint.state_sha256, receipt_sha256=report.receipt_sha256,
                                      workspace_sha256=report.checkpoint.workspace_sha256,
                                      deployment_sha256=deployment_sha256, expires_at=expires_at)


async def reconcile_write(store: TrajectoryStore, runtime: WorkspaceRuntime, run_id: str, action_id: str,
                          deployment_sha256: str, approve: Callable[[WriteReconciliationRequest], Awaitable[bool]], *,
                          actor: Literal['local_terminal', 'acceptance_test'] = 'local_terminal') -> WriteReconciliation:
    if not callable(approve) or actor not in {'local_terminal', 'acceptance_test'} or store.connection.in_transaction:
        raise ValueError('reconciliation_approval_required')
    request = reconciliation_request(store, runtime, run_id, action_id, deployment_sha256, time.time() + 60)
    remaining = request.expires_at - time.time()
    if remaining <= 0:
        raise ValueError('reconciliation_approval_expired')
    confirmation = request.model_copy(deep=True)
    try:
        accepted = await asyncio.wait_for(approve(confirmation), remaining)
    except TimeoutError:
        raise ValueError('reconciliation_approval_expired') from None
    if accepted is not True or confirmation != request or time.time() >= request.expires_at:
        raise ValueError('reconciliation_not_approved')
    connection = store.connection
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        refreshed = reconciliation_request(store, runtime, run_id, action_id, deployment_sha256, request.expires_at)
        if refreshed != request or time.time() >= request.expires_at:
            raise ValueError('reconciliation_evidence_changed')
        receipt = WriteReconciliation(reconciliation_id=identifier('reconciliation'), request=request,
                                      request_sha256=digest(request.model_dump()), actor=actor, recorded_at=now())
        changed = connection.execute("""UPDATE actions SET status='ok',error_code=NULL,result_json=?,completed_at=?
            WHERE action_id=? AND run_id=? AND status='uncertain' AND result_json IS NULL AND completed_at IS NULL""",
                                     (canonical({'bytes_written': 28}), receipt.recorded_at, action_id, run_id)).rowcount
        if changed != 1:
            raise ValueError('reconciliation_action_changed')
        store.insert('human_interventions', intervention_id=receipt.reconciliation_id, run_id=run_id,
                     step_id=store.state(run_id).step_id, actor=actor, kind='correction',
                     payload_json=canonical(receipt.model_dump()), created_at=receipt.recorded_at)
    return receipt


async def terminal_confirmation(request: WriteReconciliationRequest) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    request_sha256 = digest(request.model_dump())
    print(canonical(request.model_dump()), flush=True)
    print(f'Yalnız eski yazma sonucu kaydedilecek; görev devam etmeyecek.\n'
          f'60 saniyelik süre dolmadan RECONCILE {request_sha256} yazın; diğer yanıt reddeder.', flush=True)
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
                                    and bytes(content).strip() == f'RECONCILE {request_sha256}'.encode())

    loop.add_reader(sys.stdin.fileno(), read_line)
    try:
        return await asyncio.wait_for(response, max(0, request.expires_at - time.time()))
    except TimeoutError:
        return False
    finally:
        loop.remove_reader(sys.stdin.fileno())


def main():
    parser = argparse.ArgumentParser(description='Matching hello receipt ile eski action sonucunu açık terminal onayıyla kaydet')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--action-id', required=True)
    parser.add_argument('--deployment-sha256', required=True)
    arguments = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(1, 'Uzlaştırma için etkileşimli yerel terminal ve exact-request onayı gerekli.\n')
    runtime = WorkspaceRuntime(arguments.workspace)
    store = None
    try:
        report = inspect_write_effect(arguments.database, arguments.workspace, arguments.run_id,
                                      arguments.action_id, arguments.deployment_sha256)
        if report.result != 'recorded_effect_matches' or report.action_status not in {'running', 'uncertain'}:
            raise ValueError('reconciliation_receipt_required')
        runtime.start_existing()
        store = TrajectoryStore(arguments.database)
        result = asyncio.run(reconcile_write(store, runtime, arguments.run_id, arguments.action_id,
                                             arguments.deployment_sha256, terminal_confirmation))
        print(canonical(result.model_dump()))
    except (AOSFault, OSError, ValueError, sqlite3.Error):
        parser.exit(1, 'Uzlaştırma reddedildi; boşta workspace, bağlı kanıt ve taze onay gerekli.\n')
    finally:
        runtime.stop()
        if store is not None:
            store.close()


if __name__ == '__main__':
    main()

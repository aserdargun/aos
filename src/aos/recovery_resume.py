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

from pydantic import Field

from .computer import SafetyPolicy, WorkspaceRuntime
from .contracts import AOSFault, Action, ErrorCode, Phase, Settings, TypedModel, canonical, digest, identifier, now
from .decision import DeciderEngine, DecisionEngine, FixtureDecisionEngine
from .operator import Operator
from .recovery_checkpoint import inspect_checkpoint
from .storage import TrajectoryStore


class ResumeAdmission(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['pristine_hello_resume_request'] = 'pristine_hello_resume_request'
    admission_id: str = Field(pattern='^resume-[a-f0-9]{32}$')
    run_ref: str = Field(pattern='^[a-f0-9]{64}$')
    source_snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    source_state_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    source_state_version: int = Field(ge=0)
    workspace_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    fresh_lease_ref: str = Field(pattern='^[a-f0-9]{64}$')
    effect: Literal['absent', 'exact_content']
    previously_recorded_actions: Literal[0] = 0
    requires_action_approval: Literal[True] = True
    automatic_replay_allowed: Literal[False] = False


class ResumeApproval(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    admission_id: str = Field(pattern='^resume-[a-f0-9]{32}$')
    action_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    fresh_lease_ref: str = Field(pattern='^[a-f0-9]{64}$')
    state_version: int = Field(ge=0)
    expires_at: float
    decision: Literal['approved', 'rejected', 'expired']


def require_pristine(store: TrajectoryStore, run_id: str) -> None:
    connection = store.connection
    if (connection.execute('SELECT 1 FROM actions WHERE run_id=?', (run_id,)).fetchone()
            or connection.execute('SELECT 1 FROM desktop_tasks WHERE run_id=?', (run_id,)).fetchone()
            or connection.execute('SELECT 1 FROM verifications WHERE run_id=?', (run_id,)).fetchone()
            or connection.execute('SELECT 1 FROM supervisor_escalations WHERE run_id=?', (run_id,)).fetchone()):
        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only unscheduled hello runs with zero prior actions can continue')


async def resume_hello(settings: Settings, store: TrajectoryStore, runtime: WorkspaceRuntime, engine: DecisionEngine,
                       run_id: str, approve: Callable[[Action], Awaitable[bool]], *,
                       actor: Literal['local_terminal', 'acceptance_test'] = 'local_terminal') -> dict:
    database_path = Path(store.connection.execute('PRAGMA database_list').fetchone()[2])
    if (store.lock is None or type(runtime) is not WorkspaceRuntime or runtime.descriptor is None
            or runtime.root != settings.workspace.absolute() or database_path != settings.database.absolute()
            or not callable(approve) or actor not in {'local_terminal', 'acceptance_test'}):
        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Resume requires the owned existing workspace, writer and explicit approval gate')
    require_pristine(store, run_id)
    state = store.state(run_id)
    row = store.connection.execute('SELECT status,environment_json FROM runs WHERE run_id=?', (run_id,)).fetchone()
    if (state.phase != Phase.PAUSED or state.owner != 'PAUSED' or row['status'] != 'paused'
            or state.supervisor_deployment_id is not None or json.loads(row['environment_json']).get('recovery_probe') is not False):
        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Resume requires a safely reconciled plain hello checkpoint')
    deployment_sha256 = digest(engine.identity)
    report = inspect_checkpoint(settings.database, settings.workspace, run_id, deployment_sha256,
                                workspace_descriptor=runtime.descriptor)
    if report.effect not in {'absent', 'exact_content'}:
        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Conflicting or unreadable workspace cannot continue')
    fresh_lease = identifier('lease')
    admission = ResumeAdmission(admission_id=identifier('resume'), run_ref=report.run_ref,
                                source_snapshot_sha256=report.snapshot_sha256, source_state_sha256=report.state_sha256,
                                source_state_version=state.state_version, workspace_sha256=report.workspace_sha256,
                                deployment_sha256=deployment_sha256, fresh_lease_ref=digest({'lease_id': fresh_lease}),
                                effect=report.effect)
    with store.connection:
        store.insert('human_interventions', intervention_id=identifier('intervention'), run_id=run_id,
                     step_id=state.step_id, actor=actor, kind='resume', payload_json=canonical(admission.model_dump()), created_at=now())

    async def gate(action: Action) -> None:
        action_sha256 = digest(action.model_dump(mode='json'))
        offered = Action.model_validate_json(action.model_dump_json())
        try:
            approved = await asyncio.wait_for(approve(offered), max(0, action.deadline - time.time()))
        except TimeoutError:
            approved = False
        decision = 'expired' if time.time() >= action.deadline else 'approved' if approved is True else 'rejected'
        if digest(offered.model_dump(mode='json')) != action_sha256:
            decision = 'rejected'
        receipt = ResumeApproval(admission_id=admission.admission_id, action_sha256=action_sha256,
                                 fresh_lease_ref=admission.fresh_lease_ref, state_version=action.state_version,
                                 expires_at=action.deadline, decision=decision)
        with store.connection:
            store.insert('human_interventions', intervention_id=identifier('intervention'), run_id=run_id,
                         step_id=state.step_id, actor=actor, kind='approve' if decision == 'approved' else 'reject',
                         payload_json=canonical(receipt.model_dump()), created_at=now())
        if decision != 'approved':
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Fresh exact-action approval was not granted')
        require_pristine(store, run_id)
        current = store.state(run_id)
        if (runtime.descriptor is None or runtime.root != settings.workspace.absolute()
                or digest(engine.identity) != deployment_sha256 or current.owner_lease_id != fresh_lease
                or action.owner_lease_id != fresh_lease or action.run_id != run_id):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Resume identity or ownership changed')
        try:
            refreshed = inspect_checkpoint(settings.database, settings.workspace, run_id, deployment_sha256,
                                           workspace_descriptor=runtime.descriptor)
        except (ValueError, OSError):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Checkpoint changed before resumed action') from None
        if refreshed.workspace_sha256 != report.workspace_sha256 or refreshed.effect != report.effect:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Workspace changed before resumed action')
        SafetyPolicy.check(action, current, runtime.runtime_id)

    result = await Operator(settings, store, runtime, engine).hello(owner_lease_id=fresh_lease,
                                                                  resume_state=state, execution_gate=gate)
    return {**result, 'resume_admission_id': admission.admission_id, 'automatic_replay_allowed': False}


async def terminal_approval(action: Action) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    action_sha256 = digest(action.model_dump(mode='json'))
    print(f'Taze eylem: {action.tool} /workspace/hello.txt; sabit hello içeriği ve bağımsız geri okuma.\n'
          f'Onay için 60 saniye içinde APPROVE {action_sha256} yazın; diğer yanıt reddeder.', flush=True)
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
                                    and bytes(content).strip() == f'APPROVE {action_sha256}'.encode())

    loop.add_reader(sys.stdin.fileno(), read_line)
    try:
        return await asyncio.wait_for(response, min(60, max(0, action.deadline - time.time())))
    except TimeoutError:
        return False
    finally:
        loop.remove_reader(sys.stdin.fileno())


def main():
    parser = argparse.ArgumentParser(description='Yalnız sıfır eylemli hello checkpoint: yeni lease, karar ve terminal onayıyla devam')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--engine', choices=['fixture', 'decider'], default='decider')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--model-python', type=Path)
    arguments = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(1, 'Devam için etkileşimli yerel terminal ve taze eylem onayı gerekli.\n')
    if arguments.engine == 'decider' and (arguments.manifest is None or arguments.model_python is None):
        parser.error('Gerçek motor için --manifest ve --model-python gerekli.')
    settings = Settings(workspace=arguments.workspace, database=arguments.database)
    runtime = WorkspaceRuntime(settings.workspace)
    store = None
    try:
        engine = FixtureDecisionEngine() if arguments.engine == 'fixture' else DeciderEngine(arguments.manifest, arguments.model_python)
        inspect_checkpoint(settings.database, settings.workspace, arguments.run_id, digest(engine.identity))
        runtime.start_existing()
        store = TrajectoryStore(settings.database)
        result = asyncio.run(resume_hello(settings, store, runtime, engine, arguments.run_id, terminal_approval))
        print(canonical(result))
        if result['status'] != 'succeeded':
            raise SystemExit(1)
    except (AOSFault, OSError, ValueError, sqlite3.Error):
        parser.exit(1, 'Devam reddedildi; mevcut checkpoint, workspace, deployment ve onay sınırlarını kontrol edin.\n')
    finally:
        runtime.stop()
        if store is not None:
            store.close()


if __name__ == '__main__':
    main()

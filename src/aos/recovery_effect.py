import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Action, HELLO_CONTENT, HELLO_PATH, Phase, State, TypedModel, canonical, digest, now
from .dataset_audit import audit_snapshot
from .recovery_checkpoint import CheckpointReport, inspect_checkpoint
from .workspace_identity import open_existing_workspace, workspace_identity
from .write_receipts import WriteReceipt, witness_file


class WriteEffectReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['read_only_write_effect_inspection'] = 'read_only_write_effect_inspection'
    checkpoint: CheckpointReport
    action_ref: str = Field(pattern='^[a-f0-9]{64}$')
    action_status: Literal['intent', 'running', 'ok', 'error', 'denied', 'cancelled', 'uncertain']
    receipt_sha256: str | None = Field(pattern='^[a-f0-9]{64}$')
    result: Literal['receipt_missing', 'recorded_effect_matches', 'file_not_matching']
    inspected_at: str
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False
    automatic_replay_allowed: Literal[False] = False
    uncertain_effect_resolved: Literal[False] = False

    @model_validator(mode='after')
    def receipt_consistency(self):
        if (self.result == 'receipt_missing') != (self.receipt_sha256 is None):
            raise ValueError('effect_receipt_mismatch')
        if self.result == 'recorded_effect_matches' and not self.checkpoint.criterion_observed:
            raise ValueError('effect_checkpoint_mismatch')
        return self


def bound_write(connection: sqlite3.Connection, run_id: str, action_id: str) -> sqlite3.Row:
    row = connection.execute('''SELECT actions.*,action_envelopes.envelope_json,action_envelopes.payload_sha256
        FROM actions JOIN action_envelopes USING(action_id) WHERE run_id=? AND action_id=?''', (run_id, action_id)).fetchone()
    if row is None:
        raise ValueError('write_action_missing')
    action = Action.model_validate_json(row['envelope_json'])
    current = State.model_validate_json(connection.execute('SELECT state_json FROM runtime_states WHERE run_id=?', (run_id,)).fetchone()[0])
    decision = connection.execute('SELECT * FROM decisions WHERE decision_id=? AND run_id=? AND step_id=?',
                                  (row['decision_id'], run_id, row['step_id'])).fetchone()
    snapshots = connection.execute('''SELECT state_json,content_sha256 FROM state_snapshots
        WHERE run_id=? AND step_id=? AND state_version=?''', (run_id, row['step_id'], action.state_version)).fetchall()
    if (action.action_id != action_id or action.run_id != run_id or action.task_id != current.task_id
            or action.step_id != row['step_id'] or action.runtime_id != current.runtime_id
            or action.state_version > current.state_version or action.tool != 'filesystem.write' or row['tool'] != action.tool
            or action.arguments != {'path': HELLO_PATH, 'content': HELLO_CONTENT}
            or json.loads(row['arguments_json']) != action.arguments or action.idempotency_key != row['idempotency_key']
            or action.selected_option != 'write_file' or row['actual_option'] not in {None, 'write_file'}
            or digest(action.model_dump(mode='json')) != row['payload_sha256'] or not snapshots
            or decision is None or decision['policy_result'] != 'allow' or decision['selected_option'] != 'write_file'):
        raise ValueError('write_action_binding_invalid')
    for snapshot in snapshots:
        state = State.model_validate_json(snapshot['state_json'])
        if (digest(state.model_dump(mode='json')) != snapshot['content_sha256'] or state.phase != Phase.EXECUTE
                or state.owner != 'AGENT' or state.owner_lease_id != action.owner_lease_id
                or state.run_id != run_id or state.task_id != action.task_id or state.step_id != action.step_id
                or state.state_version != action.state_version or state.deployment_id != current.deployment_id
                or state.runtime_id != action.runtime_id or state.task_kind != 'hello'
                or state.authorized_path != HELLO_PATH or state.authorized_content != HELLO_CONTENT):
            raise ValueError('write_execution_state_invalid')
    return row


def inspect_write_effect(database: Path, workspace: Path, run_id: str, action_id: str, deployment_sha256: str,
                         *, workspace_descriptor: int | None = None) -> WriteEffectReport:
    if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
        raise ValueError('write_action_invalid')
    descriptor = open_existing_workspace(workspace) if workspace_descriptor is None else os.dup(workspace_descriptor)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checkpoint = inspect_checkpoint(database, workspace, run_id, deployment_sha256, workspace_descriptor=descriptor)
        with audit_snapshot(database) as (connection, identity):
            if identity['sha256'] != checkpoint.snapshot_sha256:
                raise ValueError('write_snapshot_changed')
            row = bound_write(connection, run_id, action_id)
            rows = connection.execute("""SELECT payload_json,run_id,step_id FROM observations
                WHERE action_id=? AND kind='filesystem.write_receipt' LIMIT 2""", (action_id,)).fetchall()
            receipt = None
            result = 'receipt_missing'
            if rows:
                if len(rows) != 1 or rows[0]['run_id'] != run_id or rows[0]['step_id'] != row['step_id']:
                    raise ValueError('write_receipt_ambiguous')
                receipt = WriteReceipt.model_validate_json(rows[0]['payload_json'])
                if (receipt.run_ref != checkpoint.run_ref or receipt.action_ref != digest({'action_id': action_id})
                        or receipt.envelope_sha256 != row['payload_sha256'] or receipt.workspace_sha256 != checkpoint.workspace_sha256
                        or row['status'] not in {'running', 'uncertain', 'ok'} or row['actual_option'] != 'write_file'):
                    raise ValueError('write_receipt_binding_invalid')
                result = 'file_not_matching'
                try:
                    target = os.open('hello.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    try:
                        if witness_file(descriptor, target) == receipt.file and checkpoint.criterion_observed:
                            result = 'recorded_effect_matches'
                    finally:
                        os.close(target)
                except (OSError, ValueError):
                    pass
            check = open_existing_workspace(workspace)
            try:
                if digest(workspace_identity(workspace, check).model_dump()) != checkpoint.workspace_sha256:
                    raise ValueError('write_workspace_changed')
            finally:
                os.close(check)
            return WriteEffectReport(checkpoint=checkpoint, action_ref=digest({'action_id': action_id}),
                                     action_status=row['status'], receipt_sha256=digest(receipt.model_dump()) if receipt else None,
                                     result=result, inspected_at=now())
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description='Kalıcı hello yazma kanıtını salt okunur denetler; eylemi çözmez veya tekrar etmez')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--action-id', required=True)
    parser.add_argument('--deployment-sha256', required=True)
    arguments = parser.parse_args()
    try:
        print(canonical(inspect_write_effect(arguments.database, arguments.workspace, arguments.run_id,
                                             arguments.action_id, arguments.deployment_sha256).model_dump()))
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(1, 'Yazma kanıtı denetlenemedi; checkpoint, eylem bağı ve boşta workspace gerekli.\n')


if __name__ == '__main__':
    main()

import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import AOSFault, HELLO_CONTENT, REPO_ROOT, Settings, canonical, digest
from aos.dataset import validator
from aos.decision import FixtureDecisionEngine
from aos.export import export_run
from aos.operator import Operator
from aos.recovery_effect import WriteEffectReport, inspect_write_effect
from aos.recovery_resume import resume_hello
from aos.storage import TrajectoryStore
from aos.write_receipts import WriteReceipt


CRASH_SCRIPT = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import ComputerGateway, WorkspaceRuntime
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine, DeciderEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore
settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
store = TrajectoryStore(settings.database)
runtime = WorkspaceRuntime(settings.workspace)
runtime.start()
stage = sys.argv[3]
original_write = runtime.write
original_receipt = ComputerGateway.record_write_receipt
original_execute = ComputerGateway.execute
def crash_write(path, content):
    if stage != 'before_write':
        original_write(path, content)
    os._exit(73)
def crash_receipt(self, action, evidence):
    if stage != 'before_receipt':
        original_receipt(self, action, evidence)
    os._exit(73)
def crash_result(self, action, decision_id):
    original_execute(self, action, decision_id)
    os._exit(73)
if stage in {'before_write','after_write'}:
    runtime.write = crash_write
elif stage in {'before_receipt','after_receipt'}:
    ComputerGateway.record_write_receipt = crash_receipt
else:
    ComputerGateway.execute = crash_result
engine = FixtureDecisionEngine() if len(sys.argv) == 4 else DeciderEngine(Path(sys.argv[4]), Path(sys.argv[5]))
asyncio.run(Operator(settings, store, runtime, engine).hello())
'''


class WriteEffectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database, self.workspace = self.root / 'store.sqlite', self.root / 'workspace'
        store = TrajectoryStore(self.database)
        runtime = WorkspaceRuntime(self.workspace)
        runtime.start()
        try:
            result = asyncio.run(Operator(Settings(database=self.database, workspace=self.workspace), store, runtime,
                                          FixtureDecisionEngine()).hello())
            self.assertEqual(result['status'], 'succeeded')
            self.run_id = result['run_id']
            self.action_id = store.connection.execute("SELECT action_id FROM actions WHERE tool='filesystem.write'").fetchone()[0]
        finally:
            runtime.stop()
            store.close()

    def inspect(self):
        return inspect_write_effect(self.database, self.workspace, self.run_id, self.action_id, digest(FixtureDecisionEngine.identity))

    def receipt(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return json.loads(connection.execute("SELECT payload_json FROM observations WHERE kind='filesystem.write_receipt'").fetchone()[0])

    def mutate(self, query, parameters=()):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(query, parameters)

    def test_durable_receipt_binds_exclusive_fd_and_is_private_in_export(self):
        receipt = WriteReceipt.model_validate(self.receipt())
        self.assertEqual(receipt.file.content_sha256, hashlib.sha256(HELLO_CONTENT.encode()).hexdigest())
        self.assertEqual(receipt.file.inode, (self.workspace / 'hello.txt').stat().st_ino)
        store = TrajectoryStore(self.database, readonly=True)
        try:
            exported = canonical(export_run(store, self.run_id))
            self.assertNotIn('filesystem.write_receipt', exported)
            self.assertNotIn('modified_ns', exported)
            result = json.loads(store.connection.execute('SELECT result_json FROM actions WHERE action_id=?', (self.action_id,)).fetchone()[0])
            self.assertEqual(result, {'bytes_written': 28})
        finally:
            store.close()

    def test_matching_report_neither_resolves_nor_authorizes_or_mutates(self):
        before = self.database.read_bytes()
        report = self.inspect()
        self.assertEqual(report.result, 'recorded_effect_matches')
        self.assertEqual(self.database.read_bytes(), before)
        for field in ('execution_authorized', 'resume_authorized', 'automatic_replay_allowed', 'uncertain_effect_resolved'):
            self.assertFalse(getattr(report, field))
        for private in (str(self.workspace), self.run_id, self.action_id, 'owner_uid', 'modified_ns'):
            self.assertNotIn(private, report.model_dump_json())

    def test_same_bytes_rewritten_are_not_the_recorded_file_version(self):
        target = self.workspace / 'hello.txt'
        before = target.stat()
        target.write_text(HELLO_CONTENT)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.inspect().result, 'file_not_matching')
        self.assertTrue(self.inspect().checkpoint.criterion_observed)

    def test_same_content_replacement_is_not_the_created_inode(self):
        target = self.workspace / 'hello.txt'
        target.rename(self.workspace / 'original')
        target.write_text(HELLO_CONTENT)
        self.assertEqual(self.inspect().result, 'file_not_matching')

    def test_absent_symlink_hardlink_fifo_and_oversize_do_not_match(self):
        target = self.workspace / 'hello.txt'
        target.unlink()
        self.assertEqual(self.inspect().result, 'file_not_matching')
        outside = self.root / 'outside'
        outside.write_text(HELLO_CONTENT)
        target.symlink_to(outside)
        self.assertEqual(self.inspect().result, 'file_not_matching')
        target.unlink()
        os.link(outside, target)
        self.assertEqual(self.inspect().result, 'file_not_matching')
        target.unlink()
        os.mkfifo(target)
        self.assertEqual(self.inspect().result, 'file_not_matching')
        target.unlink()
        target.write_bytes(b'x' * 4097)
        self.assertEqual(self.inspect().result, 'file_not_matching')

    def test_old_matching_content_without_receipt_stays_unproven(self):
        self.mutate("DELETE FROM observations WHERE kind='filesystem.write_receipt'")
        report = self.inspect()
        self.assertEqual(report.result, 'receipt_missing')
        self.assertIsNone(report.receipt_sha256)
        self.assertTrue(report.checkpoint.criterion_observed)

    def test_receipt_binding_and_duplicate_rows_fail_closed(self):
        original = self.receipt()
        for key in ('run_ref', 'action_ref', 'workspace_sha256', 'envelope_sha256'):
            with self.subTest(key=key):
                self.mutate("UPDATE observations SET payload_json=? WHERE kind='filesystem.write_receipt'",
                            (canonical({**original, key: 'a' * 64}),))
                with self.assertRaisesRegex(ValueError, 'write_receipt_binding_invalid'):
                    self.inspect()
        self.mutate("UPDATE observations SET payload_json=? WHERE kind='filesystem.write_receipt'", (canonical(original),))
        self.mutate("""INSERT INTO observations SELECT 'synthetic-duplicate',run_id,step_id,action_id,kind,payload_json,created_at
            FROM observations WHERE kind='filesystem.write_receipt'""")
        with self.assertRaisesRegex(ValueError, 'write_receipt_ambiguous'):
            self.inspect()

    def test_action_and_historical_execution_state_binding_are_checked(self):
        self.mutate("UPDATE actions SET actual_option='ask_human' WHERE action_id=?", (self.action_id,))
        with self.assertRaisesRegex(ValueError, 'write_action_binding_invalid'):
            self.inspect()
        self.mutate("UPDATE actions SET actual_option='write_file' WHERE action_id=?", (self.action_id,))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            envelope = json.loads(connection.execute('SELECT envelope_json FROM action_envelopes WHERE action_id=?', (self.action_id,)).fetchone()[0])
            row = connection.execute('SELECT snapshot_id,state_json FROM state_snapshots WHERE state_version=?', (envelope['state_version'],)).fetchone()
            state = {**json.loads(row[1]), 'state_version': envelope['state_version'] + 1}
            connection.execute('UPDATE state_snapshots SET state_json=?,content_sha256=? WHERE snapshot_id=?',
                               (canonical(state), digest(state), row[0]))
        with self.assertRaisesRegex(ValueError, 'write_execution_state_invalid'):
            self.inspect()

    def test_changed_snapshot_between_two_reads_is_rejected(self):
        from aos.recovery_checkpoint import inspect_checkpoint

        def changed(*arguments, **keywords):
            report = inspect_checkpoint(*arguments, **keywords)
            self.mutate("UPDATE runs SET training_eligible=1")
            return report

        with patch('aos.recovery_effect.inspect_checkpoint', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'write_snapshot_changed'):
                self.inspect()

    def test_active_writer_workspace_lock_and_foreign_action_are_denied(self):
        runtime = WorkspaceRuntime(self.workspace)
        runtime.start_existing()
        try:
            with self.assertRaises(BlockingIOError):
                self.inspect()
        finally:
            runtime.stop()
        with self.assertRaisesRegex(ValueError, 'write_action_missing'):
            inspect_write_effect(self.database, self.workspace, self.run_id, 'not-this-action', digest(FixtureDecisionEngine.identity))

    def test_receipt_audit_failure_leaves_effect_without_a_success_claim(self):
        database, workspace = self.root / 'audit.sqlite', self.root / 'audit-workspace'
        store = TrajectoryStore(database)
        runtime = WorkspaceRuntime(workspace)
        runtime.start()
        original = store.insert

        def fail_receipt(table, **values):
            if table == 'observations' and values.get('kind') == 'filesystem.write_receipt':
                raise sqlite3.OperationalError('synthetic receipt audit failure')
            return original(table, **values)

        try:
            with patch.object(store, 'insert', side_effect=fail_receipt):
                with self.assertRaises(sqlite3.OperationalError):
                    asyncio.run(Operator(Settings(database=database, workspace=workspace), store, runtime, FixtureDecisionEngine()).hello())
            self.assertEqual(store.connection.execute('SELECT status FROM actions').fetchone()[0], 'running')
            self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
            self.assertEqual((workspace / 'hello.txt').read_text(), HELLO_CONTENT)
        finally:
            runtime.stop()
            store.close()

    def test_replacement_before_fd_witness_cannot_get_a_receipt(self):
        from aos.write_receipts import witness_file

        database, workspace = self.root / 'race.sqlite', self.root / 'race-workspace'
        store = TrajectoryStore(database)
        runtime = WorkspaceRuntime(workspace)
        runtime.start()

        def replace_file(directory, descriptor):
            target = workspace / 'hello.txt'
            target.rename(workspace / 'original')
            target.write_text(HELLO_CONTENT)
            return witness_file(directory, descriptor)

        try:
            with patch('aos.computer.witness_file', side_effect=replace_file):
                result = asyncio.run(Operator(Settings(database=database, workspace=workspace), store, runtime, FixtureDecisionEngine()).hello())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(store.connection.execute('SELECT status FROM actions').fetchone()[0], 'uncertain')
            self.assertEqual(store.connection.execute("SELECT count(*) FROM observations WHERE kind='filesystem.write_receipt'").fetchone()[0], 0)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
        finally:
            runtime.stop()
            store.close()

    def test_actual_crashes_on_both_sides_of_receipt_commit_never_replay(self):
        for stage in ('before_write', 'after_write', 'before_receipt', 'after_receipt', 'after_result'):
            with self.subTest(stage=stage):
                database, workspace = self.root / f'{stage}.sqlite', self.root / stage
                result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(database), str(workspace), stage],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 73, result.stderr)
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                    run_id, action_id = connection.execute('SELECT run_id,action_id FROM actions').fetchone()
                paths = [path for path in (database, Path(str(database) + '-wal')) if path.exists()]
                before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
                report = inspect_write_effect(database, workspace, run_id, action_id, digest(FixtureDecisionEngine.identity))
                expected = 'recorded_effect_matches' if stage in {'after_receipt', 'after_result'} else 'receipt_missing'
                self.assertEqual(report.result, expected)
                self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
                store = TrajectoryStore(database)
                runtime = WorkspaceRuntime(workspace)
                runtime.start_existing()
                try:
                    self.assertEqual(store.connection.execute('SELECT status FROM actions').fetchone()[0],
                                     'ok' if stage == 'after_result' else 'uncertain')
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)

                    async def never_approve(action):
                        self.fail('A prior action cannot reach the pristine resume approval gate')

                    with self.assertRaises(AOSFault):
                        asyncio.run(resume_hello(Settings(database=database, workspace=workspace), store, runtime,
                                                FixtureDecisionEngine(), run_id, never_approve))
                finally:
                    runtime.stop()
                    store.close()
                self.assertEqual(inspect_write_effect(database, workspace, run_id, action_id,
                                                      digest(FixtureDecisionEngine.identity)).result, expected)
                self.assertEqual((workspace / 'hello.txt').exists(), stage != 'before_write')

    def test_schema_fixture_and_false_authority_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/write_receipt.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model, value in (('write_receipt', WriteReceipt, self.receipt()), ('write_effect', WriteEffectReport, self.inspect().model_dump())):
            self.assertEqual({'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()},
                             json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text()))
            validator(name).validate(value)
            validator(name).validate(fixture['receipt' if name == 'write_receipt' else 'report'])
        report = self.inspect().model_dump()
        for key in ('execution_authorized', 'resume_authorized', 'automatic_replay_allowed', 'uncertain_effect_resolved'):
            self.assertFalse(validator('write_effect').is_valid({**report, key: True}))
        with self.assertRaises(ValueError):
            WriteEffectReport.model_validate({**report, 'receipt_sha256': None})
        with self.assertRaises(ValueError):
            WriteEffectReport.model_validate({**report, 'checkpoint': {**report['checkpoint'], 'effect': 'absent', 'criterion_observed': False}})

    def test_cli_generic_error_and_minimized_success(self):
        command = [sys.executable, '-m', 'aos.recovery_effect', '--database', str(self.database), '--workspace', str(self.workspace),
                   '--run-id', self.run_id, '--action-id', self.action_id, '--deployment-sha256', digest(FixtureDecisionEngine.identity)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'], 'recorded_effect_matches')
        command[-1] = 'private-invalid'
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('private-invalid', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


@unittest.skipUnless(os.environ.get('AOS_REAL_TASK_TESTS') == '1', 'Real Decider requires AOS_REAL_TASK_TESTS=1')
class RealWriteEffectTests(unittest.TestCase):
    def test_real_decider_crash_after_durable_receipt_keeps_action_uncertain(self):
        from aos.decision import DeciderEngine

        root = Path(tempfile.mkdtemp(prefix='effect-real-', dir=REPO_ROOT / 'data'))
        database, workspace = root / 'store.sqlite', root / 'workspace'
        manifest = REPO_ROOT / 'models/decider-manifest.json'
        python = Path(os.environ['AOS_MODEL_PYTHON']).absolute()
        result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(database), str(workspace), 'after_receipt', str(manifest), str(python)],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 73, result.stderr)
        store = TrajectoryStore(database)
        try:
            run_id, action_id = store.connection.execute('SELECT run_id,action_id FROM actions').fetchone()
            report = inspect_write_effect(database, workspace, run_id, action_id, digest(DeciderEngine(manifest, python).identity))
            self.assertEqual(report.result, 'recorded_effect_matches')
            self.assertEqual(report.action_status, 'uncertain')
            self.assertFalse(report.uncertain_effect_resolved)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 1)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
            self.assertEqual(store.connection.execute('SELECT training_eligible FROM runs').fetchone()[0], 0)
            print(canonical({'evidence_directory': str(root), 'real_decider_write_effect': report.model_dump()}))
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import pty
import re
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aos.computer import ComputerGateway, WorkspaceRuntime
from aos.contracts import AOSFault, Action, HELLO_CONTENT, REPO_ROOT, Settings, canonical, digest
from aos.dataset import validator
from aos.decision import FixtureDecisionEngine
from aos.recovery_effect import inspect_write_effect
from aos.recovery_reconcile import WriteReconciliation, WriteReconciliationRequest, reconcile_write
from aos.recovery_resume import resume_hello
from aos.storage import TrajectoryStore
from test_recovery_effect import CRASH_SCRIPT


async def approve(request):
    return True


class WriteReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database, self.workspace = self.root / 'store.sqlite', self.root / 'workspace'
        self.crash(self.database, self.workspace, 'after_receipt')
        self.store = TrajectoryStore(self.database)
        self.addCleanup(self.store.close)
        self.runtime = WorkspaceRuntime(self.workspace)
        self.runtime.start_existing()
        self.addCleanup(self.runtime.stop)
        self.run_id, self.action_id = self.store.connection.execute('SELECT run_id,action_id FROM actions').fetchone()
        self.deployment = digest(FixtureDecisionEngine.identity)

    def crash(self, database, workspace, stage):
        result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(database), str(workspace), stage],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)

    async def reconcile(self, confirmation=approve):
        return await reconcile_write(self.store, self.runtime, self.run_id, self.action_id, self.deployment,
                                     confirmation, actor='acceptance_test')

    def assert_pending(self):
        self.assertEqual(self.store.connection.execute('SELECT status FROM actions').fetchone()[0], 'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 0)

    async def test_commit_is_durable_without_run_success_or_fresh_authority(self):
        state = self.store.state(self.run_id)
        original = (self.workspace / 'hello.txt').stat()
        receipt = await self.reconcile()
        self.assertEqual(receipt.request_sha256, digest(receipt.request.model_dump()))
        action = self.store.connection.execute('SELECT * FROM actions').fetchone()
        self.assertEqual(action['status'], 'ok')
        self.assertIsNone(action['error_code'])
        self.assertEqual(json.loads(action['result_json']), {'bytes_written': 28})
        self.assertEqual(self.store.state(self.run_id), state)
        self.assertEqual(tuple(self.store.connection.execute('SELECT status,outcome,training_eligible FROM runs').fetchone()),
                         ('paused', 'unknown', 0))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
        self.assertEqual((self.workspace / 'hello.txt').stat().st_mtime_ns, original.st_mtime_ns)
        saved = self.store.connection.execute('SELECT payload_json,actor,kind FROM human_interventions').fetchone()
        self.assertEqual(WriteReconciliation.model_validate_json(saved[0]), receipt)
        self.assertEqual(tuple(saved)[1:], ('acceptance_test', 'correction'))
        inspection = inspect_write_effect(self.database, self.workspace, self.run_id, self.action_id, self.deployment,
                                          workspace_descriptor=self.runtime.descriptor)
        self.assertEqual(inspection.result, 'recorded_effect_matches')
        self.assertEqual(inspection.action_status, 'ok')
        self.assertFalse(inspection.uncertain_effect_resolved)
        for private in (str(self.workspace), self.run_id, self.action_id, 'owner_uid', 'modified_ns'):
            self.assertNotIn(private, receipt.model_dump_json())

        async def forbidden(action):
            self.fail('Reconciliation cannot authorize resumed execution')

        with self.assertRaises(AOSFault):
            await resume_hello(Settings(database=self.database, workspace=self.workspace), self.store,
                               self.runtime, FixtureDecisionEngine(), self.run_id, forbidden)
        envelope = Action.model_validate_json(self.store.connection.execute('SELECT envelope_json FROM action_envelopes').fetchone()[0])
        with patch.object(self.runtime, 'write', side_effect=AssertionError('Never replay')):
            cached = ComputerGateway(self.store, self.runtime).execute(envelope, action['decision_id'])
        self.assertEqual(cached, {'bytes_written': 28})
        with self.assertRaises(ValueError):
            await self.reconcile(forbidden)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 1)

    async def test_rejection_nonboolean_missing_gate_and_mutated_confirmation(self):
        for response in (False, None, 1, 'yes'):
            async def reject(request):
                return response
            with self.assertRaises(ValueError):
                await self.reconcile(reject)
            self.assert_pending()
        with self.assertRaises(ValueError):
            await self.reconcile(None)

        async def mutate(request):
            request.action_ref = 'a' * 64
            return True

        with self.assertRaises(ValueError):
            await self.reconcile(mutate)
        self.assert_pending()

    async def test_expiry_and_cancellation_do_not_record_a_result(self):
        clock = time.time()

        async def expired(request):
            time_patch.return_value = clock + 61
            return True

        with patch('aos.recovery_reconcile.time.time', return_value=clock) as time_patch:
            with self.assertRaises(ValueError):
                await self.reconcile(expired)
        self.assert_pending()

        async def cancelled(request):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.reconcile(cancelled)
        self.assert_pending()

    async def test_file_rewrite_during_approval_is_rejected(self):
        async def rewrite(request):
            (self.workspace / 'hello.txt').write_text(HELLO_CONTENT)
            return True

        with self.assertRaises(ValueError):
            await self.reconcile(rewrite)
        self.assert_pending()

    async def test_database_change_during_approval_is_rejected(self):
        async def change(request):
            with self.store.connection:
                self.store.connection.execute("UPDATE tasks SET original_goal='synthetic change'")
            return True

        with self.assertRaises(ValueError):
            await self.reconcile(change)
        self.assert_pending()

    async def test_workspace_replacement_during_approval_is_rejected(self):
        async def replace(request):
            self.workspace.rename(self.root / 'original')
            self.workspace.mkdir()
            (self.workspace / 'hello.txt').write_text(HELLO_CONTENT)
            return True

        with self.assertRaises(ValueError):
            await self.reconcile(replace)
        self.assert_pending()

    async def test_audit_failure_rolls_back_action_result(self):
        with patch.object(self.store, 'insert', side_effect=sqlite3.OperationalError('synthetic audit failure')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.reconcile()
        self.assert_pending()
        self.assertFalse(self.store.connection.in_transaction)

    async def test_missing_wrong_and_duplicate_receipts_fail_before_approval(self):
        original = dict(self.store.connection.execute("SELECT * FROM observations WHERE kind='filesystem.write_receipt'").fetchone())
        async def forbidden(request):
            self.fail('Invalid evidence must not prompt')

        for mode in ('missing', 'wrong', 'duplicate'):
            with self.subTest(mode=mode):
                with self.store.connection:
                    self.store.connection.execute("DELETE FROM observations WHERE kind='filesystem.write_receipt'")
                    if mode != 'missing':
                        values = dict(original)
                        if mode == 'wrong':
                            payload = json.loads(values['payload_json'])
                            payload['action_ref'] = 'a' * 64
                            values['payload_json'] = canonical(payload)
                        self.store.insert('observations', **values)
                        if mode == 'duplicate':
                            self.store.insert('observations', **{**values, 'observation_id': 'synthetic-duplicate'})
                with self.assertRaises(ValueError):
                    await self.reconcile(forbidden)
                self.assert_pending()

    async def test_conflicting_status_deployment_and_runtime_are_rejected(self):
        original_deployment = self.deployment
        self.deployment = 'a' * 64
        with self.assertRaises(ValueError):
            await self.reconcile()
        self.deployment = original_deployment
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='cancelled'")
        with self.assertRaises(ValueError):
            await self.reconcile()
        self.assert_pending()
        self.runtime.stop()
        with self.assertRaises(ValueError):
            await self.reconcile()

    async def test_concurrent_confirmations_can_only_commit_once(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wait(request):
            entered.set()
            await release.wait()
            return True

        pending = asyncio.create_task(self.reconcile(wait))
        await entered.wait()
        try:
            await self.reconcile()
        finally:
            release.set()
        with self.assertRaises(ValueError):
            await pending
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 1)

    async def test_schemas_and_synthetic_fixture(self):
        receipt = await self.reconcile()
        fixture = json.loads((REPO_ROOT / 'examples/write_reconciliation.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model, value in (('write_reconciliation', WriteReconciliation, receipt.model_dump()),
                                   ('write_reconciliation_request', WriteReconciliationRequest, receipt.request.model_dump())):
            self.assertEqual({'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()},
                             json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text()))
            validator(name).validate(value)
            for field in ('execution_authorized', 'resume_authorized', 'automatic_replay_allowed'):
                self.assertFalse(validator(name).is_valid({**value, field: True}))
        WriteReconciliation.model_validate(fixture['receipt'])
        with self.assertRaises(ValueError):
            WriteReconciliation.model_validate({**receipt.model_dump(), 'request_sha256': 'a' * 64})

    async def test_no_receipt_crash_windows_cannot_be_resolved(self):
        for stage in ('before_write', 'after_write', 'before_receipt', 'after_result'):
            database, workspace = self.root / f'{stage}.sqlite', self.root / stage
            self.crash(database, workspace, stage)
            store = TrajectoryStore(database)
            runtime = WorkspaceRuntime(workspace)
            runtime.start_existing()
            try:
                run_id, action_id = store.connection.execute('SELECT run_id,action_id FROM actions').fetchone()
                with self.assertRaises(ValueError):
                    await reconcile_write(store, runtime, run_id, action_id, self.deployment, approve, actor='acceptance_test')
                self.assertEqual(store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 0)
            finally:
                runtime.stop()
                store.close()

    async def test_process_crash_before_and_after_commit_is_atomic(self):
        script = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import WorkspaceRuntime
from aos.contracts import digest
from aos.decision import FixtureDecisionEngine
from aos.recovery_reconcile import reconcile_write
from aos.storage import TrajectoryStore
store = TrajectoryStore(Path(sys.argv[1]))
runtime = WorkspaceRuntime(Path(sys.argv[2]))
runtime.start_existing()
run_id, action_id = store.connection.execute('SELECT run_id,action_id FROM actions').fetchone()
original = store.insert
def crash_insert(table, **values):
    original(table, **values)
    os._exit(74)
if sys.argv[3] == 'before_commit':
    store.insert = crash_insert
async def approve(request):
    return True
asyncio.run(reconcile_write(store, runtime, run_id, action_id, digest(FixtureDecisionEngine.identity), approve, actor='acceptance_test'))
os._exit(74)
'''
        for stage in ('before_commit', 'after_commit'):
            database, workspace = self.root / f'{stage}.sqlite', self.root / stage
            self.crash(database, workspace, 'after_receipt')
            result = subprocess.run([sys.executable, '-c', script, str(database), str(workspace), stage],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 74, result.stderr)
            store = TrajectoryStore(database)
            try:
                self.assertEqual(store.connection.execute('SELECT status FROM actions').fetchone()[0],
                                 'ok' if stage == 'after_commit' else 'uncertain')
                self.assertEqual(store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0],
                                 1 if stage == 'after_commit' else 0)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
                self.assertEqual(store.connection.execute('SELECT status FROM runs').fetchone()[0], 'paused')
            finally:
                store.close()

    async def test_cli_requires_tty_without_creating_database(self):
        database = self.root / 'missing.sqlite'
        result = subprocess.run([sys.executable, '-m', 'aos.recovery_reconcile', '--database', str(database),
                                 '--workspace', str(self.workspace), '--run-id', self.run_id, '--action-id', self.action_id,
                                 '--deployment-sha256', self.deployment], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(database.exists())
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(result.stdout, '')

    async def test_cli_exact_request_pty_accept_and_reject(self):
        for answer in ('reject', 'eof', 'oversize', 'wrong_digest', 'accept'):
            accepted = answer == 'accept'
            database, workspace = self.root / f'pty-{answer}.sqlite', self.root / f'pty-{answer}'
            self.crash(database, workspace, 'after_receipt')
            with closing(sqlite3.connect(database)) as connection:
                run_id, action_id = connection.execute('SELECT run_id,action_id FROM actions').fetchone()
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, '-m', 'aos.recovery_reconcile', '--database', str(database),
                                        '--workspace', str(workspace), '--run-id', run_id, '--action-id', action_id,
                                        '--deployment-sha256', self.deployment], stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = bytearray()
            try:
                deadline = time.monotonic() + 10
                matched = None
                while time.monotonic() < deadline and matched is None:
                    if select.select([master], [], [], 0.1)[0]:
                        output.extend(os.read(master, 8192))
                        matched = re.search(rb'RECONCILE ([a-f0-9]{64})', output)
                self.assertIsNotNone(matched, output)
                responses = {'reject': b'NO\n', 'eof': b'\x04',
                             'oversize': b'RECONCILE ' + matched.group(1) + b' ' * 256 + b'\n',
                             'wrong_digest': b'RECONCILE ' + b'0' * 64 + b'\n',
                             'accept': b'RECONCILE ' + matched.group(1) + b'\n'}
                os.write(master, responses[answer])
                self.assertEqual(process.wait(timeout=10), 0 if accepted else 1, output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute('SELECT status FROM actions').fetchone()[0], 'ok' if accepted else 'uncertain')
                self.assertEqual(connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 1 if accepted else 0)


@unittest.skipUnless(os.environ.get('AOS_REAL_TASK_TESTS') == '1', 'Real Decider requires AOS_REAL_TASK_TESTS=1')
class RealWriteReconciliationTests(unittest.TestCase):
    def test_real_decider_receipt_crash_explicit_resolution_without_resume(self):
        from aos.decision import DeciderEngine

        root = Path(tempfile.mkdtemp(prefix='reconcile-real-', dir=REPO_ROOT / 'data'))
        database, workspace = root / 'store.sqlite', root / 'workspace'
        manifest = REPO_ROOT / 'models/decider-manifest.json'
        python = Path(os.environ['AOS_MODEL_PYTHON']).absolute()
        result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(database), str(workspace), 'after_receipt', str(manifest), str(python)],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 73, result.stderr)
        store = TrajectoryStore(database)
        runtime = WorkspaceRuntime(workspace)
        runtime.start_existing()
        try:
            run_id, action_id = store.connection.execute('SELECT run_id,action_id FROM actions').fetchone()
            receipt = asyncio.run(reconcile_write(store, runtime, run_id, action_id, digest(DeciderEngine(manifest, python).identity),
                                                   approve, actor='acceptance_test'))
            self.assertEqual(store.connection.execute('SELECT status FROM actions').fetchone()[0], 'ok')
            self.assertEqual(tuple(store.connection.execute('SELECT status,outcome,training_eligible FROM runs').fetchone()),
                             ('paused', 'unknown', 0))
            self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 1)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
            print(canonical({'evidence_directory': str(root), 'real_decider_reconciliation': receipt.model_dump()}))
        finally:
            runtime.stop()
            store.close()


if __name__ == '__main__':
    unittest.main()

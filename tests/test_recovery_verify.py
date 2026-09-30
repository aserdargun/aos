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
from aos.contracts import AOSFault, HELLO_CONTENT, Phase, REPO_ROOT, Settings, canonical, digest
from aos.dataset import validator
from aos.decision import DeciderEngine, FixtureDecisionEngine
from aos.export import export_run
from aos.recovery_reconcile import reconcile_write
from aos.recovery_verify import VerificationAdmission, verify_reconciled_hello
from aos.storage import TrajectoryStore
from test_recovery_effect import CRASH_SCRIPT


async def accept(request):
    return True


async def prepare(root, engine, real=False):
    settings = Settings(database=root / 'store.sqlite', workspace=root / 'workspace')
    command = [sys.executable, '-c', CRASH_SCRIPT, str(settings.database), str(settings.workspace), 'after_receipt']
    if real:
        command.extend([str(engine.manifest), str(engine.python)])
    crash = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=180 if real else 10)
    if crash.returncode != 73:
        raise AssertionError(crash.stderr)
    store = TrajectoryStore(settings.database)
    runtime = WorkspaceRuntime(settings.workspace)
    try:
        runtime.start_existing()
        run_id, action_id = store.connection.execute('SELECT run_id,action_id FROM actions').fetchone()
        receipt = await reconcile_write(store, runtime, run_id, action_id, digest(engine.identity), accept, actor='acceptance_test')
        return settings, run_id, action_id, receipt.reconciliation_id
    finally:
        runtime.stop()
        store.close()


class RecoveryVerifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = FixtureDecisionEngine()
        self.settings, self.run_id, self.action_id, self.reconciliation_id = await prepare(self.root, self.engine)
        self.store = TrajectoryStore(self.settings.database)
        self.addCleanup(self.store.close)
        self.runtime = WorkspaceRuntime(self.settings.workspace)
        self.runtime.start_existing()
        self.addCleanup(self.runtime.stop)
        self.offers = []
        self.old_state = self.store.state(self.run_id)

    async def approve(self, action):
        self.offers.append(action)
        self.assertEqual(action.tool, 'filesystem.read')
        self.assertNotEqual(action.owner_lease_id, self.old_state.owner_lease_id)
        return True

    async def verify(self, approval=None):
        return await verify_reconciled_hello(self.settings, self.store, self.runtime, self.engine, self.run_id,
                                             self.action_id, self.reconciliation_id, approval or self.approve, actor='acceptance_test')

    def assert_no_verification(self, reads=0):
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 1 + reads)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='filesystem.write'").fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT training_eligible FROM runs').fetchone()[0], 0)

    async def test_reconciled_write_finishes_with_two_fresh_approved_reads_without_replay(self):
        original = dict(self.store.connection.execute('SELECT * FROM actions WHERE action_id=?', (self.action_id,)).fetchone())
        metadata = (self.settings.workspace / 'hello.txt').stat()
        with patch.object(self.runtime, 'write', side_effect=AssertionError('Never replay write')):
            result = await self.verify()
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['verified'])
        self.assertEqual(result['run_id'], self.run_id)
        self.assertFalse(result['write_replayed'])
        self.assertFalse(result['automatic_replay_allowed'])
        self.assertEqual(len(self.offers), 2)
        self.assertNotEqual(self.offers[0].action_id, self.offers[1].action_id)
        self.assertEqual(dict(self.store.connection.execute('SELECT * FROM actions WHERE action_id=?', (self.action_id,)).fetchone()), original)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='filesystem.read' AND status='ok'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM decisions').fetchone()[0], 2)
        self.assertEqual(self.store.state(self.run_id).owner, 'PAUSED')
        self.assertNotEqual(self.store.state(self.run_id).owner_lease_id, self.offers[0].owner_lease_id)
        self.assertEqual((self.settings.workspace / 'hello.txt').stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertEqual((self.settings.workspace / 'hello.txt').stat().st_ino, metadata.st_ino)
        approvals = self.store.connection.execute("SELECT payload_json FROM human_interventions WHERE kind='approve'").fetchall()
        self.assertEqual([json.loads(row[0])['action_sha256'] for row in approvals], [digest(action.model_dump()) for action in self.offers])
        validator('trajectory_export').validate(export_run(self.store, self.run_id))
        self.assertEqual(self.store.connection.execute('SELECT training_eligible FROM runs').fetchone()[0], 0)

    async def test_first_rejection_never_reads_or_reuses_admission(self):
        async def reject(action):
            return False

        self.assertEqual((await self.verify(reject))['status'], 'failed')
        self.assert_no_verification()
        self.assertEqual(self.store.state(self.run_id).owner, 'PAUSED')
        with self.assertRaises((AOSFault, ValueError)):
            await self.verify()

    async def test_second_rejection_preserves_only_first_read(self):
        async def reject_second(action):
            self.offers.append(action)
            return len(self.offers) == 1

        self.assertEqual((await self.verify(reject_second))['status'], 'failed')
        self.assert_no_verification(reads=1)
        self.assertEqual(len(self.offers), 2)

    async def test_cancelled_approval_revokes_lease_without_read(self):
        waiting = asyncio.Event()

        async def wait(action):
            waiting.set()
            await asyncio.Future()

        task = asyncio.create_task(self.verify(wait))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.state(self.run_id).owner, 'PAUSED')
        self.assert_no_verification()

    async def test_modified_approval_cannot_authorize_read(self):
        async def mutate(action):
            action.arguments['path'] = '/workspace/private'
            return True

        self.assertEqual((await self.verify(mutate))['status'], 'failed')
        self.assert_no_verification()

    async def test_expired_approval_cannot_authorize_read(self):
        async def expire(action):
            clock = patch('aos.recovery_verify.time.time', return_value=action.deadline + 1)
            clock.start()
            self.addCleanup(clock.stop)
            return True

        self.assertEqual((await self.verify(expire))['status'], 'failed')
        self.assert_no_verification()

    async def test_missing_file_cannot_be_recreated(self):
        (self.settings.workspace / 'hello.txt').unlink()
        with patch.object(self.runtime, 'write', side_effect=AssertionError('Never recreate')):
            with self.assertRaises(ValueError):
                await self.verify()
        self.assert_no_verification()
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())

    async def test_equal_content_new_inode_is_rejected(self):
        target = self.settings.workspace / 'hello.txt'
        target.rename(self.settings.workspace / 'original')
        target.write_text(HELLO_CONTENT)
        with self.assertRaises(ValueError):
            await self.verify()
        self.assert_no_verification()

    async def test_file_removed_after_admission_never_becomes_a_write_option(self):
        original_read = self.runtime.read

        def remove_before_observation(path):
            (self.settings.workspace / 'hello.txt').unlink()
            return original_read(path)

        with patch.object(self.runtime, 'read', side_effect=remove_before_observation), \
                patch.object(self.runtime, 'write', side_effect=AssertionError('Never recreate')):
            self.assertEqual((await self.verify())['status'], 'failed')
        self.assert_no_verification()
        self.assertEqual(self.offers, [])
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())

    async def test_rewrite_during_approval_is_rejected(self):
        async def rewrite(action):
            (self.settings.workspace / 'hello.txt').write_text(HELLO_CONTENT)
            return True

        self.assertEqual((await self.verify(rewrite))['status'], 'failed')
        self.assert_no_verification()

    async def test_file_replaced_after_independent_read_cannot_claim_success(self):
        original_read = self.runtime.read
        reads = 0

        def replace_after_read(path):
            nonlocal reads
            content = original_read(path)
            reads += 1
            if reads == 3:
                target = self.settings.workspace / 'hello.txt'
                target.rename(self.settings.workspace / 'original')
                target.write_text(HELLO_CONTENT)
            return content

        with patch.object(self.runtime, 'read', side_effect=replace_after_read):
            self.assertEqual((await self.verify())['status'], 'failed')
        self.assert_no_verification(reads=2)

    async def test_missing_duplicate_or_wrong_correction_cannot_admit(self):
        original = dict(self.store.connection.execute("SELECT * FROM human_interventions WHERE kind='correction'").fetchone())
        for mode in ('missing', 'duplicate', 'wrong'):
            with self.subTest(mode=mode):
                with self.store.connection:
                    self.store.connection.execute("DELETE FROM human_interventions WHERE kind='correction'")
                    if mode != 'missing':
                        values = dict(original)
                        if mode == 'wrong':
                            payload = json.loads(values['payload_json'])
                            payload['request_sha256'] = 'a' * 64
                            values['payload_json'] = canonical(payload)
                        self.store.insert('human_interventions', **values)
                        if mode == 'duplicate':
                            self.store.insert('human_interventions', **{**values, 'intervention_id': 'synthetic-duplicate'})
                with self.assertRaises(ValueError):
                    await self.verify()
                self.assert_no_verification()

    async def test_deployment_change_during_approval_is_rejected(self):
        async def change(action):
            self.engine.identity = {**self.engine.identity, 'deployment_id': 'synthetic-changed'}
            return True

        self.assertEqual((await self.verify(change))['status'], 'failed')
        self.assert_no_verification()

    async def test_workspace_replaced_during_approval_is_rejected(self):
        async def change(action):
            self.settings.workspace.rename(self.root / 'original-workspace')
            self.settings.workspace.mkdir()
            (self.settings.workspace / 'hello.txt').write_text(HELLO_CONTENT)
            return True

        self.assertEqual((await self.verify(change))['status'], 'failed')
        self.assert_no_verification()

    async def test_deleted_first_approval_prevents_second_read(self):
        async def remove_approval(action):
            self.offers.append(action)
            if len(self.offers) == 2:
                with self.store.connection:
                    self.store.connection.execute("DELETE FROM human_interventions WHERE kind='approve'")
            return True

        self.assertEqual((await self.verify(remove_approval))['status'], 'failed')
        self.assert_no_verification(reads=1)

    async def test_correction_changed_after_admission_prevents_read(self):
        async def change_correction(action):
            with self.store.connection:
                self.store.connection.execute("UPDATE human_interventions SET actor='local_terminal' WHERE kind='correction'")
            return True

        self.assertEqual((await self.verify(change_correction))['status'], 'failed')
        self.assert_no_verification()

    async def test_takeover_during_approval_preserves_new_owner_and_never_reads(self):
        async def takeover(action):
            current = self.store.state(self.run_id)
            taken = current.advance(Phase.CANCELLED, owner='HUMAN', owner_lease_id='synthetic-human-lease')
            self.store.save_state(current, taken)
            self.store.finish(taken, 'cancelled', 'unknown')
            return True

        with self.assertRaises(AOSFault):
            await self.verify(takeover)
        self.assert_no_verification()
        self.assertEqual(self.store.state(self.run_id).owner, 'HUMAN')
        self.assertEqual(self.store.state(self.run_id).owner_lease_id, 'synthetic-human-lease')
        self.assertEqual(self.store.state(self.run_id).phase, Phase.CANCELLED)

    async def test_write_is_not_a_model_option(self):
        async def malicious(state, options):
            self.assertNotIn('write_file', [option.id for option in options])
            return {'selected_option': 'write_file', 'probabilities': {'write_file': 1.0}}

        with patch.object(self.engine, 'decide', side_effect=malicious), patch.object(self.runtime, 'write', side_effect=AssertionError('No write')):
            self.assertEqual((await self.verify())['status'], 'failed')
        self.assert_no_verification()

    async def test_admission_audit_failure_preserves_paused_state(self):
        with patch.object(self.store, 'insert', side_effect=sqlite3.OperationalError('synthetic audit failure')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.verify()
        self.assertEqual(self.store.state(self.run_id), self.old_state)
        self.assert_no_verification()

    async def test_approval_audit_failure_revokes_lease_and_never_reads(self):
        original = self.store.insert

        def fail_approval(table, **values):
            if table == 'human_interventions' and values.get('kind') == 'approve':
                raise sqlite3.OperationalError('synthetic approval audit failure')
            return original(table, **values)

        with patch.object(self.store, 'insert', side_effect=fail_approval):
            with self.assertRaises(sqlite3.OperationalError):
                await self.verify()
        self.assertEqual(self.store.state(self.run_id).owner, 'PAUSED')
        self.assert_no_verification()

    async def test_concurrent_attempt_cannot_reuse_reconciliation(self):
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def wait(action):
            waiting.set()
            await release.wait()
            return True

        task = asyncio.create_task(self.verify(wait))
        await waiting.wait()
        try:
            with self.assertRaises((AOSFault, ValueError)):
                await self.verify()
        finally:
            release.set()
        self.assertEqual((await task)['status'], 'succeeded')

    async def test_schema_fixture_binds_read_only_admission(self):
        await self.verify()
        payload = json.loads(self.store.connection.execute("SELECT payload_json FROM human_interventions WHERE kind='resume'").fetchone()[0])
        fixture = json.loads((REPO_ROOT / 'examples/recovery_verify.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('recovery_verify').validate(payload)
        validator('recovery_verify').validate(fixture['admission'])
        VerificationAdmission.model_validate(payload)
        self.assertEqual({'$schema': 'https://json-schema.org/draft/2020-12/schema', **VerificationAdmission.model_json_schema()},
                         json.loads((REPO_ROOT / 'schemas/recovery_verify.schema.json').read_text()))
        for change in ({'write_authorized': True}, {'automatic_replay_allowed': True}, {'maximum_read_actions': 3},
                       {'allowed_tool': 'filesystem.write'}, {'requires_action_approval': False}, {'raw_path': str(self.root)}):
            self.assertFalse(validator('recovery_verify').is_valid({**payload, **change}))

    async def test_crash_after_admission_or_first_read_never_reuses_authority(self):
        script = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import ComputerGateway, WorkspaceRuntime
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine
from aos.recovery_verify import verify_reconciled_hello
from aos.storage import TrajectoryStore
settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
store = TrajectoryStore(settings.database)
runtime = WorkspaceRuntime(settings.workspace)
runtime.start_existing()
engine = FixtureDecisionEngine()
original = ComputerGateway.execute
def crash_read(self, action, decision_id):
    result = original(self, action, decision_id)
    os._exit(75)
async def crash_decide(state, options):
    os._exit(75)
if sys.argv[6] == 'first_read':
    ComputerGateway.execute = crash_read
else:
    engine.decide = crash_decide
async def approve(action):
    return True
asyncio.run(verify_reconciled_hello(settings, store, runtime, engine, sys.argv[3], sys.argv[4], sys.argv[5], approve, actor='acceptance_test'))
'''
        for stage in ('admission', 'first_read'):
            settings, run_id, action_id, reconciliation_id = await prepare(self.root / stage, FixtureDecisionEngine())
            result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', script, str(settings.database),
                                                               str(settings.workspace), run_id, action_id, reconciliation_id, stage],
                                             capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 75, result.stderr)
            store = TrajectoryStore(settings.database)
            runtime = WorkspaceRuntime(settings.workspace)
            runtime.start_existing()
            try:
                async def forbidden(action):
                    self.fail('Crash receipt cannot authorize another read')

                with self.assertRaises((AOSFault, ValueError)):
                    await verify_reconciled_hello(settings, store, runtime, FixtureDecisionEngine(), run_id, action_id,
                                                  reconciliation_id, forbidden, actor='acceptance_test')
                self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 2 if stage == 'first_read' else 1)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
                self.assertEqual(store.state(run_id).owner, 'PAUSED')
            finally:
                runtime.stop()
                store.close()

    async def test_cli_requires_tty_before_creating_database(self):
        database = self.root / 'missing.sqlite'
        result = subprocess.run([sys.executable, '-m', 'aos.recovery_verify', '--database', str(database),
                                 '--workspace', str(self.settings.workspace), '--run-id', self.run_id, '--action-id', self.action_id,
                                 '--reconciliation-id', self.reconciliation_id, '--engine', 'fixture'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(database.exists())

    async def test_cli_two_distinct_terminal_approvals(self):
        settings, run_id, action_id, reconciliation_id = await prepare(self.root / 'pty', FixtureDecisionEngine())
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, '-m', 'aos.recovery_verify', '--database', str(settings.database),
                                    '--workspace', str(settings.workspace), '--run-id', run_id, '--action-id', action_id,
                                    '--reconciliation-id', reconciliation_id, '--engine', 'fixture'],
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        approved = set()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        break
                    output.extend(chunk)
                    for action_hash in re.findall(rb'APPROVE ([a-f0-9]{64})', output):
                        if action_hash not in approved:
                            approved.add(action_hash)
                            os.write(master, b'APPROVE ' + action_hash + b'\n')
            self.assertEqual(process.wait(timeout=5), 0, output)
            self.assertEqual(len(approved), 2, output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        with closing(sqlite3.connect(settings.database)) as connection:
            self.assertEqual(connection.execute('SELECT status FROM runs').fetchone()[0], 'succeeded')
            self.assertEqual(connection.execute('SELECT count(*) FROM actions').fetchone()[0], 3)

    async def test_cli_rejects_eof_and_oversized_confirmation_without_reads(self):
        for answer in ('eof', 'oversize', 'wrong_digest'):
            settings, run_id, action_id, reconciliation_id = await prepare(self.root / answer, FixtureDecisionEngine())
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, '-m', 'aos.recovery_verify', '--database', str(settings.database),
                                        '--workspace', str(settings.workspace), '--run-id', run_id, '--action-id', action_id,
                                        '--reconciliation-id', reconciliation_id, '--engine', 'fixture'],
                                       stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = bytearray()
            try:
                deadline = time.monotonic() + 10
                matched = None
                while time.monotonic() < deadline and matched is None:
                    if select.select([master], [], [], 0.1)[0]:
                        output.extend(os.read(master, 8192))
                        matched = re.search(rb'APPROVE ([a-f0-9]{64})', output)
                self.assertIsNotNone(matched, output)
                responses = {'eof': b'\x04', 'oversize': b'APPROVE ' + matched.group(1) + b' ' * 256 + b'\n',
                             'wrong_digest': b'APPROVE ' + b'0' * 64 + b'\n'}
                os.write(master, responses[answer])
                self.assertEqual(process.wait(timeout=5), 1, output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
            with closing(sqlite3.connect(settings.database)) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM actions').fetchone()[0], 1)
                self.assertEqual(connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)


@unittest.skipUnless(os.environ.get('AOS_REAL_TASK_TESTS') == '1', 'Real Decider requires AOS_REAL_TASK_TESTS=1')
class RealRecoveryVerifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_decider_crash_reconcile_fresh_reads_and_verified_success(self):
        root = Path(tempfile.mkdtemp(prefix='verify-real-', dir=REPO_ROOT / 'data'))
        engine = DeciderEngine(REPO_ROOT / 'models/decider-manifest.json', Path(os.environ['AOS_MODEL_PYTHON']).absolute())
        settings, run_id, action_id, reconciliation_id = await prepare(root, engine, real=True)
        store = TrajectoryStore(settings.database)
        runtime = WorkspaceRuntime(settings.workspace)
        runtime.start_existing()
        try:
            with patch.object(runtime, 'write', side_effect=AssertionError('No repeated write')):
                result = await verify_reconciled_hello(settings, store, runtime, engine, run_id, action_id,
                                                       reconciliation_id, accept, actor='acceptance_test')
            self.assertEqual(result['status'], 'succeeded')
            self.assertTrue(result['verified'])
            self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 2)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls WHERE role='system2'").fetchone()[0], 0)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 3)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 1)
            self.assertEqual(store.connection.execute('SELECT training_eligible FROM runs').fetchone()[0], 0)
            print(canonical({'evidence_directory': str(root), 'real_decider_verification': result}))
        finally:
            runtime.stop()
            store.close()


if __name__ == '__main__':
    unittest.main()

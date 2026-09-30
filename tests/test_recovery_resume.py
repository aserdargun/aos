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

from aos.computer import WorkspaceRuntime
from aos.contracts import AOSFault, HELLO_CONTENT, REPO_ROOT, Settings, canonical, digest, now
from aos.dataset import validator
from aos.decision import DeciderEngine, FixtureDecisionEngine
from aos.recovery_resume import ResumeAdmission, ResumeApproval, resume_hello
from aos.storage import TrajectoryStore


CRASH_SCRIPT = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import WorkspaceRuntime
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine, DeciderEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore
class CrashRuntime(WorkspaceRuntime):
    def write(self, path, content):
        if sys.argv[3] == 'after_write':
            super().write(path, content)
        os._exit(73)
async def crash_gate(action):
    os._exit(73)
settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
store = TrajectoryStore(settings.database)
runtime = CrashRuntime(settings.workspace)
runtime.start()
engine = FixtureDecisionEngine() if len(sys.argv) == 4 else DeciderEngine(Path(sys.argv[4]), Path(sys.argv[5]))
asyncio.run(Operator(settings, store, runtime, engine).hello(execution_gate=crash_gate if sys.argv[3] == 'approval' else None))
'''


class RecoveryResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(database=self.root / 'source.sqlite', workspace=self.root / 'workspace')
        await asyncio.to_thread(self.crash, 'approval')
        with closing(sqlite3.connect(self.settings.database)) as connection:
            self.run_id = connection.execute('SELECT run_id FROM runs').fetchone()[0]
            self.old_lease = json.loads(connection.execute('SELECT state_json FROM runtime_states').fetchone()[0])['owner_lease_id']
        self.store = TrajectoryStore(self.settings.database)
        self.addCleanup(self.store.close)
        self.runtime = WorkspaceRuntime(self.settings.workspace)
        self.runtime.start_existing()
        self.addCleanup(self.runtime.stop)
        self.engine = FixtureDecisionEngine()
        self.offers = []

    def crash(self, stage):
        result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(self.settings.database),
                                 str(self.settings.workspace), stage], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)

    async def approve(self, action):
        self.offers.append(action)
        self.assertNotEqual(action.owner_lease_id, self.old_lease)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        return True

    async def resume(self, approve=None):
        return await resume_hello(self.settings, self.store, self.runtime, self.engine, self.run_id,
                                  approve or self.approve, actor='acceptance_test')

    async def test_actual_pre_action_crash_resumes_same_run_with_new_decision_lease_and_approval(self):
        result = await self.resume()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['run_id'], self.run_id)
        self.assertFalse(result['automatic_replay_allowed'])
        self.assertEqual(len(self.offers), 1)
        self.assertEqual((self.settings.workspace / 'hello.txt').read_text(), HELLO_CONTENT)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM decisions').fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 2)
        events = self.store.connection.execute('SELECT kind,payload_json FROM human_interventions ORDER BY rowid').fetchall()
        self.assertEqual([row['kind'] for row in events], ['resume', 'approve'])
        admission = ResumeAdmission.model_validate_json(events[0]['payload_json'])
        receipt = ResumeApproval.model_validate_json(events[1]['payload_json'])
        self.assertEqual(receipt.admission_id, admission.admission_id)
        self.assertEqual(receipt.action_sha256, digest(self.offers[0].model_dump(mode='json')))
        self.assertEqual(receipt.fresh_lease_ref, digest({'lease_id': self.offers[0].owner_lease_id}))

    async def test_rejection_never_executes_and_is_terminal(self):
        async def reject(action):
            return False

        result = await self.resume(reject)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())
        with self.assertRaises(AOSFault):
            await self.resume()

    async def test_cancellation_while_waiting_revokes_ownership_without_action(self):
        waiting = asyncio.Event()

        async def wait(action):
            waiting.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.resume(wait))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.state(self.run_id).owner, 'PAUSED')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())

    async def test_expired_action_approval_is_rejected(self):
        async def expire(action):
            self.clock = patch('aos.recovery_resume.time.time', return_value=action.deadline + 1)
            self.clock.start()
            self.addCleanup(self.clock.stop)
            return True

        result = await self.resume(expire)
        self.assertEqual(result['status'], 'failed')
        receipt = json.loads(self.store.connection.execute("SELECT payload_json FROM human_interventions WHERE kind='reject'").fetchone()[0])
        self.assertEqual(receipt['decision'], 'expired')
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())

    async def test_changed_approval_payload_is_not_transferred(self):
        async def changed(action):
            action.arguments['content'] = 'unauthorized'
            return True

        self.assertEqual((await self.resume(changed))['status'], 'failed')
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())

    async def test_existing_exact_file_is_only_read_after_new_approval(self):
        target = self.settings.workspace / 'hello.txt'
        target.write_text(HELLO_CONTENT)
        metadata = target.stat()
        self.assertEqual((await self.resume())['status'], 'succeeded')
        self.assertEqual(self.offers[0].tool, 'filesystem.read')
        self.assertEqual(target.stat().st_mtime_ns, metadata.st_mtime_ns)

    async def test_changed_workspace_or_deployment_before_approval_blocks_effect(self):
        async def change_identity(action):
            self.engine.identity = {**self.engine.identity, 'deployment_id': 'changed'}
            return True

        self.assertEqual((await self.resume(change_identity))['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_file_appearing_while_waiting_is_preserved(self):
        async def change_file(action):
            (self.settings.workspace / 'hello.txt').write_text('keep this')
            return True

        self.assertEqual((await self.resume(change_file))['status'], 'failed')
        self.assertEqual((self.settings.workspace / 'hello.txt').read_text(), 'keep this')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_runtime_stopped_while_waiting_never_records_action(self):
        async def stop_runtime(action):
            self.runtime.stop()
            return True

        self.assertEqual((await self.resume(stop_runtime))['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_directory_replaced_while_waiting_never_writes_either_directory(self):
        original = self.root / 'original'

        async def replace_directory(action):
            self.settings.workspace.rename(original)
            self.settings.workspace.mkdir()
            return True

        self.assertEqual((await self.resume(replace_directory))['status'], 'failed')
        self.assertFalse((original / 'hello.txt').exists())
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    async def test_concurrent_resume_does_not_replace_active_admission(self):
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def wait(action):
            waiting.set()
            await release.wait()
            return True

        task = asyncio.create_task(self.resume(wait))
        await waiting.wait()
        try:
            with self.assertRaises(AOSFault):
                await self.resume()
        finally:
            release.set()
        self.assertEqual((await task)['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM human_interventions WHERE kind='resume'").fetchone()[0], 1)

    async def test_scheduled_task_is_not_adopted_by_cli_recovery(self):
        with self.store.connection:
            self.store.insert('desktop_sessions', session_id='synthetic-session', runtime_id=self.runtime.runtime_id,
                              image_id='synthetic-image', owner='PAUSED', lease_id='revoked', generation=1,
                              status='paused', created_at=now(), updated_at=now())
            self.store.insert('desktop_tasks', job_id='synthetic-job', session_id='synthetic-session', run_id=self.run_id,
                              kind='hello', lease_id='revoked', generation=1, status='cancelled', real_model=0,
                              created_at=now(), updated_at=now(), runtime_id=self.runtime.runtime_id)
        with self.assertRaises(AOSFault):
            await self.resume()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 0)

    async def test_conflict_and_wrong_initial_deployment_fail_before_admission(self):
        target = self.settings.workspace / 'hello.txt'
        target.write_text('keep this')
        with self.assertRaises(AOSFault):
            await self.resume()
        target.unlink()
        self.engine.identity = {**self.engine.identity, 'deployment_id': 'changed'}
        with self.assertRaises(ValueError):
            await self.resume()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 0)

    async def test_snapshot_tamper_and_missing_gate_are_denied(self):
        with self.assertRaises(AOSFault):
            await resume_hello(self.settings, self.store, self.runtime, self.engine, self.run_id, None)
        with self.store.connection:
            self.store.connection.execute("UPDATE state_snapshots SET content_sha256=?", ('a' * 64,))
        with self.assertRaises(ValueError):
            await self.resume()

    async def test_resume_admission_failure_cannot_reach_action(self):
        with patch.object(self.store, 'insert', side_effect=sqlite3.OperationalError('synthetic failure')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.resume()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertEqual(self.store.state(self.run_id).phase.value, 'PAUSED')

    async def test_approval_audit_failure_cannot_reach_action(self):
        original = self.store.insert

        def fail_approval(table, **values):
            if table == 'human_interventions' and values['kind'] == 'approve':
                raise sqlite3.OperationalError('synthetic approval audit failure')
            return original(table, **values)

        with patch.object(self.store, 'insert', side_effect=fail_approval):
            with self.assertRaises(sqlite3.OperationalError):
                await self.resume()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.settings.workspace / 'hello.txt').exists())

    async def test_second_crash_after_approval_does_not_reuse_historical_approval(self):
        settings = Settings(database=self.root / 'twice.sqlite', workspace=self.root / 'twice')
        first = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', CRASH_SCRIPT, str(settings.database),
                                                         str(settings.workspace), 'approval'], capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 73, first.stderr)
        script = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import ComputerGateway, WorkspaceRuntime
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine
from aos.recovery_resume import resume_hello
from aos.storage import TrajectoryStore
async def approve(action):
    return True
def crash(self, action, decision_id):
    os._exit(74)
settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
store = TrajectoryStore(settings.database)
runtime = WorkspaceRuntime(settings.workspace)
runtime.start_existing()
run_id = store.connection.execute('SELECT run_id FROM runs').fetchone()[0]
ComputerGateway.execute = crash
asyncio.run(resume_hello(settings, store, runtime, FixtureDecisionEngine(), run_id, approve, actor='acceptance_test'))
'''
        second = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', script, str(settings.database), str(settings.workspace)],
                                          capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 74, second.stderr)
        store = TrajectoryStore(settings.database)
        runtime = WorkspaceRuntime(settings.workspace)
        runtime.start_existing()
        try:
            run_id = store.connection.execute('SELECT run_id FROM runs').fetchone()[0]
            old_receipt = json.loads(store.connection.execute("SELECT payload_json FROM human_interventions WHERE kind='approve'").fetchone()[0])
            offers = []

            async def approve(action):
                self.assertNotEqual(digest(action.model_dump(mode='json')), old_receipt['action_sha256'])
                self.assertNotEqual(digest({'lease_id': action.owner_lease_id}), old_receipt['fresh_lease_ref'])
                offers.append(action)
                return True

            self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
            self.assertFalse((settings.workspace / 'hello.txt').exists())
            result = await resume_hello(settings, store, runtime, self.engine, run_id, approve, actor='acceptance_test')
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(len(offers), 1)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM human_interventions WHERE kind='approve'").fetchone()[0], 2)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 2)
        finally:
            runtime.stop()
            store.close()

    async def test_actual_intent_or_effect_crash_cannot_resume_even_if_file_matches(self):
        for stage in ('before_write', 'after_write'):
            with self.subTest(stage=stage):
                settings = Settings(database=self.root / f'{stage}.sqlite', workspace=self.root / stage)
                result = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(settings.database), str(settings.workspace), stage],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 73, result.stderr)
                store = TrajectoryStore(settings.database)
                runtime = WorkspaceRuntime(settings.workspace)
                runtime.start_existing()
                try:
                    run_id = store.connection.execute('SELECT run_id FROM runs').fetchone()[0]
                    with self.assertRaises(AOSFault):
                        await resume_hello(settings, store, runtime, self.engine, run_id, self.approve)
                    self.assertEqual(store.connection.execute('SELECT status FROM actions').fetchone()[0], 'uncertain')
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM human_interventions').fetchone()[0], 0)
                    self.assertEqual((settings.workspace / 'hello.txt').exists(), stage == 'after_write')
                finally:
                    runtime.stop()
                    store.close()

    async def test_schema_and_synthetic_receipts_reject_authority_expansion(self):
        fixture = json.loads((REPO_ROOT / 'examples/recovery_resume.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model, key in (('recovery_resume', ResumeAdmission, 'admission'), ('recovery_resume_approval', ResumeApproval, 'approval')):
            self.assertEqual({'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()},
                             json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text()))
            validator(name).validate(fixture[key])
        for changes in ({'automatic_replay_allowed': True}, {'previously_recorded_actions': 1},
                        {'requires_action_approval': False}, {'scope': '/'}):
            self.assertFalse(validator('recovery_resume').is_valid({**fixture['admission'], **changes}))


class ResumeTerminalTests(unittest.TestCase):
    def test_noninteractive_cli_does_not_create_database_or_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run([sys.executable, '-m', 'aos.recovery_resume', '--database', str(root / 'absent.sqlite'),
                                     '--workspace', str(root / 'workspace'), '--run-id', 'absent', '--engine', 'fixture'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(list(root.iterdir()), [])

    def test_real_pty_requires_exact_new_digest_before_effect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, workspace = root / 'source.sqlite', root / 'workspace'
            crashed = subprocess.run([sys.executable, '-c', CRASH_SCRIPT, str(database), str(workspace), 'approval'],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(crashed.returncode, 73, crashed.stderr)
            with closing(sqlite3.connect(database)) as connection:
                run_id = connection.execute('SELECT run_id FROM runs').fetchone()[0]
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, '-m', 'aos.recovery_resume', '--database', str(database),
                                        '--workspace', str(workspace), '--run-id', run_id, '--engine', 'fixture'],
                                       stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = b''
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and b' yaz' not in output:
                    if select.select([master], [], [], .1)[0]:
                        output += os.read(master, 4096)
                match = re.search(rb'APPROVE ([a-f0-9]{64})', output)
                self.assertIsNotNone(match, output.decode(errors='replace'))
                self.assertFalse((workspace / 'hello.txt').exists())
                os.write(master, b'APPROVE ' + match[1] + b'\n')
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual((workspace / 'hello.txt').read_text(), HELLO_CONTENT)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)


@unittest.skipUnless(os.environ.get('AOS_REAL_TASK_TESTS') == '1', 'Real Decider requires AOS_REAL_TASK_TESTS=1')
class RealResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_decider_process_crash_then_fresh_approved_same_run(self):
        root = Path(tempfile.mkdtemp(prefix='resume-real-', dir=REPO_ROOT / 'data'))
        settings = Settings(database=root / 'store.sqlite', workspace=root / 'workspace')
        manifest = REPO_ROOT / 'models/decider-manifest.json'
        python = Path(os.environ['AOS_MODEL_PYTHON']).absolute()
        result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', CRASH_SCRIPT, str(settings.database), str(settings.workspace),
                                         'approval', str(manifest), str(python)], capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 73, result.stderr)
        store = TrajectoryStore(settings.database)
        runtime = WorkspaceRuntime(settings.workspace)
        runtime.start_existing()
        try:
            run_id = store.connection.execute('SELECT run_id FROM runs').fetchone()[0]
            offers = []

            async def approve(action):
                offers.append(action)
                return True

            outcome = await resume_hello(settings, store, runtime, DeciderEngine(manifest, python), run_id,
                                         approve, actor='acceptance_test')
            self.assertEqual(outcome['status'], 'succeeded')
            self.assertTrue(outcome['real_model'])
            self.assertEqual(len(offers), 1)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 2)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
            self.assertEqual(store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 1)
            print(canonical({'evidence_directory': str(root), 'real_decider_resume': outcome}))
        finally:
            runtime.stop()
            store.close()


if __name__ == '__main__':
    unittest.main()

import asyncio
from contextlib import closing
import hashlib
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
from aos.contracts import AOSFault, HELLO_CONTENT, Phase, REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.decision import DeciderEngine, FixtureDecisionEngine
from aos.recovery_finalize import FinalizationReceipt, FinalizationReport, FinalizationRequest, finalize_hello, inspect_finalization
from aos.storage import TrajectoryStore
from test_recovery_verify import accept, prepare


VERIFY_CRASH_SCRIPT = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import ComputerGateway, WorkspaceRuntime
from aos.contracts import Phase, Settings
from aos.decision import DeciderEngine, FixtureDecisionEngine
from aos.operator import Operator
from aos.recovery_verify import verify_reconciled_hello
from aos.storage import TrajectoryStore
settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
store = TrajectoryStore(settings.database)
runtime = WorkspaceRuntime(settings.workspace)
runtime.start_existing()
stage = sys.argv[3]
run_id, action_id = store.connection.execute('SELECT run_id,action_id FROM actions').fetchone()
correction_id = store.connection.execute("SELECT intervention_id FROM human_interventions WHERE kind='correction'").fetchone()[0]
original_insert, original_advance, original_finish = store.insert, Operator.advance, store.finish
original_execute = ComputerGateway.execute
def crash_insert(table, **values):
    if table == 'verifications' and stage == 'before_verification':
        os._exit(76)
    original_insert(table, **values)
def crash_advance(self, state, phase, **changes):
    if phase == Phase.SUCCEEDED and stage == 'after_verification':
        os._exit(76)
    result = original_advance(self, state, phase, **changes)
    if phase == Phase.SUCCEEDED and stage == 'after_state':
        os._exit(76)
    return result
def crash_finish(*arguments, **keywords):
    original_finish(*arguments, **keywords)
    if stage == 'after_finish':
        os._exit(76)
def crash_execute(self, action, decision_id):
    result = original_execute(self, action, decision_id)
    if stage == 'after_first_read':
        os._exit(76)
    return result
store.insert, Operator.advance, store.finish = crash_insert, crash_advance, crash_finish
ComputerGateway.execute = crash_execute
async def approve(action):
    if stage == 'after_admission':
        os._exit(76)
    return True
engine = FixtureDecisionEngine() if len(sys.argv) == 4 else DeciderEngine(Path(sys.argv[4]), Path(sys.argv[5]))
asyncio.run(verify_reconciled_hello(settings, store, runtime, engine, run_id, action_id, correction_id, approve, actor='acceptance_test'))
runtime.stop()
store.close()
'''


FINALIZE_CRASH_SCRIPT = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import WorkspaceRuntime
from aos.contracts import digest
from aos.decision import FixtureDecisionEngine
from aos.recovery_finalize import finalize_hello
from aos.storage import TrajectoryStore
store = TrajectoryStore(Path(sys.argv[1]))
runtime = WorkspaceRuntime(Path(sys.argv[2]))
runtime.start_existing()
run_id, action_id = store.connection.execute("SELECT run_id,action_id FROM actions WHERE tool='filesystem.write'").fetchone()
admission_id = store.connection.execute("SELECT intervention_id FROM human_interventions WHERE kind='resume'").fetchone()[0]
original = store.insert
def crash_insert(table, **values):
    original(table, **values)
    if table == 'human_interventions':
        os._exit(77)
if sys.argv[3] == 'before_commit':
    store.insert = crash_insert
async def approve(request):
    return True
asyncio.run(finalize_hello(store, runtime, run_id, action_id, admission_id, digest(FixtureDecisionEngine.identity), approve, actor='acceptance_test'))
os._exit(77)
'''


async def interrupted(root, stage='after_verification', engine=None):
    engine = engine or FixtureDecisionEngine()
    real = engine.identity['real_model']
    settings, run_id, action_id, correction_id = await prepare(root, engine, real)
    command = [sys.executable, '-c', VERIFY_CRASH_SCRIPT, str(settings.database), str(settings.workspace), stage]
    if real:
        command.extend([str(engine.manifest), str(engine.python)])
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=180 if real else 10)
    if result.returncode != (0 if stage == 'complete' else 76):
        raise AssertionError(result.stderr)
    with closing(sqlite3.connect(settings.database.absolute().as_uri() + '?mode=ro', uri=True)) as connection:
        admission_id = connection.execute("SELECT intervention_id FROM human_interventions WHERE kind='resume'").fetchone()[0]
    return settings, run_id, action_id, admission_id


class RecoveryFinalizeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, self.run_id, self.action_id, self.admission_id = await interrupted(self.root)
        self.deployment = digest(FixtureDecisionEngine.identity)
        self.store = None
        self.runtime = None

    def startup(self):
        self.runtime = WorkspaceRuntime(self.settings.workspace)
        self.runtime.start_existing()
        self.addCleanup(self.runtime.stop)
        self.store = TrajectoryStore(self.settings.database)
        self.addCleanup(self.store.close)

    def inspect(self):
        return inspect_finalization(self.settings.database, self.settings.workspace, self.run_id, self.action_id,
                                     self.admission_id, self.deployment,
                                     workspace_descriptor=self.runtime.descriptor if self.runtime else None)

    async def finalize(self, approval=accept):
        return await finalize_hello(self.store, self.runtime, self.run_id, self.action_id, self.admission_id,
                                    self.deployment, approval, actor='acceptance_test')

    def evidence_counts(self):
        return {table: self.store.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('actions', 'observations', 'verifications', 'decisions', 'model_calls')}

    def mutate(self, query, parameters=()):
        with self.store.connection:
            self.store.connection.execute(query, parameters)

    def assert_paused(self):
        self.assertEqual(tuple(self.store.connection.execute('SELECT status,outcome,training_eligible FROM runs').fetchone()),
                         ('paused', 'unknown', 0))
        self.assertEqual(self.store.state(self.run_id).phase, Phase.PAUSED)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM human_interventions WHERE json_extract(payload_json,'$.mode')='finalized_verified_hello'").fetchone()[0], 0)

    async def test_read_only_inspection_preserves_database_and_wal(self):
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.root.glob('store.sqlite*') if not path.name.endswith('-shm')}
        report = self.inspect()
        self.assertEqual(report.disposition, 'requires_startup')
        self.assertEqual(report.read_actions, 2)
        self.assertIsNotNone(report.evidence_sha256)
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in self.root.glob('store.sqlite*') if not path.name.endswith('-shm')})
        self.assertNotIn(str(self.root), report.model_dump_json())
        self.assertNotIn(HELLO_CONTENT.strip(), report.model_dump_json())
        self.assertNotIn(self.run_id, report.model_dump_json())
        validator('recovery_finalize_report').validate(report.model_dump())

    async def test_atomic_finalization_has_no_execution_or_new_evidence(self):
        self.startup()
        self.assertEqual(self.inspect().disposition, 'ready_to_finalize')
        previous = self.store.state(self.run_id)
        counts = self.evidence_counts()
        original_actions = [dict(row) for row in self.store.connection.execute('SELECT * FROM actions')]
        with patch.object(ComputerGateway, 'execute', side_effect=AssertionError('No new tools')), \
                patch.object(FixtureDecisionEngine, 'decide', side_effect=AssertionError('No new model')):
            receipt = await self.finalize()
        self.assertEqual(self.evidence_counts(), counts)
        self.assertEqual(original_actions, [dict(row) for row in self.store.connection.execute('SELECT * FROM actions')])
        current = self.store.state(self.run_id)
        self.assertEqual(current.phase, Phase.SUCCEEDED)
        self.assertEqual(current.owner, 'PAUSED')
        self.assertNotEqual(current.owner_lease_id, previous.owner_lease_id)
        self.assertEqual(current.state_version, previous.state_version + 1)
        self.assertEqual(tuple(self.store.connection.execute('SELECT status,outcome,training_eligible FROM runs').fetchone()), ('succeeded', 'passed', 0))
        self.assertEqual(self.inspect().disposition, 'already_finalized')
        self.assertEqual(receipt.request_sha256, digest(receipt.request.model_dump()))
        validator('recovery_finalize').validate(receipt.model_dump())
        validator('recovery_finalize_request').validate(receipt.request.model_dump())
        with self.assertRaises(AOSFault):
            previous.advance(Phase.SUCCEEDED)

    async def test_second_finalization_does_not_ask_or_write(self):
        self.startup()
        await self.finalize()
        before = self.inspect()

        async def unexpected(request):
            raise AssertionError('No repeated confirmation')

        with self.assertRaises(ValueError):
            await self.finalize(unexpected)
        self.assertEqual(before, self.inspect())

    async def test_audit_failure_rolls_back_state_run_step_and_snapshot(self):
        self.startup()
        before = self.inspect()
        insert = self.store.insert

        def fail_audit(table, **values):
            if table == 'human_interventions':
                raise sqlite3.OperationalError('synthetic audit failure')
            insert(table, **values)

        with patch.object(self.store, 'insert', side_effect=fail_audit):
            with self.assertRaises(sqlite3.OperationalError):
                await self.finalize()
        self.assert_paused()
        self.assertEqual(before, self.inspect())

    async def test_rejected_approval_has_no_metadata_effect(self):
        self.startup()

        async def reject(request):
            return False

        before = self.inspect()
        with self.assertRaises(ValueError):
            await self.finalize(reject)
        self.assertEqual(before, self.inspect())
        self.assert_paused()

    async def test_expired_approval_is_rejected(self):
        self.startup()

        async def expire(request):
            clock = patch('aos.recovery_finalize.time.time', return_value=request.expires_at + 1)
            clock.start()
            self.addCleanup(clock.stop)
            return True

        with self.assertRaises(ValueError):
            await self.finalize(expire)
        self.assert_paused()

    async def test_expiry_before_commit_rolls_back_complete_metadata_transaction(self):
        self.startup()
        before = self.inspect()
        original = self.store.insert

        def expire_after_audit(table, **values):
            original(table, **values)
            if table == 'human_interventions':
                payload = json.loads(values['payload_json'])
                clock = patch('aos.recovery_finalize.time.time', return_value=payload['request']['expires_at'] + 1)
                clock.start()
                self.addCleanup(clock.stop)

        with patch.object(self.store, 'insert', side_effect=expire_after_audit):
            with self.assertRaises(ValueError):
                await self.finalize()
        self.assertEqual(before, self.inspect())
        self.assert_paused()

    async def test_cancelled_approval_does_not_finalize(self):
        self.startup()
        waiting = asyncio.Event()

        async def wait(request):
            waiting.set()
            await asyncio.Future()

        task = asyncio.create_task(self.finalize(wait))
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_paused()

    async def test_mutated_confirmation_is_not_an_approval(self):
        self.startup()

        async def mutate(request):
            object.__setattr__(request.source, 'evidence_sha256', '0' * 64)
            return True

        with self.assertRaises(ValueError):
            await self.finalize(mutate)
        self.assert_paused()

    async def test_stale_database_during_confirmation_is_rejected(self):
        self.startup()

        async def mutate(request):
            self.mutate("UPDATE tasks SET original_goal='synthetic changed source'")
            return True

        with self.assertRaises(ValueError):
            await self.finalize(mutate)
        self.assert_paused()

    async def test_equal_bytes_replacement_during_confirmation_is_rejected(self):
        self.startup()

        async def replace(request):
            target = self.settings.workspace / 'hello.txt'
            target.rename(self.settings.workspace / 'previous')
            target.write_text(HELLO_CONTENT)
            return True

        with self.assertRaises(ValueError):
            await self.finalize(replace)
        self.assertEqual(self.inspect().disposition, 'file_not_matching')
        self.assert_paused()

    async def test_missing_file_is_never_recreated(self):
        self.startup()
        target = self.settings.workspace / 'hello.txt'
        target.unlink()
        self.assertEqual(self.inspect().disposition, 'file_not_matching')
        with self.assertRaises(ValueError):
            await self.finalize()
        self.assertFalse(target.exists())
        self.assert_paused()

    async def test_tampered_positive_verification_and_relations_fail_closed(self):
        self.startup()
        backup = sqlite3.connect(':memory:')
        self.addCleanup(backup.close)
        self.store.connection.backup(backup)
        changes = [
            ("UPDATE verifications SET actual_json=?", (canonical('not hello'),)),
            ("UPDATE verifications SET method='synthetic_pass'", ()),
            ("UPDATE verifications SET action_id=?", (self.action_id,)),
            ("UPDATE observations SET action_id=? WHERE kind='filesystem.read'", (self.action_id,)),
            ("UPDATE observations SET payload_json=? WHERE kind='filesystem.read'", (canonical({'content': 'wrong'}),)),
            ("UPDATE verifications SET evidence_refs_json='[]'", ()),
            ("UPDATE verifications SET verifier='untrusted'", ()),
            ("UPDATE verifications SET created_at='2000-01-01T00:00:00+00:00'", ()),
            ("UPDATE actions SET result_json=? WHERE tool='filesystem.read'", (canonical({'content': 'wrong'}),)),
            ("UPDATE actions SET actual_option='write_file' WHERE tool='filesystem.read'", ()),
            ("UPDATE human_interventions SET actor='model' WHERE kind='approve'", ()),
            ("DELETE FROM human_interventions WHERE kind='approve'", ()),
            ("UPDATE human_interventions SET payload_json=json_set(payload_json,'$.fresh_lease_ref',?) WHERE kind='approve'", ('0' * 64,)),
            ("UPDATE human_interventions SET payload_json=json_set(payload_json,'$.reconciliation_sha256',?) WHERE kind='resume'", ('0' * 64,)),
            ("UPDATE human_interventions SET payload_json=json_set(payload_json,'$.request_sha256',?) WHERE kind='correction'", ('0' * 64,)),
            ("UPDATE action_envelopes SET payload_sha256=? WHERE action_id!=?", ('0' * 64, self.action_id)),
            ("UPDATE state_snapshots SET content_sha256=? WHERE state_version=0", ('0' * 64,)),
            ("UPDATE runs SET training_eligible=1", ()),
        ]
        for query, parameters in changes:
            if 'state_version=0' in query:
                admission = json.loads(self.store.connection.execute("SELECT payload_json FROM human_interventions WHERE kind='resume'").fetchone()[0])
                query = query.replace('state_version=0', f"state_version={admission['source_state_version']}")
            with self.subTest(query=query):
                self.store.connection.execute('BEGIN')
                self.store.connection.execute(query, parameters)
                self.store.connection.commit()
                try:
                    with self.assertRaises(ValueError):
                        self.inspect()
                finally:
                    backup.backup(self.store.connection)

    async def test_process_crash_classification_matrix(self):
        expected = {'after_admission': ('incomplete', 0), 'after_first_read': ('incomplete', 1),
                    'before_verification': ('incomplete', 2), 'after_state': ('requires_startup', 2),
                    'after_finish': ('already_finalized', 2), 'complete': ('already_finalized', 2)}
        for stage, (disposition, reads) in expected.items():
            with self.subTest(stage=stage):
                settings, run_id, action_id, admission_id = await interrupted(self.root / stage, stage)
                report = inspect_finalization(settings.database, settings.workspace, run_id, action_id, admission_id, self.deployment)
                self.assertEqual((report.disposition, report.read_actions), (disposition, reads))
                store = TrajectoryStore(settings.database)
                runtime = WorkspaceRuntime(settings.workspace)
                try:
                    runtime.start_existing()
                    if stage == 'after_state':
                        await finalize_hello(store, runtime, run_id, action_id, admission_id, self.deployment, accept, actor='acceptance_test')
                        self.assertEqual(store.state(run_id).phase, Phase.SUCCEEDED)
                    else:
                        with self.assertRaises(ValueError):
                            await finalize_hello(store, runtime, run_id, action_id, admission_id, self.deployment, accept, actor='acceptance_test')
                finally:
                    runtime.stop()
                    store.close()

    async def test_finalizer_crash_before_and_after_commit_is_atomic(self):
        for stage in ('before_commit', 'after_commit'):
            settings, run_id, action_id, admission_id = await interrupted(self.root / stage)
            result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', FINALIZE_CRASH_SCRIPT, str(settings.database),
                                                              str(settings.workspace), stage], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 77, result.stderr)
            store = TrajectoryStore(settings.database)
            try:
                report = inspect_finalization(settings.database, settings.workspace, run_id, action_id, admission_id, self.deployment)
                self.assertEqual(report.disposition, 'ready_to_finalize' if stage == 'before_commit' else 'already_finalized')
                self.assertEqual(store.connection.execute("SELECT count(*) FROM human_interventions WHERE json_extract(payload_json,'$.mode')='finalized_verified_hello'").fetchone()[0],
                                 0 if stage == 'before_commit' else 1)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 1)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 3)
            finally:
                store.close()

    async def test_wrong_deployment_and_workspace_lock_are_rejected(self):
        with self.assertRaises(ValueError):
            inspect_finalization(self.settings.database, self.settings.workspace, self.run_id, self.action_id, self.admission_id, '0' * 64)
        self.startup()
        with self.assertRaises(OSError):
            inspect_finalization(self.settings.database, self.settings.workspace, self.run_id, self.action_id, self.admission_id, self.deployment)

    async def test_duplicate_verification_or_admission_is_rejected(self):
        self.startup()
        backup = sqlite3.connect(':memory:')
        self.addCleanup(backup.close)
        self.store.connection.backup(backup)
        for table, primary, condition in (('verifications', 'verification_id', '1=1'),
                                           ('human_interventions', 'intervention_id', "kind='resume'"),
                                           ('human_interventions', 'intervention_id', "kind='correction'")):
            with self.subTest(table=table, condition=condition):
                row = dict(self.store.connection.execute(f'SELECT * FROM {table} WHERE {condition} LIMIT 1').fetchone())
                row[primary] += '-duplicate'
                with self.store.connection:
                    self.store.insert(table, **row)
                with self.assertRaises(ValueError):
                    self.inspect()
                backup.backup(self.store.connection)

    async def test_cancelled_run_with_complete_evidence_is_not_finalizable(self):
        self.startup()
        previous = self.store.state(self.run_id)
        cancelled = previous.advance(Phase.CANCELLED)
        self.store.save_state(previous, cancelled)
        self.store.finish(cancelled, 'cancelled', 'unknown')
        self.assertEqual(self.inspect().disposition, 'blocked')
        with self.assertRaises(ValueError):
            await self.finalize()
        self.assertEqual(self.store.state(self.run_id).phase, Phase.CANCELLED)

    async def test_finalization_audit_cannot_be_forged_or_removed_from_state_binding(self):
        self.startup()
        await self.finalize()
        self.mutate("UPDATE human_interventions SET payload_json=json_set(payload_json,'$.finalized_state_sha256',?) WHERE json_extract(payload_json,'$.mode')='finalized_verified_hello'",
                    ('0' * 64,))
        with self.assertRaises(ValueError):
            self.inspect()
        self.mutate("DELETE FROM human_interventions WHERE json_extract(payload_json,'$.mode')='finalized_verified_hello'")
        with self.assertRaises(ValueError):
            self.inspect()

    async def test_concurrent_confirmations_only_one_can_finalize(self):
        self.startup()
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def delayed(request):
            waiting.set()
            await release.wait()
            return True

        pending = asyncio.create_task(self.finalize(delayed))
        await waiting.wait()
        await self.finalize()
        release.set()
        with self.assertRaises(ValueError):
            await pending
        self.assertEqual(self.inspect().disposition, 'already_finalized')
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM human_interventions WHERE json_extract(payload_json,'$.mode')='finalized_verified_hello'").fetchone()[0], 1)

    async def test_unowned_runtime_and_writer_are_rejected(self):
        self.startup()
        unowned = WorkspaceRuntime(self.settings.workspace)
        with self.assertRaises(ValueError):
            await finalize_hello(self.store, unowned, self.run_id, self.action_id, self.admission_id, self.deployment, accept)
        reader = TrajectoryStore(self.settings.database, readonly=True)
        try:
            with self.assertRaises(ValueError):
                await finalize_hello(reader, self.runtime, self.run_id, self.action_id, self.admission_id, self.deployment, accept)
        finally:
            reader.close()

    def command(self, database=None):
        return [sys.executable, '-m', 'aos.recovery_finalize', '--database', str(database or self.settings.database),
                '--workspace', str(self.settings.workspace), '--run-id', self.run_id, '--action-id', self.action_id,
                '--admission-id', self.admission_id, '--deployment-sha256', self.deployment]

    async def test_cli_read_only_without_tty_and_writer_requires_tty(self):
        result = subprocess.run(self.command(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['disposition'], 'requires_startup')
        missing = self.root / 'missing.sqlite'
        result = subprocess.run(self.command(missing) + ['--finalize'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(missing.exists())
        self.assertFalse(Path(str(missing) + '.lock').exists())
        self.assertNotIn('Traceback', result.stderr)

    async def test_cli_exact_digest_pty_accept_reject_eof_oversize(self):
        for answer in ('reject', 'eof', 'oversize', 'wrong_digest', 'accept'):
            master, slave = pty.openpty()
            process = subprocess.Popen(self.command() + ['--finalize'], stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = bytearray()
            try:
                deadline = time.monotonic() + 10
                matched = None
                while time.monotonic() < deadline and matched is None:
                    if select.select([master], [], [], 0.1)[0]:
                        output.extend(os.read(master, 8192))
                        matched = re.search(rb'FINALIZE ([a-f0-9]{64})', output)
                self.assertIsNotNone(matched, output)
                responses = {'reject': b'NO\n', 'eof': b'\x04', 'oversize': b'FINALIZE ' + matched.group(1) + b' ' * 256 + b'\n',
                             'wrong_digest': b'FINALIZE ' + b'0' * 64 + b'\n', 'accept': b'FINALIZE ' + matched.group(1) + b'\n'}
                os.write(master, responses[answer])
                self.assertEqual(process.wait(timeout=10), 0 if answer == 'accept' else 1, output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
        self.assertEqual(self.inspect().disposition, 'already_finalized')

    async def test_synthetic_contracts_and_authority_rejection(self):
        fixture = json.loads((REPO_ROOT / 'examples/recovery_finalize.json').read_text())
        self.assertTrue(fixture['synthetic'])
        receipt = FinalizationReceipt.model_validate(fixture['receipt'])
        self.assertEqual(receipt.actor, 'acceptance_test')
        report = receipt.request.source
        for model, payload in ((FinalizationReport, report.model_dump()), (FinalizationRequest, receipt.request.model_dump()),
                               (FinalizationReceipt, receipt.model_dump())):
            for field in ('execution_authorized', 'resume_authorized', 'automatic_replay_allowed'):
                with self.assertRaises(ValueError):
                    model.model_validate({**payload, field: True})
        with self.assertRaises(ValueError):
            FinalizationReport.model_validate({**report.model_dump(), 'evidence_sha256': None})
        with self.assertRaises(ValueError):
            FinalizationRequest.model_validate({**receipt.request.model_dump(), 'source': {**report.model_dump(), 'disposition': 'incomplete'}})

    async def test_canonical_schemas_match_typed_contracts(self):
        for name, model in (('recovery_finalize_report', FinalizationReport), ('recovery_finalize_request', FinalizationRequest),
                            ('recovery_finalize', FinalizationReceipt)):
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()})


@unittest.skipUnless(os.environ.get('AOS_REAL_TASK_TESTS') == '1', 'Real Decider requires AOS_REAL_TASK_TESTS=1')
class RealFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_decider_verification_crash_finalization_without_new_model_or_tools(self):
        root = Path(tempfile.mkdtemp(prefix='finalize-real-', dir=REPO_ROOT / 'data'))
        engine = DeciderEngine(REPO_ROOT / 'models/decider-manifest.json', Path(os.environ['AOS_MODEL_PYTHON']).absolute())
        settings, run_id, action_id, admission_id = await interrupted(root, engine=engine)
        store = TrajectoryStore(settings.database)
        runtime = WorkspaceRuntime(settings.workspace)
        try:
            runtime.start_existing()
            with patch.object(ComputerGateway, 'execute', side_effect=AssertionError('No replay')), \
                    patch.object(DeciderEngine, 'decide', side_effect=AssertionError('No new model')):
                receipt = await finalize_hello(store, runtime, run_id, action_id, admission_id, digest(engine.identity), accept, actor='acceptance_test')
            self.assertEqual(tuple(store.connection.execute('SELECT status,outcome,training_eligible FROM runs').fetchone()), ('succeeded', 'passed', 0))
            self.assertEqual(store.connection.execute("SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 2)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 2)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 3)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 1)
            print(canonical({'evidence_directory': str(root), 'real_decider_finalization': receipt.model_dump()}))
        finally:
            runtime.stop()
            store.close()


if __name__ == '__main__':
    unittest.main()

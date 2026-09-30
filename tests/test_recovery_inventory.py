import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, Settings, canonical, digest, now
from aos.dataset import validator
from aos.dataset_audit import audit_snapshot
from aos.decision import FixtureDecisionEngine
from aos.operator import Operator
from aos.recovery import RecoveryReport, inspect_recovery, recovery_inventory
from aos.storage import TrajectoryStore


class RecoveryInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'source.sqlite'
        self.store = TrajectoryStore(self.database)
        self.addCleanup(self.store.close)
        self.store.connection.execute('PRAGMA journal_mode=WAL')
        self.settings = Settings(workspace=self.root / 'workspace', database=self.database)
        runtime = WorkspaceRuntime(self.settings.workspace)
        runtime.start()
        self.addCleanup(runtime.stop)
        result = asyncio.run(Operator(self.settings, self.store, runtime, FixtureDecisionEngine()).hello())
        self.run_id = result['run_id']
        with self.store.connection:
            self.store.insert('desktop_sessions', session_id='synthetic-session', runtime_id='synthetic-runtime', image_id='synthetic-image',
                              owner='AGENT', lease_id='synthetic-private-lease', generation=0, status='running', created_at=now(), updated_at=now())
            self.job('synthetic-job', 'succeeded', self.run_id)

    def job(self, job_id, status, run_id=None, created_at=None):
        self.store.insert('desktop_tasks', job_id=job_id, session_id='synthetic-session', run_id=run_id, kind='hello',
                          lease_id='synthetic-private-lease', generation=0, status=status, real_model=0,
                          created_at=created_at or now(), updated_at=now(), runtime_id='synthetic-runtime')

    def unfinished(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='running'")
            self.store.connection.execute("UPDATE actions SET status='intent'")
            self.store.connection.execute("UPDATE desktop_tasks SET status='waiting_approval'")
            self.store.insert('desktop_approvals', approval_id='synthetic-private-approval', job_id='synthetic-job',
                              envelope_json=canonical({'secret': 'synthetic-private-payload'}), action_sha256='a' * 64,
                              expires_at=1, status='approved', created_at=now(), updated_at=now())
            self.store.insert('desktop_inputs', input_id='synthetic-private-input', session_id='synthetic-session',
                              lease_id='synthetic-private-lease', generation=0, tool='type_note', status='running', created_at=now())

    def test_settled_records_are_not_current_runtime_or_execution_proof(self):
        report = recovery_inventory(self.database)
        self.assertFalse(report.requires_inspection)
        self.assertFalse(report.live_state_verified)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.resume_authorized)
        self.assertFalse(report.automatic_replay_allowed)
        self.assertEqual(report.totals.sessions_not_stopped, 1)
        self.assertEqual(report.jobs[0].status, 'succeeded')
        self.assertEqual(report.jobs[0].job_ref, digest({'job_id': 'synthetic-job'}))
        self.assertEqual(report.jobs[0].reasons, [])

    def test_unfinished_snapshot_does_not_reconcile_mutate_or_expose_private_content(self):
        self.unfinished()
        paths = [self.database, Path(str(self.database) + '-wal')]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        report = recovery_inventory(self.database)
        self.assertTrue(report.requires_inspection)
        self.assertEqual(report.totals.action_intent, 2)
        self.assertEqual(report.totals.approval_approved, 1)
        self.assertEqual(report.totals.input_running, 1)
        self.assertEqual(report.jobs[0].reasons, ['action_effect_unresolved', 'approval_not_terminal', 'job_unsettled', 'run_unsettled'])
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'approved')
        for secret in (str(self.database), self.run_id, 'synthetic-private', 'synthetic-job', 'Hello from the local agent'):
            self.assertNotIn(secret, report.model_dump_json())

    def test_snapshot_frozen_committed_wal_and_uncommitted_updates(self):
        self.unfinished()
        with audit_snapshot(self.database) as (snapshot, identity):
            with self.store.connection:
                self.store.connection.execute("UPDATE actions SET status='uncertain'")
            old = inspect_recovery(snapshot, identity)
            self.assertEqual(old.totals.action_intent, 2)
            with self.assertRaises(sqlite3.OperationalError):
                snapshot.execute("UPDATE runs SET status='failed'")
        self.assertEqual(recovery_inventory(self.database).totals.action_uncertain, 2)
        self.store.connection.execute("UPDATE actions SET status='ok'")
        try:
            self.assertEqual(recovery_inventory(self.database).totals.action_uncertain, 2)
        finally:
            self.store.connection.rollback()

    def test_after_writer_reconciliation_stale_approval_stays_revoked_uncertain_stays_visible(self):
        self.unfinished()
        self.store.reconcile()
        report = recovery_inventory(self.database)
        self.assertEqual(report.jobs[0].status, 'cancelled')
        self.assertEqual(report.jobs[0].run_status, 'paused')
        self.assertEqual(report.totals.action_uncertain, 2)
        self.assertEqual(report.totals.input_uncertain, 1)
        self.assertEqual(report.totals.approval_approved, 0)
        self.assertTrue(report.requires_inspection)
        self.assertFalse(report.resume_authorized)

    def test_prioritization_truncation_and_totals_cover_omitted_jobs(self):
        with self.store.connection:
            self.job('synthetic-queued', 'queued', created_at='2000-01-01T00:00:00Z')
            self.job('synthetic-cancelled', 'cancelled')
        report = recovery_inventory(self.database, limit=1)
        self.assertTrue(report.jobs_truncated)
        self.assertEqual(report.totals.jobs, 3)
        self.assertEqual(report.totals.jobs_requiring_inspection, 1)
        self.assertEqual(report.jobs[0].status, 'queued')

    def test_orphan_run_and_status_mismatch_are_visible(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET status='failed'")
        self.assertIn('job_run_status_mismatch', recovery_inventory(self.database).jobs[0].reasons)
        with self.store.connection:
            self.store.connection.execute('DELETE FROM desktop_tasks')
            self.store.connection.execute("UPDATE runs SET status='paused'")
        report = recovery_inventory(self.database)
        self.assertEqual(report.jobs, [])
        self.assertEqual(report.totals.unsettled_runs_without_jobs, 1)
        self.assertTrue(report.requires_inspection)

    def test_missing_symlink_invalid_schema_and_record_limits_fail_closed(self):
        missing = self.root / 'absent.sqlite'
        with self.assertRaises(ValueError):
            recovery_inventory(missing)
        self.assertFalse(missing.exists())
        link = self.root / 'link.sqlite'
        link.symlink_to(self.database)
        with self.assertRaises(ValueError):
            recovery_inventory(link)
        for limit in (0, 101, True, '30'):
            with self.assertRaisesRegex(ValueError, 'invalid_recovery_limit'):
                recovery_inventory(self.database, limit)
        with patch('aos.recovery.MAX_RECORDS', 0), self.assertRaisesRegex(ValueError, 'recovery_record_limit'):
            recovery_inventory(self.database)
        with self.store.connection:
            self.store.connection.execute('CREATE TABLE unexpected (value TEXT)')
        with self.assertRaisesRegex(ValueError, 'unsupported_database_schema'):
            recovery_inventory(self.database)

    def test_v7_is_read_without_migration_and_cli_errors_do_not_leak(self):
        path = self.root / 'v7.sqlite'
        with closing(sqlite3.connect(path)) as connection, connection:
            for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:7]:
                connection.executescript(migration.read_text())
        before = path.read_bytes()
        self.assertEqual(recovery_inventory(path).totals.jobs, 0)
        self.assertEqual(before, path.read_bytes())
        result = subprocess.run([sys.executable, '-m', 'aos.recovery', '--database', str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['execution_authorized'])
        result = subprocess.run([sys.executable, '-m', 'aos.recovery', '--database', str(self.root / 'private-missing')],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('private-missing', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_schema_fixture_and_authority_expansion_rejected(self):
        expected = {'$schema': 'https://json-schema.org/draft/2020-12/schema', **RecoveryReport.model_json_schema()}
        self.assertEqual(expected, json.loads((REPO_ROOT / 'schemas/recovery_inventory.schema.json').read_text()))
        fixture = json.loads((REPO_ROOT / 'examples/recovery_inventory.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('recovery_inventory').validate(fixture['report'])
        report = recovery_inventory(self.database).model_dump()
        validator('recovery_inventory').validate(report)
        for changes in ({'execution_authorized': True}, {'resume_authorized': True}, {'live_state_verified': True},
                        {'automatic_replay_allowed': True}, {'raw_state': 'private'}, {'job_limit': 101}):
            self.assertFalse(validator('recovery_inventory').is_valid({**report, **changes}))

    def test_actual_process_crash_preserves_pending_record_without_replay(self):
        database = self.root / 'crashed.sqlite'
        workspace = self.root / 'crashed-workspace'
        script = '''
import asyncio, os, sys
from pathlib import Path
from aos.contracts import Settings
from aos.storage import TrajectoryStore
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.decision import FixtureDecisionEngine
from test_desktop_tasks import FixtureDesktop
async def crash():
    settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
    store = TrajectoryStore(settings.database)
    runtime = FixtureDesktop(settings.workspace)
    runtime.start()
    controller = DesktopController(store, runtime)
    scheduler = DesktopScheduler(controller, settings, FixtureDecisionEngine())
    state = controller.state()
    scheduler.start(state['lease_id'], state['generation'])
    for attempt in range(1000):
        if scheduler.status()['approval']:
            os._exit(73)
        await asyncio.sleep(.001)
    os._exit(74)
asyncio.run(crash())
'''
        result = subprocess.run([sys.executable, '-c', script, str(database), str(workspace)], cwd=REPO_ROOT / 'tests',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)
        before = database.read_bytes()
        report = recovery_inventory(database)
        self.assertEqual(report.totals.approval_pending, 1)
        self.assertEqual(report.jobs[0].status, 'waiting_approval')
        self.assertEqual(report.jobs[0].run_status, 'running')
        self.assertEqual(database.read_bytes(), before)
        self.assertFalse((workspace / 'hello.txt').exists())
        restarted = TrajectoryStore(database)
        try:
            after = recovery_inventory(database)
            self.assertEqual(after.totals.approval_pending, 0)
            self.assertEqual(after.jobs[0].status, 'cancelled')
            self.assertEqual(after.jobs[0].run_status, 'paused')
            self.assertFalse(after.resume_authorized)
            self.assertEqual(restarted.connection.execute('SELECT status FROM desktop_approvals').fetchone()[0], 'revoked')
            self.assertEqual(restarted.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        finally:
            restarted.close()
        self.assertFalse((workspace / 'hello.txt').exists())


if __name__ == '__main__':
    unittest.main()

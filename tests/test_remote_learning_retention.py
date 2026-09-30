import asyncio
from contextlib import redirect_stderr
import io
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
import httpx
from pydantic import ValidationError

from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop_console import create_console
from aos.local_app import BASE, managed_backend_command
from aos.remote_learning_consent import RemoteLearningConsent, RemoteLearningConsents, RemoteLearningRevocation
from aos.remote_learning_retention import (RETENTION_STALE_AFTER_SECONDS, sweep_managed_remote_learning_retention,
                                           sweep_remote_learning_retention,
                                           run_managed_retention_loop, RetentionTelemetry)
from aos.remote_learning_stream import _open_store


class RemoteLearningRetentionTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_learning_consent.json').read_text())
        self.example = fixture['consent']
        self.current = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)

    def consent(self, root: Path, *, run_id: str, expires_at: datetime) -> tuple[str, Path]:
        root.mkdir(mode=0o700, exist_ok=True)
        record = RemoteLearningConsent.model_validate({**self.example, 'run_id': run_id,
                                                        'retention_days': 1,
                                                        'expires_at': expires_at.isoformat()})
        checksum = digest(record.model_dump())
        path = root / (checksum + '.json')
        path.write_text(canonical(record.model_dump()))
        path.chmod(0o600)
        return checksum, path

    @staticmethod
    def outbox(root: Path, checksum: str) -> Path:
        root.parent.mkdir(mode=0o700, exist_ok=True)
        connection, directory = _open_store(root)
        try:
            connection.execute('INSERT INTO source_binding VALUES(1,?,?,?,?,?,?,?,?)',
                               ('1' * 64, '2' * 64, 'synthetic-run', '3' * 64,
                                '4' * 64, '5' * 64, checksum, '6' * 64))
            connection.commit()
        finally:
            connection.close()
            os.close(directory)
        return root / 'remote-learning-stream.sqlite'

    def test_retention_schema_preserves_attestation_boundary(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())
        revocation_schema = json.loads((REPO_ROOT / 'schemas/remote_learning_revocation.schema.json').read_text())
        report_schema = json.loads((REPO_ROOT / 'schemas/remote_learning_retention_report.schema.json').read_text())
        managed_schema = json.loads((REPO_ROOT / 'schemas/remote_learning_managed_retention_report.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(revocation_schema)
        jsonschema.Draft202012Validator.check_schema(report_schema)
        jsonschema.Draft202012Validator.check_schema(managed_schema)
        jsonschema.Draft202012Validator(revocation_schema).validate(fixture['retention_revocation'])
        jsonschema.Draft202012Validator(report_schema).validate(fixture['retention_report'])
        jsonschema.Draft202012Validator(managed_schema).validate(fixture['managed_retention_report'])
        RemoteLearningRevocation.model_validate(fixture['retention_revocation'])
        invalid = {**fixture['retention_revocation'], 'local_operator_attested': True}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(revocation_schema).validate(invalid)
        with self.assertRaises(ValidationError):
            RemoteLearningRevocation.model_validate(invalid)

    def test_deadline_and_exact_purge_are_idempotent(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            outbox = root / 'outbox'
            due, _ = self.consent(consents, run_id='synthetic-due',
                                  expires_at=self.current - timedelta(days=1, seconds=1))
            future, _ = self.consent(consents, run_id='synthetic-future',
                                     expires_at=self.current - timedelta(hours=23))
            due_file = self.outbox(outbox / due, due)
            future_file = self.outbox(outbox / future, future)
            store = RemoteLearningConsents(consents)
            with self.assertRaises(ValueError):
                store.expire_for_retention(future, now=self.current)
            early = sweep_remote_learning_retention(consents=consents, outbox_dir=outbox,
                                                    now=self.current - timedelta(seconds=2))
            self.assertEqual(early['candidate_count'], 0)
            self.assertTrue(due_file.exists())
            report = sweep_remote_learning_retention(consents=consents, outbox_dir=outbox,
                                                     now=self.current)
            self.assertEqual((report['candidate_count'], report['retention_due_count'],
                              report['purged_count'], report['outbox_absent_count'],
                              report['failed_consent_sha256']), (1, 1, 1, 0, []))
            self.assertFalse(due_file.exists())
            self.assertTrue(future_file.exists())
            marker = store.revocation(due)
            self.assertEqual(marker.mode, 'automatic_remote_metadata_retention_expiry')
            self.assertFalse(marker.local_operator_attested)
            self.assertEqual((consents / (due + '.revoked.json')).stat().st_mode & 0o777, 0o600)
            again = sweep_remote_learning_retention(consents=consents, outbox_dir=outbox,
                                                    now=self.current)
            self.assertEqual(again['purged_count'], 0)
            self.assertEqual(again['outbox_absent_count'], 1)
            self.assertEqual(store.revocation(due), marker)

    def test_failed_exact_purge_keeps_marker_and_other_outbox(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            outbox = root / 'outbox'
            due, _ = self.consent(consents, run_id='synthetic-due',
                                  expires_at=self.current - timedelta(days=2))
            future, _ = self.consent(consents, run_id='synthetic-future',
                                     expires_at=self.current - timedelta(hours=1))
            due_file = self.outbox(outbox / due, due)
            future_file = self.outbox(outbox / future, future)
            sidecar = due_file.parent / 'unexpected-file'
            sidecar.write_text('synthetic')
            failed = sweep_remote_learning_retention(consents=consents, outbox_dir=outbox,
                                                     now=self.current)
            self.assertEqual(failed['failed_consent_sha256'], [due])
            self.assertTrue(due_file.exists())
            self.assertTrue(future_file.exists())
            self.assertFalse(RemoteLearningConsents(consents).revocation(due).local_operator_attested)
            sidecar.unlink()
            retried = sweep_remote_learning_retention(consents=consents, outbox_dir=outbox,
                                                      now=self.current)
            self.assertEqual(retried['purged_count'], 1)
            self.assertFalse(due_file.exists())
            self.assertTrue(future_file.exists())

    def test_cli_enforces_due_retention_without_time_override(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            outbox = root / 'outbox'
            due, _ = self.consent(consents, run_id='synthetic-due',
                                  expires_at=datetime.now(timezone.utc) - timedelta(days=2))
            due_file = self.outbox(outbox / due, due)
            command = [sys.executable, '-m', 'aos.remote_learning_retention',
                       '--consents', str(consents), '--outbox-dir', str(outbox)]
            result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True,
                                    text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['purged_count'], 1)
            self.assertFalse(due_file.exists())

    def test_managed_sweep_visits_session_roots_and_rejects_alias(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            sessions = root / 'sessions'
            sessions.mkdir(mode=0o700)
            due, _ = self.consent(consents, run_id='synthetic-due',
                                  expires_at=self.current - timedelta(days=2))
            empty = sessions / ('app-' + '1' * 32)
            empty.mkdir(mode=0o700)
            selected = sessions / ('app-' + '2' * 32)
            selected.mkdir(mode=0o700)
            due_file = self.outbox(selected / 'remote-learning-outbox' / due, due)
            alias = sessions / ('app-' + '3' * 32)
            alias.symlink_to(selected, target_is_directory=True)
            report = sweep_managed_remote_learning_retention(
                consents=consents, sessions_root=sessions, now=self.current)
            self.assertEqual(report['session_count'], 3)
            self.assertEqual(report['purged_count'], 1)
            self.assertEqual(report['failed_sessions'], [alias.name])
            self.assertFalse(due_file.exists())
            self.assertEqual(RemoteLearningConsents(consents).revocation(due).mode,
                             'automatic_remote_metadata_retention_expiry')

    def test_only_pinned_managed_launch_enables_periodic_sweep(self):
        managed = BASE / ('app-' + '1' * 32)
        unmanaged = REPO_ROOT / 'data' / 'synthetic-session'
        self.assertIn('--managed-retention', managed_backend_command(managed, 'fixture', 9))
        self.assertNotIn('--managed-retention', managed_backend_command(unmanaged, 'fixture', 9))
        rejected = subprocess.run([sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                                   '--managed-retention'], cwd=REPO_ROOT,
                                  capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('pinned private local-app session', rejected.stderr)

    def test_managed_cli_sweeps_private_sessions(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            sessions = root / 'sessions'
            sessions.mkdir(mode=0o700)
            due, _ = self.consent(consents, run_id='synthetic-due',
                                  expires_at=datetime.now(timezone.utc) - timedelta(days=2))
            session = sessions / ('app-' + 'a' * 32)
            session.mkdir(mode=0o700)
            due_file = self.outbox(session / 'remote-learning-outbox' / due, due)
            command = [sys.executable, '-m', 'aos.remote_learning_retention',
                       '--consents', str(consents), '--sessions-root', str(sessions)]
            result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True,
                                    text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual((report['session_count'], report['purged_count']), (1, 1))
            self.assertFalse(due_file.exists())
            command[-1] = str(root / 'missing-sessions')
            missing = subprocess.run(command, cwd=REPO_ROOT, capture_output=True,
                                     text=True, timeout=10, check=False)
            self.assertNotEqual(missing.returncode, 0)


class RemoteLearningRetentionLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_retry_and_clean_cancellation(self):
        results = []
        completed = asyncio.Event()

        def sweep(**_arguments):
            if not results:
                raise OSError('synthetic_retry')
            return {'session_count': 1, 'purged_count': 1,
                    'failed_sessions': [], 'failed_consent_sha256': []}

        def receive(report, failure):
            results.append((report, failure))
            if report is not None:
                completed.set()

        with patch('aos.remote_learning_retention.sweep_managed_remote_learning_retention',
                   side_effect=sweep):
            task = asyncio.create_task(run_managed_retention_loop(
                consents=Path('synthetic-consents'), sessions_root=Path('synthetic-sessions'),
                interval_seconds=0.01, on_result=receive))
            try:
                await asyncio.wait_for(completed.wait(), timeout=2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0][1], OSError)
        self.assertEqual(results[1][0]['purged_count'], 1)
        with self.assertRaises(ValueError):
            await run_managed_retention_loop(
                consents=Path('synthetic-consents'), sessions_root=Path('synthetic-sessions'),
                interval_seconds=0)

    async def test_status_callback_failure_does_not_stop_future_sweeps(self):
        attempts = []
        completed = asyncio.Event()
        errors = io.StringIO()

        def sweep(**_arguments):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise OSError('private synthetic path')
            return {'session_count': 1, 'purged_count': 0,
                    'failed_sessions': [], 'failed_consent_sha256': []}

        def receive(_report, _failure):
            if len(attempts) < 3:
                raise ValueError('private callback detail')
            completed.set()

        with patch('aos.remote_learning_retention.sweep_managed_remote_learning_retention',
                   side_effect=sweep), redirect_stderr(errors):
            task = asyncio.create_task(run_managed_retention_loop(
                consents=Path('synthetic-consents'), sessions_root=Path('synthetic-sessions'),
                interval_seconds=0.01, on_result=receive))
            try:
                await asyncio.wait_for(completed.wait(), timeout=2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(errors.getvalue().count('status callback failed'), 2)
        self.assertNotIn('private', errors.getvalue())


class RetentionTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_is_content_free_and_owner_only(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_learning_retention_status.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(fixture['retention_status'])
        jsonschema.Draft202012Validator(schema).validate(fixture['stale_retention_status'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **fixture['stale_retention_status'], 'session_count': 2})
        telemetry = RetentionTelemetry(enabled=True)
        self.assertEqual(telemetry.snapshot()['state'], 'pending')
        report = fixture['managed_retention_report']
        telemetry.record(report, None)
        self.assertEqual(telemetry.snapshot()['state'], 'ok')
        self.assertEqual(telemetry.snapshot()['purged_count'], 1)
        telemetry.record({**report, 'failed_sessions': ['app-' + 'a' * 32]}, None)
        self.assertEqual(telemetry.snapshot()['state'], 'incomplete')
        self.assertEqual(telemetry.snapshot()['failed_session_count'], 1)
        telemetry.record({**report, 'failed_sessions': ['not-a-managed-session']}, None)
        self.assertEqual(telemetry.snapshot()['state'], 'unavailable')
        telemetry.record(None, OSError('synthetic private path /secret'))
        self.assertNotIn('/secret', json.dumps(telemetry.snapshot()))
        self.assertIsNone(telemetry.snapshot()['failed_session_count'])
        self.assertEqual(RetentionTelemetry(enabled=False).snapshot()['state'], 'disabled')

        fresh = RetentionTelemetry(enabled=True)
        fresh.record(report, None)
        fresh._last_attempt_monotonic -= RETENTION_STALE_AFTER_SECONDS + 1
        stale = fresh.snapshot()
        self.assertEqual(stale['state'], 'stale')
        self.assertIsNone(stale['session_count'])
        self.assertIsNone(stale['purged_count'])
        self.assertIsNone(stale['failed_session_count'])
        self.assertIsNone(stale['failed_consent_count'])
        self.assertEqual(stale['last_attempt_at'], fresh.last_attempt_at)
        fresh.record(report, None)
        self.assertEqual(fresh.snapshot()['state'], 'ok')
        fresh._last_attempt_time -= timedelta(seconds=RETENTION_STALE_AFTER_SECONDS + 1)
        self.assertEqual(fresh.snapshot()['state'], 'stale')

        incomplete = RetentionTelemetry(enabled=True)
        incomplete.record({**report, 'failed_sessions': ['app-' + 'a' * 32]}, None)
        self.assertEqual(incomplete.snapshot()['state'], 'incomplete')
        incomplete._last_attempt_monotonic -= RETENTION_STALE_AFTER_SECONDS + 1
        self.assertEqual(incomplete.snapshot()['state'], 'stale')
        self.assertIsNone(incomplete.snapshot()['failed_session_count'])
        incomplete.record(report, None)
        self.assertEqual(incomplete.snapshot()['state'], 'ok')

        origin = 'http://127.0.0.1:8765'
        app = create_console(object(), 'synthetic-token', origin, REPO_ROOT / 'computer',
                             retention_status=telemetry.snapshot)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=origin, headers={'Origin': origin}) as client:
            self.assertEqual((await client.get('/api/retention')).status_code, 401)
            self.assertEqual((await client.post('/api/login', json={'token': 'synthetic-token'})).status_code, 200)
            response = await client.get('/api/retention')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), telemetry.snapshot())
            self.assertNotIn('/secret', response.text)
            self.assertEqual((await client.get('/api/retention?path=/tmp/other')).status_code, 400)


if __name__ == '__main__':
    unittest.main()

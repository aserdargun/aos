from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical
from aos.dataset import validator
from aos.recovery import recovery_inventory
from aos.recovery_backup import BackupManifest, backup_database, restore_backup, verify_backup
from aos.storage import TrajectoryStore
import test_recovery_inventory as fixtures


class RecoveryBackupTests(unittest.TestCase):
    setUp = fixtures.RecoveryInventoryTests.setUp
    job = fixtures.RecoveryInventoryTests.job
    unfinished = fixtures.RecoveryInventoryTests.unfinished

    def backup(self):
        output = self.root / 'backup'
        backup_database(self.database, output)
        return output

    def test_wal_backup_restore_preserves_rows_not_live_authority(self):
        self.unfinished()
        paths = [self.database, Path(str(self.database) + '-wal')]
        before = {path: path.read_bytes() for path in paths}
        output = self.backup()
        report = verify_backup(output)
        self.assertEqual(report['schema_version_number'], 17)
        self.assertFalse(report['resume_authorized'])
        self.assertFalse(report['runtime_assets_included'])
        self.assertFalse(report['review_journal_included'])
        self.assertEqual((output / 'store.sqlite').read_bytes()[18:20], b'\x01\x01')
        for path in output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        destination = self.root / 'restored.sqlite'
        restored = restore_backup(output, destination)
        self.assertEqual(restored['status'], 'database_copy_restored_requires_reconciliation')
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        inventory = recovery_inventory(destination)
        self.assertEqual(inventory.totals.approval_approved, 1)
        self.assertEqual(inventory.totals.action_intent, 2)
        self.assertEqual(inventory.totals.input_running, 1)
        writer = TrajectoryStore(destination)
        try:
            inventory = recovery_inventory(destination)
            self.assertEqual(inventory.totals.approval_approved, 0)
            self.assertEqual(inventory.totals.action_uncertain, 2)
            self.assertEqual(inventory.jobs[0].status, 'cancelled')
        finally:
            writer.close()
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        self.assertEqual(verify_backup(output), report)

    def test_uncommitted_changes_are_excluded(self):
        self.store.connection.execute("UPDATE runs SET status='paused'")
        try:
            output = self.backup()
            report = recovery_inventory(output / 'store.sqlite')
            self.assertEqual(report.jobs[0].run_status, 'succeeded')
        finally:
            self.store.connection.rollback()

    def test_existing_database_directory_lock_or_sidecars_are_never_overwritten(self):
        output = self.backup()
        before = self.database.read_bytes()
        with self.assertRaises(FileExistsError):
            backup_database(self.database, output)
        with self.assertRaisesRegex(ValueError, 'restore_destination_not_new'):
            restore_backup(output, self.database)
        self.assertEqual(self.database.read_bytes(), before)
        for suffix in ('-wal', '-shm', '-journal', '.lock'):
            destination = self.root / ('target' + suffix + '.sqlite')
            sidecar = Path(str(destination) + suffix)
            sidecar.write_text('synthetic existing data')
            with self.assertRaisesRegex(ValueError, 'restore_destination_not_new'):
                restore_backup(output, destination)
            self.assertEqual(sidecar.read_text(), 'synthetic existing data')
            self.assertFalse(destination.exists())

    def test_private_paths_permissions_symlinks_and_hardlinks(self):
        output = self.backup()
        database = output / 'store.sqlite'
        database.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private_regular_file_required'):
            verify_backup(output)
        database.chmod(0o600)
        link = self.root / 'database-link'
        os.link(database, link)
        with self.assertRaisesRegex(ValueError, 'private_regular_file_required'):
            verify_backup(output)
        link.unlink()
        link.symlink_to(output, target_is_directory=True)
        with self.assertRaises(ValueError):
            verify_backup(link)
        destination = self.root / 'restored.sqlite'
        destination.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'restore_destination_not_new'):
            restore_backup(output, destination)
        unsafe = self.root / 'public'
        unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'private_directory_required'):
            restore_backup(output, unsafe / 'restore.sqlite')

    def test_content_manifest_source_binding_and_extra_files_are_checked(self):
        output = self.backup()
        manifest_path = output / 'manifest.json'
        original = manifest_path.read_bytes()
        for change in ({'database_sha256': 'f' * 64}, {'source_snapshot_sha256': 'f' * 64},
                       {'schema_sha256': 'f' * 64}, {'migrations': {}}, {'execution_authorized': True}):
            manifest_path.write_text(canonical({**json.loads(original), **change}))
            with self.assertRaises(ValueError):
                verify_backup(output)
        manifest_path.write_bytes(original)
        extra = output / 'store.sqlite-wal'
        extra.write_bytes(b'synthetic')
        with self.assertRaisesRegex(ValueError, 'backup_file_set_invalid'):
            verify_backup(output)
        extra.unlink()
        database = output / 'store.sqlite'
        database.write_bytes(database.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, 'backup_content_mismatch'):
            verify_backup(output)

    def test_rehashed_invalid_database_is_not_accepted(self):
        output = self.backup()
        database = output / 'store.sqlite'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE unexpected (value TEXT)')
        payload = database.read_bytes()
        manifest_path = output / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest.update(database_bytes=len(payload), database_sha256=hashlib.sha256(payload).hexdigest(),
                        source_snapshot_sha256=hashlib.sha256(payload).hexdigest())
        manifest_path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError, 'backup_integrity_failure'):
            verify_backup(output)

    def test_fsync_failure_is_not_success_or_overwrite_permission(self):
        output = self.root / 'partial'
        with patch('aos.recovery_backup.os.fsync', side_effect=OSError('synthetic I/O failure')):
            with self.assertRaises(OSError):
                backup_database(self.database, output)
        self.assertTrue(output.is_dir())
        with self.assertRaises(ValueError):
            verify_backup(output)
        with self.assertRaises(FileExistsError):
            backup_database(self.database, output)

    def test_v7_stays_v7_and_cli_summary_is_minimized(self):
        source = self.root / 'v7.sqlite'
        with closing(sqlite3.connect(source)) as connection, connection:
            for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:7]:
                connection.executescript(migration.read_text())
        output = self.root / 'v7-backup'
        backup_database(source, output)
        result = subprocess.run([sys.executable, '-m', 'aos.recovery_backup', 'verify', '--backup', str(output)],
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['schema_version_number'], 7)
        self.assertNotIn(str(self.root), result.stdout)
        self.assertFalse(json.loads(result.stdout)['execution_authorized'])

    def test_canonical_schema_and_synthetic_fixture(self):
        schema = json.loads((REPO_ROOT / 'schemas/recovery_backup.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **BackupManifest.model_json_schema()})
        fixture = json.loads((REPO_ROOT / 'examples/recovery_backup.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('recovery_backup').validate(fixture['manifest'])
        output = self.backup()
        manifest = json.loads((output / 'manifest.json').read_text())
        validator('recovery_backup').validate(manifest)
        for change in ({'execution_authorized': True}, {'resume_authorized': True}, {'database_file': '../outside'}, {'raw_state': 'secret'}):
            self.assertFalse(validator('recovery_backup').is_valid({**manifest, **change}))


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from aos.contracts import REPO_ROOT
from aos.dataset_audit import audit_snapshot
from aos.owned_parameter_skill_candidate import OwnedParameterSkillCandidateSession
from aos.owned_parameter_skill_review import OwnedParameterSkillReviewSession
from aos.owned_parameter_skill_review_history import (
    MIGRATION_REQUIRED, OwnedParameterSkillReviewHistory)

from test_owned_parameter_skill_candidate import create_accepted_bootstrap


class OwnedParameterSkillReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        self.source = create_accepted_bootstrap(self)
        self.candidates = OwnedParameterSkillCandidateSession(
            self.source.directory, self.source.manifest_sha256, self.source.database,
            self.source.journal_directory, self.source.candidate_directory)
        _, self.candidate_sha = self.candidates.preview(self.source.intent_sha256)
        self.candidates.publish(self.source.intent_sha256, self.candidate_sha, 'PUBLISH_MANUAL_CANDIDATE')
        self.reviews = OwnedParameterSkillReviewSession(self.candidates, self.source.root / 'review-history-test')
        self.review, self.review_sha = self.reviews.preview(self.candidate_sha)
        self.history = OwnedParameterSkillReviewHistory(self.source.database)
        self.directory_sha = self.review['review_directory_sha256']

    def test_readonly_empty_history_does_not_change_database_or_bootstrap(self):
        before = self.source.database.read_bytes()
        self.assertEqual(self.history.records(self.directory_sha), {})
        self.assertEqual(self.source.database.read_bytes(), before)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)

    def test_exact_authorization_is_atomic_idempotent_and_independent_of_missing_file(self):
        self.history.authorize(self.review)
        self.history.authorize(self.review)
        self.assertEqual(self.history.records(self.directory_sha),
                         {self.review_sha + '.accept.json': self.review})
        self.assertFalse(self.reviews.store.directory.exists())
        fresh = OwnedParameterSkillReviewHistory(self.source.database)
        self.assertEqual(fresh.records(self.directory_sha), self.history.records(self.directory_sha))
        self.assertEqual(self.source.store.connection.execute(
            'SELECT count(*) FROM owned_parameter_skill_review_history').fetchone()[0], 1)

    def test_append_only_sql_and_revocation_binding_reject_changes(self):
        self.reviews.accept(self.candidate_sha, self.review_sha, 'ACCEPT_MANUAL_REVIEW')
        revocation, _checksum = self.reviews.revocation_preview(self.review_sha)
        connection = self.source.store.connection
        for statement in ('DELETE FROM owned_parameter_skill_review_history',
                          "UPDATE owned_parameter_skill_review_history SET created_at='changed'"):
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()
        for changes in ({'candidate_sha256': 'a' * 64}, {'source_fingerprint_sha256': 'b' * 64},
                        {'review_sha256': 'c' * 64}, {'review_parent_identity':
                            revocation['review_parent_identity'] | {'inode': revocation['review_parent_identity']['inode'] + 1}}):
            with self.assertRaises(ValueError):
                self.history.authorize(revocation | changes)
        self.assertEqual(len(self.history.records(self.directory_sha)), 1)

    def test_invalid_claims_and_duplicate_candidate_slot_fail_closed(self):
        self.history.authorize(self.review)
        for changes in ({'reviewed': 1}, {'activation_authorized': True}, {'model_calls': 1},
                        {'extra': 'not canonical review'}, {'source_run_ref': 'a' * 64}):
            with self.assertRaises(ValueError):
                self.history.authorize(self.review | changes)
        self.assertEqual(len(self.history.records(self.directory_sha)), 1)

    def test_changed_sql_schema_and_database_identity_reject_read_and_write(self):
        self.history.records(self.directory_sha)
        connection = self.source.store.connection
        connection.execute('DROP TRIGGER owned_parameter_skill_review_history_no_delete')
        connection.commit()
        with self.assertRaisesRegex(ValueError, 'schema_changed'):
            self.history.records(self.directory_sha)
        with self.assertRaisesRegex(ValueError, 'schema_changed'):
            self.history.authorize(self.review)
        original = self.source.database.with_name('original-history.sqlite')
        self.source.database.rename(original)
        shutil.copyfile(original, self.source.database)
        self.source.database.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'database_changed'):
            self.history.records(self.directory_sha)

    def test_legacy_twenty_is_readonly_rejected_and_append_migration_preserves_rows(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / 'legacy.sqlite'
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        database.chmod(0o600)
        migrations = sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))
        for migration in migrations[:20]:
            connection.executescript(migration.read_text())
        connection.execute("INSERT INTO tasks VALUES('synthetic-task','original','normalized','en','[]','[]','2026-09-30T00:00:00Z')")
        connection.commit()
        before = database.read_bytes()
        history = OwnedParameterSkillReviewHistory(database)
        with self.assertRaisesRegex(ValueError, MIGRATION_REQUIRED):
            history.records(self.directory_sha)
        self.assertEqual(database.read_bytes(), before)
        with audit_snapshot(database) as (_snapshot, identity):
            self.assertEqual(len(identity['migrations']), 20)
        original = connection.execute('SELECT * FROM tasks').fetchall()
        connection.executescript(migrations[20].read_text())
        self.assertEqual(connection.execute('SELECT * FROM tasks').fetchall(), original)
        self.assertEqual(history.records(self.directory_sha), {})
        with audit_snapshot(database) as (_snapshot, identity):
            self.assertEqual(len(identity['migrations']), 21)

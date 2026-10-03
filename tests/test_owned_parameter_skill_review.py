"""Manual review tests over actual CPU fixture/operator/TLS bootstrap evidence."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.dataset_audit import audit_snapshot
from aos.owned_parameter_skill_candidate import HUMAN_CONFIRMATION, OwnedParameterSkillCandidateSession
from aos.owned_parameter_skill_review import (
    ACCEPT_CONFIRMATION, RECOVERY_CONFIRMATION, REVOKE_CONFIRMATION, OwnedParameterSkillReview,
    OwnedParameterSkillReviewRecovery,
    OwnedParameterSkillReviewRevocation, OwnedParameterSkillReviewSession,
    OwnedParameterSkillReviewStatus, review_summary,
)

from test_owned_parameter_skill_candidate import create_accepted_bootstrap


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS preparation')
class OwnedParameterSkillReviewTests(unittest.TestCase):
    def setUp(self):
        self.source = create_accepted_bootstrap(self)
        self.candidates, self.candidate, self.candidate_sha256 = self.publish_candidate(self.source)
        self.directory = self.source.directory / 'manual-skill-reviews'
        self.session = OwnedParameterSkillReviewSession(self.candidates, self.directory)

    def publish_candidate(self, source):
        candidates = OwnedParameterSkillCandidateSession(
            source.directory, source.manifest_sha256, source.database,
            source.journal_directory, source.candidate_directory)
        candidate, checksum = candidates.preview(source.intent_sha256)
        candidates.publish(source.intent_sha256, checksum, HUMAN_CONFIRMATION)
        return candidates, candidate, checksum

    def new_session(self, directory=None):
        return OwnedParameterSkillReviewSession(self.candidates, directory or self.directory)

    def accept(self):
        review, checksum = self.session.preview(self.candidate_sha256)
        self.assertEqual(self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
                         (review, checksum))
        return review, checksum

    def source_bytes(self):
        return {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for directory in (self.source.directory, self.source.journal_directory,
                                  self.source.candidate_directory)
                for path in directory.rglob('*')
                if path.is_file() and self.directory not in path.parents}

    def test_actual_source_bound_manual_review_and_permanent_revocation_without_source_effects(self):
        before = self.source_bytes()
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            before_snapshot = identity['sha256']
        review, checksum = self.session.preview(self.candidate_sha256)
        self.assertFalse(self.directory.exists())
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            self.assertEqual(identity['sha256'], before_snapshot)
        self.assertEqual(review['candidate_sha256'], self.candidate_sha256)
        self.assertEqual(review['source_fingerprint_sha256'], self.candidate['source_fingerprint_sha256'])
        self.assertEqual(review['source_run_ref'], self.source.receipt['recipe_audit']['run_ref'])
        self.assertEqual(review['recipe_sha256'], digest(self.candidate['recipe']))
        self.assertEqual(review['field_binding_sha256'], digest(self.candidate['field_bindings']))
        self.assertEqual(review['form_body_sha256'], self.candidate['form_body_sha256'])
        self.assertEqual(review['scope'], self.candidate['scope'])
        self.assertEqual(review['review_scope'], 'synthetic_manual_recipe_review')
        self.assertEqual(review['reviewer'], 'local_authenticated_user')
        self.assertTrue(review['reviewed'])
        validator('owned_parameter_skill_review').validate(review)
        self.assertEqual(self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
                         (review, checksum))
        accepted_path = self.directory / (checksum + '.accept.json')
        original_receipt = (accepted_path.stat().st_ino, accepted_path.stat().st_mtime_ns,
                            accepted_path.read_bytes())
        self.assertEqual(accepted_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
                         (review, checksum))
        status, status_sha256 = self.new_session().read(checksum)
        self.assertEqual(status['status'], 'accepted')
        self.assertEqual(status['review'], review)
        self.assertTrue(status['reviewed'])
        self.assertEqual(status['revocations'], [])
        self.assertEqual(status_sha256, digest(status))
        validator('owned_parameter_skill_review_status').validate(status)
        revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        self.assertEqual(list(self.directory.iterdir()), [accepted_path])
        self.assertEqual(revocation['review_sha256'], checksum)
        self.assertFalse(revocation['reviewed'])
        validator('owned_parameter_skill_review_revocation').validate(revocation)
        self.assertEqual(self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION),
                         (revocation, revocation_sha256))
        revoked_path = self.directory / (revocation_sha256 + '.revoke.json')
        original_revocation = (revoked_path.stat().st_ino, revoked_path.stat().st_mtime_ns,
                               revoked_path.read_bytes())
        self.assertEqual(self.new_session().revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION),
                         (revocation, revocation_sha256))
        self.assertEqual((revoked_path.stat().st_ino, revoked_path.stat().st_mtime_ns,
                          revoked_path.read_bytes()), original_revocation)
        revoked_status, revoked_sha256 = self.new_session().read(checksum)
        self.assertEqual(revoked_sha256, digest(revoked_status))
        self.assertNotEqual(revoked_sha256, status_sha256)
        self.assertEqual(revoked_status['status'], 'revoked')
        self.assertFalse(revoked_status['reviewed'])
        self.assertEqual(revoked_status['review'], review)
        self.assertEqual(revoked_status['revocations'], [
            {'revocation_sha256': revocation_sha256, 'revocation': revocation}])
        for session in (self.session, self.new_session()):
            with self.assertRaises(ValueError):
                session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)
            with self.assertRaises(ValueError):
                session.preview(self.candidate_sha256)
        self.assertEqual((accepted_path.stat().st_ino, accepted_path.stat().st_mtime_ns,
                          accepted_path.read_bytes()), original_receipt)
        self.assertEqual(self.candidates.read(self.candidate_sha256), (self.candidate, self.candidate_sha256))
        self.assertFalse(self.candidate['reviewed'])
        self.assertEqual(self.source_bytes(), before)
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            after_authorized_metadata = identity['sha256']
        self.assertNotEqual(after_authorized_metadata, before_snapshot)
        self.assertEqual(len(self.session.store.history.records(review['review_directory_sha256'])), 2)
        self.session.read(checksum)
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            self.assertEqual(identity['sha256'], after_authorized_metadata)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM trajectory_labels').fetchone()[0], 0)

    def test_explicit_human_literals_and_exact_confirmation_hashes_required(self):
        _review, checksum = self.session.preview(self.candidate_sha256)
        with self.assertRaises(TypeError):
            self.session.accept(self.candidate_sha256, checksum)
        for value in (None, True, '', 'ACCEPT', REVOKE_CONFIRMATION):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.session.accept(self.candidate_sha256, checksum, value)
        with self.assertRaises(ValueError):
            self.session.accept(self.candidate_sha256, 'a' * 64, ACCEPT_CONFIRMATION)
        self.assertFalse(self.directory.exists())
        self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)
        _revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        with self.assertRaises(TypeError):
            self.session.revoke(checksum, revocation_sha256)
        for value in (None, True, '', 'REVOKE', ACCEPT_CONFIRMATION):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.session.revoke(checksum, revocation_sha256, value)
        with self.assertRaises(ValueError):
            self.session.revoke(checksum, checksum, REVOKE_CONFIRMATION)
        self.assertEqual(self.session.read(checksum)[0]['status'], 'accepted')

    def test_unpublished_candidate_and_unaccepted_review_cannot_create_records(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.session.preview('a' * 64)
        _review, checksum = self.session.preview(self.candidate_sha256)
        with self.assertRaises(FileNotFoundError):
            self.session.read(checksum)
        with self.assertRaises(FileNotFoundError):
            self.session.revocation_preview(checksum)
        self.assertFalse(self.directory.exists())

    def test_review_destination_and_parent_identity_bound_to_confirmation(self):
        review, checksum = self.session.preview(self.candidate_sha256)
        self.assertEqual(review['review_directory_sha256'], digest({'directory': str(self.directory)}))
        moved_directory = self.source.directory / 'different-manual-reviews'
        with self.assertRaises(ValueError):
            self.new_session(moved_directory).accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)
        self.assertFalse(moved_directory.exists())
        parent = self.source.root / 'private-review-parent'
        parent.mkdir(mode=0o700)
        session = self.new_session(parent / 'reviews')
        _review, reviewed_checksum = session.preview(self.candidate_sha256)
        renamed_parent = self.source.root / 'renamed-private-review-parent'
        parent.rename(renamed_parent)
        parent.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            session.accept(self.candidate_sha256, reviewed_checksum, ACCEPT_CONFIRMATION)

    def test_every_operation_reaudits_changed_source_and_rejects_stale_review(self):
        _review, checksum = self.accept()
        _revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        connection = self.source.store.connection
        connection.execute("UPDATE actions SET status='error' WHERE tool='browser.form.fill'")
        connection.commit()
        for operation in (
            lambda: self.session.preview(self.candidate_sha256),
            lambda: self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
            lambda: self.session.read(checksum),
            lambda: self.session.revocation_preview(checksum),
            lambda: self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_revoked_review_still_requires_current_source_provenance(self):
        _review, checksum = self.accept()
        _revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)
        source_path = self.source.directory / 'remote-form-skill-recipe.json'
        source_path.write_bytes(source_path.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            self.session.read(checksum)
        with self.assertRaises(ValueError):
            self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)

    def test_retained_store_rejects_revocation_disappearance_immediately_after_write(self):
        _review, checksum = self.accept()
        _revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)
        (self.directory / (revocation_sha256 + '.revoke.json')).unlink()
        for operation in (
            lambda: self.session.read(checksum),
            lambda: self.session.preview(self.candidate_sha256),
            lambda: self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
            lambda: self.session.revocation_preview(checksum),
            lambda: self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION),
        ):
            with self.assertRaisesRegex(ValueError, 'observed_record_missing'):
                operation()
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_retained_store_rejects_acceptance_disappearance_immediately_after_write(self):
        _review, checksum = self.accept()
        (self.directory / (checksum + '.accept.json')).unlink()
        for operation in (
            lambda: self.session.read(checksum),
            lambda: self.session.preview(self.candidate_sha256),
            lambda: self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
            lambda: self.session.revocation_preview(checksum),
        ):
            with self.assertRaisesRegex(ValueError, 'observed_record_missing'):
                operation()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_retained_reader_tracks_validated_records_from_existing_store(self):
        _review, checksum = self.accept()
        reader = self.new_session()
        reader.read(checksum)
        (self.directory / (checksum + '.accept.json')).unlink()
        with self.assertRaisesRegex(ValueError, 'observed_record_missing'):
            reader.read(checksum)

    def test_fresh_process_session_rejects_missing_revocation_and_exact_recovery_preserves_revoked(self):
        _review, checksum = self.accept()
        revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)
        path = self.directory / (revocation_sha256 + '.revoke.json')
        original = path.read_bytes()
        path.unlink()
        fresh = self.new_session()
        for operation in (
            lambda: fresh.read(checksum),
            lambda: fresh.preview(self.candidate_sha256),
            lambda: fresh.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
            lambda: fresh.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION),
        ):
            with self.assertRaisesRegex(ValueError, 'anchored_record_missing'):
                operation()
        history_before = fresh.store.history.records(revocation['review_directory_sha256'])
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            snapshot_before = identity['sha256']
        recovery, recovery_sha256 = fresh.recovery_preview(revocation_sha256)
        self.assertEqual(recovery['record'], revocation)
        self.assertEqual(recovery['record_stage'], 'revoke')
        self.assertFalse(recovery['reviewed'])
        self.assertFalse(path.exists())
        validator('owned_parameter_skill_review_recovery').validate(recovery)
        self.assertEqual(fresh.recover(revocation_sha256, recovery_sha256, RECOVERY_CONFIRMATION),
                         (recovery, recovery_sha256))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.new_session().read(checksum)[0]['status'], 'revoked')
        self.assertEqual(fresh.store.history.records(revocation['review_directory_sha256']), history_before)
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            self.assertEqual(identity['sha256'], snapshot_before)
        with self.assertRaises(ValueError):
            self.new_session().accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)

    def test_authorized_acceptance_precedes_filesystem_publication_and_requires_explicit_recovery(self):
        review, checksum = self.session.preview(self.candidate_sha256)
        with patch('aos.web_goal_execution_journal._write_private_child',
                   side_effect=OSError('Synthetic crash before private filesystem publication')):
            with self.assertRaises(OSError):
                self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)
        self.assertEqual(list(self.directory.iterdir()), [])
        anchored = self.session.store.history.records(review['review_directory_sha256'])
        self.assertEqual(anchored, {checksum + '.accept.json': review})
        fresh = self.new_session()
        with self.assertRaisesRegex(ValueError, 'anchored_record_missing'):
            fresh.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)
        recovery, recovery_sha256 = fresh.recovery_preview(checksum)
        self.assertEqual(recovery['record_stage'], 'accept')
        self.assertEqual(recovery['record'], review)
        self.assertEqual(fresh.recover(checksum, recovery_sha256, RECOVERY_CONFIRMATION),
                         (recovery, recovery_sha256))
        self.assertEqual(self.new_session().read(checksum)[0]['status'], 'accepted')
        self.assertEqual(fresh.store.history.records(review['review_directory_sha256']), anchored)

    def test_authorized_revocation_crash_cannot_reaccept_and_recovery_restores_revocation(self):
        _review, checksum = self.accept()
        revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        with patch('aos.web_goal_execution_journal._write_private_child',
                   side_effect=OSError('Synthetic crash before revocation publication')):
            with self.assertRaises(OSError):
                self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)
        fresh = self.new_session()
        with self.assertRaisesRegex(ValueError, 'anchored_record_missing'):
            fresh.read(checksum)
        with self.assertRaisesRegex(ValueError, 'anchored_record_missing'):
            fresh.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)
        recovery, recovery_sha256 = fresh.recovery_preview(revocation_sha256)
        self.assertEqual(recovery['record'], revocation)
        fresh.recover(revocation_sha256, recovery_sha256, RECOVERY_CONFIRMATION)
        self.assertEqual(fresh.read(checksum)[0]['status'], 'revoked')

    def test_recovery_requires_explicit_exact_confirmation_and_missing_authorized_record(self):
        _review, checksum = self.accept()
        with self.assertRaises(ValueError):
            self.session.recovery_preview(checksum)
        (self.directory / (checksum + '.accept.json')).unlink()
        fresh = self.new_session()
        recovery, recovery_sha256 = fresh.recovery_preview(checksum)
        for value in (None, True, '', ACCEPT_CONFIRMATION):
            with self.assertRaises(ValueError):
                fresh.recover(checksum, recovery_sha256, value)
        with self.assertRaises(TypeError):
            fresh.recover(checksum, recovery_sha256)
        with self.assertRaises(ValueError):
            fresh.recover(checksum, 'a' * 64, RECOVERY_CONFIRMATION)
        with self.assertRaises(ValueError):
            fresh.recovery_preview('a' * 64)
        self.assertEqual(list(self.directory.iterdir()), [])
        summary = review_summary(recovery, recovery_sha256)
        self.assertEqual(summary['record_sha256'], checksum)
        self.assertEqual(summary['recovery_sha256'], recovery_sha256)
        for flag in ('reviewed', 'activation_authorized', 'execution_authorized', 'training_ready',
                     'native_model_verified', 'gpu_release_verified'):
            self.assertIs(summary[flag], False)
            with self.assertRaises(ValueError):
                OwnedParameterSkillReviewRecovery.model_validate_json(canonical(recovery | {flag: True}))
            self.assertFalse(validator('owned_parameter_skill_review_recovery').is_valid(recovery | {flag: True}))

    def test_recovery_rejects_changed_source_destination_or_existing_altered_file(self):
        review, checksum = self.accept()
        path = self.directory / (checksum + '.accept.json')
        path.unlink()
        recovery, recovery_sha256 = self.session.recovery_preview(checksum)
        different_directory = self.source.directory / 'different-manual-reviews'
        different_directory.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.new_session(different_directory).recovery_preview(checksum)
        path.write_text(canonical(review) + ' ')
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.session.recover(checksum, recovery_sha256, RECOVERY_CONFIRMATION)
        self.assertEqual(path.read_text(), canonical(review) + ' ')
        path.unlink()
        self.source.store.connection.execute("UPDATE actions SET status='error' WHERE tool='browser.form.fill'")
        self.source.store.connection.commit()
        with self.assertRaises(ValueError):
            self.session.recovery_preview(checksum)
        with self.assertRaises(ValueError):
            self.session.recover(checksum, recovery_sha256, RECOVERY_CONFIRMATION)
        self.assertFalse(path.exists())

    def test_recovery_of_missing_acceptance_keeps_anchored_revocation_effective(self):
        _review, checksum = self.accept()
        _revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)
        (self.directory / (checksum + '.accept.json')).unlink()
        fresh = self.new_session()
        recovery, recovery_sha256 = fresh.recovery_preview(checksum)
        fresh.recover(checksum, recovery_sha256, RECOVERY_CONFIRMATION)
        self.assertEqual(fresh.read(checksum)[0]['status'], 'revoked')
        self.assertEqual(recovery['record_stage'], 'accept')

    def test_filesystem_only_unanchored_receipts_are_never_adopted_into_history(self):
        review, checksum = self.session.preview(self.candidate_sha256)
        self.directory.mkdir(mode=0o700)
        path = self.directory / (checksum + '.accept.json')
        path.write_text(canonical(review))
        path.chmod(0o600)
        for operation in (
            lambda: self.session.read(checksum),
            lambda: self.session.preview(self.candidate_sha256),
            lambda: self.session.accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
            lambda: self.session.recovery_preview(checksum),
        ):
            with self.assertRaisesRegex(ValueError, 'unanchored_record'):
                operation()
        self.assertEqual(self.session.store.history.records(review['review_directory_sha256']), {})

    def test_unrelated_runtime_database_changes_preserve_deterministic_review_identity(self):
        review, checksum = self.accept()
        self.source.store.connection.execute("UPDATE desktop_sessions SET status='stopped',generation=generation+1")
        self.source.store.connection.commit()
        self.assertEqual(self.new_session().preview(self.candidate_sha256), (review, checksum))
        self.assertEqual(self.new_session().read(checksum)[0]['status'], 'accepted')
        self.assertEqual(self.new_session().accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION),
                         (review, checksum))

    def test_actual_ignored_runtime_data_layout_keeps_reviews_outside_checkout(self):
        temporary = tempfile.TemporaryDirectory(prefix='app-manual-review-test-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        source = create_accepted_bootstrap(self, runtime_directory=Path(temporary.name))
        candidates, _candidate, candidate_sha256 = self.publish_candidate(source)
        session = OwnedParameterSkillReviewSession(candidates, source.directory / 'manual-skill-reviews')
        self.assertIn(REPO_ROOT / 'data', source.database.parents)
        self.assertNotIn(REPO_ROOT, session.store.directory.parents)
        review, checksum = session.preview(candidate_sha256)
        self.assertEqual(session.accept(candidate_sha256, checksum, ACCEPT_CONFIRMATION), (review, checksum))
        self.assertEqual(session.read(checksum)[0]['status'], 'accepted')
        revocation, revocation_sha256 = session.revocation_preview(checksum)
        self.assertEqual(session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION),
                         (revocation, revocation_sha256))
        self.assertEqual(session.read(checksum)[0]['status'], 'revoked')

    def test_review_paths_reject_checkout_candidate_journal_and_execution_workspace(self):
        for path in (REPO_ROOT / 'data' / 'reviews', REPO_ROOT / 'src' / 'reviews',
                     self.source.directory, self.source.candidate_directory,
                     self.source.candidate_directory / 'reviews', self.source.journal_directory,
                     self.source.journal_directory / 'reviews', self.source.root):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.new_session(path)
        forbidden = self.source.root / 'workspace' / 'reviews'
        with self.assertRaises(ValueError):
            self.new_session(forbidden).preview(self.candidate_sha256)
        self.assertFalse(forbidden.exists())

    def test_model_schema_and_summary_reject_claim_escalation_and_inconsistent_status(self):
        review, checksum = self.accept()
        revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        status, status_sha256 = self.session.read(checksum)
        for payload, sha256, model, schema in (
            (review, checksum, OwnedParameterSkillReview, 'owned_parameter_skill_review'),
            (revocation, revocation_sha256, OwnedParameterSkillReviewRevocation,
             'owned_parameter_skill_review_revocation'),
            (status, status_sha256, OwnedParameterSkillReviewStatus, 'owned_parameter_skill_review_status'),
        ):
            summary = review_summary(payload, sha256)
            self.assertNotIn('Synthetic candidate note', canonical(summary))
            self.assertNotIn('Synthetic candidate record', canonical(summary))
            for flag in ('native_model_verified', 'released_skill_verified', 'site_outcome_verified',
                         'account_scope_verified', 'held_out_independence_verified', 'activation_authorized',
                         'execution_authorized', 'training_ready', 'gpu_release_verified',
                         'replay_authorized', 'runtime_started'):
                self.assertIs(summary[flag], False)
                with self.subTest(schema=schema, flag=flag), self.assertRaises(ValueError):
                    model.model_validate_json(canonical(payload | {flag: True}))
                self.assertFalse(validator(schema).is_valid(payload | {flag: True}))
            for value in (1, 'true'):
                with self.subTest(schema=schema, value=value), self.assertRaises(ValueError):
                    model.model_validate_json(canonical(payload | {'reviewed': value}))
                self.assertFalse(validator(schema).is_valid(payload | {'reviewed': value}))
        for changes in ({'reviewed': False}, {'status': 'revoked'}):
            with self.assertRaises(ValueError):
                OwnedParameterSkillReviewStatus.model_validate_json(canonical(status | changes))
            self.assertFalse(validator('owned_parameter_skill_review_status').is_valid(status | changes))
        altered = deepcopy(status)
        altered['review']['candidate_sha256'] = 'a' * 64
        with self.assertRaises(ValueError):
            OwnedParameterSkillReviewStatus.model_validate_json(canonical(altered))

    def test_private_store_privacy_canonical_and_orphan_revocation_guards(self):
        review, checksum = self.accept()
        path = self.directory / (checksum + '.accept.json')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        path.chmod(0o600)
        hardlink = self.source.root / 'review-hardlink.json'
        os.link(path, hardlink)
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        hardlink.unlink()
        path.write_text(canonical(review) + '\n')
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        path.write_text(canonical(review))
        _revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)
        path.unlink()
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        with self.assertRaises(ValueError):
            self.new_session().accept(self.candidate_sha256, checksum, ACCEPT_CONFIRMATION)

    def test_tampered_review_and_revocation_source_bindings_fail_closed(self):
        review, checksum = self.accept()
        path = self.directory / (checksum + '.accept.json')
        review['candidate_sha256'] = 'a' * 64
        path.write_text(canonical(review))
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        review['candidate_sha256'] = self.candidate_sha256
        path.write_text(canonical(review))
        revocation, revocation_sha256 = self.session.revocation_preview(checksum)
        self.session.revoke(checksum, revocation_sha256, REVOKE_CONFIRMATION)
        old_path = self.directory / (revocation_sha256 + '.revoke.json')
        revocation['scope'] = revocation['scope'] | {'account_role': 'synthetic-foreign-role'}
        changed_sha256 = digest(revocation)
        old_path.rename(self.directory / (changed_sha256 + '.revoke.json'))
        (self.directory / (changed_sha256 + '.revoke.json')).write_text(canonical(revocation))
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)


if __name__ == '__main__':
    unittest.main()

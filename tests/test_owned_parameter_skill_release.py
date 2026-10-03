"""Manual release chains over actual CPU fixture, TLS and original DB evidence."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.owned_parameter_project import APPLICATIONS
from aos.owned_parameter_skill_candidate import OwnedParameterSkillCandidateSession
from aos.owned_parameter_skill_review import OwnedParameterSkillReviewSession
from aos.owned_parameter_skill_release import (
    RELEASE_CONFIRMATION, ROLLBACK_CONFIRMATION, SELECT_CONFIRMATION,
    OwnedParameterSkillRelease, OwnedParameterSkillReleaseSession,
    OwnedParameterSkillSelection, release_summary,
)
from aos.owned_parameter_skill_release_history import MIGRATION_REQUIRED, OwnedParameterSkillReleaseHistory
from aos.storage import TrajectoryStore

import test_owned_parameter_project_desktop as desktop_fixture
from test_owned_parameter_skill_candidate import create_accepted_bootstrap


def publish_reviewed_source(source):
    candidates = OwnedParameterSkillCandidateSession(
        source.directory, source.manifest_sha256, source.database,
        source.journal_directory, source.candidate_directory)
    candidate, candidate_sha256 = candidates.preview(source.intent_sha256)
    candidates.publish(source.intent_sha256, candidate_sha256, 'PUBLISH_MANUAL_CANDIDATE')
    reviews = OwnedParameterSkillReviewSession(candidates, source.directory.parent / 'manual-reviews')
    review, review_sha256 = reviews.preview(candidate_sha256)
    reviews.accept(candidate_sha256, review_sha256, 'ACCEPT_MANUAL_REVIEW')
    return SimpleNamespace(source=source, candidates=candidates, candidate=candidate,
                           candidate_sha256=candidate_sha256, reviews=reviews,
                           review=review, review_sha256=review_sha256)


def create_shared_reviewed_bootstraps(test_case):
    fixture = desktop_fixture.OwnedParameterProjectDesktopTests()
    test_case.addCleanup(fixture.doCleanups)
    fixture.setUp()
    database = fixture.root / 'original-shared.sqlite'
    store = TrajectoryStore(database)
    test_case.addCleanup(store.close)
    sources = []
    for index in range(2):
        receipt = fixture.execute_case(index, APPLICATIONS[0], 'Synthetic record ' + str(index),
                                       'Synthetic version ' + str(index), store=store,
                                       expected_run_count=index + 1)
        root = fixture.root / ('case-' + str(index))
        intent_path, = (root / 'project-execution').glob('*.intent.json')
        intent = json.loads(intent_path.read_text())
        source = SimpleNamespace(root=root, directory=root / 'project',
            manifest_sha256=intent['source']['manifest_sha256'], database=database,
            journal_directory=root / 'project-execution', candidate_directory=root / 'manual-candidates',
            intent_sha256=intent_path.name[:64], receipt=receipt, store=store)
        sources.append(publish_reviewed_source(source))
    catalog = fixture.root / 'release-catalog'
    for source in sources:
        source.releases = OwnedParameterSkillReleaseSession(source.reviews, catalog)
    return sources


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS preparation')
class OwnedParameterSkillReleaseTests(unittest.TestCase):
    def setUp(self):
        self.source = create_accepted_bootstrap(self)
        self.reviewed = publish_reviewed_source(self.source)
        self.directory = self.source.root / 'release-catalog'
        self.session = OwnedParameterSkillReleaseSession(self.reviewed.reviews, self.directory)

    def publish(self):
        record, checksum = self.session.preview(self.reviewed.review_sha256)
        self.assertEqual(self.session.release(self.reviewed.review_sha256, None, checksum,
                                             RELEASE_CONFIRMATION), (record, checksum))
        return record, checksum

    def test_release_and_selection_reaudit_real_source_without_execution_or_model_effects(self):
        database_before = self.source.database.read_bytes()
        release, checksum = self.session.preview(self.reviewed.review_sha256)
        self.assertEqual(self.source.database.read_bytes(), database_before)
        self.assertFalse(self.directory.exists())
        self.assertEqual(release['family']['scope'], self.reviewed.candidate['scope'])
        validator('owned_parameter_skill_release').validate(release)
        self.assertEqual(self.publish(), (release, checksum))
        self.assertEqual(self.session.release(self.reviewed.review_sha256, None, checksum,
                                             RELEASE_CONFIRMATION), (release, checksum))
        selection, selection_sha256 = self.session.selection_preview(checksum)
        validator('owned_parameter_skill_selection').validate(selection)
        self.assertEqual(self.session.select(checksum, None, 'select', selection_sha256,
                                            SELECT_CONFIRMATION), (selection, selection_sha256))
        self.assertEqual(self.session.select(checksum, None, 'select', selection_sha256,
                                            SELECT_CONFIRMATION), (selection, selection_sha256))
        fresh = OwnedParameterSkillReleaseSession(self.reviewed.reviews, self.directory)
        self.assertEqual(fresh.read(checksum), (release, checksum))
        catalog = fresh.inventory()
        self.assertFalse(catalog['source_current_verified'])
        self.assertEqual(catalog['families'][0]['selection_sha256'], selection_sha256)
        self.assertEqual(catalog['families'][0]['selected_release_sha256'], checksum)
        for record, record_sha256 in ((release, checksum), (selection, selection_sha256)):
            summary = release_summary(record, record_sha256)
            self.assertEqual(summary['record_sha256'], digest(record))
            self.assertNotIn(str(self.source.root), canonical(summary))
            self.assertNotIn('Synthetic candidate', canonical(summary))
            for key in ('activation_authorized', 'execution_authorized', 'training_ready',
                        'native_model_verified', 'released_skill_verified', 'gpu_release_verified'):
                self.assertIs(summary[key], False)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        connection = self.source.store.connection
        self.assertEqual(connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(connection.execute('SELECT count(*) FROM trajectory_labels').fetchone()[0], 0)

    def test_explicit_confirmation_and_no_invented_model_authority(self):
        record, checksum = self.session.preview(self.reviewed.review_sha256)
        for confirmation in (None, True, '', SELECT_CONFIRMATION):
            with self.assertRaises(ValueError):
                self.session.release(self.reviewed.review_sha256, None, checksum, confirmation)
        with self.assertRaises(ValueError):
            self.session.release(self.reviewed.review_sha256, None, 'a' * 64, RELEASE_CONFIRMATION)
        self.assertFalse(self.directory.exists())
        self.publish()
        selection, selection_sha256 = self.session.selection_preview(checksum)
        for confirmation in (None, True, '', RELEASE_CONFIRMATION, ROLLBACK_CONFIRMATION):
            with self.assertRaises(ValueError):
                self.session.select(checksum, None, 'select', selection_sha256, confirmation)
        with self.assertRaises(ValueError):
            self.session.select(checksum, None, 'select', 'a' * 64, SELECT_CONFIRMATION)
        for model, value in ((OwnedParameterSkillRelease, record), (OwnedParameterSkillSelection, selection)):
            for change in ({'reviewed': 1}, {'model_calls': 1}, {'execution_authorized': True},
                           {'native_model_verified': True}, {'training_ready': True},
                           {'source_event_ids': ['fabricated']}):
                with self.assertRaises(ValueError):
                    model.model_validate_json(canonical(value | change))

    def test_revoked_target_and_changed_source_reject_read_and_selection(self):
        _release, checksum = self.publish()
        selection, selection_sha256 = self.session.selection_preview(checksum)
        connection = self.source.store.connection
        connection.execute("UPDATE actions SET status='error' WHERE tool='browser.form.fill'")
        connection.commit()
        for operation in (lambda: self.session.read(checksum),
                          lambda: self.session.selection_preview(checksum),
                          lambda: self.session.select(checksum, None, 'select', selection_sha256,
                                                      SELECT_CONFIRMATION)):
            with self.assertRaises(ValueError):
                operation()
        connection.execute("UPDATE actions SET status='ok' WHERE tool='browser.form.fill'")
        connection.commit()
        self.assertEqual(self.session.read(checksum)[1], checksum)
        reviews = self.reviewed.reviews
        _revocation, revocation_sha256 = reviews.revocation_preview(self.reviewed.review_sha256)
        reviews.revoke(self.reviewed.review_sha256, revocation_sha256, 'REVOKE_MANUAL_REVIEW')
        for operation in (lambda: self.session.read(checksum),
                          lambda: self.session.selection_preview(checksum),
                          lambda: self.session.select(checksum, None, 'select', selection_sha256,
                                                      SELECT_CONFIRMATION),
                          lambda: self.session.store.history.authorize(selection)):
            with self.assertRaises(ValueError):
                operation()
        self.assertIsNone(self.session.inventory()['families'][0]['selection_sha256'])

    def test_replacement_database_rejected_by_retained_and_fresh_original_identity(self):
        _release, checksum = self.publish()
        original = self.source.database.with_name('original.sqlite')
        self.source.database.rename(original)
        shutil.copyfile(original, self.source.database)
        self.source.database.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'database_changed'):
            self.session.read(checksum)
        fresh_reviews = OwnedParameterSkillReviewSession(self.reviewed.candidates,
                                                         self.reviewed.reviews.store.directory)
        fresh = OwnedParameterSkillReleaseSession(fresh_reviews, self.directory)
        with self.assertRaisesRegex(ValueError, 'row_changed'):
            fresh.inventory()

    def test_family_semantics_are_reaudited_after_structurally_valid_anchor(self):
        release, _checksum = self.session.preview(self.reviewed.review_sha256)
        changed_family = release['family'] | {'outcome_sha256': 'a' * 64}
        changed = release | {'family': changed_family, 'family_sha256': digest(changed_family)}
        checksum = digest(changed)
        with self.session.store._locked(create=True, write=True) as (descriptor, _workspace, _identity):
            self.session.store._write(descriptor, checksum + '.release.json', changed)
        self.assertEqual(len(self.session.inventory()['families']), 1)
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            self.session.read(checksum)
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            self.session.selection_preview(checksum)

    def test_fresh_process_style_inventory_rejects_deleted_and_unanchored_records(self):
        _release, checksum = self.publish()
        _selection, selection_sha256 = self.session.selection_preview(checksum)
        self.session.select(checksum, None, 'select', selection_sha256, SELECT_CONFIRMATION)
        path = self.directory / (selection_sha256 + '.selection.json')
        original = path.read_bytes()
        path.unlink()
        fresh = OwnedParameterSkillReleaseSession(self.reviewed.reviews, self.directory)
        with self.assertRaisesRegex(ValueError, 'anchored_inventory'):
            fresh.inventory()
        with self.assertRaisesRegex(ValueError, 'anchored_inventory'):
            fresh.selection_preview(checksum)
        path.write_bytes(original)
        path.chmod(0o600)
        extra = self.directory / ('a' * 64 + '.selection.json')
        extra.write_bytes(original)
        extra.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'anchored_inventory'):
            fresh.inventory()

    def test_append_before_file_failure_is_anchored_and_fail_closed_on_restart(self):
        _record, checksum = self.session.preview(self.reviewed.review_sha256)
        with patch('aos.web_goal_execution_journal._write_private_child', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.session.release(self.reviewed.review_sha256, None, checksum, RELEASE_CONFIRMATION)
        fresh = OwnedParameterSkillReleaseSession(self.reviewed.reviews, self.directory)
        with self.assertRaisesRegex(ValueError, 'anchored_inventory'):
            fresh.inventory()
        self.assertEqual(self.source.store.connection.execute(
            'SELECT count(*) FROM owned_parameter_skill_release_history').fetchone()[0], 1)

    def test_append_only_schema_and_original_database_identity(self):
        self.publish()
        connection = self.source.store.connection
        for statement in ('DELETE FROM owned_parameter_skill_release_history',
                          "UPDATE owned_parameter_skill_release_history SET created_at='changed'"):
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()
        connection.execute('DROP TRIGGER owned_parameter_skill_release_history_no_delete')
        connection.commit()
        with self.assertRaisesRegex(ValueError, 'schema_changed'):
            self.session.inventory()

    def test_missing_migration_is_readonly_and_new_migration_preserves_legacy_rows(self):
        path = self.source.root / 'legacy.sqlite'
        connection = sqlite3.connect(path)
        self.addCleanup(connection.close)
        path.chmod(0o600)
        migrations = sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))
        for migration in migrations[:21]:
            connection.executescript(migration.read_text())
        connection.execute("INSERT INTO tasks VALUES('synthetic-original','original','normalized','en','[]','[]','2026-09-30T00:00:00Z')")
        connection.commit()
        before = path.read_bytes()
        history = OwnedParameterSkillReleaseHistory(path)
        with self.assertRaisesRegex(ValueError, MIGRATION_REQUIRED):
            history.records('a' * 64)
        self.assertEqual(before, path.read_bytes())
        original = connection.execute('SELECT * FROM tasks').fetchall()
        connection.executescript(migrations[21].read_text())
        self.assertEqual(connection.execute('SELECT * FROM tasks').fetchall(), original)
        self.assertEqual(history.records('a' * 64), {})

    def test_path_and_source_destination_pins_reject_overlap_and_changed_parent(self):
        for directory in (self.source.directory, self.source.candidate_directory,
                          self.reviewed.reviews.store.directory, self.source.journal_directory,
                          self.source.root, REPO_ROOT / 'data/release-forbidden'):
            with self.assertRaises(ValueError):
                OwnedParameterSkillReleaseSession(self.reviewed.reviews, directory)
        _record, checksum = self.session.preview(self.reviewed.review_sha256)
        different = OwnedParameterSkillReleaseSession(self.reviewed.reviews, self.source.root / 'other-catalog')
        with self.assertRaises(ValueError):
            different.release(self.reviewed.review_sha256, None, checksum, RELEASE_CONFIRMATION)


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS preparation')
class OwnedParameterSkillVersionTests(unittest.TestCase):
    def test_competing_source_selections_accept_only_one_exact_empty_head(self):
        first, second = create_shared_reviewed_bootstraps(self)
        _first_release, first_sha = first.releases.preview(first.review_sha256)
        first.releases.release(first.review_sha256, None, first_sha, RELEASE_CONFIRMATION)
        _second_release, second_sha = second.releases.preview(second.review_sha256, first_sha)
        second.releases.release(second.review_sha256, first_sha, second_sha, RELEASE_CONFIRMATION)
        proposals = [(source.releases, release_sha,
                      source.releases.selection_preview(release_sha)[1])
                     for source, release_sha in ((first, first_sha), (second, second_sha))]
        barrier = threading.Barrier(2)

        def select(proposal):
            session, release_sha, selection_sha = proposal
            barrier.wait(timeout=5)
            try:
                return session.select(release_sha, None, 'select', selection_sha, SELECT_CONFIRMATION)
            except ValueError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(select, proposals))
        self.assertEqual(sum(isinstance(result, tuple) for result in results), 1)
        self.assertIn('owned_parameter_selection_head_stale', results)
        self.assertEqual(first.releases.inventory()['families'][0]['sequence'], 1)

    def test_two_real_bootstraps_original_database_release_select_and_explicit_rollback(self):
        first, second = create_shared_reviewed_bootstraps(self)
        first_release, first_sha = first.releases.preview(first.review_sha256)
        first.releases.release(first.review_sha256, None, first_sha, RELEASE_CONFIRMATION)
        _first_selection, first_selection_sha = first.releases.selection_preview(first_sha)
        first.releases.select(first_sha, None, 'select', first_selection_sha, SELECT_CONFIRMATION)
        second_release, second_sha = second.releases.preview(second.review_sha256, first_sha)
        self.assertEqual(first_release['family_sha256'], second_release['family_sha256'])
        self.assertNotEqual(first_release['recipe_sha256'], second_release['recipe_sha256'])
        self.assertNotEqual(first_release['source_run_ref'], second_release['source_run_ref'])
        self.assertEqual(second_release['revision'], 2)
        second.releases.release(second.review_sha256, first_sha, second_sha, RELEASE_CONFIRMATION)
        _second_selection, second_selection_sha = second.releases.selection_preview(second_sha, first_selection_sha)
        with self.assertRaises(ValueError):
            second.releases.select(second_sha, None, 'select', second_selection_sha, SELECT_CONFIRMATION)
        second.releases.select(second_sha, first_selection_sha, 'select', second_selection_sha, SELECT_CONFIRMATION)
        with self.assertRaisesRegex(ValueError, 'direction_invalid'):
            first.releases.selection_preview(first_sha, second_selection_sha, 'select')
        rollback, rollback_sha = first.releases.selection_preview(first_sha, second_selection_sha, 'rollback')
        with self.assertRaises(ValueError):
            first.releases.select(first_sha, second_selection_sha, 'rollback', rollback_sha, SELECT_CONFIRMATION)
        first.releases.select(first_sha, second_selection_sha, 'rollback', rollback_sha, ROLLBACK_CONFIRMATION)
        self.assertEqual(rollback['sequence'], 3)
        self.assertEqual(rollback['previous_release_sha256'], second_sha)
        current = second.releases.inventory()['families'][0]
        self.assertEqual(current['selected_release_sha256'], first_sha)
        self.assertEqual(current['selection_sha256'], rollback_sha)
        self.assertEqual(len(current['releases']), 2)
        with self.assertRaisesRegex(ValueError, 'head_stale'):
            second.releases.select(second_sha, first_selection_sha, 'select', second_selection_sha,
                                   SELECT_CONFIRMATION)
        with self.assertRaises((ValueError, FileNotFoundError)):
            second.releases.read(first_sha)
        self.assertEqual(first.releases.read(first_sha), (first_release, first_sha))
        connection = first.source.store.connection
        self.assertEqual(connection.execute('SELECT count(*) FROM actions').fetchone()[0], 14)
        self.assertEqual(connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(connection.execute('SELECT count(*) FROM trajectory_labels').fetchone()[0], 0)
        self.assertEqual(connection.execute('SELECT count(*) FROM owned_parameter_skill_release_history').fetchone()[0], 5)


if __name__ == '__main__':
    unittest.main()

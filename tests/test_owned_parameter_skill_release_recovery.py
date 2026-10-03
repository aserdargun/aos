from contextlib import closing
from copy import deepcopy
import json
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT
from aos.owned_parameter_skill_release import (
    OwnedParameterSkillReleaseRecovery, RECOVERY_CONFIRMATION, SELECT_CONFIRMATION, release_summary,
)
from aos.web_goal_execution_journal import WebGoalExecutionJournal
import test_owned_parameter_skill_release as release_fixture


@unittest.skipUnless(shutil.which('openssl'), 'Requires synthetic owned TLS fixture')
class ReleaseRecoveryTests(unittest.TestCase):
    setUp = release_fixture.OwnedParameterSkillReleaseTests.setUp
    publish = release_fixture.OwnedParameterSkillReleaseTests.publish

    def database_snapshot(self):
        with closing(sqlite3.connect(f'file:{self.source.database}?mode=ro', uri=True)) as connection:
            return tuple((table, tuple(connection.execute('SELECT * FROM ' + table).fetchall()))
                for table, in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())

    def test_missing_release_and_selection_restore_only_original_bytes_with_unchanged_database(self):
        release, release_sha256 = self.publish()
        selection, selection_sha256 = self.session.selection_preview(release_sha256)
        self.session.select(release_sha256, None, 'select', selection_sha256, SELECT_CONFIRMATION)
        for stage, checksum, record in (('release', release_sha256, release),
                                        ('selection', selection_sha256, selection)):
            with self.subTest(stage=stage):
                path = self.directory / (checksum + '.' + stage + '.json')
                original = path.read_bytes()
                path.unlink()
                before = self.database_snapshot()
                with self.assertRaises(ValueError):
                    self.session.inventory()
                proposal, confirmation = self.session.recovery_preview(checksum)
                self.assertEqual(proposal['record'], record)
                self.assertEqual(proposal['record_stage'], stage)
                self.assertFalse(proposal['execution_authorized'])
                self.assertEqual(release_summary(proposal, confirmation)['recovery_sha256'], confirmation)
                self.assertEqual(self.database_snapshot(), before)
                self.assertEqual(self.session.recover(checksum, confirmation, RECOVERY_CONFIRMATION),
                                 (proposal, confirmation))
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(self.database_snapshot(), before)
                self.assertEqual(self.session.inventory()['families'][0]['selected_release_sha256'], release_sha256)
                with self.assertRaisesRegex(ValueError, 'not_missing'):
                    self.session.recover(checksum, confirmation, RECOVERY_CONFIRMATION)

    def test_failed_original_file_write_has_explicit_recovery_without_second_authorization(self):
        record, checksum = self.session.preview(self.reviewed.review_sha256)
        with patch.object(WebGoalExecutionJournal, '_write', side_effect=OSError('synthetic failed publication')):
            with self.assertRaises(OSError):
                self.session.release(self.reviewed.review_sha256, None, checksum, 'RELEASE_MANUAL_SKILL')
        before = self.database_snapshot()
        proposal, confirmation = self.session.recovery_preview(checksum)
        self.assertEqual(proposal['record'], record)
        self.session.recover(checksum, confirmation, RECOVERY_CONFIRMATION)
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(self.session.read(checksum), (record, checksum))

    def test_wrong_confirmation_unknown_anchor_changed_file_and_revoked_review_fail_closed(self):
        record, checksum = self.publish()
        path = self.directory / (checksum + '.release.json')
        original = path.read_bytes()
        path.unlink()
        proposal, confirmation = self.session.recovery_preview(checksum)
        before = self.database_snapshot()
        for arguments in ((checksum, 'f' * 64, RECOVERY_CONFIRMATION),
                          (checksum, confirmation, 'APPROVE_ALL'),
                          ('f' * 64, confirmation, RECOVERY_CONFIRMATION)):
            with self.assertRaises(ValueError):
                self.session.recover(*arguments)
            self.assertFalse(path.exists())
        self.assertEqual(self.database_snapshot(), before)
        path.write_bytes(original + b' ')
        with self.assertRaises(ValueError):
            self.session.recovery_preview(checksum)
        path.unlink()
        revoke, revoke_sha256 = self.reviewed.reviews.revocation_preview(self.reviewed.review_sha256)
        self.reviewed.reviews.revoke(self.reviewed.review_sha256, revoke_sha256, 'REVOKE_MANUAL_REVIEW')
        with self.assertRaises(ValueError):
            self.session.recover(checksum, confirmation, RECOVERY_CONFIRMATION)
        self.assertFalse(path.exists())
        changed = deepcopy(proposal)
        changed['record']['reviewed'] = False
        with self.assertRaises(ValueError):
            OwnedParameterSkillReleaseRecovery.model_validate(changed)

    def test_schema_matches_closed_typed_recovery_model(self):
        schema = json.loads((REPO_ROOT / 'schemas/owned_parameter_skill_release_recovery.schema.json').read_text())
        schema.pop('$schema')
        self.assertEqual(schema, OwnedParameterSkillReleaseRecovery.model_json_schema())


if __name__ == '__main__':
    unittest.main()

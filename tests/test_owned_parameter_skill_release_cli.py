"""Public release CLI over actual audited CPU fixture/operator/TLS evidence."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest

from aos.contracts import REPO_ROOT, canonical
from aos.owned_parameter_skill_candidate import (
    HUMAN_CONFIRMATION, OwnedParameterSkillCandidateSession,
)
from aos.owned_parameter_skill_release_cli import main
from aos.owned_parameter_skill_review import (
    ACCEPT_CONFIRMATION, REVOKE_CONFIRMATION, OwnedParameterSkillReviewSession,
)

from test_owned_parameter_skill_candidate import create_accepted_bootstrap


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS authoring')
class OwnedParameterSkillReleaseCliTests(unittest.TestCase):
    def setUp(self):
        self.source = create_accepted_bootstrap(self)
        self.review_directory = self.source.root / 'manual-reviews'
        self.release_directory = self.source.root / 'manual-releases'
        self.candidates = OwnedParameterSkillCandidateSession(
            self.source.directory, self.source.manifest_sha256, self.source.database,
            self.source.journal_directory, self.source.candidate_directory)
        candidate, candidate_sha256 = self.candidates.preview(self.source.intent_sha256)
        self.candidates.publish(self.source.intent_sha256, candidate_sha256, HUMAN_CONFIRMATION)
        self.reviews = OwnedParameterSkillReviewSession(self.candidates, self.review_directory)
        review, self.review_sha256 = self.reviews.preview(candidate_sha256)
        self.reviews.accept(candidate_sha256, self.review_sha256, ACCEPT_CONFIRMATION)

    def arguments(self, command, *options):
        return [command,
                '--project-directory', str(self.source.directory),
                '--project-manifest-sha256', self.source.manifest_sha256,
                '--database', str(self.source.database),
                '--bootstrap-journal-directory', str(self.source.journal_directory),
                '--candidate-directory', str(self.source.candidate_directory),
                '--review-directory', str(self.review_directory),
                '--release-directory', str(self.release_directory), *options]

    def invoke(self, command, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(self.arguments(command, *options))
        return result, stdout.getvalue(), stderr.getvalue()

    def fresh_process(self, command, *options):
        return subprocess.run(
            [sys.executable, '-m', 'aos.owned_parameter_skill_release_cli',
             *self.arguments(command, *options)], cwd=self.source.root,
            env=dict(os.environ, PYTHONPATH=str(REPO_ROOT / 'src')),
            capture_output=True, text=True, timeout=20)

    def execution_snapshot(self):
        tables = ('desktop_tasks', 'runs', 'runtime_states', 'actions', 'model_calls',
                  'desktop_approvals', 'human_interventions', 'desktop_sessions')
        return {table: hashlib.sha256(canonical([
            dict(row) for row in self.source.store.connection.execute(
                'SELECT * FROM ' + table + ' ORDER BY rowid')]).encode()).hexdigest()
                for table in tables}

    def private_report(self, output):
        self.assertNotIn(str(self.source.root), output)
        self.assertNotIn('Synthetic candidate record', output)
        self.assertNotIn('Synthetic candidate note', output)
        report = json.loads(output)
        for flag in ('execution_authorized', 'activation_authorized', 'training_ready'):
            self.assertIs(report[flag], False)
        return report

    def publish_release(self):
        status, output, error = self.invoke('preview', '--review-sha256', self.review_sha256)
        self.assertEqual(status, 0, error)
        checksum = self.private_report(output)['release_sha256']
        status, output, error = self.invoke(
            'release', '--review-sha256', self.review_sha256,
            '--confirm-release-sha256', checksum, '--human-confirmation', 'RELEASE_MANUAL_SKILL')
        self.assertEqual(status, 0, error)
        self.assertEqual(self.private_report(output)['release_sha256'], checksum)
        return checksum

    def select_release(self, checksum):
        status, output, error = self.invoke('select-preview', '--release-sha256', checksum)
        self.assertEqual(status, 0, error)
        selection_sha256 = self.private_report(output)['selection_sha256']
        status, output, error = self.invoke(
            'select', '--release-sha256', checksum,
            '--confirm-selection-sha256', selection_sha256,
            '--human-confirmation', 'SELECT_MANUAL_SKILL')
        self.assertEqual(status, 0, error)
        self.assertEqual(self.private_report(output)['selection_sha256'], selection_sha256)
        return selection_sha256

    def test_public_preview_release_selection_and_fresh_process_readback_without_replay(self):
        before = self.execution_snapshot()
        status, output, error = self.invoke('preview', '--review-sha256', self.review_sha256)
        self.assertEqual(status, 0, error)
        self.private_report(output)
        self.assertFalse(self.release_directory.exists())
        release_sha256 = self.publish_release()
        selection_sha256 = self.select_release(release_sha256)
        read = self.fresh_process('read', '--release-sha256', release_sha256)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(self.private_report(read.stdout)['release_sha256'], release_sha256)
        catalog = self.fresh_process('catalog')
        self.assertEqual(catalog.returncode, 0, catalog.stderr)
        report = self.private_report(catalog.stdout)
        self.assertIs(report['source_current_verified'], False)
        self.assertEqual(report['families'][0]['selection_sha256'], selection_sha256)
        self.assertEqual(report['families'][0]['selected_release_sha256'], release_sha256)
        self.assertEqual(self.execution_snapshot(), before)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)

    def test_exact_confirmation_and_expected_head_reject_without_new_records(self):
        before = self.execution_snapshot()
        status, output, error = self.invoke(
            'release', '--review-sha256', self.review_sha256,
            '--confirm-release-sha256', '0' * 64, '--human-confirmation', 'RELEASE_MANUAL_SKILL')
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertNotIn(str(self.source.root), error)
        self.assertFalse(self.release_directory.exists())
        release_sha256 = self.publish_release()
        self.select_release(release_sha256)
        count = self.source.store.connection.execute(
            'SELECT count(*) FROM owned_parameter_skill_release_history').fetchone()[0]
        status, output, error = self.invoke(
            'select-preview', '--release-sha256', release_sha256,
            '--expected-selection-sha256', 'f' * 64)
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertNotIn('Traceback', error)
        self.assertEqual(self.source.store.connection.execute(
            'SELECT count(*) FROM owned_parameter_skill_release_history').fetchone()[0], count)
        self.assertEqual(self.execution_snapshot(), before)

    def test_revoked_target_is_denied_by_new_cli_process(self):
        release_sha256 = self.publish_release()
        revocation, checksum = self.reviews.revocation_preview(self.review_sha256)
        self.reviews.revoke(self.review_sha256, checksum, REVOKE_CONFIRMATION)
        before = self.execution_snapshot()
        for command in ('read', 'select-preview'):
            with self.subTest(command=command):
                result = self.fresh_process(command, '--release-sha256', release_sha256)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, '')
                self.assertNotIn(str(self.source.root), result.stderr)
                self.assertNotIn('Synthetic candidate', result.stderr)
        self.assertEqual(self.execution_snapshot(), before)

    def test_missing_selection_file_denies_fresh_catalog_instead_of_rolling_back(self):
        release_sha256 = self.publish_release()
        selection_sha256 = self.select_release(release_sha256)
        selected_file, = self.release_directory.glob(selection_sha256 + '.*.json')
        selected_file.unlink()
        before = self.execution_snapshot()
        result = self.fresh_process('catalog')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')
        self.assertNotIn(str(self.release_directory), result.stderr)
        self.assertFalse(selected_file.exists())
        self.assertEqual(self.execution_snapshot(), before)

    def test_explicit_fresh_process_recovery_restores_only_original_missing_release_and_selection(self):
        release_sha256 = self.publish_release()
        selection_sha256 = self.select_release(release_sha256)
        before = self.execution_snapshot()
        database_before = tuple(self.source.store.connection.iterdump())
        for record_stage, record_sha256 in (('release', release_sha256), ('selection', selection_sha256)):
            with self.subTest(record_sha256=record_sha256):
                record_file, = self.release_directory.glob(record_sha256 + '.*.json')
                original = record_file.read_bytes()
                record_file.unlink()
                denied = self.fresh_process('catalog')
                self.assertEqual(denied.returncode, 1, denied.stdout)
                self.assertFalse(record_file.exists())
                preview = self.fresh_process('recovery-preview', '--record-sha256', record_sha256)
                self.assertEqual(preview.returncode, 0, preview.stderr)
                preview_report = self.private_report(preview.stdout)
                self.assertEqual(preview_report['record_sha256'], record_sha256)
                self.assertEqual(preview_report['record_stage'], record_stage)
                self.assertIs(preview_report['reviewed'], False)
                proposal_sha256 = preview_report['recovery_sha256']
                self.assertFalse(record_file.exists())
                wrong_confirmation = self.fresh_process(
                    'recover', '--record-sha256', record_sha256,
                    '--confirm-recovery-sha256', '0' * 64,
                    '--human-confirmation', 'RESTORE_ANCHORED_RELEASE_RECORD')
                self.assertEqual(wrong_confirmation.returncode, 1, wrong_confirmation.stdout)
                self.assertEqual(wrong_confirmation.stdout, '')
                self.assertNotIn(str(self.source.root), wrong_confirmation.stderr)
                self.assertFalse(record_file.exists())
                restored = self.fresh_process(
                    'recover', '--record-sha256', record_sha256,
                    '--confirm-recovery-sha256', proposal_sha256,
                    '--human-confirmation', 'RESTORE_ANCHORED_RELEASE_RECORD')
                self.assertEqual(restored.returncode, 0, restored.stderr)
                report = self.private_report(restored.stdout)
                self.assertEqual(report['recovery_sha256'], proposal_sha256)
                self.assertEqual(record_file.read_bytes(), original)
                self.assertEqual(tuple(self.source.store.connection.iterdump()), database_before)
                self.assertEqual(self.execution_snapshot(), before)
                existing = self.fresh_process('recovery-preview', '--record-sha256', record_sha256)
                self.assertEqual(existing.returncode, 1, existing.stdout)
                self.assertEqual(record_file.read_bytes(), original)
        catalog = self.fresh_process('catalog')
        self.assertEqual(catalog.returncode, 0, catalog.stderr)
        family = self.private_report(catalog.stdout)['families'][0]
        self.assertEqual(family['selection_sha256'], selection_sha256)
        self.assertEqual(family['selected_release_sha256'], release_sha256)
        self.assertEqual(tuple(self.source.store.connection.iterdump()), database_before)

    def test_unanchored_recovery_hash_and_corrupt_existing_file_are_never_restored(self):
        release_sha256 = self.publish_release()
        record_file, = self.release_directory.glob(release_sha256 + '.*.json')
        record_file.write_text('synthetic corruption; not an authorized original')
        corrupt = record_file.read_bytes()
        database_before = tuple(self.source.store.connection.iterdump())
        for record_sha256 in (release_sha256, 'f' * 64):
            result = self.fresh_process('recovery-preview', '--record-sha256', record_sha256)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(result.stdout, '')
            self.assertNotIn(str(self.source.root), result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(record_file.read_bytes(), corrupt)
            self.assertEqual(tuple(self.source.store.connection.iterdump()), database_before)
        record_file.unlink()
        revocation, revocation_sha256 = self.reviews.revocation_preview(self.review_sha256)
        self.reviews.revoke(self.review_sha256, revocation_sha256, REVOKE_CONFIRMATION)
        database_before = tuple(self.source.store.connection.iterdump())
        denied = self.fresh_process('recovery-preview', '--record-sha256', release_sha256)
        self.assertEqual(denied.returncode, 1, denied.stdout)
        self.assertFalse(record_file.exists())
        self.assertEqual(tuple(self.source.store.connection.iterdump()), database_before)

    def test_public_cross_version_rollback_requires_original_target_source_arguments(self):
        from test_owned_parameter_skill_release import create_shared_reviewed_bootstraps

        first, second = create_shared_reviewed_bootstraps(self)
        self.source = first.source
        self.review_directory = first.reviews.store.directory
        self.release_directory = first.releases.store.directory
        self.review_sha256 = first.review_sha256
        before = self.execution_snapshot()
        first_sha256 = self.publish_release()
        first_selection_sha256 = self.select_release(first_sha256)
        self.source = second.source
        self.review_directory = second.reviews.store.directory
        self.review_sha256 = second.review_sha256
        status, output, error = self.invoke(
            'preview', '--review-sha256', second.review_sha256,
            '--expected-parent-release-sha256', first_sha256)
        self.assertEqual(status, 0, error)
        second_sha256 = self.private_report(output)['release_sha256']
        status, output, error = self.invoke(
            'release', '--review-sha256', second.review_sha256,
            '--expected-parent-release-sha256', first_sha256,
            '--confirm-release-sha256', second_sha256, '--human-confirmation', 'RELEASE_MANUAL_SKILL')
        self.assertEqual(status, 0, error)
        status, output, error = self.invoke(
            'select-preview', '--release-sha256', second_sha256,
            '--expected-selection-sha256', first_selection_sha256)
        self.assertEqual(status, 0, error)
        second_selection_sha256 = self.private_report(output)['selection_sha256']
        status, output, error = self.invoke(
            'select', '--release-sha256', second_sha256,
            '--expected-selection-sha256', first_selection_sha256,
            '--confirm-selection-sha256', second_selection_sha256,
            '--human-confirmation', 'SELECT_MANUAL_SKILL')
        self.assertEqual(status, 0, error)
        options = ('--release-sha256', first_sha256,
                   '--expected-selection-sha256', second_selection_sha256)
        rejected = self.fresh_process('rollback-preview', *options)
        self.assertEqual(rejected.returncode, 1, rejected.stdout)
        self.source = first.source
        self.review_directory = first.reviews.store.directory
        preview = self.fresh_process('rollback-preview', *options)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        rollback_sha256 = self.private_report(preview.stdout)['selection_sha256']
        rollback = self.fresh_process(
            'rollback', *options, '--confirm-selection-sha256', rollback_sha256,
            '--human-confirmation', 'ROLLBACK_MANUAL_SKILL')
        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        result = self.private_report(rollback.stdout)
        self.assertEqual(result['release_sha256'], first_sha256)
        self.assertEqual(result['previous_release_sha256'], second_sha256)
        self.assertEqual(result['sequence'], 3)
        self.assertEqual(self.execution_snapshot(), before)


class OwnedParameterSkillReleaseRecoveryArgumentsTests(unittest.TestCase):
    def test_recovery_requires_exact_record_hash_confirmation_hash_and_human_literal(self):
        common = ['--project-directory', '/synthetic/project', '--project-manifest-sha256', 'a' * 64,
                  '--database', '/synthetic/database', '--bootstrap-journal-directory', '/synthetic/journal',
                  '--candidate-directory', '/synthetic/candidates', '--review-directory', '/synthetic/reviews',
                  '--release-directory', '/synthetic/releases']
        invalid = [
            ['recovery-preview', *common],
            ['recovery-preview', *common, '--record-sha256', 'A' * 64],
            ['recover', *common, '--record-sha256', 'b' * 64],
            ['recover', *common, '--record-sha256', 'b' * 64, '--confirm-recovery-sha256', 'c' * 64],
            ['recover', *common, '--record-sha256', 'b' * 64, '--confirm-recovery-sha256', 'c' * 64,
             '--human-confirmation', 'RELEASE_MANUAL_SKILL'],
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                main(arguments)
            self.assertEqual(rejected.exception.code, 2)


if __name__ == '__main__':
    unittest.main()

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical
from aos.dataset_audit import expected_schema, schema_signature
from aos.owned_parameter_project import APPLICATIONS
from aos.owned_parameter_skill_cli import main

from test_owned_parameter_skill_candidate import create_accepted_bootstrap


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS authoring')
class OwnedParameterSkillCliTests(unittest.TestCase):
    def setUp(self):
        self.source = create_accepted_bootstrap(self)

    def arguments(self, command, *extra):
        source = self.source
        arguments = [command, '--project-directory', str(source.directory),
                     '--project-manifest-sha256', source.manifest_sha256,
                     '--database', str(source.database),
                     '--bootstrap-journal-directory', str(source.journal_directory),
                     '--candidate-directory', str(source.candidate_directory)]
        if command in ('review-preview', 'review-accept', 'review-read', 'revoke-preview', 'revoke',
                       'recovery-preview', 'recover'):
            arguments += ['--review-directory', str(self.source.candidate_directory.parent / 'manual-skill-reviews')]
        elif command != 'read':
            arguments += ['--intent-sha256', source.intent_sha256]
        return arguments + list(extra)

    def invoke(self, command, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(self.arguments(command, *extra))
        return result, stdout.getvalue(), stderr.getvalue()

    def snapshot(self, *, include_database=True):
        return {str(path.relative_to(self.source.root)):
                (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in self.source.root.rglob('*')
                if path.is_file() and not path.name.endswith('-shm')
                and (include_database or path not in (
                    self.source.database, Path(str(self.source.database) + '-wal')))}

    def execution_snapshot(self):
        tables = ('desktop_tasks', 'runs', 'runtime_states', 'actions', 'model_calls',
                  'desktop_approvals', 'human_interventions', 'desktop_sessions')
        return {table: hashlib.sha256(canonical([
            dict(row) for row in self.source.store.connection.execute(
                'SELECT * FROM ' + table + ' ORDER BY rowid')]).encode()).hexdigest()
                for table in tables}

    def history_snapshot(self):
        records = [dict(row) for row in self.source.store.connection.execute(
            'SELECT * FROM owned_parameter_skill_review_history ORDER BY record_sha256')]
        return len(records), hashlib.sha256(canonical(records).encode()).hexdigest()

    def assert_private_summary(self, output):
        self.assertNotIn('Synthetic candidate record', output)
        self.assertNotIn('Synthetic candidate note', output)
        self.assertNotIn(str(self.source.root), output)
        report = json.loads(output)
        self.assertEqual(len(report['candidate_sha256']), 64)
        self.assertEqual(report['intent_sha256'], self.source.intent_sha256)
        self.assertEqual(report['manifest_sha256'], self.source.manifest_sha256)
        self.assertTrue(report['manual_recipe_execution_verified'])
        for flag in ('training_ready', 'native_model_verified', 'activation_authorized',
                     'released_skill_verified', 'gpu_release_verified'):
            self.assertIs(report[flag], False)
        return report

    def publish(self, checksum):
        return self.invoke('publish', '--confirm-candidate-sha256', checksum,
                           '--human-confirmation', 'PUBLISH_MANUAL_CANDIDATE')

    def published_candidate(self):
        status, output, error = self.invoke('preview')
        self.assertEqual(status, 0, error)
        checksum = json.loads(output)['candidate_sha256']
        status, output, error = self.publish(checksum)
        self.assertEqual(status, 0, error)
        return checksum

    def assert_private_review_summary(self, output):
        self.assertNotIn('Synthetic candidate record', output)
        self.assertNotIn('Synthetic candidate note', output)
        self.assertNotIn(str(self.source.root), output)
        self.assertNotIn(str(self.source.candidate_directory.parent), output)
        report = json.loads(output)
        for flag in ('training_ready', 'native_model_verified', 'activation_authorized',
                     'released_skill_verified', 'gpu_release_verified', 'execution_authorized'):
            self.assertIs(report[flag], False)
        return report

    def accept_review(self, candidate_sha256, review_sha256):
        return self.invoke('review-accept', '--candidate-sha256', candidate_sha256,
                           '--confirm-review-sha256', review_sha256,
                           '--human-confirmation', 'ACCEPT_MANUAL_REVIEW')

    def revoke_review(self, review_sha256, revocation_sha256):
        return self.invoke('revoke', '--review-sha256', review_sha256,
                           '--confirm-revocation-sha256', revocation_sha256,
                           '--human-confirmation', 'REVOKE_MANUAL_REVIEW')

    def recover_record(self, record_sha256, recovery_sha256):
        return self.invoke('recover', '--record-sha256', record_sha256,
                           '--confirm-recovery-sha256', recovery_sha256,
                           '--human-confirmation', 'RESTORE_ANCHORED_REVIEW_RECORD')

    def fresh_process(self, command, *options):
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')
        return subprocess.run(
            [sys.executable, '-m', 'aos.owned_parameter_skill_cli'] + self.arguments(command, *options),
            cwd=self.source.root, env=environment, capture_output=True, text=True, timeout=20)

    def accepted_review(self):
        candidate_sha256 = self.published_candidate()
        status, output, error = self.invoke('review-preview', '--candidate-sha256', candidate_sha256)
        self.assertEqual(status, 0, error)
        review_sha256 = json.loads(output)['review_sha256']
        self.assertEqual(self.accept_review(candidate_sha256, review_sha256)[0], 0)
        return candidate_sha256, review_sha256

    def test_fresh_process_deleted_revocation_requires_exact_recovery_and_remains_revoked(self):
        candidate_sha256, review_sha256 = self.accepted_review()
        status, output, error = self.invoke('revoke-preview', '--review-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        revocation_sha256 = json.loads(output)['revocation_sha256']
        self.assertEqual(self.revoke_review(review_sha256, revocation_sha256)[0], 0)
        review_directory = self.source.candidate_directory.parent / 'manual-skill-reviews'
        revocation_file = review_directory / (revocation_sha256 + '.revoke.json')
        original = revocation_file.read_bytes()
        revocation_file.unlink()
        execution_before = self.execution_snapshot()
        history_before = self.history_snapshot()
        for command, options in (
                ('review-read', ['--review-sha256', review_sha256]),
                ('review-accept', ['--candidate-sha256', candidate_sha256,
                                   '--confirm-review-sha256', review_sha256,
                                   '--human-confirmation', 'ACCEPT_MANUAL_REVIEW'])):
            with self.subTest(command=command):
                result = self.fresh_process(command, *options)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, '')
        result = self.fresh_process('recovery-preview', '--record-sha256', revocation_sha256)
        self.assertEqual(result.returncode, 0, result.stderr)
        recovery_sha256 = self.assert_private_review_summary(result.stdout)['recovery_sha256']
        self.assertFalse(revocation_file.exists())
        result = self.fresh_process(
            'recover', '--record-sha256', revocation_sha256,
            '--confirm-recovery-sha256', recovery_sha256,
            '--human-confirmation', 'RESTORE_ANCHORED_REVIEW_RECORD')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_private_review_summary(result.stdout)
        self.assertEqual(revocation_file.read_bytes(), original)
        result = self.fresh_process('review-read', '--review-sha256', review_sha256)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'revoked')
        self.assertEqual(execution_before, self.execution_snapshot())
        self.assertEqual(history_before, self.history_snapshot())

    def test_authorized_anchor_before_file_failure_can_restore_only_exact_original_record(self):
        candidate_sha256 = self.published_candidate()
        status, output, error = self.invoke('review-preview', '--candidate-sha256', candidate_sha256)
        self.assertEqual(status, 0, error)
        review_sha256 = json.loads(output)['review_sha256']
        execution_before = self.execution_snapshot()
        with patch('aos.owned_parameter_skill_review._ReviewStore._materialize',
                   side_effect=OSError('synthetic disk failure')):
            status, output, error = self.accept_review(candidate_sha256, review_sha256)
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        review_file = self.source.candidate_directory.parent / 'manual-skill-reviews' / (review_sha256 + '.accept.json')
        self.assertFalse(review_file.exists())
        self.assertEqual(self.invoke('review-read', '--review-sha256', review_sha256)[0], 1)
        history_before = self.history_snapshot()
        self.assertEqual(history_before[0], 1)
        status, output, error = self.invoke('recovery-preview', '--record-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        recovery_sha256 = self.assert_private_review_summary(output)['recovery_sha256']
        self.assertEqual(self.recover_record(review_sha256, '0' * 64)[0], 1)
        self.assertFalse(review_file.exists())
        status, output, error = self.recover_record(review_sha256, recovery_sha256)
        self.assertEqual(status, 0, error)
        self.assert_private_review_summary(output)
        self.assertTrue(review_file.exists())
        status, output, error = self.invoke('review-read', '--review-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(output)['status'], 'accepted')
        self.assertEqual(execution_before, self.execution_snapshot())
        self.assertEqual(history_before, self.history_snapshot())

    def test_recovery_rejects_wrong_record_changed_source_and_existing_corruption(self):
        _candidate_sha256, review_sha256 = self.accepted_review()
        review_file = self.source.candidate_directory.parent / 'manual-skill-reviews' / (review_sha256 + '.accept.json')
        original = review_file.read_bytes()
        review_file.unlink()
        before = self.snapshot()
        self.assertEqual(self.invoke('recovery-preview', '--record-sha256', '0' * 64)[0], 1)
        self.assertEqual(before, self.snapshot())
        status, output, error = self.invoke('recovery-preview', '--record-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        recovery_sha256 = json.loads(output)['recovery_sha256']
        review_file.write_bytes(original + b' ')
        review_file.chmod(0o600)
        before = self.snapshot()
        self.assertEqual(self.recover_record(review_sha256, recovery_sha256)[0], 1)
        self.assertEqual(before, self.snapshot())
        review_file.unlink()
        source_file = self.source.directory / 'parameter-review.json'
        source_file.write_bytes(source_file.read_bytes() + b' ')
        before = self.snapshot()
        self.assertEqual(self.recover_record(review_sha256, recovery_sha256)[0], 1)
        self.assertEqual(self.invoke('recovery-preview', '--record-sha256', review_sha256)[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_recovery_requires_full_literal_and_exact_hash(self):
        for options in (
                ['--record-sha256', '0' * 64],
                ['--record-sha256', '0' * 64, '--confirm-recovery-sha256', '0' * 64,
                 '--human-confirmation', 'RESTORE'],
                ['--record-sha256', '0' * 64, '--confirm-recovery-sha256', 'invalid',
                 '--human-confirmation', 'RESTORE_ANCHORED_REVIEW_RECORD']):
            with self.subTest(options=options), self.assertRaises(SystemExit) as rejected:
                self.invoke('recover', *options)
            self.assertEqual(rejected.exception.code, 2)

    def test_missing_history_migration_reports_precise_error_without_migrating(self):
        candidate_sha256 = self.published_candidate()
        self.source.store.connection.executescript('''
            DROP TRIGGER scientist_one_unresolved_turn;
            DROP TRIGGER scientist_resolved_intent_immutable;
            DROP TRIGGER scientist_observed_intent_immutable;
            DROP INDEX scientist_session_turns;
            CREATE UNIQUE INDEX scientist_one_unresolved_turn ON scientist_turn_intents(session_id);
            DROP TABLE scientist_no_admission_closures;
            DROP TABLE scientist_turn_resolutions;
            DROP TABLE scientist_evidence_responses;
            DROP TABLE scientist_evidence_controls;
            DROP TABLE scientist_admission_history;
            DROP TABLE owned_parameter_skill_release_history;
            DROP TABLE owned_parameter_skill_review_history;
            DROP TABLE scientist_lab_readbacks;
            DELETE FROM schema_migrations WHERE version IN (21,22,23,24,25,26,27,28);
        ''')
        self.source.store.connection.commit()
        reference_schema, reference_versions, _hashes = expected_schema(20)
        self.assertEqual(schema_signature(self.source.store.connection), reference_schema)
        self.assertEqual([tuple(row) for row in self.source.store.connection.execute(
            'SELECT version,name FROM schema_migrations ORDER BY version')], reference_versions)
        before = self.snapshot()
        status, output, error = self.invoke('review-preview', '--candidate-sha256', candidate_sha256)
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertEqual(json.loads(error), {
            'status': 'rejected', 'error': 'owned_parameter_skill_review_history_migration_required',
            'migration_required': True})
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(str(self.source.root), error)
        self.assertIsNone(self.source.store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='owned_parameter_skill_review_history'").fetchone())

    def test_exact_independent_review_and_revocation_have_no_runtime_authority(self):
        candidate_sha256 = self.published_candidate()
        before = self.snapshot()
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            status, output, error = self.invoke('review-preview', '--candidate-sha256', candidate_sha256)
        self.assertEqual(status, 0, error)
        review_sha256 = self.assert_private_review_summary(output)['review_sha256']
        self.assertEqual(before, self.snapshot())
        review_directory = self.source.candidate_directory.parent / 'manual-skill-reviews'
        self.assertFalse(review_directory.exists())
        status, output, error = self.accept_review(candidate_sha256, review_sha256)
        self.assertEqual(status, 0, error)
        self.assertEqual(self.assert_private_review_summary(output)['review_sha256'], review_sha256)
        before = self.snapshot()
        self.assertEqual(self.accept_review(candidate_sha256, review_sha256)[0], 0)
        self.assertEqual(before, self.snapshot())
        status, output, error = self.invoke('review-read', '--review-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        self.assert_private_review_summary(output)
        self.assertEqual(before, self.snapshot())
        status, output, error = self.invoke('revoke-preview', '--review-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        revocation_sha256 = self.assert_private_review_summary(output)['revocation_sha256']
        self.assertEqual(before, self.snapshot())
        status, output, error = self.revoke_review(review_sha256, revocation_sha256)
        self.assertEqual(status, 0, error)
        self.assert_private_review_summary(output)
        before = self.snapshot()
        status, output, error = self.invoke('review-read', '--review-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        self.assertEqual(self.assert_private_review_summary(output)['status'], 'revoked')
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.revoke_review(review_sha256, revocation_sha256)[0], 0)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.accept_review(candidate_sha256, review_sha256)[0], 1)
        status, output, error = self.invoke('read', '--candidate-sha256', candidate_sha256)
        self.assertEqual(status, 0, error)
        self.assertEqual(self.assert_private_summary(output)['status'], 'awaiting_manual_review')

    def test_review_confirmation_change_source_change_and_wrong_revocation_hash_reject(self):
        candidate_sha256 = self.published_candidate()
        status, output, error = self.invoke('review-preview', '--candidate-sha256', candidate_sha256)
        self.assertEqual(status, 0, error)
        review_sha256 = json.loads(output)['review_sha256']
        before = self.snapshot()
        self.assertEqual(self.accept_review(candidate_sha256, '0' * 64)[0], 1)
        self.assertEqual(self.accept_review('0' * 64, review_sha256)[0], 1)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.accept_review(candidate_sha256, review_sha256)[0], 0)
        before = self.snapshot()
        self.assertEqual(self.revoke_review(review_sha256, '0' * 64)[0], 1)
        self.assertEqual(before, self.snapshot())
        source_file = self.source.directory / 'parameter-review.json'
        source_file.write_bytes(source_file.read_bytes() + b' ')
        before = self.snapshot()
        self.assertEqual(self.invoke('review-read', '--review-sha256', review_sha256)[0], 1)
        self.assertEqual(self.invoke('review-preview', '--candidate-sha256', candidate_sha256)[0], 1)
        self.assertEqual(before, self.snapshot())

    def test_review_workflow_retains_original_ignored_data_evidence(self):
        temporary = tempfile.TemporaryDirectory(prefix='manual-review-cli-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        self.source = create_accepted_bootstrap(self, runtime_directory=Path(temporary.name))
        candidate_sha256 = self.published_candidate()
        before = self.snapshot(include_database=False)
        execution_before = self.execution_snapshot()
        status, output, error = self.invoke('review-preview', '--candidate-sha256', candidate_sha256)
        self.assertEqual(status, 0, error)
        review_sha256 = self.assert_private_review_summary(output)['review_sha256']
        self.assertEqual(self.accept_review(candidate_sha256, review_sha256)[0], 0)
        status, output, error = self.invoke('review-read', '--review-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        self.assert_private_review_summary(output)
        status, output, error = self.invoke('revoke-preview', '--review-sha256', review_sha256)
        self.assertEqual(status, 0, error)
        revocation_sha256 = json.loads(output)['revocation_sha256']
        self.assertEqual(self.revoke_review(review_sha256, revocation_sha256)[0], 0)
        self.assertEqual(before, self.snapshot(include_database=False))
        self.assertEqual(execution_before, self.execution_snapshot())

    def test_review_and_revocation_require_exact_literals_hashes_and_private_directory(self):
        for command, options in (
                ('review-accept', ['--candidate-sha256', '0' * 64]),
                ('review-accept', ['--candidate-sha256', '0' * 64,
                                  '--confirm-review-sha256', '0' * 64,
                                  '--human-confirmation', 'ACCEPT']),
                ('revoke', ['--review-sha256', '0' * 64]),
                ('revoke', ['--review-sha256', '0' * 64,
                            '--confirm-revocation-sha256', 'invalid',
                            '--human-confirmation', 'REVOKE_MANUAL_REVIEW'])):
            with self.subTest(command=command, options=options), self.assertRaises(SystemExit) as rejected:
                self.invoke(command, *options)
            self.assertEqual(rejected.exception.code, 2)
        candidate_sha256 = self.published_candidate()
        arguments = self.arguments('review-preview', '--candidate-sha256', candidate_sha256)
        directory_argument = arguments.index('--review-directory') + 1
        arguments[directory_argument] = str(REPO_ROOT / 'data' / 'never-created-manual-review-cli')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(arguments), 1)
        self.assertFalse((REPO_ROOT / 'data' / 'never-created-manual-review-cli').exists())

    def test_public_review_wrapper_in_fresh_processes(self):
        if not (REPO_ROOT / '.venv/bin/python').exists():
            self.skipTest('Requires installed development virtualenv')
        candidate_sha256 = self.published_candidate()
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')

        def execute(command, *options):
            result = subprocess.run(
                [str(REPO_ROOT / 'scripts/aos-parameter-skill')] + self.arguments(command, *options),
                cwd=self.source.root, env=environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return self.assert_private_review_summary(result.stdout)

        review_sha256 = execute('review-preview', '--candidate-sha256', candidate_sha256)['review_sha256']
        accepted = execute('review-accept', '--candidate-sha256', candidate_sha256,
                           '--confirm-review-sha256', review_sha256,
                           '--human-confirmation', 'ACCEPT_MANUAL_REVIEW')
        self.assertEqual(accepted['review_sha256'], review_sha256)
        self.assertEqual(execute('review-read', '--review-sha256', review_sha256)['status'], 'accepted')
        revocation_sha256 = execute('revoke-preview', '--review-sha256', review_sha256)['revocation_sha256']
        execute('revoke', '--review-sha256', review_sha256,
                '--confirm-revocation-sha256', revocation_sha256,
                '--human-confirmation', 'REVOKE_MANUAL_REVIEW')
        self.assertEqual(execute('review-read', '--review-sha256', review_sha256)['status'], 'revoked')

    def test_preview_is_read_only_private_and_opens_no_listener(self):
        before = self.snapshot()
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            status, output, error = self.invoke('preview')
        self.assertEqual(status, 0, error)
        self.assert_private_summary(output)
        self.assertFalse(self.source.candidate_directory.exists())
        self.assertEqual(before, self.snapshot())

    def test_both_applications_exact_publish_read_and_idempotence(self):
        for application in APPLICATIONS:
            with self.subTest(application=application):
                if application != APPLICATIONS[0]:
                    self.source = create_accepted_bootstrap(self, application)
                status, output, error = self.invoke('preview')
                self.assertEqual(status, 0, error)
                checksum = self.assert_private_summary(output)['candidate_sha256']
                status, output, error = self.publish(checksum)
                self.assertEqual(status, 0, error)
                self.assertEqual(self.assert_private_summary(output)['candidate_sha256'], checksum)
                before = self.snapshot()
                status, output, error = self.publish(checksum)
                self.assertEqual(status, 0, error)
                self.assertEqual(before, self.snapshot())
                status, output, error = self.invoke('read', '--candidate-sha256', checksum)
                self.assertEqual(status, 0, error)
                self.assertEqual(self.assert_private_summary(output)['candidate_sha256'], checksum)
                self.assertEqual(before, self.snapshot())

    def test_changed_review_hash_intent_and_destination_reject_before_publication(self):
        status, output, error = self.invoke('preview')
        self.assertEqual(status, 0, error)
        checksum = json.loads(output)['candidate_sha256']
        before = self.snapshot()
        status, output, error = self.publish('0' * 64)
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertEqual(before, self.snapshot())
        original_intent = self.source.intent_sha256
        self.source.intent_sha256 = '0' * 64
        self.assertEqual(self.publish(checksum)[0], 1)
        self.source.intent_sha256 = original_intent
        self.source.candidate_directory = self.source.root / 'different-candidates'
        self.assertEqual(self.publish(checksum)[0], 1)
        self.assertFalse(self.source.candidate_directory.exists())

    def test_wrong_manifest_database_and_missing_receipt_fail_closed(self):
        original_manifest = self.source.manifest_sha256
        self.source.manifest_sha256 = '0' * 64
        status, output, error = self.invoke('preview')
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.source.manifest_sha256 = original_manifest
        original_database = self.source.database
        self.source.database = self.source.root / 'not-the-original.sqlite'
        self.assertEqual(self.invoke('preview')[0], 1)
        self.assertFalse(self.source.database.exists())
        self.source.database = original_database
        next(self.source.journal_directory.glob('*.accepted.json')).unlink()
        self.assertEqual(self.invoke('preview')[0], 1)
        self.assertFalse(self.source.candidate_directory.exists())
        self.assertNotIn(str(self.source.root), error)

    def test_source_change_and_in_checkout_destination_reject(self):
        status, output, error = self.invoke('preview')
        self.assertEqual(status, 0, error)
        checksum = json.loads(output)['candidate_sha256']
        original_destination = self.source.candidate_directory
        self.source.candidate_directory = REPO_ROOT / 'data' / 'never-created-manual-skill-cli'
        self.assertEqual(self.invoke('preview')[0], 1)
        self.assertFalse(self.source.candidate_directory.exists())
        self.source.candidate_directory = original_destination
        source_file = self.source.directory / 'parameter-review.json'
        source_file.write_bytes(source_file.read_bytes() + b' ')
        before = self.snapshot()
        self.assertEqual(self.publish(checksum)[0], 1)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.source.candidate_directory.exists())

    def test_changed_original_terminal_status_rejects_reviewed_publication(self):
        status, output, error = self.invoke('preview')
        self.assertEqual(status, 0, error)
        checksum = json.loads(output)['candidate_sha256']
        self.source.store.connection.execute("UPDATE desktop_tasks SET status='failed'")
        self.source.store.connection.commit()
        before = self.snapshot()
        status, output, error = self.publish(checksum)
        self.assertEqual(status, 1)
        self.assertEqual(output, '')
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.source.candidate_directory.exists())

    def test_original_managed_evidence_inside_ignored_data_supports_public_workflow(self):
        temporary = tempfile.TemporaryDirectory(prefix='manual-skill-cli-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        runtime_directory = Path(temporary.name)
        self.source = create_accepted_bootstrap(self, runtime_directory=runtime_directory)
        self.assertIn(REPO_ROOT / 'data', self.source.database.parents)
        self.assertIn(REPO_ROOT / 'data', self.source.journal_directory.parents)
        self.assertNotIn(REPO_ROOT, self.source.directory.parents)
        self.assertNotIn(REPO_ROOT, self.source.candidate_directory.parents)
        before = self.snapshot()
        status, output, error = self.invoke('preview')
        self.assertEqual(status, 0, error)
        checksum = self.assert_private_summary(output)['candidate_sha256']
        self.assertEqual(before, self.snapshot())
        status, output, error = self.publish(checksum)
        self.assertEqual(status, 0, error)
        self.assert_private_summary(output)
        status, output, error = self.invoke('read', '--candidate-sha256', checksum)
        self.assertEqual(status, 0, error)
        self.assert_private_summary(output)
        self.assertEqual(before, self.snapshot())

    def test_runtime_path_exception_does_not_allow_source_tree_or_project_and_candidate(self):
        original_database = self.source.database
        original_journal = self.source.journal_directory
        for relative in ('src/private.sqlite', 'docs/private.sqlite', '.private/nested/private.sqlite'):
            with self.subTest(relative=relative):
                self.source.database = REPO_ROOT / relative
                self.assertEqual(self.invoke('preview')[0], 1)
                self.source.database = original_database
                self.source.journal_directory = (REPO_ROOT / relative).parent
                self.assertEqual(self.invoke('preview')[0], 1)
                self.source.journal_directory = original_journal
        self.source.database = REPO_ROOT
        self.assertEqual(self.invoke('preview')[0], 1)
        self.source.database = original_database
        self.source.candidate_directory = REPO_ROOT / 'data' / 'never-created-manual-skill-candidate'
        self.assertEqual(self.invoke('preview')[0], 1)
        self.assertFalse(self.source.candidate_directory.exists())
        self.source.candidate_directory = self.source.root / 'manual-skill-candidates'
        self.source.directory = REPO_ROOT / 'data'
        self.assertEqual(self.invoke('preview')[0], 1)

    def test_publication_requires_exact_literal_and_full_hash(self):
        for options in ([], ['--confirm-candidate-sha256', '0' * 64],
                        ['--confirm-candidate-sha256', '0' * 64,
                         '--human-confirmation', 'PUBLISH'],
                        ['--confirm-candidate-sha256', 'not-a-hash',
                         '--human-confirmation', 'PUBLISH_MANUAL_CANDIDATE']):
            with self.subTest(options=options), self.assertRaises(SystemExit) as rejected:
                self.invoke('publish', *options)
            self.assertEqual(rejected.exception.code, 2)
        self.assertFalse(self.source.candidate_directory.exists())

    def test_public_wrapper_and_module_in_fresh_processes(self):
        if not (REPO_ROOT / '.venv/bin/python').exists():
            self.skipTest('Requires installed development virtualenv')
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(REPO_ROOT / 'src')

        def execute(command, *options, module=False):
            prefix = ([sys.executable, '-m', 'aos.owned_parameter_skill_cli'] if module else
                      [str(REPO_ROOT / 'scripts/aos-parameter-skill')])
            result = subprocess.run(prefix + self.arguments(command, *options),
                                    cwd=self.source.root, env=environment,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return self.assert_private_summary(result.stdout)

        checksum = execute('preview', module=True)['candidate_sha256']
        self.assertEqual(execute('preview')['candidate_sha256'], checksum)
        published = execute('publish', '--confirm-candidate-sha256', checksum,
                            '--human-confirmation', 'PUBLISH_MANUAL_CANDIDATE')
        self.assertEqual(published['candidate_sha256'], checksum)
        self.assertEqual(execute('read', '--candidate-sha256', checksum)['candidate_sha256'], checksum)


if __name__ == '__main__':
    unittest.main()

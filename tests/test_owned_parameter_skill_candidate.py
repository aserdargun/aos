"""CPU fixture-engine tests with actual operator, owned TLS and trajectory audits."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.dataset_audit import audit_snapshot
from aos.owned_parameter_project import APPLICATIONS
from aos.owned_parameter_skill_candidate import (
    HUMAN_CONFIRMATION, OwnedParameterSkillCandidate, OwnedParameterSkillCandidateSession,
    candidate_summary,
)

import test_owned_parameter_project_desktop as desktop_fixture


def create_accepted_bootstrap(test_case, application_key=APPLICATIONS[0], *, runtime_directory=None):
    fixture = desktop_fixture.OwnedParameterProjectDesktopTests()
    test_case.addCleanup(fixture.doCleanups)
    fixture.setUp()
    external_root = fixture.root
    project_directory = external_root / 'case-0' / 'project'
    if runtime_directory is None:
        receipt = fixture.execute_case(
            0, application_key, 'Synthetic candidate record', 'Synthetic candidate note')
    else:
        fixture.root = Path(runtime_directory)
        project_directory = external_root / 'project'
        provision = desktop_fixture.provision_owned_parameter_project

        def provision_external_source(_runtime_source_directory, *arguments, **keywords):
            return provision(project_directory, *arguments, **keywords)

        with patch.object(desktop_fixture, 'provision_owned_parameter_project',
                          side_effect=provision_external_source):
            receipt = fixture.execute_case(
                0, application_key, 'Synthetic candidate record', 'Synthetic candidate note')
    root = fixture.root / 'case-0'
    database = root / 'trajectory.sqlite'
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    test_case.addCleanup(connection.close)
    intent_path, = (root / 'project-execution').glob('*.intent.json')
    intent = json.loads(intent_path.read_text())
    return SimpleNamespace(
        root=root, directory=project_directory, manifest_sha256=intent['source']['manifest_sha256'],
        database=database, journal_directory=root / 'project-execution',
        candidate_directory=project_directory.parent / 'manual-skill-candidates',
        intent_sha256=intent_path.name[:64],
        receipt=receipt, store=SimpleNamespace(connection=connection), fixture=fixture)


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS preparation')
class OwnedParameterSkillCandidateTests(unittest.TestCase):
    def setUp(self):
        self.source = create_accepted_bootstrap(self)
        self.session = self.new_session()

    def new_session(self, source=None, **changes):
        source = source or self.source
        arguments = {key: getattr(source, key) for key in (
            'directory', 'manifest_sha256', 'database', 'journal_directory', 'candidate_directory')}
        return OwnedParameterSkillCandidateSession(**(arguments | changes))

    def source_files(self):
        return {str(path.relative_to(self.source.root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for directory in (self.source.directory, self.source.journal_directory)
            for path in directory.rglob('*') if path.is_file()}

    def publish(self):
        candidate, checksum = self.session.preview(self.source.intent_sha256)
        self.assertEqual(self.session.publish(self.source.intent_sha256, checksum, HUMAN_CONFIRMATION),
                         (candidate, checksum))
        return candidate, checksum

    def test_read_only_preview_and_idempotent_private_publication_actual_audit(self):
        before = self.source_files()
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            before_snapshot = identity['sha256']
        candidate, checksum = self.session.preview(self.source.intent_sha256)
        self.assertFalse(self.source.candidate_directory.exists())
        self.assertEqual(validator('owned_parameter_skill_candidate').validate(candidate), None)
        self.assertEqual(candidate['bootstrap_receipt'], self.source.receipt)
        self.assertEqual(candidate['recipe_audit'], self.source.receipt['recipe_audit'])
        self.assertEqual(candidate['source_snapshot_sha256'],
                         self.source.receipt['recipe_audit']['source_snapshot_sha256'])
        self.assertEqual(candidate['recipe_audit']['consumed_approval_count'], 6)
        self.assertEqual(candidate['recipe_audit']['submit_count'], 1)
        self.assertEqual(candidate['parameters'], {'record-id': 'Synthetic candidate record',
                                                 'note-text': 'Synthetic candidate note'})
        self.assertEqual(candidate['form_body_sha256'],
                         hashlib.sha256(candidate['form_body'].encode('ascii')).hexdigest())
        self.assertEqual(checksum, digest(candidate))
        self.assertEqual(self.session.publish(self.source.intent_sha256, checksum, HUMAN_CONFIRMATION),
                         (candidate, checksum))
        path = self.source.candidate_directory / (checksum + '.json')
        before_publication = (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.source.candidate_directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(self.session.publish(self.source.intent_sha256, checksum, HUMAN_CONFIRMATION),
                         (candidate, checksum))
        self.assertEqual(self.new_session().read(checksum), (candidate, checksum))
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()), before_publication)
        self.assertEqual(self.source_files(), before)
        with audit_snapshot(self.source.database) as (_snapshot, identity):
            self.assertEqual(identity['sha256'], before_snapshot)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM trajectory_labels').fetchone()[0], 0)

    def test_two_app_candidates_preserve_scope_body_bindings_and_honest_flags(self):
        for application in APPLICATIONS:
            source = self.source if application == APPLICATIONS[0] else create_accepted_bootstrap(self, application)
            candidate, checksum = self.new_session(source).preview(source.intent_sha256)
            summary = candidate_summary(candidate, checksum)
            self.assertEqual(summary['scope']['application_id'], application)
            self.assertEqual(summary['kind'], 'audited_manual_bootstrap_candidate')
            self.assertEqual(summary['status'], 'awaiting_manual_review')
            self.assertTrue(summary['manual_recipe_execution_verified'])
            self.assertEqual(summary['model_calls'], 0)
            for flag in ('native_model_verified', 'released_skill_verified', 'site_outcome_verified',
                         'account_scope_verified', 'held_out_independence_verified', 'reviewed',
                         'activation_authorized', 'execution_authorized', 'training_ready',
                         'gpu_release_verified', 'replay_authorized', 'runtime_started'):
                self.assertIs(summary[flag], False)
            expected = {'contact_name', 'note'} if application == APPLICATIONS[0] else {'item_code', 'note'}
            self.assertEqual({binding['form_field_name'] for binding in candidate['field_bindings']}, expected)
            self.assertNotIn('source_event_ids', candidate)
            self.assertNotIn('Synthetic candidate note', canonical(summary))
            self.assertNotIn('Synthetic candidate record', canonical(summary))

    def test_actual_bootstrap_in_ignored_managed_runtime_data_with_external_authored_sources(self):
        temporary = tempfile.TemporaryDirectory(prefix='app-manual-skill-test-', dir=REPO_ROOT / 'data')
        self.addCleanup(temporary.cleanup)
        source = create_accepted_bootstrap(self, runtime_directory=Path(temporary.name))
        self.assertIn(REPO_ROOT / 'data', source.database.parents)
        self.assertIn(REPO_ROOT / 'data', source.journal_directory.parents)
        self.assertNotIn(REPO_ROOT, source.directory.parents)
        self.assertNotIn(REPO_ROOT, source.candidate_directory.parents)
        session = self.new_session(source)
        candidate, checksum = session.preview(source.intent_sha256)
        self.assertFalse(source.candidate_directory.exists())
        self.assertEqual(session.publish(source.intent_sha256, checksum, HUMAN_CONFIRMATION),
                         (candidate, checksum))
        self.assertEqual(self.new_session(source).read(checksum), (candidate, checksum))
        self.assertEqual(candidate['bootstrap_receipt'], source.receipt)
        self.assertEqual(candidate['database_identity']['inode'], source.database.stat().st_ino)
        self.assertEqual(source.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(source.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)

    def test_checkout_source_roots_remain_rejected_for_each_path_role(self):
        for field in ('directory', 'database', 'journal_directory', 'candidate_directory'):
            for path in (REPO_ROOT, REPO_ROOT / 'src' / 'private-test-runtime'):
                with self.subTest(field=field, path=path), self.assertRaises(ValueError):
                    self.new_session(**{field: path})
        for field in ('directory', 'candidate_directory'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.new_session(**{field: REPO_ROOT / 'data' / 'private-test-runtime'})
        for field in ('database', 'journal_directory'):
            for path in (REPO_ROOT / 'data', REPO_ROOT / 'data' / 'README.md',
                         REPO_ROOT / 'data' / '..' / 'src' / 'private-test-runtime'):
                with self.subTest(field=field, path=path), self.assertRaises(ValueError):
                    self.new_session(**{field: path})

    def test_wrong_sha_confirmation_destination_and_unresolved_journal_do_not_publish(self):
        candidate, checksum = self.session.preview(self.source.intent_sha256)
        for confirmation in (True, '', 'PUBLISH', None):
            with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                self.session.publish(self.source.intent_sha256, checksum, confirmation)
        with self.assertRaises(ValueError):
            self.session.publish(self.source.intent_sha256, 'a' * 64, HUMAN_CONFIRMATION)
        with self.assertRaises(ValueError):
            self.new_session(candidate_directory=self.source.root / 'different-candidates').publish(
                self.source.intent_sha256, checksum, HUMAN_CONFIRMATION)
        self.assertFalse(self.source.candidate_directory.exists())
        accepted_path, = self.source.journal_directory.glob('*.accepted.json')
        accepted_path.unlink()
        with self.assertRaises(ValueError):
            self.session.preview(self.source.intent_sha256)
        self.assertFalse(self.source.candidate_directory.exists())

    def test_unrelated_database_and_current_session_changes_keep_candidate_hash(self):
        candidate, checksum = self.publish()
        connection = self.source.store.connection
        connection.execute("UPDATE desktop_sessions SET owner='HUMAN',status='stopped',generation=generation+1")
        connection.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (
            'synthetic-unrelated-task', 'Synthetic other task', 'Synthetic other task', 'en',
            '[]', '{}', '2026-09-30T00:00:00Z'))
        connection.commit()
        self.assertEqual(self.session.preview(self.source.intent_sha256), (candidate, checksum))
        self.assertEqual(self.session.publish(self.source.intent_sha256, checksum, HUMAN_CONFIRMATION),
                         (candidate, checksum))
        self.assertEqual(self.new_session().read(checksum), (candidate, checksum))

    def test_changed_original_terminal_state_and_authority_are_rejected(self):
        _candidate, checksum = self.publish()
        connection = self.source.store.connection
        for column, value in (('status', 'failed'), ('generation', 123456), ('real_model', 1)):
            before = connection.execute('SELECT ' + column + ' FROM desktop_tasks').fetchone()[0]
            connection.execute('UPDATE desktop_tasks SET ' + column + '=?', (value,))
            connection.commit()
            with self.subTest(column=column), self.assertRaises(ValueError):
                self.new_session().read(checksum)
            connection.execute('UPDATE desktop_tasks SET ' + column + '=?', (before,))
            connection.commit()
        run_id = self.source.receipt['run_identity']['run_id']
        connection.execute("UPDATE runs SET status='failed' WHERE run_id=?", (run_id,))
        connection.commit()
        with self.assertRaises(ValueError):
            self.session.preview(self.source.intent_sha256)

    def test_mutated_run_content_rejects_read_and_stale_publish(self):
        _candidate, checksum = self.publish()
        connection = self.source.store.connection
        connection.execute("UPDATE tasks SET original_goal='Synthetic changed original goal'")
        connection.commit()
        with self.assertRaises(ValueError):
            self.session.read(checksum)
        with self.assertRaises(ValueError):
            self.session.publish(self.source.intent_sha256, checksum, HUMAN_CONFIRMATION)

    def test_actual_recipe_auditor_rejects_changed_action_approval_evidence(self):
        _candidate, checksum = self.publish()
        connection = self.source.store.connection
        connection.execute("UPDATE actions SET status='error' WHERE tool='browser.form.fill'")
        connection.commit()
        with self.assertRaises(ValueError):
            self.session.preview(self.source.intent_sha256)
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)

    def test_changed_current_source_and_receipt_rejected(self):
        _candidate, checksum = self.publish()
        source_path = self.source.directory / 'remote-form-skill-recipe.json'
        original = source_path.read_bytes()
        source_path.write_bytes(original + b' ')
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        source_path.write_bytes(original)
        accepted_path, = self.source.journal_directory.glob('*.accepted.json')
        receipt = json.loads(accepted_path.read_text())
        receipt['recipe_audit']['stages'][0]['approval_ref'] = 'a' * 64
        receipt['recipe_audit_sha256'] = digest(receipt['recipe_audit'])
        accepted_path.write_text(canonical(receipt))
        with self.assertRaises(ValueError):
            self.session.preview(self.source.intent_sha256)
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)

    def test_model_and_canonical_schema_reject_claim_escalation_and_mismatched_pins(self):
        candidate, _checksum = self.session.preview(self.source.intent_sha256)
        for changes in ({'native_model_verified': True}, {'model_calls': True},
                        {'training_ready': 0}, {'manual_recipe_execution_verified': 1},
                        {'source_event_ids': ['fabricated-event']}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    OwnedParameterSkillCandidate.model_validate_json(canonical(candidate | changes))
                self.assertFalse(validator('owned_parameter_skill_candidate').is_valid(candidate | changes))
        for field in ('intent_sha256', 'manifest_sha256', 'receipt_sha256', 'run_binding_sha256',
                      'recipe_audit_sha256', 'form_body_sha256'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                OwnedParameterSkillCandidate.model_validate_json(canonical(candidate | {field: 'a' * 64}))
        altered = deepcopy(candidate)
        altered['field_bindings'][0]['form_field_name'] = 'foreign_field'
        with self.assertRaises(ValueError):
            OwnedParameterSkillCandidate.model_validate_json(canonical(altered))

    def test_public_symlink_hardlink_and_noncanonical_candidate_stores_fail_closed(self):
        candidate, checksum = self.publish()
        path = self.source.candidate_directory / (checksum + '.json')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        path.chmod(0o600)
        link = self.source.root / 'candidate-hardlink.json'
        os.link(path, link)
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        link.unlink()
        path.write_text(canonical(candidate) + '\n')
        with self.assertRaises(ValueError):
            self.new_session().read(checksum)
        path.unlink()
        original = self.source.root / 'private-candidate.json'
        original.write_text(canonical(candidate))
        original.chmod(0o600)
        path.symlink_to(original)
        with self.assertRaises(OSError):
            self.new_session().read(checksum)

    def test_database_symlink_public_file_and_replacement_identity_rejected(self):
        candidate, checksum = self.publish()
        database_link = self.source.root / 'database-symlink.sqlite'
        database_link.symlink_to(self.source.database)
        with self.assertRaises(OSError):
            self.new_session(database=database_link).preview(self.source.intent_sha256)
        self.source.database.chmod(0o644)
        with self.assertRaises(ValueError):
            self.session.read(checksum)
        self.source.database.chmod(0o600)
        copied_database = self.source.root / 'private-copy.sqlite'
        connection = sqlite3.connect(copied_database)
        self.addCleanup(connection.close)
        self.source.store.connection.backup(connection)
        copied_database.chmod(0o600)
        self.assertNotEqual(copied_database.stat().st_ino, candidate['database_identity']['inode'])
        with self.assertRaises(ValueError):
            self.new_session(database=copied_database).read(checksum)


if __name__ == '__main__':
    unittest.main()

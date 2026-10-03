"""Selected manual reuse compilation over actual original CPU bootstrap evidence."""

from copy import deepcopy
import hashlib
import shutil
import unittest

from aos.contracts import canonical, digest
from aos.dataset import validator
from aos.owned_parameter_project import APPLICATIONS, read_owned_parameter_project
from aos.owned_parameter_skill_release import (
    OwnedParameterSkillReleaseSession, RELEASE_CONFIRMATION, SELECT_CONFIRMATION, ROLLBACK_CONFIRMATION,
)
from aos.owned_parameter_skill_reuse import (
    MODE, OwnedParameterSkillReuseAdmission, OwnedParameterSkillReuseSession, OwnedParameterSkillReuseSource,
)

from test_owned_parameter_skill_candidate import create_accepted_bootstrap
from test_owned_parameter_skill_release import create_shared_reviewed_bootstraps, publish_reviewed_source


def selected_manual_source(test_case, application=APPLICATIONS[0]):
    source = create_accepted_bootstrap(test_case, application)
    reviewed = publish_reviewed_source(source)
    reviewed.releases = OwnedParameterSkillReleaseSession(reviewed.reviews, source.root / 'release-catalog')
    _release, reviewed.release_sha256 = reviewed.releases.preview(reviewed.review_sha256)
    reviewed.releases.release(reviewed.review_sha256, None, reviewed.release_sha256, RELEASE_CONFIRMATION)
    _selection, reviewed.selection_sha256 = reviewed.releases.selection_preview(reviewed.release_sha256)
    reviewed.releases.select(reviewed.release_sha256, None, 'select', reviewed.selection_sha256,
                             SELECT_CONFIRMATION)
    return reviewed


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS preparation')
class OwnedParameterSkillReuseTests(unittest.TestCase):
    def setUp(self):
        self.reviewed = selected_manual_source(self)
        self.source = self.reviewed.source
        self.session = OwnedParameterSkillReuseSession(self.reviewed.releases)
        self.parameters = {'record-id': 'Synthetic new record', 'note-text': 'New manual reuse note'}

    def preview(self, parameters=None):
        return self.session.preview(self.reviewed.release_sha256, self.reviewed.selection_sha256,
                                    self.parameters if parameters is None else parameters)

    def source_bytes(self):
        return {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in self.source.root.rglob('*')
                if path.is_file() and path != self.source.database.with_name(self.source.database.name + '-shm')}

    def test_new_parameters_compile_exact_selected_recipe_without_source_or_database_writes(self):
        original = read_owned_parameter_project(self.source.directory, self.source.manifest_sha256)
        before = self.source_bytes()
        admission, checksum = self.preview()
        validator('owned_parameter_skill_reuse_admission').validate(admission)
        validator('owned_parameter_skill_reuse_source').validate(admission['source'])
        self.assertEqual(checksum, digest(admission))
        self.assertEqual(self.preview(dict(reversed(list(self.parameters.items())))), (admission, checksum))
        self.assertEqual(self.session.current_source(admission), admission['source'])
        bundle = self.session.bundle(admission)
        self.assertEqual(bundle['mode'], MODE)
        self.assertEqual(bundle['admission_sha256'], checksum)
        self.assertEqual(bundle['manifest'], original['manifest'])
        self.assertEqual(bundle['manifest']['invocation_sha256'], original['invocation_sha256'])
        self.assertNotEqual(bundle['invocation_sha256'], bundle['manifest']['invocation_sha256'])
        self.assertEqual(bundle['parameters'], self.parameters)
        self.assertEqual(dict(bundle['fields']), {'contact_name': self.parameters['record-id'],
                                                 'note': self.parameters['note-text']})
        self.assertEqual(self.session.revalidate(admission).model_dump(mode='json'), bundle['invocation'])
        for key in ('profile_sha256', 'task_sha256', 'skill_sha256', 'recipe_sha256',
                    'certificate_file', 'key_file', 'certificate_sha256', 'recipe', 'field_bindings'):
            self.assertEqual(bundle[key], original[key])
        for key in ('form_plan_sha256', 'state_plan_sha256', 'invocation_sha256', 'body'):
            self.assertNotEqual(bundle[key], original[key])
        self.assertEqual(bundle['bodies']['entry'], original['bodies']['entry'])
        self.assertEqual(bundle['bodies']['before'], original['bodies']['before'])
        self.assertNotEqual(bundle['bodies']['after'], original['bodies']['after'])
        held_out = [case.model_dump(mode='json') for case in original['skill_plan'].cases if case.cohort == 'held_out']
        self.assertEqual([case.model_dump(mode='json') for case in bundle['skill_plan'].cases
                          if case.cohort == 'held_out'], held_out)
        self.assertEqual(bundle['skill_plan'].source_variant_sha256, original['skill_plan'].source_variant_sha256)
        self.assertEqual(bundle['recipe'].skill_sha256, original['recipe'].skill_sha256)
        self.assertEqual(self.source_bytes(), before)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(self.source.store.connection.execute('SELECT count(*) FROM trajectory_labels').fetchone()[0], 0)
        for key in ('reviewed', 'execution_authorized', 'execution_performed', 'released_skill_verified',
                    'native_model_verified', 'activation_authorized', 'training_ready', 'gpu_release_verified',
                    'runtime_started', 'held_out_independence_verified'):
            self.assertIs(admission[key], False)
        self.assertTrue(admission['source']['release']['reviewed'])

    def test_second_application_preserves_its_exact_field_scope(self):
        reviewed = selected_manual_source(self, APPLICATIONS[1])
        session = OwnedParameterSkillReuseSession(reviewed.releases)
        admission, _checksum = session.preview(reviewed.release_sha256, reviewed.selection_sha256, self.parameters)
        bundle = session.bundle(admission)
        self.assertEqual(admission['source']['scope']['application_id'], APPLICATIONS[1])
        self.assertEqual(dict(bundle['fields']), {'item_code': self.parameters['record-id'],
                                                 'note': self.parameters['note-text']})
        self.assertNotEqual(admission['source']['family_sha256'],
                            self.reviewed.releases.read(self.reviewed.release_sha256)[0]['family_sha256'])

    def test_parameter_bounds_scope_unchanged_map_and_structural_case_collision_fail_closed(self):
        before = self.source_bytes()
        invalid = [None, [], {}, self.parameters | {'account_role': 'administrator'},
                   {'record-id': 'missing-note'}, self.parameters | {'note-text': True},
                   self.parameters | {'note-text': ''}, self.parameters | {'note-text': 'x' * 129},
                   self.parameters | {'note-text': '€' * 100},
                   self.parameters | {'note-text': 'line\nbreak'}, self.reviewed.candidate['parameters'],
                   {'record-id': 'Synthetic held-out A', 'note-text': 'Synthetic held-out note A'}]
        for parameters in invalid:
            with self.subTest(parameters=parameters), self.assertRaises((ValueError, TypeError)):
                self.session.preview(self.reviewed.release_sha256, self.reviewed.selection_sha256, parameters)
        self.assertEqual(self.source_bytes(), before)

    def test_changed_source_and_revoked_review_close_preview_bundle_and_action_callback(self):
        admission, _checksum = self.preview()
        path = self.source.directory / 'remote-form-skill-recipe.json'
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        for operation in (self.preview, lambda: self.session.bundle(admission),
                          lambda: self.session.current_source(admission), lambda: self.session.revalidate(admission)):
            with self.assertRaises(ValueError):
                operation()
        path.write_bytes(original)
        self.assertEqual(self.session.current_source(admission), admission['source'])
        _record, checksum = self.reviewed.reviews.revocation_preview(self.reviewed.review_sha256)
        self.reviewed.reviews.revoke(self.reviewed.review_sha256, checksum, 'REVOKE_MANUAL_REVIEW')
        for operation in (self.preview, lambda: self.session.bundle(admission),
                          lambda: self.session.current_source(admission), lambda: self.session.revalidate(admission)):
            with self.assertRaises(ValueError):
                operation()

    def test_typed_and_fresh_recompile_checks_reject_admission_tampering(self):
        admission, _checksum = self.preview()
        for field in ('execution_authorized', 'execution_performed', 'reviewed', 'activation_authorized',
                      'native_model_verified', 'released_skill_verified', 'training_ready'):
            for value in (True, 0):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    OwnedParameterSkillReuseAdmission.model_validate_json(canonical(admission | {field: value}))
        for field in ('invocation_sha256', 'form_body_sha256', 'skill_plan_sha256', 'state_plan_sha256'):
            with self.assertRaises(ValueError):
                self.session.bundle(admission | {field: 'a' * 64})
        changed = deepcopy(admission)
        changed['source']['source_run_ref'] = 'a' * 64
        with self.assertRaises(ValueError):
            OwnedParameterSkillReuseSource.model_validate_json(canonical(changed['source']))
        changed = deepcopy(admission)
        changed['source']['certificate_sha256'] = 'a' * 64
        OwnedParameterSkillReuseAdmission.model_validate_json(canonical(changed))
        with self.assertRaisesRegex(ValueError, 'admission_changed'):
            self.session.bundle(changed)
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            self.session.current_source(changed)

    def test_unselected_or_unknown_hashes_and_bootstrap_run_are_not_reuse_evidence(self):
        for release_sha, selection_sha in (('a' * 64, self.reviewed.selection_sha256),
                                           (self.reviewed.release_sha256, 'a' * 64)):
            with self.assertRaises(ValueError):
                self.session.preview(release_sha, selection_sha, self.parameters)
        admission, _checksum = self.preview()
        with self.assertRaises(ValueError):
            self.session.audit(admission, self.source.database,
                               self.source.receipt['run_identity']['run_id'])
        with self.assertRaisesRegex(ValueError, 'original_database_required'):
            self.session.audit(admission, self.source.root / 'different.sqlite', 'run-' + 'a' * 32)
        self.assertFalse((self.source.root / 'different.sqlite').exists())

    def test_same_release_reselected_under_new_head_invalidates_old_admission(self):
        first, second = create_shared_reviewed_bootstraps(self)
        _record, first_sha = first.releases.preview(first.review_sha256)
        first.releases.release(first.review_sha256, None, first_sha, RELEASE_CONFIRMATION)
        _record, first_selection = first.releases.selection_preview(first_sha)
        first.releases.select(first_sha, None, 'select', first_selection, SELECT_CONFIRMATION)
        session = OwnedParameterSkillReuseSession(first.releases)
        admission, admission_sha = session.preview(first_sha, first_selection, self.parameters)
        wrong_source = OwnedParameterSkillReuseSession(second.releases)
        with self.assertRaises((ValueError, FileNotFoundError)):
            wrong_source.preview(first_sha, first_selection, self.parameters)
        _record, second_sha = second.releases.preview(second.review_sha256, first_sha)
        second.releases.release(second.review_sha256, first_sha, second_sha, RELEASE_CONFIRMATION)
        _record, second_selection = second.releases.selection_preview(second_sha, first_selection)
        second.releases.select(second_sha, first_selection, 'select', second_selection, SELECT_CONFIRMATION)
        for operation in (lambda: session.current_source(admission), lambda: session.bundle(admission)):
            with self.assertRaisesRegex(ValueError, 'selection_changed'):
                operation()
        _record, rollback_sha = first.releases.selection_preview(first_sha, second_selection, 'rollback')
        first.releases.select(first_sha, second_selection, 'rollback', rollback_sha, ROLLBACK_CONFIRMATION)
        with self.assertRaisesRegex(ValueError, 'selection_changed'):
            session.current_source(admission)
        current, current_sha = session.preview(first_sha, rollback_sha, self.parameters)
        self.assertNotEqual(current_sha, admission_sha)
        self.assertEqual(current['invocation'], admission['invocation'])
        self.assertEqual(current['source']['selection_sha256'], rollback_sha)
        self.assertEqual(session.current_source(current), current['source'])


if __name__ == '__main__':
    unittest.main()

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from aos.contracts import canonical, digest
from aos.desktop_tasks import DesktopScheduler
from aos.owned_skill_reuse_admission import (
    assert_owned_skill_reuse_current, load_owned_skill_reuse_admission,
    make_owned_skill_reuse_admission, persist_owned_skill_reuse_admission,
    validate_owned_skill_reuse_admission,
)


class OwnedSkillReuseAdmissionTests(unittest.TestCase):
    def setUp(self):
        example = json.loads(Path('examples/owned_skill_reuse_admission.json').read_text())
        self.preview = example['admission']['preview']
        self.controller = {
            'session_id': 'desktop-session-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            'runtime_id': 'runtime-ffffffffffffffffffffffffffffffff',
            'lease_id': 'lease-11111111111111111111111111111111',
            'generation': 1,
        }
        self.manager = 'app-dddddddddddddddddddddddddddddddd'

    def test_make_and_validate_admission_binds_fresh_controller(self):
        admission = make_owned_skill_reuse_admission(
            self.preview, manager_session=self.manager,
            controller_state=self.controller)
        self.assertEqual(admission['preview_sha256'], digest(self.preview))
        self.assertEqual(admission['manager_session'], self.manager)
        self.assertEqual(admission['desktop_session_id'], self.controller['session_id'])
        self.assertFalse(admission['execution_authorized'])
        self.assertIs(validate_owned_skill_reuse_admission(admission), admission)

    def test_preview_digest_or_false_claim_mutation_fails_closed(self):
        admission = make_owned_skill_reuse_admission(
            self.preview, manager_session=self.manager,
            controller_state=self.controller)
        altered = {**admission, 'preview_sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'admission_invalid'):
            validate_owned_skill_reuse_admission(altered)
        altered = {**admission, 'old_actions_replayed': True}
        with self.assertRaisesRegex(ValueError, 'admission_invalid'):
            validate_owned_skill_reuse_admission(altered)
        malformed_preview = dict(admission['preview'])
        malformed_preview.pop('purpose')
        altered = {**admission, 'preview': malformed_preview,
                   'preview_sha256': digest(malformed_preview)}
        with self.assertRaisesRegex(ValueError, 'admission_invalid'):
            validate_owned_skill_reuse_admission(altered)

    def test_manager_cannot_reopen_old_session_or_malformed_controller(self):
        with self.assertRaisesRegex(ValueError, 'manager_session_invalid'):
            make_owned_skill_reuse_admission(
                self.preview,
                manager_session=self.preview['previous_manager_session'],
                controller_state=self.controller)
        altered = {**self.controller, 'generation': True}
        with self.assertRaisesRegex(ValueError, 'controller_identity_invalid'):
            make_owned_skill_reuse_admission(
                self.preview, manager_session=self.manager,
                controller_state=altered)

    def test_private_admission_roundtrip_and_hash_binding(self):
        admission = make_owned_skill_reuse_admission(
            self.preview, manager_session=self.manager,
            controller_state=self.controller)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'owned-skill-reuse-admission.json'
            checksum = persist_owned_skill_reuse_admission(path, admission)
            loaded, loaded_checksum = load_owned_skill_reuse_admission(path, checksum)
            self.assertEqual(loaded, admission)
            self.assertEqual(loaded_checksum, checksum)
            state = {**self.controller}

            class Lock:
                def assert_current(self):
                    return True

            assert_owned_skill_reuse_current(path, checksum, Lock(), state, self.manager)
            with self.assertRaisesRegex(ValueError, 'controller_changed'):
                assert_owned_skill_reuse_current(
                    path, checksum, Lock(), {**state, 'generation': 2}, self.manager)
            linked = path.with_name('linked-admission.json')
            os.link(path, linked)
            with self.assertRaisesRegex(ValueError, 'file_invalid'):
                load_owned_skill_reuse_admission(path, checksum)


class OwnedSkillReusePreviewPinTests(unittest.TestCase):
    def test_historical_audit_joins_fresh_lease_runtime_and_original_source(self):
        from aos.web_application_binding import WebTaskAdmissionDraft

        binding_fixture = json.loads(
            Path('examples/web_application_binding.json').read_text())
        draft = WebTaskAdmissionDraft.model_validate_json(
            canonical(binding_fixture['draft'])).model_dump(mode='json')
        admission = {
            'desktop_session_id': 'desktop-session-new',
            'runtime_id': draft['runtime']['parent_runtime_id'],
            'lease_id': 'lease-new', 'generation': 7,
        }
        source_run_id = 'run-source-origin'
        source_preview = {
            'source_desktop_session_id': 'desktop-session-old',
            'source_run_ref': digest({'run_id': source_run_id}),
        }
        initial_state = {'runtime_id': draft['runtime']['runtime_id'],
                         'owner_lease_id': admission['lease_id']}
        task = {
            'session_id': admission['desktop_session_id'],
            'kind': 'browser_remote_form', 'status': 'succeeded',
            'run_id': 'run-fresh', 'lease_id': admission['lease_id'],
            'generation': admission['generation'],
            'runtime_id': draft['runtime']['runtime_id'],
        }
        session = {'runtime_id': admission['runtime_id']}
        binding = {
            'browser_runtime_id': draft['runtime']['runtime_id'],
            'draft_json': canonical(draft),
        }
        source_job = {
            'session_id': source_preview['source_desktop_session_id'],
            'kind': 'browser_remote_form', 'status': 'succeeded',
            'run_id': source_run_id,
        }
        check = DesktopScheduler._owned_reuse_identity_joins_match
        self.assertTrue(check(admission, source_preview, 'run-fresh', initial_state,
                              task, session, binding, source_job))
        for key, value in (('lease_id', 'lease-other'),
                           ('generation', admission['generation'] + 1),
                           ('runtime_id', 'browser-' + '0' * 32)):
            changed_task = {**task, key: value}
            self.assertFalse(check(admission, source_preview, 'run-fresh', initial_state,
                                   changed_task, session, binding, source_job), key)
        changed_state = {**initial_state, 'owner_lease_id': 'lease-other'}
        self.assertFalse(check(admission, source_preview, 'run-fresh', changed_state,
                               task, session, binding, source_job))
        changed_binding = {**binding, 'browser_runtime_id': 'browser-' + '0' * 32}
        self.assertFalse(check(admission, source_preview, 'run-fresh', initial_state,
                               task, session, changed_binding, source_job))
        changed_admission = {**admission, 'runtime_id': 'desktop-' + '0' * 32}
        self.assertFalse(check(changed_admission, source_preview, 'run-fresh', initial_state,
                               task, session, binding, source_job))
        self.assertFalse(check(admission, source_preview, 'run-fresh', initial_state,
                               task, session, binding,
                               {**source_job, 'session_id': admission['desktop_session_id']}))
        self.assertFalse(check(admission, source_preview, 'run-fresh', initial_state,
                               task, session, binding,
                               {**source_job, 'run_id': 'run-other'}))

    def test_reuse_admission_rejects_a_different_valid_release_family_candidate(self):
        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        candidate = {
            'source_fingerprint_sha256': '1' * 64,
            'source_group_sha256': '2' * 64,
            'profile_sha256': '3' * 64,
        }
        prepared = {
            'source': {'manifest_sha256': '4' * 64},
            'candidate': candidate,
            'source_run_ref': '5' * 64,
            'source_invocation_sha256': '6' * 64,
            'candidate_sha256': '7' * 64,
            'recipe_sha256': '8' * 64,
            'skill_sha256': '9' * 64,
        }
        release = {
            'family_sha256': 'a' * 64,
            'family': {'profile_sha256': candidate['profile_sha256']},
            'candidate_sha256': prepared['candidate_sha256'],
            'review_sha256': 'e' * 64,
            'source_run_ref': prepared['source_run_ref'],
            'recipe_sha256': prepared['recipe_sha256'],
            'source_group_sha256': candidate['source_group_sha256'],
            'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
            'skill_sha256': prepared['skill_sha256'],
            'evidence_execution_sha256': 'b' * 64,
        }
        preview = {
            'source_manifest_sha256': '4' * 64,
            'source_run_ref': '5' * 64,
            'source_invocation_sha256': '6' * 64,
            'source_fingerprint_sha256': '1' * 64,
            'family_sha256': 'a' * 64,
            'release_sha256': 'c' * 64,
            'selection_sha256': 'd' * 64,
            'review_sha256': 'e' * 64,
            'candidate_sha256': '7' * 64,
            'recipe_sha256': '8' * 64,
            'review_evidence_execution_sha256': 'b' * 64,
        }
        scheduler._owned_skill_reuse = {'admission': {'preview': preview}}
        scheduler._load_owned_release = lambda _sha: release
        scheduler._assert_owned_skill_reuse_candidate_binding(
            prepared, 'e' * 64, 'c' * 64, 'd' * 64)
        changed = {**prepared, 'candidate_sha256': 'f' * 64}
        with self.assertRaisesRegex(ValueError, 'candidate_binding_changed'):
            scheduler._assert_owned_skill_reuse_candidate_binding(
                changed, 'e' * 64, 'c' * 64, 'd' * 64)

    def test_reuse_preview_requires_release_and_changes_exact_preview_pin(self):
        base = {
            'candidate_sha256': 'a' * 64, 'source_run_ref': 'b' * 64,
            'source_invocation_sha256': 'c' * 64, 'source_group_sha256': 'd' * 64,
            'profile_sha256': 'e' * 64, 'skill_sha256': 'f' * 64,
            'case_key': 'dev-beta', 'parameter_variant_sha256': '1' * 64,
            'invocation_sha256': '2' * 64, 'recipe_sha256': '3' * 64,
            'steps': [{'step_key': 'save-record', 'operation': 'submit_form'}],
            'purpose': 'development_variation', 'independent_held_out': False,
            'form_plan': Mock(model_dump=Mock(return_value={'form': 'plan'})),
            'state_plan': Mock(model_dump=Mock(return_value={'state': 'plan'})),
        }
        checksum = '4' * 64
        with self.assertRaisesRegex(ValueError, 'pin_invalid'):
            DesktopScheduler._owned_candidate_execution_preview_payload(
                base, 'a' * 64, 'b' * 64, 'c' * 64,
                reuse_admission_sha256=checksum)
        version12 = DesktopScheduler._owned_candidate_execution_preview_payload(
            base, 'a' * 64, 'b' * 64, 'c' * 64,
            review_sha256='5' * 64, release_sha256='6' * 64,
            selection_sha256='7' * 64)
        version13 = DesktopScheduler._owned_candidate_execution_preview_payload(
            base, 'a' * 64, 'b' * 64, 'c' * 64,
            review_sha256='5' * 64, release_sha256='6' * 64,
            selection_sha256='7' * 64, reuse_admission_sha256=checksum)
        self.assertEqual(version13['schema_version'], '1.3')
        self.assertEqual(version13['reuse_admission_sha256'], checksum)
        self.assertNotEqual(version12['preview_sha256'], version13['preview_sha256'])
        self.assertEqual(version13['preview_sha256'], digest({
            key: value for key, value in version13.items() if key != 'preview_sha256'}))

    def test_v13_bundle_roundtrips_reuse_admission_without_downgrade(self):
        from tests.test_owned_candidate_execution_persistence import (
            DumpValue, OwnedCandidateExecutionPersistenceTests)
        from aos.owned_form_candidate_execution import (
            load_candidate_execution_bundle, persist_candidate_execution_bundle)

        base = OwnedCandidateExecutionPersistenceTests()
        base.setUp()
        self.addCleanup(base.tearDown)
        example = json.loads(Path('examples/owned_skill_reuse_admission.json').read_text())
        preview_context = dict(example['admission']['preview'])
        preview_context.update({
            'source_run_ref': base.source_run_ref,
            'source_invocation_sha256': base.prepared['source_invocation_sha256'],
            'source_manifest_sha256': base.candidate['source_context']['manifest_sha256'],
            'candidate_sha256': base.candidate_sha256,
            'recipe_sha256': digest(base.candidate['recipe']),
            'review_sha256': '4' * 64,
            'release_sha256': '5' * 64,
            'selection_sha256': '6' * 64,
        })
        reuse_admission = make_owned_skill_reuse_admission(
            preview_context, manager_session='app-dddddddddddddddddddddddddddddddd',
            controller_state={
                'session_id': 'desktop-session-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                'runtime_id': 'runtime-ffffffffffffffffffffffffffffffff',
                'lease_id': 'lease-11111111111111111111111111111111',
                'generation': 1,
            })
        review_sha256, release_sha256, selection_sha256 = '4' * 64, '5' * 64, '6' * 64
        prepared = {**base.prepared,
                    'candidate_sha256': base.candidate_sha256,
                    'profile_sha256': base.candidate['profile_sha256'],
                    'skill_sha256': base.invocation['skill_sha256'],
                    'case_key': base.invocation['case_key'],
                    'steps': [{'step_key': item['step_key'],
                               'operation': item['operation']}
                              for item in base.invocation['steps']],
                    'purpose': 'development_variation',
                    'independent_held_out': False}
        execution_preview = DesktopScheduler._owned_candidate_execution_preview_payload(
            prepared, base.candidate_sha256, base.source_run_ref,
            base.prepared['source_invocation_sha256'], review_sha256=review_sha256,
            release_sha256=release_sha256, selection_sha256=selection_sha256,
            reuse_admission_sha256=digest(reuse_admission))
        prepared['preview_sha256'] = execution_preview['preview_sha256']
        execution_sha256 = digest({
            'preview_sha256': execution_preview['preview_sha256'],
            'admission_sha256': digest(base.admission),
            'source_run_ref': base.source_run_ref,
            'release_sha256': release_sha256,
            'selection_sha256': selection_sha256,
            'reuse_admission_sha256': digest(reuse_admission),
        })
        directory, _ = persist_candidate_execution_bundle(
            base.candidate_directory, base.candidate_sha256, execution_sha256,
            prepared, DumpValue(base.admission), review_sha256=review_sha256,
            release_sha256=release_sha256, selection_sha256=selection_sha256,
            reuse_admission_sha256=digest(reuse_admission),
            reuse_admission=reuse_admission)
        loaded, _ = load_candidate_execution_bundle(
            base.candidate_directory, execution_sha256)
        self.assertEqual(loaded['manifest']['schema_version'], '1.3')
        self.assertEqual(loaded['reuse-admission'], reuse_admission)
        manifest = json.loads((directory / 'manifest.json').read_bytes())
        manifest['schema_version'] = '1.2'
        (directory / 'manifest.json').write_text(
            canonical(manifest))
        (directory / 'manifest.json').chmod(0o600)
        with self.assertRaises(ValueError):
            load_candidate_execution_bundle(base.candidate_directory,
                                            execution_sha256)


if __name__ == '__main__':
    unittest.main()

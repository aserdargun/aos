import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aos.contracts import canonical, digest
from aos.owned_form_candidate_execution import (
    _read_private_child, load_candidate_execution_bundle,
    persist_candidate_execution_bundle, persist_candidate_execution_completion,
)
from aos.workspace_identity import open_existing_workspace


class DumpValue:
    def __init__(self, value):
        self.value = value

    def model_dump(self, mode='json'):
        return self.value


class OwnedCandidateExecutionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.candidate_directory = self.root / 'candidate-execution-bundles'
        self.source_run_id = 'run-source-test'
        self.source_run_ref = digest({'run_id': self.source_run_id})
        self.candidate = {
            'source_run_ref': self.source_run_ref,
            'source_context': {'manifest_sha256': '6' * 64,
                               'invocation_sha256': '5' * 64},
            'source_group_sha256': '1' * 64,
            'profile_sha256': '2' * 64,
            'recipe': {'skill_sha256': '3' * 64},
        }
        self.candidate_sha256 = digest(self.candidate)
        self.recipe = {'schema_version': '1.0', 'skill_sha256': '3' * 64}
        self.plan = {'schema_version': '1.0', 'cases': []}
        self.inputs = {'schema_version': '1.0', 'plan_sha256': digest(self.plan), 'cases': []}
        self.form_body = b'message=beta'
        self.state_after = b'<html><h1 id="outcome">beta</h1></html>'
        self.form_plan = {
            'body_sha256': hashlib.sha256(self.form_body).hexdigest(),
            'body_bytes': len(self.form_body),
        }
        self.state_plan = {
            'expected_after_sha256': hashlib.sha256(self.state_after).hexdigest(),
        }
        self.invocation = {
            'recipe_sha256': digest(self.recipe),
            'skill_sha256': '3' * 64,
            'form_plan_sha256': digest(self.form_plan),
            'state_plan_sha256': digest(self.state_plan),
            'skill_plan_sha256': digest(self.plan),
            'case_inputs_sha256': digest(self.inputs),
            'case_key': 'dev-beta',
            'steps': [{'step_key': 'open-entry', 'operation': 'open_entry'}],
        }
        self.invocation_sha256 = digest(self.invocation)
        self.parameter_variant_sha256 = '4' * 64
        self.admission = {
            'candidate_sha256': self.candidate_sha256,
            'invocation_sha256': self.invocation_sha256,
            'parameter_variant_sha256': self.parameter_variant_sha256,
            'skill_sha256': '3' * 64,
        }
        self.prepared = {
            'candidate': self.candidate,
            'plan': DumpValue(self.plan),
            'inputs': DumpValue(self.inputs),
            'form_plan': DumpValue(self.form_plan),
            'state_plan': DumpValue(self.state_plan),
            'field_bindings': [],
            'recipe': DumpValue(self.recipe),
            'invocation': self.invocation,
            'form_body': self.form_body,
            'state_after': self.state_after,
            'source_run_id': self.source_run_id,
            'source_run_ref': self.source_run_ref,
            'source_invocation_sha256': '5' * 64,
            'source_group_sha256': self.candidate['source_group_sha256'],
            'parameter_variant_sha256': self.parameter_variant_sha256,
            'invocation_sha256': self.invocation_sha256,
            'recipe_sha256': digest(self.recipe),
            'preview_sha256': None,
        }
        preview = {
            'candidate_sha256': self.candidate_sha256,
            'source_run_ref': self.source_run_ref,
            'source_invocation_sha256': self.prepared['source_invocation_sha256'],
            'source_group_sha256': self.prepared['source_group_sha256'],
            'profile_sha256': self.candidate['profile_sha256'],
            'skill_sha256': self.invocation['skill_sha256'],
            'case_key': self.invocation['case_key'],
            'parameter_variant_sha256': self.parameter_variant_sha256,
            'invocation_sha256': self.invocation_sha256,
            'recipe_sha256': digest(self.recipe),
            'steps': [{'step_key': step['step_key'], 'operation': step['operation']}
                      for step in self.invocation['steps']],
            'purpose': 'development_variation',
            'independent_held_out': False,
            'schema_version': '1.0', 'available': True, 'status': 'preview',
            'form_plan_sha256': digest(self.form_plan),
            'state_plan_sha256': digest(self.state_plan), 'report': None,
        }
        self.prepared['preview_sha256'] = digest(preview)
        self.execution_sha256 = digest({
            'preview_sha256': self.prepared['preview_sha256'],
            'admission_sha256': digest(self.admission),
            'source_run_ref': self.source_run_ref,
        })
        self.bundle_directory, self.manifest_sha256 = persist_candidate_execution_bundle(
            self.candidate_directory, self.candidate_sha256, self.execution_sha256,
            self.prepared, DumpValue(self.admission))

    def tearDown(self):
        self.temporary.cleanup()

    def _rewrite_json(self, path, mutate):
        value = json.loads(path.read_bytes())
        mutate(value)
        path.write_bytes(canonical(value).encode())
        path.chmod(0o600)

    def test_bundle_load_is_repeatable_and_binds_execution_identity(self):
        loaded, checksum = load_candidate_execution_bundle(
            self.candidate_directory, self.execution_sha256)
        loaded_again, checksum_again = load_candidate_execution_bundle(
            self.candidate_directory, self.execution_sha256)

        self.assertEqual(checksum, self.manifest_sha256)
        self.assertEqual(checksum_again, checksum)
        self.assertEqual(loaded_again['manifest'], loaded['manifest'])

    def test_reviewed_bundle_pins_schema_preview_and_cannot_be_downgraded(self):
        review_sha256 = 'f' * 64
        preview = {
            'candidate_sha256': self.candidate_sha256,
            'source_run_ref': self.source_run_ref,
            'source_invocation_sha256': self.prepared['source_invocation_sha256'],
            'source_group_sha256': self.prepared['source_group_sha256'],
            'profile_sha256': self.candidate['profile_sha256'],
            'skill_sha256': self.invocation['skill_sha256'],
            'case_key': self.invocation['case_key'],
            'parameter_variant_sha256': self.parameter_variant_sha256,
            'invocation_sha256': self.invocation_sha256,
            'recipe_sha256': digest(self.recipe),
            'steps': [{'step_key': step['step_key'], 'operation': step['operation']}
                      for step in self.invocation['steps']],
            'purpose': 'development_variation', 'independent_held_out': False,
            'schema_version': '1.1', 'available': True, 'status': 'preview',
            'form_plan_sha256': digest(self.form_plan),
            'state_plan_sha256': digest(self.state_plan), 'report': None,
            'review_sha256': review_sha256,
        }
        prepared = {**self.prepared, 'preview_sha256': digest(preview)}
        execution_sha256 = digest({
            'preview_sha256': prepared['preview_sha256'],
            'admission_sha256': digest(self.admission),
            'source_run_ref': self.source_run_ref,
        })
        directory, _ = persist_candidate_execution_bundle(
            self.candidate_directory, self.candidate_sha256, execution_sha256,
            prepared, DumpValue(self.admission), review_sha256=review_sha256)
        loaded, _ = load_candidate_execution_bundle(
            self.candidate_directory, execution_sha256)
        self.assertEqual(loaded['manifest']['schema_version'], '1.1')
        self.assertEqual(loaded['manifest']['review_sha256'], review_sha256)
        from aos.owned_form_candidate_execution import audit_persisted_candidate_execution
        with self.assertRaisesRegex(ValueError, 'requires_review_audit'):
            audit_persisted_candidate_execution(
                self.candidate_directory, execution_sha256,
                candidate_session=None, database=self.root / 'store.sqlite')

        manifest_path = directory / 'manifest.json'
        self._rewrite_json(manifest_path, lambda manifest: (
            manifest.__setitem__('schema_version', '1.0'),
            manifest.pop('review_sha256', None)))
        with self.assertRaisesRegex(ValueError, 'preview_pin_changed'):
            load_candidate_execution_bundle(self.candidate_directory,
                                            execution_sha256)

    def test_reviewed_bundle_hash_change_is_rejected(self):
        review_sha256 = 'e' * 64
        preview = {
            'candidate_sha256': self.candidate_sha256,
            'source_run_ref': self.source_run_ref,
            'source_invocation_sha256': self.prepared['source_invocation_sha256'],
            'source_group_sha256': self.prepared['source_group_sha256'],
            'profile_sha256': self.candidate['profile_sha256'],
            'skill_sha256': self.invocation['skill_sha256'],
            'case_key': self.invocation['case_key'],
            'parameter_variant_sha256': self.parameter_variant_sha256,
            'invocation_sha256': self.invocation_sha256,
            'recipe_sha256': digest(self.recipe),
            'steps': [{'step_key': step['step_key'], 'operation': step['operation']}
                      for step in self.invocation['steps']],
            'purpose': 'development_variation', 'independent_held_out': False,
            'schema_version': '1.1', 'available': True, 'status': 'preview',
            'form_plan_sha256': digest(self.form_plan),
            'state_plan_sha256': digest(self.state_plan), 'report': None,
            'review_sha256': review_sha256,
        }
        prepared = {**self.prepared, 'preview_sha256': digest(preview)}
        execution_sha256 = digest({
            'preview_sha256': prepared['preview_sha256'],
            'admission_sha256': digest(self.admission),
            'source_run_ref': self.source_run_ref,
        })
        persist_candidate_execution_bundle(
            self.candidate_directory, self.candidate_sha256, execution_sha256,
            prepared, DumpValue(self.admission), review_sha256=review_sha256)
        manifest_path = self.candidate_directory / execution_sha256 / 'manifest.json'
        self._rewrite_json(manifest_path,
                           lambda manifest: manifest.__setitem__('review_sha256', '0' * 64))
        with self.assertRaises(ValueError):
            load_candidate_execution_bundle(self.candidate_directory,
                                            execution_sha256)

    def test_relabelled_execution_directory_and_manifest_are_rejected(self):
        new_identity = 'a' * 64
        relabelled = self.bundle_directory.with_name(new_identity)
        self.bundle_directory.rename(relabelled)
        manifest_path = relabelled / 'manifest.json'
        self._rewrite_json(manifest_path,
                           lambda manifest: manifest.__setitem__(
                               'candidate_execution_sha256', new_identity))

        with self.assertRaisesRegex(ValueError, 'manifest_invalid'):
            load_candidate_execution_bundle(self.candidate_directory, new_identity)

    def test_manifest_claim_mutation_is_rejected(self):
        manifest_path = self.bundle_directory / 'manifest.json'
        self._rewrite_json(manifest_path,
                           lambda manifest: manifest.__setitem__('training_ready', True))
        with self.assertRaisesRegex(ValueError, 'manifest_invalid'):
            load_candidate_execution_bundle(self.candidate_directory,
                                            self.execution_sha256)

    def test_single_artifact_mutation_is_rejected(self):
        artifact = self.bundle_directory / 'form-body.bin'
        artifact.write_bytes(b'message=changed')
        artifact.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'file_changed'):
            load_candidate_execution_bundle(self.candidate_directory,
                                            self.execution_sha256)

    def test_symlinked_execution_directory_is_rejected(self):
        moved = self.bundle_directory.with_name('moved-bundle')
        self.bundle_directory.rename(moved)
        self.bundle_directory.symlink_to(moved, target_is_directory=True)

        with self.assertRaises(OSError):
            load_candidate_execution_bundle(self.candidate_directory,
                                            self.execution_sha256)

    def test_symlinked_intermediate_bundle_directory_is_rejected(self):
        moved = self.candidate_directory.with_name('moved-bundle-root')
        self.candidate_directory.rename(moved)
        self.candidate_directory.symlink_to(moved, target_is_directory=True)

        with self.assertRaises(OSError):
            load_candidate_execution_bundle(self.candidate_directory,
                                            self.execution_sha256)

    def test_completion_pins_are_checked_and_duplicate_write_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'completion_binding_changed'):
            persist_candidate_execution_completion(
                self.bundle_directory, self.execution_sha256,
                'f' * 64, self.invocation_sha256, 'job-test', 'run-execution')
        self.assertFalse((self.bundle_directory / 'completion.json').exists())

        persist_candidate_execution_completion(
            self.bundle_directory, self.execution_sha256, self.candidate_sha256,
            self.invocation_sha256, 'job-test', 'run-execution')
        loaded, _ = load_candidate_execution_bundle(self.candidate_directory,
                                                     self.execution_sha256)
        self.assertEqual(loaded['completion']['run_id'], 'run-execution')
        with self.assertRaises(FileExistsError):
            persist_candidate_execution_completion(
                self.bundle_directory, self.execution_sha256,
                self.candidate_sha256, self.invocation_sha256,
                'job-test', 'run-execution')

    def test_private_reader_detects_pathname_replacement_after_open(self):
        path = self.root / 'private.json'
        path.write_bytes(b'original')
        path.chmod(0o600)
        directory_fd = open_existing_workspace(self.root)
        original_read = os.read
        replaced = False

        def replace_after_read(descriptor, count):
            nonlocal replaced
            content = original_read(descriptor, count)
            if not replaced:
                replaced = True
                os.unlink(path.name, dir_fd=directory_fd)
                replacement_fd = os.open(
                    path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=directory_fd)
                try:
                    os.write(replacement_fd, b'replaced')
                finally:
                    os.close(replacement_fd)
            return content

        try:
            with patch('aos.owned_form_candidate_execution.os.read',
                       side_effect=replace_after_read):
                with self.assertRaisesRegex(ValueError, 'file_changed'):
                    _read_private_child(directory_fd, path.name, 64)
        finally:
            os.close(directory_fd)


class OwnedCandidatePersistedAuditTests(unittest.TestCase):
    def test_audit_dispatches_revalidated_invocation_as_json(self):
        from aos.owned_form_candidate_execution import audit_persisted_candidate_execution

        candidate = {'source_run_ref': '1' * 64, 'source_group_sha256': '2' * 64}
        source = {'task': SimpleNamespace(model_dump=lambda mode='json': {}),
                  'source_parameters': {}}
        manifest = {
            'schema_version': '1.0',
            'source_run_id': 'run-source', 'source_invocation_sha256': '3' * 64,
            'source_run_ref': '1' * 64, 'source_group_sha256': '2' * 64,
            'candidate_sha256': '4' * 64,
            'candidate_execution_sha256': '7' * 64,
        }
        invocation_data = {'recipe_sha256': '5' * 64, 'case_key': 'dev-beta'}
        invocation = SimpleNamespace(model_dump=lambda mode='json': invocation_data,
                                     case_key='dev-beta')
        admission = object()
        candidate_session = SimpleNamespace(
            candidate_directory=Path('/private/candidate'),
            directory=Path('/private/source'), profiles=object(), pages=object(),
            manifest_sha256='8' * 64,
            execution_source=Mock(return_value=(candidate, source)))
        loaded = {
            'manifest': manifest, 'validation-plan': {}, 'case-inputs': {},
            'form-plan': {}, 'state-plan': {}, 'field-bindings': [],
            'recipe': {}, 'invocation': invocation_data, 'admission': {},
            'completion': {'run_id': 'run-execution'},
        }
        sentinel = SimpleNamespace(model_dump=lambda mode='json': invocation_data)
        shared_snapshot = (object(), {'sha256': '9' * 64})

        with patch('aos.owned_form_candidate_execution.load_candidate_execution_bundle',
                   return_value=(loaded, '6' * 64)), \
             patch('aos.site_skill_form_recipe_candidate.parse_site_skill_form_recipe_candidate'), \
             patch('aos.site_skill_form_recipe_candidate_execution.CandidateExecutionAdmission') as admission_type, \
             patch('aos.site_skill_form_recipe_candidate_execution.audit_candidate_execution') as audit, \
             patch('aos.site_skill_case_binding.SiteSkillCaseInputs') as inputs_type, \
             patch('aos.site_skill_case_binding.SiteSkillFormFieldBinding') as binding_type, \
             patch('aos.site_skill_form_recipe.SiteSkillFormRecipe') as recipe_type, \
             patch('aos.site_skill_form_recipe.SiteSkillFormRecipeInvocation') as invocation_type, \
             patch('aos.site_skill_validation.SiteSkillValidationPlan') as plan_type, \
             patch('aos.web_https_form_state_probe.WebHTTPSFormStatePlan') as state_type, \
             patch('aos.web_https_form_transport.WebHTTPSFormPlan') as form_type, \
             patch('aos.web_application_binding.WebTaskContract') as task_type, \
             patch('aos.site_skill.SiteSkillStore'):
            admission_type.model_validate_json.return_value = admission
            invocation_type.model_validate_json.return_value = invocation
            audit.return_value = {'status': 'test-report'}
            for model_type in (inputs_type, recipe_type, plan_type, state_type,
                               form_type, task_type):
                model_type.model_validate_json.return_value = sentinel

            result = audit_persisted_candidate_execution(
                Path('/private/bundles'), '7' * 64,
                candidate_session=candidate_session, database=Path('/private/store.sqlite'),
                _audited_snapshot=shared_snapshot)

        self.assertEqual(result, {'status': 'test-report'})
        self.assertIs(audit.call_args.kwargs['invocation'], invocation_data)
        self.assertIs(audit.call_args.kwargs['_audited_snapshot'], shared_snapshot)

    def test_v15_persisted_audit_checks_runtime_admission_in_shared_snapshot(self):
        from aos.owned_form_candidate_execution import audit_persisted_candidate_execution

        candidate = {'source_run_ref': '1' * 64, 'source_group_sha256': '2' * 64}
        source = {'task': SimpleNamespace(model_dump=lambda mode='json': {}),
                  'source_parameters': {}}
        manifest = {
            'schema_version': '1.5', 'source_run_id': 'run-source',
            'source_invocation_sha256': '3' * 64,
            'source_run_ref': '1' * 64, 'source_group_sha256': '2' * 64,
            'candidate_sha256': '4' * 64, 'candidate_execution_sha256': '7' * 64,
            'preview_sha256': '8' * 64, 'adapter_admission_sha256': '9' * 64,
            'reuse_admission_sha256': 'a' * 64, 'invocation_sha256': 'b' * 64,
        }
        completion = {'run_id': 'run-execution', 'job_id': 'job-execution'}
        invocation_data = {'recipe_sha256': '5' * 64, 'case_key': 'dev-beta'}
        invocation = SimpleNamespace(model_dump=lambda mode='json': invocation_data,
                                     case_key='dev-beta')
        candidate_session = SimpleNamespace(
            candidate_directory=Path('/private/candidate'),
            directory=Path('/private/source'), profiles=object(), pages=object(),
            manifest_sha256='c' * 64,
            execution_source=Mock(return_value=(candidate, source)))
        loaded = {
            'manifest': manifest, 'validation-plan': {}, 'case-inputs': {},
            'form-plan': {}, 'state-plan': {}, 'field-bindings': [],
            'recipe': {}, 'invocation': invocation_data, 'admission': {},
            'reuse-admission': {'runtime_id': 'runtime', 'lease_id': 'lease',
                                'generation': 3},
            'adapter-admission': {'authorization_sha256': 'd' * 64},
            'completion': completion,
        }
        sentinel = SimpleNamespace(model_dump=lambda mode='json': invocation_data)
        shared_snapshot = (object(), {'sha256': 'e' * 64})

        with patch('aos.owned_form_candidate_execution.load_candidate_execution_bundle',
                   return_value=(loaded, '6' * 64)), \
             patch('aos.site_skill_form_recipe_candidate.parse_site_skill_form_recipe_candidate'), \
             patch('aos.site_skill_form_recipe_candidate_execution.CandidateExecutionAdmission') as admission_type, \
             patch('aos.site_skill_form_recipe_candidate_execution.audit_candidate_execution') as audit, \
             patch('aos.site_skill_case_binding.SiteSkillCaseInputs') as inputs_type, \
             patch('aos.site_skill_case_binding.SiteSkillFormFieldBinding') as binding_type, \
             patch('aos.site_skill_form_recipe.SiteSkillFormRecipe') as recipe_type, \
             patch('aos.site_skill_form_recipe.SiteSkillFormRecipeInvocation') as invocation_type, \
             patch('aos.site_skill_validation.SiteSkillValidationPlan') as plan_type, \
             patch('aos.web_https_form_state_probe.WebHTTPSFormStatePlan') as state_type, \
             patch('aos.web_https_form_transport.WebHTTPSFormPlan') as form_type, \
             patch('aos.web_application_binding.WebTaskContract') as task_type, \
             patch('aos.site_skill.SiteSkillStore'), \
             patch('aos.owned_adapter_admission.verify_runtime_admission', return_value=True) as verify:
            admission_type.model_validate_json.return_value = object()
            invocation_type.model_validate_json.return_value = invocation
            audit.return_value = {'status': 'test-report'}
            for model_type in (inputs_type, recipe_type, plan_type, state_type,
                               form_type, task_type):
                model_type.model_validate_json.return_value = sentinel

            result = audit_persisted_candidate_execution(
                Path('/private/bundles'), '7' * 64,
                candidate_session=candidate_session,
                database=Path('/private/store.sqlite'), _audited_snapshot=shared_snapshot,
                _allow_reviewed_base_report=True)

        self.assertEqual(result, {'status': 'test-report'})
        self.assertIs(verify.call_args.args[0], shared_snapshot[0])
        self.assertEqual(verify.call_args.args[1]['run_id'], 'run-execution')
        self.assertEqual(verify.call_args.args[2], loaded['adapter-admission'])
        self.assertEqual(verify.call_args.args[3], loaded['reuse-admission'])


class OwnedCandidateReviewedStartTests(unittest.TestCase):
    def test_real_scheduler_returns_reviewed_start_schema_and_exact_hash(self):
        from aos.desktop_tasks import DesktopScheduler

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        invocation = {'case_key': 'dev-gamma', 'skill_sha256': '1' * 64}
        admission_data = {'invocation_sha256': digest(invocation)}
        admission = SimpleNamespace(
            invocation_sha256=digest(invocation), recipe_sha256='2' * 64,
            parameter_variant_sha256='3' * 64,
            model_dump=lambda mode='json': admission_data)
        model_result = SimpleNamespace(model_dump=lambda mode='json': invocation)
        profile_sha256 = '4' * 64
        skill_sha256 = '1' * 64
        candidate = {'source_group_sha256': '5' * 64,
                     'source_fingerprint_sha256': '6' * 64}
        plan = SimpleNamespace(model_dump=lambda mode='json': {'plan': 'value'})
        state_plan = SimpleNamespace(model_dump=lambda mode='json': {'state': 'value'})
        form_plan = SimpleNamespace(
            model_dump=lambda mode='json': {'form': 'value'},
            entry_url='https://form.invalid:34567/entry',
            submit_url='https://form.invalid:34567/submit',
            receipt_url='https://form.invalid:34567/receipt')
        state_plan.state_url = 'https://form.invalid:34567/state'
        session = SimpleNamespace(directory=root, candidate_directory=root / 'candidate',
                                  manifest_sha256='7' * 64)
        source = {'manifest': {'origin': 'https://form.invalid:34567',
                               'certificate_sha256': '8' * 64},
                  'pages': object(), 'task': object(), 'source_parameters': {}}
        prepared = {
            'candidate': candidate, 'session': session, 'source': source,
            'profile_sha256': profile_sha256, 'skill_sha256': skill_sha256,
            'plan': plan, 'inputs': SimpleNamespace(model_dump=lambda mode='json': {}),
            'form_plan': form_plan, 'state_plan': state_plan,
            'field_bindings': [], 'recipe': SimpleNamespace(model_dump=lambda mode='json': {}),
            'invocation': invocation, 'invocation_sha256': digest(invocation),
            'parameter_variant_sha256': admission.parameter_variant_sha256,
            'recipe_sha256': admission.recipe_sha256, 'form_fields': [{'field': 'message'}],
            'form_body': b'message=gamma', 'state_after': b'<p>gamma</p>',
            'steps': [{'step_key': 'save-record', 'operation': 'submit_form'}],
        }
        review_sha256 = '9' * 64
        preview_sha256 = 'a' * 64
        checked = {'preview_sha256': preview_sha256}
        fixture = SimpleNamespace(
            target=SimpleNamespace(tls_context=object(), assert_plan=Mock(),
                                  assert_state_plan=Mock()), close=Mock())
        old_names = (
            'remote_form_plan', 'remote_form_field_name', 'remote_form_value',
            'remote_form_fields', 'remote_form_tls_context',
            'remote_form_public_plan_sha256', 'remote_form_state_plan',
            'remote_form_public_state_plan_sha256', 'remote_form_cookie',
            'remote_form_cookie_sha256', 'remote_form_skill_invocation',
            'remote_form_skill_invocation_sha256', 'remote_form_skill_revalidator',
            'remote_form_candidate_admission', 'remote_form_owned_target',
            'remote_form_owned_fixture')
        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler.web_goal_planning = None
        scheduler.owned_skill_planning = None
        scheduler.task = None
        scheduler.sequences = SimpleNamespace(reserved=False)
        scheduler.job_id = 'job-source'
        scheduler._owned_form_lifecycle = 'audited'
        scheduler.prepare_owned_form_candidate_execution = Mock(return_value=prepared)
        scheduler._assert_candidate_review_for_execution = Mock()
        scheduler.controller = SimpleNamespace(state=lambda: {
            'owner': 'AGENT', 'status': 'running', 'lease_id': 'lease-exact',
            'generation': 4})
        scheduler.closed = False
        scheduler.remote_entry_profiles = object()
        scheduler.settings = SimpleNamespace(database=root / 'store.sqlite')
        scheduler.store = SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_args: SimpleNamespace(fetchone=lambda: None)))
        scheduler._owned_candidate_execution_consumed = set()
        scheduler._owned_selected_candidate_execution_consumed = set()
        scheduler._owned_candidate_execution_session_starts = 0
        scheduler._owned_selected_candidate_execution_session_starts = 0
        scheduler._owned_candidate_execution_history = {}
        scheduler._owned_candidate_execution_restore = None
        scheduler._owned_candidate_execution = None
        scheduler._active_owned_candidate_execution = None
        scheduler._owned_skill_reuse = None
        scheduler._owned_skill_reuse_admission_sha256 = None
        for name in old_names:
            setattr(scheduler, name, None)
        scheduler._start = Mock(return_value={'job_id': 'job-reviewed'})

        with patch.object(DesktopScheduler,
                          '_owned_candidate_execution_preview_payload',
                          return_value=checked), \
             patch.object(DesktopScheduler,
                          '_restore_owned_candidate_replay_inventory'), \
             patch('aos.site_skill_form_recipe_candidate.parse_site_skill_form_recipe_candidate',
                   return_value=SimpleNamespace(skill=object())), \
             patch('aos.owned_form_candidate_execution.candidate_execution_skill_store_path',
                   return_value=root / 'private-store'), \
             patch('aos.site_skill.SiteSkillStore') as skill_store_type, \
             patch('aos.site_skill_form_recipe.compile_site_skill_form_recipe_invocation',
                   return_value=invocation), \
             patch('aos.site_skill_form_recipe_candidate_execution.prepare_candidate_execution',
                   return_value=admission), \
             patch('aos.site_skill_form_recipe_candidate_execution.revalidate_candidate_execution',
                   return_value=model_result), \
             patch('aos.owned_form_candidate_execution.persist_candidate_execution_bundle',
                   return_value=(root / 'bundle', 'b' * 64)), \
             patch('aos.owned_form_candidate_execution.load_candidate_execution_bundle',
                   return_value=({'manifest': {
                       'candidate_sha256': 'c' * 64,
                       'invocation_sha256': admission.invocation_sha256,
                       'admission_sha256': digest(admission_data),
                       'review_sha256': review_sha256}}, 'b' * 64)), \
             patch('aos.owned_form_candidate_execution.bind_exact_loopback_listener',
                   return_value=os.open(os.devnull, os.O_RDONLY)), \
             patch('aos.owned_form_fixture.OwnedFormFixture', return_value=fixture), \
             patch('aos.owned_form_candidate_execution.candidate_operator_field_args',
                   return_value=('message', 'gamma')):
            skill_store_type.return_value.register.return_value = skill_sha256
            result = scheduler.start_owned_form_candidate_execution(
                candidate_sha256='c' * 64, source_run_ref='d' * 64,
                invocation_sha256='e' * 64, case_key='dev-gamma',
                development_value='gamma', preview_sha256=preview_sha256,
                confirm_sha256=preview_sha256, lease_id='lease-exact',
                generation=4, review_sha256=review_sha256)

        self.assertEqual(result['schema_version'], '1.1')
        self.assertEqual(result['review_sha256'], review_sha256)
        self.assertEqual(result['lifecycle'], 'running')
        self.assertEqual(result['job_id'], 'job-reviewed')


class OwnedCandidatePendingApprovalTests(unittest.TestCase):
    def test_exact_pending_candidate_approval_fails_closed_on_binding_change(self):
        import asyncio
        import time

        from aos.contracts import AOSFault, ErrorCode
        from aos.desktop_tasks import Approval, DesktopScheduler

        job_id = 'job-candidate'
        approval_id = 'approval-candidate'
        action_data = {'action_id': 'action-candidate'}
        row = {'approval_id': approval_id, 'job_id': job_id, 'status': 'pending',
               'action_sha256': digest(action_data), 'expires_at': time.time() + 30,
               'envelope_json': '{}'}

        class Result:
            rowcount = 1

        class Connection:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, parameters=()):
                self.statements.append((sql, parameters))
                if sql.startswith('SELECT * FROM desktop_approvals'):
                    return SimpleNamespace(fetchone=lambda: row)
                if sql.startswith("UPDATE desktop_approvals SET status='revoked'"):
                    row['status'] = 'revoked'
                    return Result()
                raise AssertionError('unexpected database operation')

        async def exercise():
            scheduler = DesktopScheduler.__new__(DesktopScheduler)
            scheduler.task = asyncio.get_running_loop().create_future()
            scheduler.job_id = job_id
            scheduler.answer = asyncio.get_running_loop().create_future()
            scheduler.store = SimpleNamespace(connection=Connection())
            scheduler._active_owned_candidate_execution = {
                'job_id': job_id, 'lifecycle': 'running'}
            scheduler.remote_form_owned_target = None
            scheduler.check_lease = lambda _job: None
            scheduler.check_remote_entry_binding = lambda _job: None
            scheduler.check_remote_routes_binding = lambda _job: None
            scheduler.check_remote_static_assets_binding = lambda _job: None
            scheduler.check_remote_form_binding = lambda _job: (_ for _ in ()).throw(
                AOSFault(ErrorCode.UNSAFE_ACTION, 'binding changed'))
            action = SimpleNamespace(model_dump=lambda mode='json': action_data)
            approval = SimpleNamespace(
                approval_id=approval_id, job_id=job_id, action=action,
                action_sha256=digest(action_data))

            with patch.object(Approval, 'model_validate_json', return_value=approval):
                with self.assertRaises(AOSFault):
                    scheduler._respond(approval_id, '0' * 64, True)
                self.assertEqual(row['status'], 'pending')
                self.assertFalse(scheduler.answer.done())

                with self.assertRaises(AOSFault):
                    scheduler._respond(approval_id, digest(action_data), True)
                self.assertEqual(row['status'], 'revoked')
                self.assertTrue(scheduler.answer.done())
                self.assertIsInstance(scheduler.answer.exception(), AOSFault)
            scheduler.task.cancel()

        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()

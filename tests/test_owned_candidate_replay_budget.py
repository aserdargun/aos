import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aos.contracts import digest
from aos.desktop_tasks import DesktopScheduler
from aos.owned_form_candidate_execution import (
    load_candidate_execution_bundle,
    persist_candidate_execution_bundle,
)


class DumpValue:
    def __init__(self, value):
        self.value = value

    def model_dump(self, mode='json'):
        return self.value


class OwnedCandidateReplayBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.scheduler = self._scheduler()

    def tearDown(self):
        self.temporary.cleanup()

    def _scheduler(self, *, source_session_id='session-current'):
        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler._owned_candidate_execution_history = {}
        scheduler._owned_candidate_execution_consumed = set()
        scheduler._owned_selected_candidate_execution_consumed = set()
        scheduler._owned_candidate_execution_session_starts = 0
        scheduler._owned_selected_candidate_execution_session_starts = 0
        scheduler._owned_skill_reuse = None
        scheduler._owned_form_run_id = 'run-source'
        scheduler._owned_form_source_job_id = 'job-source'
        scheduler._owned_skill_reuse = None
        scheduler.controller = SimpleNamespace(session_id='session-current')
        row = {'job_id': 'job-source', 'session_id': source_session_id,
               'kind': 'browser_remote_form', 'status': 'succeeded',
               'run_id': 'run-source'}
        scheduler.store = SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_args: SimpleNamespace(fetchone=lambda: row)))
        return scheduler

    def _bundle(self, *, persist=True):
        source_run_id = 'run-source'
        source_run_ref = digest({'run_id': source_run_id})
        source_invocation_sha256 = '1' * 64
        source_manifest_sha256 = '2' * 64
        skill_sha256 = '3' * 64
        form_body = b'message=beta'
        state_after = b'<html><h1 id="outcome">beta</h1></html>'
        plan = {'schema_version': '1.0', 'cases': []}
        inputs = {'schema_version': '1.0', 'plan_sha256': digest(plan), 'cases': []}
        form_plan = {'body_sha256': hashlib.sha256(form_body).hexdigest(),
                     'body_bytes': len(form_body)}
        state_plan = {'expected_after_sha256': hashlib.sha256(state_after).hexdigest()}
        recipe = {'schema_version': '1.0', 'skill_sha256': skill_sha256}
        candidate = {
            'source_run_ref': source_run_ref,
            'source_group_sha256': '4' * 64,
            'profile_sha256': '5' * 64,
            'source_context': {
                'manifest_sha256': source_manifest_sha256,
                'invocation_sha256': source_invocation_sha256,
            },
            'recipe': recipe,
        }
        candidate_sha256 = digest(candidate)
        invocation = {
            'recipe_sha256': digest(recipe), 'skill_sha256': skill_sha256,
            'form_plan_sha256': digest(form_plan),
            'state_plan_sha256': digest(state_plan),
            'skill_plan_sha256': digest(plan),
            'case_inputs_sha256': digest(inputs), 'case_key': 'dev-beta',
            'steps': [{'step_key': 'open-entry', 'operation': 'open_entry'}],
        }
        invocation_sha256 = digest(invocation)
        admission = {
            'candidate_sha256': candidate_sha256,
            'invocation_sha256': invocation_sha256,
            'parameter_variant_sha256': '6' * 64,
            'skill_sha256': skill_sha256,
        }
        preview = {
            'candidate_sha256': candidate_sha256,
            'source_run_ref': source_run_ref,
            'source_invocation_sha256': source_invocation_sha256,
            'source_group_sha256': candidate['source_group_sha256'],
            'profile_sha256': candidate['profile_sha256'],
            'skill_sha256': skill_sha256, 'case_key': invocation['case_key'],
            'parameter_variant_sha256': admission['parameter_variant_sha256'],
            'invocation_sha256': invocation_sha256,
            'recipe_sha256': digest(recipe),
            'steps': [{'step_key': item['step_key'],
                       'operation': item['operation']}
                      for item in invocation['steps']],
            'purpose': 'development_variation', 'independent_held_out': False,
            'schema_version': '1.0', 'available': True, 'status': 'preview',
            'form_plan_sha256': digest(form_plan),
            'state_plan_sha256': digest(state_plan), 'report': None,
        }
        preview_sha256 = digest(preview)
        execution_sha256 = digest({
            'preview_sha256': preview_sha256,
            'admission_sha256': digest(admission),
            'source_run_ref': source_run_ref,
        })
        prepared = {
            'preview_sha256': preview_sha256, 'candidate': candidate,
            'source_run_id': source_run_id, 'source_run_ref': source_run_ref,
            'source_invocation_sha256': source_invocation_sha256,
            'source_group_sha256': candidate['source_group_sha256'],
            'parameter_variant_sha256': admission['parameter_variant_sha256'],
            'invocation_sha256': invocation_sha256,
            'recipe_sha256': digest(recipe), 'invocation': invocation,
            'plan': DumpValue(plan), 'inputs': DumpValue(inputs),
            'form_plan': DumpValue(form_plan), 'state_plan': DumpValue(state_plan),
            'field_bindings': [], 'recipe': DumpValue(recipe),
            'form_body': form_body, 'state_after': state_after,
        }
        directory = None
        if persist:
            directory, _manifest_sha256 = persist_candidate_execution_bundle(
                self.root / 'candidate-execution-bundles', candidate_sha256,
                execution_sha256, prepared, DumpValue(admission))
        form_model = DumpValue(form_plan)
        form_model.entry_url = 'https://form.invalid:34567/entry'
        form_model.submit_url = 'https://form.invalid:34567/submit'
        form_model.receipt_url = 'https://form.invalid:34567/receipt'
        state_model = DumpValue(state_plan)
        state_model.state_url = 'https://form.invalid:34567/state'
        prepared.update({
            'profile_sha256': candidate['profile_sha256'],
            'skill_sha256': skill_sha256, 'plan': DumpValue(plan),
            'inputs': DumpValue(inputs), 'form_plan': form_model,
            'state_plan': state_model, 'field_bindings': [],
            'recipe': DumpValue(recipe), 'form_fields': (('message', 'beta'),),
            'source': {'manifest': {
                'origin': 'https://form.invalid:34567',
                'certificate_sha256': '7' * 64},
                'pages': object(), 'task': object(), 'source_parameters': {}},
            'form_fields': (('message', 'beta'),),
            'steps': [{'step_key': 'open-entry', 'operation': 'open_entry'}],
        })
        session = SimpleNamespace(directory=self.root,
                                  candidate_directory=self.root / 'candidates',
                                  manifest_sha256=source_manifest_sha256)
        prepared['session'] = session
        return {
            'session': session,
            'source_run_ref': source_run_ref,
            'source_invocation_sha256': source_invocation_sha256,
            'candidate_sha256': candidate_sha256,
            'preview_sha256': preview_sha256,
            'execution_sha256': execution_sha256,
            'prepared': prepared, 'admission': admission,
            'invocation': invocation, 'candidate': candidate,
            'skill_sha256': skill_sha256,
            'directory': directory,
        }

    def test_incomplete_persisted_bundle_restores_replay_without_spending_new_quota(self):
        bundle = self._bundle()
        persisted, _ = load_candidate_execution_bundle(
            self.root / 'candidate-execution-bundles', bundle['execution_sha256'])
        self.assertNotIn('completion', persisted)
        prepared = {'session': bundle['session']}
        self.scheduler._restore_owned_candidate_replay_inventory(
            prepared, bundle['source_run_ref'], bundle['source_invocation_sha256'])

        self.assertIn(bundle['preview_sha256'],
                      self.scheduler._owned_candidate_execution_consumed)
        self.assertEqual(self.scheduler._owned_candidate_execution_session_starts, 0)
        self.assertEqual(self.scheduler._owned_selected_candidate_execution_session_starts, 0)
        with self.assertRaisesRegex(ValueError, 'confirmation_invalid_or_consumed'):
            self.scheduler._assert_owned_candidate_execution_start_available(
                bundle['preview_sha256'], selected_lane=True)
        fresh_preview = 'a' * 64
        self.scheduler._assert_owned_candidate_execution_start_available(
            fresh_preview, selected_lane=False)

    def test_current_source_allows_only_pristine_missing_inventory(self):
        prepared = {'session': SimpleNamespace(
            directory=self.root, manifest_sha256='2' * 64)}
        with patch('aos.owned_form_candidate_execution.load_candidate_execution_replay_inventory',
                   return_value={'direct': frozenset(), 'selected': frozenset()}) as loader:
            self.scheduler._restore_owned_candidate_replay_inventory(
                prepared, '3' * 64, '4' * 64)
        self.assertIs(loader.call_args.kwargs['allow_missing'], True)

        self.scheduler._owned_candidate_execution_history['old-run'] = {}
        with patch('aos.owned_form_candidate_execution.load_candidate_execution_replay_inventory',
                   side_effect=ValueError('owned_candidate_replay_inventory_missing')) as loader:
            with self.assertRaisesRegex(ValueError, 'inventory_missing'):
                self.scheduler._restore_owned_candidate_replay_inventory(
                    prepared, '3' * 64, '4' * 64)
        self.assertIs(loader.call_args.kwargs['allow_missing'], False)

    def test_historical_source_cannot_treat_missing_inventory_as_fresh(self):
        scheduler = self._scheduler(source_session_id='session-previous')
        prepared = {'session': SimpleNamespace(
            directory=self.root, manifest_sha256='2' * 64)}
        with patch('aos.owned_form_candidate_execution.load_candidate_execution_replay_inventory',
                   side_effect=ValueError('owned_candidate_replay_inventory_missing')) as loader:
            with self.assertRaisesRegex(ValueError, 'inventory_missing'):
                scheduler._restore_owned_candidate_replay_inventory(
                    prepared, '3' * 64, '4' * 64)
        self.assertIs(loader.call_args.kwargs['allow_missing'], False)

    def test_direct_and_selected_quotas_are_separate_from_replay_set_size(self):
        for index in range(32):
            self.scheduler._owned_candidate_execution_consumed.add(
                f'{index:064x}')
        self.assertEqual(self.scheduler._owned_candidate_execution_session_starts, 0)
        self.assertEqual(self.scheduler._owned_selected_candidate_execution_session_starts, 0)
        for index in range(4):
            self.scheduler._consume_owned_candidate_execution_start(
                f'{100 + index:064x}', selected_lane=False)
        with self.assertRaisesRegex(ValueError, 'confirmation_invalid_or_consumed'):
            self.scheduler._consume_owned_candidate_execution_start(
                f'{104:064x}', selected_lane=False)
        for index in range(4):
            self.scheduler._consume_owned_candidate_execution_start(
                f'{200 + index:064x}', selected_lane=True)
        with self.assertRaisesRegex(ValueError, 'confirmation_invalid_or_consumed'):
            self.scheduler._consume_owned_candidate_execution_start(
                f'{204:064x}', selected_lane=True)
        self.assertEqual(self.scheduler._owned_candidate_execution_session_starts, 4)
        self.assertEqual(self.scheduler._owned_selected_candidate_execution_session_starts, 4)

    def test_failed_job_start_burns_the_direct_preview_at_start_boundary(self):
        preview_sha256 = 'b' * 64

        def fail_start():
            raise RuntimeError('start failed')

        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            self.scheduler._start_owned_candidate_execution_job(
                preview_sha256, False, fail_start)
        self.assertEqual(self.scheduler._owned_candidate_execution_session_starts, 1)
        self.assertIn(preview_sha256,
                      self.scheduler._owned_candidate_execution_consumed)

    def test_failed_selected_job_start_burns_its_selected_preview(self):
        preview_sha256 = 'd' * 64

        with self.assertRaisesRegex(RuntimeError, 'selected start failed'):
            self.scheduler._start_owned_candidate_execution_job(
                preview_sha256, True,
                lambda: (_ for _ in ()).throw(RuntimeError('selected start failed')))
        self.assertEqual(self.scheduler._owned_selected_candidate_execution_session_starts, 1)
        self.assertIn(preview_sha256,
                      self.scheduler._owned_selected_candidate_execution_consumed)

    def test_bundle_persisted_before_fixture_bind_failure_burns_replay(self):
        from aos.desktop_tasks import DesktopScheduler

        bundle = self._bundle(persist=False)
        prepared = bundle['prepared']
        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler.web_goal_planning = None
        scheduler.owned_skill_planning = None
        scheduler.task = None
        scheduler.sequences = SimpleNamespace(reserved=False)
        scheduler.job_id = 'job-source'
        scheduler._owned_form_lifecycle = 'audited'
        scheduler._owned_form_run_id = 'run-source'
        scheduler._owned_form_source_job_id = 'job-source'
        scheduler._owned_skill_reuse = None
        scheduler.controller = SimpleNamespace(
            session_id='session-current',
            state=lambda: {'owner': 'AGENT', 'status': 'running',
                           'lease_id': 'lease-current', 'generation': 3})
        row = {'job_id': 'job-source', 'session_id': 'session-current',
               'kind': 'browser_remote_form', 'status': 'succeeded',
               'run_id': 'run-source'}
        scheduler.store = SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_args: SimpleNamespace(fetchone=lambda: row)))
        scheduler.closed = False
        scheduler.remote_entry_profiles = object()
        scheduler.settings = SimpleNamespace(database=self.root / 'store.sqlite')
        scheduler._owned_candidate_execution_history = {}
        scheduler._owned_candidate_execution_consumed = set()
        scheduler._owned_selected_candidate_execution_consumed = set()
        scheduler._owned_candidate_execution_session_starts = 0
        scheduler._owned_selected_candidate_execution_session_starts = 0
        scheduler._owned_candidate_execution = None
        scheduler._active_owned_candidate_execution = None
        scheduler.prepare_owned_form_candidate_execution = lambda *_args: prepared
        scheduler._owned_candidate_execution_preview_payload = lambda *_args, **_kwargs: {
            'preview_sha256': bundle['preview_sha256']}
        scheduler._start = lambda *_args: self.fail('fixture bind failure must precede job start')
        for name in (
                'remote_form_plan', 'remote_form_field_name', 'remote_form_value',
                'remote_form_fields', 'remote_form_tls_context',
                'remote_form_public_plan_sha256', 'remote_form_state_plan',
                'remote_form_public_state_plan_sha256', 'remote_form_cookie',
                'remote_form_cookie_sha256', 'remote_form_skill_invocation',
                'remote_form_skill_invocation_sha256', 'remote_form_skill_revalidator',
                'remote_form_candidate_admission', 'remote_form_owned_target',
                'remote_form_owned_fixture'):
            setattr(scheduler, name, None)

        admission_data = bundle['admission']
        invocation = bundle['invocation']
        skill_sha256 = bundle['skill_sha256']
        preview = {'preview_sha256': bundle['preview_sha256']}
        bind = patch('aos.owned_form_candidate_execution.bind_exact_loopback_listener',
                     side_effect=ValueError('port unavailable'))
        with bind as bind_mock, \
             patch('aos.site_skill_form_recipe_candidate.parse_site_skill_form_recipe_candidate',
                   return_value=SimpleNamespace(skill=object())), \
             patch('aos.owned_form_candidate_execution.candidate_execution_skill_store_path',
                   return_value=self.root / 'private-skill-store'), \
             patch('aos.site_skill.SiteSkillStore') as store_type, \
             patch('aos.site_skill_form_recipe.compile_site_skill_form_recipe_invocation',
                   return_value=invocation), \
             patch('aos.site_skill_form_recipe_candidate_execution.prepare_candidate_execution',
                   return_value=SimpleNamespace(
                       invocation_sha256=digest(invocation),
                       recipe_sha256=bundle['prepared']['recipe_sha256'],
                       parameter_variant_sha256=bundle['prepared']['parameter_variant_sha256'],
                       model_dump=lambda mode='json': admission_data)), \
             patch('aos.site_skill_form_recipe_candidate_execution.revalidate_candidate_execution',
                   return_value=SimpleNamespace(model_dump=lambda mode='json': invocation)), \
             patch('aos.owned_form_candidate_execution.candidate_operator_field_args',
                   return_value=('message', 'beta')):
            store_type.return_value.register.return_value = skill_sha256
            kwargs = {
                'candidate_sha256': bundle['candidate_sha256'],
                'source_run_ref': bundle['source_run_ref'],
                'invocation_sha256': bundle['source_invocation_sha256'],
                'case_key': 'dev-beta', 'development_value': 'beta',
                'preview_sha256': preview['preview_sha256'],
                'confirm_sha256': preview['preview_sha256'],
                'lease_id': 'lease-current', 'generation': 3,
            }
            with self.assertRaisesRegex(ValueError, 'fixture_unavailable'):
                scheduler.start_owned_form_candidate_execution(**kwargs)

            saved, _ = load_candidate_execution_bundle(
                self.root / 'candidate-execution-bundles', bundle['execution_sha256'])
            self.assertNotIn('completion', saved)
            self.assertEqual(scheduler._owned_candidate_execution_session_starts, 0)
            with self.assertRaisesRegex(ValueError, 'confirmation_invalid_or_consumed'):
                scheduler.start_owned_form_candidate_execution(**kwargs)

        bind_mock.assert_called_once()
        self.assertEqual(scheduler._owned_candidate_execution_session_starts, 0)

    def test_replay_cannot_switch_between_direct_and_selected_lanes(self):
        preview_sha256 = 'c' * 64
        self.scheduler._consume_owned_candidate_execution_start(
            preview_sha256, selected_lane=False)
        with self.assertRaisesRegex(ValueError, 'confirmation_invalid_or_consumed'):
            self.scheduler._consume_owned_candidate_execution_start(
                preview_sha256, selected_lane=True)


if __name__ == '__main__':
    unittest.main()

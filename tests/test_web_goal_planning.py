import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from unittest.mock import Mock
from types import SimpleNamespace
import sqlite3

from aos.contracts import digest
from aos.web_goal_planner import WebGoalCatalog
from aos.web_goal_planning import WebGoalPlanning
from aos.desktop_tasks import DesktopScheduler
from aos.owned_skill_planning import planning_case_key
from aos.site_skill_case_binding import parameter_variant_sha256
from test_web_goal_planner import fixture_planner, fixtures, response_for


class WebGoalPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_catalogs_free_goal_private_intent_proposal_and_fresh_selection(self):
        example = fixtures()
        for case in (example['cases'][0], example['cases'][2]):
            with self.subTest(catalog=case['catalog']), tempfile.TemporaryDirectory() as directory:
                catalog = WebGoalCatalog.model_validate(example['catalogs'][case['catalog']])
                authority = {'runtime_id': 'synthetic-runtime', 'lease_id': 'synthetic-lease', 'generation': 2,
                             'desktop_session_id': 'synthetic-session'}
                planner = fixture_planner()
                service = WebGoalPlanning(planner, Path(directory) / 'proposals',
                    lambda lease, generation: (deepcopy(authority), catalog), AsyncMock())

                async def model(instance, goal, evidence):
                    instance.before_model_call()
                    return instance.parse_response(response_content, evidence)

                from aos.contracts import canonical
                response_content = canonical(response_for(case, catalog))
                with patch('aos.web_goal_planner.BonsaiSupervisor.plan', model):
                    result = service.begin(case['goal'], 'synthetic-lease', 2, inference_consent=True,
                                           confirm_catalog_sha256=digest(catalog.model_dump(mode='json')))
                    self.assertEqual(result['status'], 'pending')
                    await service.task
                self.assertEqual(service.status()['status'], 'ready')
                self.assertFalse(service.status()['real_model'])
                checksum = service.status()['bundle_sha256']
                bundle = service.load(checksum)
                self.assertEqual(service.load(bundle['intent_sha256'])['stage'], 'intent')
                self.assertEqual(service.select(checksum, 'synthetic-lease', 2).parameters, case['parameters'])
                authority['generation'] = 3
                with self.assertRaises(ValueError):
                    service.select(checksum, 'synthetic-lease', 2)
                authority['generation'] = 2
                review = service.record_execution_review(checksum, 'a' * 64, 'synthetic-lease', 2)
                self.assertTrue(service.unresolved_execution_review())
                self.assertTrue(service.reserved)
                self.assertTrue(service.status()['execution_review_pending'])
                with self.assertRaises(RuntimeError):
                    with service.admit_reviewed_start(review):
                        self.assertFalse(service.reserved)
                        with self.assertRaises(ValueError):
                            with service.admit_reviewed_start(review):
                                self.fail('nested admission must fail')
                        raise RuntimeError('synthetic start acknowledgement failure')
                self.assertTrue(service.reserved)
                with self.assertRaises(ValueError):
                    with service.admit_reviewed_start(review | {'preview_sha256': 'b' * 64}):
                        self.fail('altered review must fail')
                with self.assertRaises(ValueError):
                    service.begin(case['goal'], 'synthetic-lease', 2, inference_consent=True,
                                  confirm_catalog_sha256=digest(catalog.model_dump(mode='json')))
                scheduler = DesktopScheduler.__new__(DesktopScheduler)
                scheduler.web_goal_planning = service
                scheduler.closed = False
                scheduler.restart_quiesced = False
                scheduler.controller = SimpleNamespace(session_id='synthetic-session', state=lambda: {
                    **authority, 'owner': 'AGENT', 'status': 'running'})
                scheduler._owned_candidate_execution_history = {'synthetic-execution': {
                    'preview_sha256': 'a' * 64, 'job_id': 'synthetic-job'}}
                scheduler._web_goal_start_attempts = {checksum: {
                    'review_sha256': digest(review), 'previous_executions': frozenset({'synthetic-execution'})}}
                with sqlite3.connect(':memory:') as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute('CREATE TABLE desktop_tasks(job_id,session_id,kind)')
                    connection.execute("INSERT INTO desktop_tasks VALUES('synthetic-job','synthetic-session','browser_remote_form')")
                    scheduler.store = SimpleNamespace(connection=connection)
                    with self.assertRaises(ValueError):
                        scheduler.recover_web_goal_start(checksum, 'synthetic-job', 'synthetic-job', 'synthetic-lease', 2)
                    scheduler._web_goal_start_attempts[checksum]['previous_executions'] = frozenset()
                    with self.assertRaises(ValueError):
                        scheduler.recover_web_goal_start(checksum, 'foreign-job', 'foreign-job', 'synthetic-lease', 2)
                    self.assertTrue(service.reserved)
                    authority['generation'] = 3
                    with self.assertRaises(ValueError):
                        scheduler.recover_web_goal_start(checksum, 'synthetic-job', 'synthetic-job', 'synthetic-lease', 3)
                    authority['generation'] = 2
                    recovered = scheduler.recover_web_goal_start(
                        checksum, 'synthetic-job', 'synthetic-job', 'synthetic-lease', 2)
                    self.assertTrue(recovered['acknowledgement_recovered'])
                    self.assertFalse(recovered['execution_authorized'])
                    with self.assertRaises(ValueError):
                        scheduler.recover_web_goal_start(checksum, 'synthetic-job', 'synthetic-job', 'synthetic-lease', 2)
                self.assertEqual(service.execution_receipt(checksum)['job_id'], 'synthetic-job')
                self.assertFalse(service.unresolved_execution_review())
                self.assertFalse(service.reserved)
                self.assertFalse(service.status()['execution_review_pending'])
                self.assertEqual(service.status()['status'], 'consumed')
                await service.close()

    async def test_consent_confirmation_and_cancellation_do_not_leave_ready_proposal(self):
        example = fixtures()
        catalog = WebGoalCatalog.model_validate(example['catalogs']['contact_notes'])
        with tempfile.TemporaryDirectory() as directory:
            wait = asyncio.Event()
            service = WebGoalPlanning(fixture_planner(), Path(directory) / 'proposals',
                lambda lease, generation: ({'runtime_id': 'synthetic-runtime'}, catalog), wait.wait)
            confirmation = digest(catalog.model_dump(mode='json'))
            with self.assertRaises(ValueError):
                service.begin('Add a note', 'synthetic-lease', 2, confirm_catalog_sha256=confirmation)
            with self.assertRaises(ValueError):
                service.begin('Add a note', 'synthetic-lease', 2, inference_consent=True,
                              confirm_catalog_sha256='0' * 64)
            self.assertFalse((Path(directory) / 'proposals').exists())
            service.begin('Add a note', 'synthetic-lease', 2, inference_consent=True,
                          confirm_catalog_sha256=confirmation)
            await asyncio.sleep(0)
            await service.cancel()
            self.assertEqual(service.status()['status'], 'cancelled')
            self.assertFalse(service.status()['model_called'])
            self.assertFalse(service.reserved)


class WebGoalExecutionReportTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('CREATE TABLE desktop_tasks(job_id,session_id,status,kind)')
        self.connection.execute("INSERT INTO desktop_tasks VALUES('synthetic-job','synthetic-session','succeeded','browser_remote_form')")
        self.scheduler = DesktopScheduler.__new__(DesktopScheduler)
        self.scheduler.controller = SimpleNamespace(session_id='synthetic-session')
        self.scheduler.store = SimpleNamespace(connection=self.connection)
        self.skill = {key: 'a' * 64 for key in ('skill_sha256', 'recipe_sha256', 'release_sha256', 'selection_sha256')}
        self.skill['skill_ref'] = 'save-message'
        parameters = {'message': 'literal.42'}
        self.bundle = {'catalog': {'profile_sha256': 'b' * 64, 'skills': [self.skill]},
                       'model_response': {'skill_ref': 'save-message', 'parameters': parameters}}
        self.scheduler.web_goal_planning = SimpleNamespace(
            execution_receipt=Mock(return_value={'job_id': 'synthetic-job',
                'authority': {'desktop_session_id': 'synthetic-session'}, 'preview_sha256': 'c' * 64}),
            load=Mock(return_value=self.bundle))
        self.scheduler._owned_candidate_execution_history = {'d' * 64: {
            'job_id': 'synthetic-job', 'candidate_execution_sha256': 'd' * 64, 'preview_sha256': 'c' * 64}}
        self.audit = {'available': True, 'status': 'verified', 'job_id': 'synthetic-job',
                      'reuse_admission_verified': True, 'release_admission_verified': True,
                      'review_admission_verified': True, 'profile_sha256': 'b' * 64,
                      'report_sha256': 'e' * 64, **self.skill,
                      'case_key': planning_case_key('Save message "literal.42"'),
                      'parameter_variant_sha256': parameter_variant_sha256(parameters)}
        self.scheduler.audit_owned_form_candidate_execution = Mock(return_value=self.audit)
        self.idle = patch.object(DesktopScheduler, 'reserved', False)
        self.idle.start()
        self.addCleanup(self.idle.stop)

    def test_job_success_requires_independent_audit_and_exact_parameter_binding(self):
        self.audit['available'] = False
        result = self.scheduler.web_goal_execution_report('f' * 64)
        self.assertFalse(result['independently_verified'])
        self.audit['available'] = True
        self.audit['parameter_variant_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.scheduler.web_goal_execution_report('f' * 64)
        self.audit['parameter_variant_sha256'] = parameter_variant_sha256({'message': 'literal.42'})
        result = self.scheduler.web_goal_execution_report('f' * 64)
        self.assertTrue(result['independently_verified'])
        self.assertFalse(result['training_ready'])
        self.assertFalse(result['gpu_release_verified'])

    def test_pending_and_failed_jobs_never_read_or_accept_model_as_success(self):
        for status in ('waiting_approval', 'running', 'failed', 'cancelled'):
            self.connection.execute('UPDATE desktop_tasks SET status=?', (status,))
            result = self.scheduler.web_goal_execution_report('f' * 64)
            self.assertFalse(result['independently_verified'])
        self.scheduler.audit_owned_form_candidate_execution.assert_not_called()

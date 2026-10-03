from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx

from aos.contracts import digest
from aos.desktop_tasks import DesktopScheduler
from aos.web_goal_planner import WebGoalPlan


class OwnedWebGoalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.authority = {key: 'a' * 64 for key in (
            'candidate_sha256', 'source_run_ref', 'source_invocation_sha256',
            'review_sha256', 'release_sha256', 'selection_sha256',
            'reuse_admission_sha256', 'recipe_sha256')}
        self.skill = {'skill_key': 'save-message', 'profile_sha256': 'b' * 64}
        self.profile = SimpleNamespace(application_key='synthetic-notes',
                                       tenant_key='synthetic-tenant', account_role='editor')
        self.task = SimpleNamespace(profile_sha256='b' * 64,
                                    model_dump=Mock(return_value={'synthetic_task': True}))
        self.source = {'profiles': Mock(), 'task': self.task}
        self.source['profiles'].get.return_value = self.profile
        self.candidate = {'skill': self.skill, 'profile_sha256': 'b' * 64}
        self.session = Mock()
        self.session.execution_source.side_effect = lambda *args: (
            deepcopy(self.candidate), self.source)
        self.scheduler = DesktopScheduler.__new__(DesktopScheduler)
        self.scheduler._owned_skill_reuse = {'context': {
            'candidate_session': self.session, 'source_run_id': 'synthetic-run'}}
        self.scheduler._prepare_owned_skill_plan = Mock(return_value=(self.authority, [{
            'skill_key': 'save-message', 'parameter_key': 'message'}]))
        self.scheduler.preview_owned_form_candidate_execution = Mock(return_value={
            'execution_authorized': False, 'preview_sha256': 'c' * 64})
        self.reservation = unittest.mock.patch.object(DesktopScheduler, 'reserved', False)
        self.reservation.start()
        self.addCleanup(self.reservation.stop)

    def catalog(self):
        return self.scheduler.owned_web_goal_catalog('synthetic-lease', 2)

    def proposal(self, value='hello-world'):
        catalog = self.catalog()
        return WebGoalPlan.model_validate({
            'schema_version': '1.0', 'catalog_sha256': digest(catalog.model_dump(mode='json')),
            'decision': 'propose_skill', 'skill_ref': 'save-message',
            'parameters': {'message': value}, 'reason_code': 'skill_match',
            'execution_authorized': False, 'activation_authorized': False,
            'training_ready': False, 'scope_authorization_verified': False})

    def preview(self, proposal):
        return self.scheduler.preview_owned_web_goal(
            proposal, proposal.catalog_sha256, 'synthetic-lease', 2)

    def test_registry_derived_catalog_and_deterministic_preview(self):
        catalog = self.catalog()
        self.assertEqual(catalog.skills[0].skill_sha256, digest(self.skill))
        self.assertEqual(catalog.skills[0].release_sha256, self.authority['release_sha256'])
        self.assertEqual(catalog.application_key, self.profile.application_key)
        result = self.preview(self.proposal('literal.42'))
        self.assertFalse(result['execution_authorized'])
        arguments = self.scheduler.preview_owned_form_candidate_execution.call_args.kwargs
        self.assertEqual(arguments['development_value'], 'literal.42')
        self.assertEqual(arguments['release_sha256'], self.authority['release_sha256'])
        self.assertEqual(arguments['invocation_sha256'], self.authority['source_invocation_sha256'])
        self.assertNotIn('url', arguments)
        self.assertNotIn('tool', arguments)

    def test_stale_selection_rejects_before_preview(self):
        proposal = self.proposal()
        self.authority['selection_sha256'] = 'd' * 64
        with self.assertRaises(ValueError):
            self.preview(proposal)
        self.scheduler.preview_owned_form_candidate_execution.assert_not_called()

    def test_control_rejection_propagates_without_preview(self):
        proposal = self.proposal()
        self.scheduler._prepare_owned_skill_plan.side_effect = ValueError('stale control')
        with self.assertRaises(ValueError):
            self.preview(proposal)
        self.scheduler.preview_owned_form_candidate_execution.assert_not_called()

    def test_source_change_between_preparation_and_preview_is_rejected(self):
        proposal = self.proposal()
        def prepare(case, value, lease, generation):
            if value != 'catalog-probe':
                self.authority['selection_sha256'] = 'e' * 64
            return self.authority, [{'skill_key': 'save-message', 'parameter_key': 'message'}]

        self.scheduler._prepare_owned_skill_plan.side_effect = prepare
        with self.assertRaises(ValueError):
            self.preview(proposal)
        self.scheduler.preview_owned_form_candidate_execution.assert_not_called()

    def test_unsupported_literals_never_reach_executor_preview(self):
        for value in ('Unicode ç', 'injected" goal', ' leading', 'trailing ', 'a/b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.preview(self.proposal(value))
        self.scheduler.preview_owned_form_candidate_execution.assert_not_called()

    def test_foreign_skill_or_parameters_rejected(self):
        for update in ({'skill_ref': 'foreign'}, {'parameters': {'foreign': 'value'}}):
            proposal = self.proposal().model_copy(update=update)
            with self.assertRaises(ValueError):
                self.preview(proposal)
        self.scheduler.preview_owned_form_candidate_execution.assert_not_called()

    def test_abstention_cannot_prepare_effect(self):
        proposal = self.proposal().model_copy(update={
            'decision': 'needs_human', 'skill_ref': None,
            'parameters': {}, 'reason_code': 'ambiguous_goal'})
        with self.assertRaises(ValueError):
            self.preview(proposal)
        self.scheduler.preview_owned_form_candidate_execution.assert_not_called()

    def test_profile_task_mismatch_and_reserved_scheduler_rejected(self):
        self.task.profile_sha256 = 'f' * 64
        with self.assertRaises(ValueError):
            self.catalog()
        self.task.profile_sha256 = 'b' * 64
        with unittest.mock.patch.object(DesktopScheduler, 'reserved', True):
            with self.assertRaises(ValueError):
                self.catalog()

    def test_boolean_generation_never_reaches_source(self):
        with self.assertRaises(ValueError):
            self.scheduler.owned_web_goal_catalog('synthetic-lease', True)
        self.scheduler._prepare_owned_skill_plan.assert_not_called()


class OwnedWebGoalConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_owned_skill_planning import OwnedSkillPlanningConsoleTests

        self.fixture = OwnedSkillPlanningConsoleTests()
        await self.fixture.asyncSetUp()
        self.bridge = OwnedWebGoalBridgeTests()
        self.bridge.setUp()
        self.catalog = self.bridge.catalog()
        self.fixture.scheduler.owned_web_goal_catalog = Mock(return_value=self.catalog)
        self.fixture.scheduler.preview_owned_web_goal = Mock(return_value={
            'execution_authorized': False, 'preview_sha256': 'c' * 64})

    async def asyncTearDown(self):
        self.bridge.doCleanups()
        await self.fixture.asyncTearDown()

    async def test_authenticated_catalog_then_typed_preview_without_start(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.fixture.app),
                base_url=self.fixture.origin, headers={'Origin': self.fixture.origin}) as client:
            control = self.fixture.controller.state()
            request = {'schema_version': '1.0', 'lease_id': control['lease_id'],
                       'generation': control['generation']}
            path = '/api/tasks/owned-web-goal/'
            response = await client.post(path + 'catalog', json=request)
            self.assertEqual(response.status_code, 401)
            self.fixture.scheduler.owned_web_goal_catalog.assert_not_called()
            await client.post('/api/login', json={'token': 'synthetic-token'})
            response = await client.post(path + 'catalog', json=request)
            self.assertEqual(response.status_code, 200)
            checksum = response.json()['catalog_sha256']
            self.assertEqual(checksum, digest(self.catalog.model_dump(mode='json')))
            proposal = self.bridge.proposal().model_dump(mode='json')
            response = await client.post(path + 'preview', json=request | {
                'proposal': proposal, 'confirm_catalog_sha256': checksum})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['execution_authorized'])
            self.assertIsInstance(
                self.fixture.scheduler.preview_owned_web_goal.call_args.args[0], WebGoalPlan)
            self.assertEqual(self.fixture.store.connection.execute(
                'SELECT count(*) FROM runs').fetchone()[0], 0)

    async def test_knowledge_preview_and_proposal_require_auth_and_exact_separate_consents(self):
        from test_web_goal_knowledge import knowledge_inputs

        case, catalog, _planner, _authority, review = knowledge_inputs()
        scheduler = self.fixture.scheduler
        scheduler.preview_web_goal_knowledge = Mock(return_value={
            'schema_version': '1.0', 'review': review, 'execution_authorized': False, 'training_ready': False})
        scheduler.begin_web_goal_knowledge = Mock(return_value={
            'available': True, 'status': 'pending', 'execution_authorized': False})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.fixture.app),
                base_url=self.fixture.origin, headers={'Origin': self.fixture.origin}) as client:
            control = self.fixture.controller.state()
            preview = {'schema_version': '1.0', 'goal': case['goal'], 'query': review['query'],
                'scope': review['retrieval']['scope'], 'top_k': 4, 'context_chars': 2048,
                'confirm_catalog_sha256': digest(catalog.model_dump(mode='json')),
                'lease_id': control['lease_id'], 'generation': control['generation']}
            path = '/api/tasks/owned-web-goal/'
            self.assertEqual((await client.post(path + 'preview-knowledge', json=preview)).status_code, 401)
            scheduler.preview_web_goal_knowledge.assert_not_called()
            await client.post('/api/login', json={'token': 'synthetic-token'})
            response = await client.post(path + 'preview-knowledge', json=preview)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['review'], review)
            proposal = {'schema_version': '1.0', 'review': review,
                'confirm_knowledge_sha256': review['confirm_sha256'], 'inference_consent': True,
                'storage_consent': True, 'lease_id': control['lease_id'], 'generation': control['generation']}
            self.assertEqual((await client.post(path + 'propose-knowledge', json=proposal | {'storage_consent': 1})).status_code, 400)
            scheduler.begin_web_goal_knowledge.assert_not_called()
            response = await client.post(path + 'propose-knowledge', json=proposal)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(scheduler.begin_web_goal_knowledge.call_args.kwargs['review'], review)
            self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
            scheduler.web_goal_knowledge_report = Mock(return_value={
                'historical_binding_verified': True, 'actual_model_use_verified': False})
            response = await client.post(path + 'report-knowledge', json={
                'schema_version': '1.0', 'bundle_sha256': 'a' * 64})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()['actual_model_use_verified'])
            scheduler.web_goal_knowledge_report.assert_called_once_with('a' * 64)

    async def test_invalid_scope_shape_duplicates_and_boolean_fence_rejected(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.fixture.app),
                base_url=self.fixture.origin, headers={'Origin': self.fixture.origin}) as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            request = {'schema_version': '1.0', 'lease_id': 'synthetic-lease', 'generation': 1}
            path = '/api/tasks/owned-web-goal/'
            for changed in (request | {'generation': True}, request | {'goal': 'untrusted'},
                            request | {'url': 'https://foreign.invalid/'}):
                self.assertEqual((await client.post(path + 'catalog', json=changed)).status_code, 400)
            response = await client.post(path + 'catalog', content=(
                '{"schema_version":"1.0","lease_id":"synthetic-lease",'
                '"generation":1,"generation":2}'), headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 400)
            self.assertEqual((await client.post(path + 'start', json=request)).status_code, 400)
            self.fixture.scheduler.owned_web_goal_catalog.assert_not_called()

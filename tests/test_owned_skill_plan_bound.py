import asyncio
import copy
import json
import unittest
import httpx
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT, digest
from aos.desktop_tasks import DesktopScheduler
from aos.owned_skill_planner import BonsaiOwnedSkillPlanner
from aos.owned_skill_planning import validate_planning_bundle
import test_owned_skill_planning as planning_helpers


def synthetic_native_contract_bundle():
    bundle = json.loads((REPO_ROOT / 'examples/owned_skill_planning_bundle.json').read_text())['bundle']
    bundle['real_model'] = True
    pins = bundle['model_pins']
    pins['owned_skill_plan_protocol'] = 'aos-owned-skill-plan-v2'
    bundle['deployment'] = {'real_model': True, 'kind': 'bonsai_native_owned_skill_planner',
                            'deployment_id': 'bonsai-' + digest(pins), 'pins': copy.deepcopy(pins)}
    verifier = BonsaiOwnedSkillPlanner.__new__(BonsaiOwnedSkillPlanner)
    verifier.identity, verifier.pins = bundle['deployment'], pins
    bundle['model_request'] = verifier.request_body(bundle['request']['goal'], bundle['evidence'])
    return bundle


class OwnedSkillPlanHistoricalTests(unittest.TestCase):
    def test_native_labelled_synthetic_contract_validates_without_runtime(self):
        bundle = synthetic_native_contract_bundle()
        with patch('socket.socket', side_effect=AssertionError('network_forbidden')), \
                patch.object(BonsaiOwnedSkillPlanner, '__init__', side_effect=AssertionError('native_construction_forbidden')):
            self.assertEqual(validate_planning_bundle(bundle), bundle)
        for changed in (
            {'real_model': False}, {'request': bundle['request'] | {'generation': 9}},
            {'model_request': {}}, {'model_pins': bundle['model_pins'] | {'max_output_tokens': 999}},
            {'model_response': bundle['model_response'] | {'parameter_value': 'different'}},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_planning_bundle(bundle | changed)


class OwnedSkillPlanBindingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = planning_helpers.OwnedSkillPlanningTests.asyncSetUp
    asyncTearDown = planning_helpers.OwnedSkillPlanningTests.asyncTearDown
    prepare = planning_helpers.OwnedSkillPlanningTests.prepare
    begin = planning_helpers.OwnedSkillPlanningTests.begin
    async def test_binding_and_consumption_require_exact_current_proposal(self):
        self.begin('Save message "synthetic-value"')
        await self.service.task
        bundle = synthetic_native_contract_bundle()
        checksum = digest(bundle)
        self.service.current.update({'bundle_sha256': checksum, 'status': 'ready'})
        self.service.load = Mock(return_value=bundle)
        self.service.prepare = Mock(return_value=(bundle['authority'], bundle['evidence']))
        request = bundle['request']
        selected = self.service.select(checksum, request['lease_id'], request['generation'])
        self.assertEqual(selected, bundle)
        with self.assertRaises(ValueError):
            self.service.select(checksum, 'wrong-lease', request['generation'])
        preview = 'c' * 64
        self.service.bind(checksum, preview)
        with self.assertRaises(ValueError):
            self.begin()
        with self.assertRaises(ValueError):
            self.service.consume(checksum, 'd' * 64)
        self.service.consume(checksum, preview)
        self.assertEqual(self.service.status()['status'], 'consumed')
        with self.assertRaises(ValueError):
            self.service.select(checksum, request['lease_id'], request['generation'])

    async def test_bound_proposal_cancel_requires_no_new_model_or_action(self):
        self.begin()
        await self.service.task
        checksum = self.service.current['bundle_sha256']
        self.service.bind(checksum, 'b' * 64)
        await self.service.cancel()
        self.assertEqual(self.service.status()['status'], 'cancelled')


class OwnedSkillPlanStartBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = DesktopScheduler.__new__(DesktopScheduler)
        self.scheduler._owned_planning_start = None
        self.scheduler.owned_skill_planning = SimpleNamespace(consume=Mock())
        self.bundle = synthetic_native_contract_bundle()
        self.checksum = digest(self.bundle)
        self.preview = 'c' * 64
        self.arguments = dict(candidate_sha256='a' * 64, source_run_ref='a' * 64,
                              invocation_sha256='a' * 64, case_key='synthetic-case',
                              development_value='synthetic-value', planning_bundle_sha256=self.checksum)

    def test_valid_bundle_alone_cannot_authorize_lower_start(self):
        with self.assertRaisesRegex(ValueError, 'explicit_start_required'):
            self.scheduler.start_owned_form_candidate_execution(
                **self.arguments, planning_bundle=self.bundle, preview_sha256=self.preview,
                confirm_sha256=self.preview, lease_id='synthetic-lease', generation=0)

    def test_wrapper_scopes_start_to_confirmed_tuple_and_clears_on_failure(self):
        self.scheduler._planning_selection = Mock(return_value=(self.bundle, self.arguments))

        def failed_start(**arguments):
            self.assertEqual(self.scheduler._owned_planning_start,
                             (self.checksum, self.preview, 'synthetic-lease', 0))
            self.assertEqual(arguments['planning_bundle'], self.bundle)
            raise ValueError('synthetic_start_failure')

        self.scheduler.start_owned_form_candidate_execution = failed_start
        with self.assertRaisesRegex(ValueError, 'synthetic_start_failure'):
            self.scheduler.start_owned_skill_plan(self.checksum, self.checksum, self.preview,
                                                  self.preview, 'synthetic-lease', 0)
        self.scheduler.owned_skill_planning.consume.assert_called_once_with(self.checksum, self.preview)
        self.assertIsNone(self.scheduler._owned_planning_start)


class OwnedSkillPlanBoundConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = planning_helpers.OwnedSkillPlanningConsoleTests()
        await self.fixture.asyncSetUp()
        self.scheduler, self.app, self.origin = self.fixture.scheduler, self.fixture.app, self.fixture.origin

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_planned_routes_require_auth_exact_version_and_no_caller_goal_overrides(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url=self.origin, headers={'Origin': self.origin}) as client:
            request = {'schema_version': '1.4', 'planning_bundle_sha256': 'a' * 64,
                       'confirm_plan_sha256': 'a' * 64, 'lease_id': 'synthetic-lease', 'generation': 0}
            response = await client.post('/api/tasks/owned-skill-plan/bind', json=request)
            self.assertEqual(response.status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            with patch.object(self.scheduler, 'bind_owned_skill_plan') as callback:
                for change in ({'schema_version': '1.3'}, {'development_value': 'overridden'},
                               {'candidate_sha256': 'b' * 64}, {'case_key': 'other'}):
                    response = await client.post('/api/tasks/owned-skill-plan/bind', json=request | change)
                    self.assertEqual(response.status_code, 400)
                response = await client.post('/api/tasks/owned-skill-plan/bind', json=request | {'generation': True})
                self.assertEqual(response.status_code, 422)
                callback.assert_not_called()

    async def test_audit_cannot_report_legacy_or_unverified_plan_as_success(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url=self.origin, headers={'Origin': self.origin}) as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            for result in ({'schema_version': '1.3', 'available': True},
                           {'schema_version': '1.4', 'available': True, 'planning_admission_verified': False}):
                with patch.object(self.scheduler, 'audit_owned_form_candidate_execution', return_value=result):
                    response = await client.post('/api/tasks/owned-skill-plan/audit', json={
                        'schema_version': '1.4', 'candidate_execution_sha256': 'a' * 64})
                    self.assertEqual(response.status_code, 409)

import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from aos.contracts import AOSFault, REPO_ROOT, Settings, canonical, digest
from aos.dataset import validator
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.owned_skill_planner import BonsaiOwnedSkillPlanner, OwnedSkillPlan
from aos.owned_skill_planning import OwnedSkillPlanning, planning_case_key
from aos.storage import TrajectoryStore
from aos.task_sequence import SequenceStart
from test_desktop_tasks import FixtureDesktop


class SyntheticPlanner(BonsaiOwnedSkillPlanner):
    def __init__(self):
        self.pins = {'protocol': 'synthetic-test-only', 'temperature': 0, 'max_output_tokens': 768}
        self.pins['owned_skill_plan_schema_sha256'] = digest(
            json.loads((REPO_ROOT / 'schemas/owned_skill_plan.schema.json').read_text()))
        self.identity = {'real_model': False, 'kind': 'fixture_owned_skill_planner',
                         'deployment_id': 'synthetic-planner', 'pins': self.pins}
        self.last_metrics = {'latency_ms': 1, 'input_tokens': 1, 'output_tokens': 1}
        self.called = asyncio.Event()
        self.release = None
        self.failure = False

    async def plan(self, problem, evidence):
        self.called.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure:
            raise ValueError('private-literal-must-not-be-returned')
        item = evidence[0]
        return OwnedSkillPlan(
            decision='invoke_selected_skill', evidence_refs=['admitted-skill'],
            case_key=item['requested_case_key'], parameter_value=item['requested_value'],
            steps=item['ordered_steps'], reason_code='skill_match')


def synthetic_authority():
    value = {'manager_session': 'synthetic-manager', 'desktop_session_id': 'synthetic-desktop',
             'runtime_id': 'synthetic-runtime', 'lease_id': 'synthetic-lease', 'generation': 0}
    value.update({key: 'a' * 64 for key in (
        'reuse_admission_sha256', 'source_manifest_sha256', 'source_run_ref',
        'source_invocation_sha256', 'source_fingerprint_sha256', 'family_sha256',
        'release_sha256', 'selection_sha256', 'review_sha256', 'candidate_sha256', 'recipe_sha256')})
    return value


class OwnedSkillPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.planner = SyntheticPlanner()
        self.authority = synthetic_authority()
        self.fixture = json.loads((REPO_ROOT / 'examples/owned_skill_plan.json').read_text())
        self.yield_gpu = AsyncMock()
        self.service = OwnedSkillPlanning(self.planner, self.root / 'plans',
                                          self.prepare, self.yield_gpu)

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    def prepare(self, case_key, value, lease, generation):
        if lease != self.authority['lease_id'] or generation != self.authority['generation']:
            raise ValueError('synthetic_control_changed')
        evidence = [] if case_key is None else [{
            **copy.deepcopy(self.fixture['admitted_skill']),
            'requested_case_key': case_key, 'requested_value': value}]
        return copy.deepcopy(self.authority), evidence

    def begin(self, goal='Save message "private-literal"'):
        return self.service.begin(goal, self.authority['lease_id'], self.authority['generation'])

    async def test_private_bundle_and_status_are_distinct(self):
        response = self.begin()
        self.assertEqual(response['status'], 'pending')
        await self.service.task
        status = self.service.status()
        self.assertEqual(status['status'], 'ready')
        self.assertFalse(status['real_model'])
        self.assertTrue(status['model_called'])
        self.assertNotIn('private-literal', canonical(status))
        bundle = self.service.load(status['bundle_sha256'])
        self.assertEqual(bundle['model_response']['parameter_value'], 'private-literal')
        self.assertEqual(bundle['request']['case_key'], planning_case_key(bundle['request']['goal']))
        self.assertFalse(bundle['execution_authorized'])
        self.assertFalse(bundle['downstream_verified'])
        self.assertEqual(os.stat(self.root / 'plans').st_mode & 0o777, 0o700)
        path = self.root / 'plans' / (status['bundle_sha256'] + '.json')
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.yield_gpu.assert_awaited_once()

    async def test_unsupported_goals_never_call_model_or_create_private_directory(self):
        for goal in ('Do not save message "gamma"', 'Save message "a" and "b"',
                     'Save message "gamma" then delete', 'anything else'):
            self.assertEqual(self.begin(goal)['status'], 'needs_human')
        self.assertFalse(self.planner.called.is_set())
        self.assertFalse(self.service.reserved)
        self.assertFalse((self.root / 'plans').exists())
        self.yield_gpu.assert_not_called()

    async def test_pending_plan_blocks_another_and_cancellation_drains(self):
        self.planner.release = asyncio.Event()
        self.begin()
        await self.planner.called.wait()
        with self.assertRaises(ValueError):
            self.begin()
        await self.service.cancel()
        self.assertFalse(self.service.reserved)
        self.assertEqual(self.service.status()['status'], 'cancelled')
        self.assertFalse((self.root / 'plans').exists())
        self.planner.release.set()
        self.begin('Mesaj alanına "delta" kaydet')
        await self.service.task
        self.assertEqual(self.service.status()['status'], 'ready')

    async def test_model_completion_rechecks_control_and_source(self):
        for field in ('generation', 'selection_sha256', 'review_sha256'):
            with self.subTest(field=field):
                self.planner.called.clear()
                self.planner.release = asyncio.Event()
                self.begin()
                await self.planner.called.wait()
                self.authority[field] = 1 if field == 'generation' else 'b' * 64
                self.planner.release.set()
                await self.service.task
                self.assertEqual(self.service.status()['status'], 'failed')
                self.assertFalse((self.root / 'plans').exists())
                self.authority = synthetic_authority()

    async def test_late_model_result_cannot_escape_cancellation_or_release_reservation_early(self):
        entered = asyncio.Event()
        draining = asyncio.Event()
        release = asyncio.Event()
        original = self.planner.plan

        async def late_plan(problem, evidence):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                draining.set()
                await release.wait()
            return await original(problem, evidence)

        self.planner.plan = late_plan
        self.begin()
        await entered.wait()
        cancellation = asyncio.create_task(self.service.cancel())
        await draining.wait()
        self.assertTrue(self.service.reserved)
        with self.assertRaises(ValueError):
            self.begin('Save message "later"')
        release.set()
        await cancellation
        self.assertEqual(self.service.status()['status'], 'cancelled')
        self.assertFalse(self.service.reserved)
        self.assertFalse((self.root / 'plans').exists())

    async def test_errors_do_not_expose_model_text(self):
        self.planner.failure = True
        self.begin()
        await self.service.task
        self.assertEqual(self.service.status()['status'], 'failed')
        self.assertNotIn('private-literal', canonical(self.service.status()))

    async def test_pin_and_artifact_tampering_is_rejected(self):
        self.begin()
        await self.service.task
        checksum = self.service.status()['bundle_sha256']
        self.planner.pins['protocol'] = 'changed'
        with self.assertRaises(ValueError):
            self.service.load(checksum)
        self.planner.pins['protocol'] = 'synthetic-test-only'
        path = self.root / 'plans' / (checksum + '.json')
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        with self.assertRaises(ValueError):
            self.service.load(checksum)
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.service.load(checksum)

    async def test_directory_symlink_and_replacement_are_rejected(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        (self.root / 'plans').symlink_to(elsewhere)
        self.begin()
        await self.service.task
        self.assertEqual(self.service.status()['status'], 'failed')
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.root / 'plans').unlink()
        self.begin()
        await self.service.task
        checksum = self.service.status()['bundle_sha256']
        (self.root / 'plans').rename(self.root / 'old-plans')
        (self.root / 'plans').mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.service.load(checksum)

    async def test_budget_and_close_fail_closed(self):
        self.service.calls = 8
        with self.assertRaises(ValueError):
            self.begin()
        self.service.calls = 0
        await self.service.close()
        with self.assertRaises(ValueError):
            self.begin()

    async def test_schema_fixture_remains_non_authorizing(self):
        fixture = json.loads((REPO_ROOT / 'examples/owned_skill_planning_bundle.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('owned_skill_planning_bundle').validate(fixture['bundle'])
        schema = json.loads((REPO_ROOT / 'schemas/owned_skill_planning_bundle.schema.json').read_text())
        plan_schema = json.loads((REPO_ROOT / 'schemas/owned_skill_plan.schema.json').read_text())
        definitions = plan_schema.pop('$defs')
        plan_schema.pop('$schema')
        self.assertEqual({key: schema['$defs'][key] for key in definitions}, definitions)
        from aos.owned_skill_knowledge_service import PlanningKnowledgeBinding

        knowledge_schema = PlanningKnowledgeBinding.model_json_schema()
        knowledge_definitions = knowledge_schema.pop('$defs')
        self.assertEqual(schema['properties']['knowledge'], knowledge_schema)
        self.assertEqual({key: schema['$defs'][key] for key in knowledge_definitions}, knowledge_definitions)
        self.assertEqual(schema['properties']['model_response'], plan_schema)
        for flag in ('execution_authorized', 'activation_authorized', 'training_ready', 'downstream_verified'):
            self.assertFalse(validator('owned_skill_planning_bundle').is_valid(
                fixture['bundle'] | {flag: True}))


class OwnedSkillPlanningConsoleTests(OwnedSkillPlanningTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        settings = Settings(workspace=self.root / 'workspace', database=self.root / 'store.sqlite')
        self.runtime = FixtureDesktop(settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(settings.database)
        self.controller = DesktopController(self.store, self.runtime)
        self.scheduler = DesktopScheduler(self.controller, settings, FixtureDecisionEngine())
        self.scheduler.owned_skill_planning = self.service
        state = self.controller.state()
        self.authority.update({key: state[key] for key in ('lease_id', 'generation')})
        self.origin = 'http://127.0.0.1:18765'
        self.app = create_console(self.controller, 'synthetic-token', self.origin, self.root,
                                  settings.database, self.scheduler)

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.runtime.stop()
        self.store.close()
        await super().asyncTearDown()

    async def test_plan_reservation_blocks_normal_and_sequence_start_without_rows(self):
        self.planner.release = asyncio.Event()
        self.begin()
        await self.planner.called.wait()
        state = self.controller.state()
        with self.assertRaises(AOSFault):
            self.scheduler.start(state['lease_id'], state['generation'])
        with self.assertRaises(AOSFault):
            self.scheduler.sequences.start(SequenceStart(
                lease_id=state['lease_id'], generation=state['generation'],
                plan={'kinds': ['hello', 'browser_form']}))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)

    async def test_source_recheck_uses_pure_preparation_under_its_own_reservation(self):
        self.planner.release = asyncio.Event()
        self.begin()
        await self.planner.called.wait()
        self.assertTrue(self.scheduler.planning_reserved)
        authority = synthetic_authority()
        admission = {key: authority[key] for key in (
            'manager_session', 'desktop_session_id', 'runtime_id', 'lease_id', 'generation')}
        admission['preview'] = {key: value for key, value in authority.items() if key not in admission}
        candidate = {'skill': {'skill_key': 'save_record'}, 'field_bindings': [
            {'parameter_key': 'record-query', 'form_field_name': 'message'}]}
        source = {'synthetic': True}
        source_method = Mock(return_value=(candidate, source))
        session = SimpleNamespace(execution_source=source_method)
        self.scheduler._owned_skill_reuse = {
            'context': {'candidate_session': session, 'source_run_id': 'synthetic-source'}}
        self.scheduler._owned_skill_reuse_admission_sha256 = 'a' * 64
        prepared = {'candidate': candidate, 'steps': self.fixture['admitted_skill']['ordered_steps']}
        state = self.controller.state()
        with patch.object(self.scheduler, '_assert_owned_skill_reuse_current', return_value=admission), \
                patch.object(self.scheduler, '_assert_owned_skill_reuse_candidate_binding') as binding, \
                patch.object(self.scheduler, 'prepare_owned_form_candidate_execution',
                             side_effect=AssertionError('public_idle_admission_must_not_be_used')), \
                patch('aos.owned_form_candidate_execution.prepare_candidate_form_run',
                      return_value=prepared) as compiler:
            checked, evidence = self.scheduler._prepare_owned_skill_plan(
                'plan-' + 'a' * 32, 'gamma', state['lease_id'], state['generation'])
        source_method.assert_called_once_with('synthetic-source', 'a' * 64, 'a' * 64)
        compiler.assert_called_once_with(candidate, 'a' * 64, source, 'plan-' + 'a' * 32, 'gamma')
        binding.assert_called_once()
        self.assertEqual(evidence[0]['requested_value'], 'gamma')
        self.assertTrue(self.scheduler.planning_reserved)

    async def test_api_returns_before_model_and_control_can_cancel(self):
        self.planner.release = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url=self.origin, headers={'Origin': self.origin}) as client:
            denied = await client.post('/api/tasks/owned-skill-plan', json={})
            self.assertEqual(denied.status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            state = self.controller.state()
            response = await asyncio.wait_for(client.post('/api/tasks/owned-skill-plan', json={
                'schema_version': '1.0', 'goal': 'Save message "private-literal"',
                'lease_id': state['lease_id'], 'generation': state['generation']}), 1)
            self.assertEqual(response.status_code, 202)
            await self.planner.called.wait()
            self.assertTrue(self.scheduler.reserved)
            response = await asyncio.wait_for(client.post('/api/control', json={'command': 'pause'}), 1)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(self.scheduler.reserved)
            self.assertEqual((await client.get('/api/tasks/owned-skill-plan')).json()['status'], 'cancelled')
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)

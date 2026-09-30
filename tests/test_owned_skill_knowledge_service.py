import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from aos.contracts import REPO_ROOT, canonical, digest
from aos.knowledge import KnowledgeStore
from aos.owned_skill_knowledge import validate_planning_knowledge_preview
from aos.owned_skill_knowledge_service import OwnedSkillKnowledgeService
from aos.owned_skill_planner import BonsaiOwnedSkillPlanner, OwnedSkillPlan
from aos.owned_skill_planning import OwnedSkillPlanning


SCOPE = {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant', 'account_role': 'reader'}
TEXT = 'Synthetic planning manual: preserve the exact admitted recipe and verify the receipt.'
GOAL = 'Save message "gamma"'


class FixtureKnowledgePlanner(BonsaiOwnedSkillPlanner):
    def __init__(self):
        fixture = json.loads((REPO_ROOT / 'examples/owned_skill_knowledge.json').read_text())
        self.pins = copy.deepcopy(fixture['responses']['preview']['model_pins'])
        self.identity = {'kind': 'fixture_owned_skill_planner', 'real_model': False,
                         'deployment_id': 'fixture-' + digest(self.pins), 'pins': self.pins}
        self.last_metrics = {'latency_ms': 1, 'input_tokens': 1, 'output_tokens': 1}
        self.last_request = None
        self.last_response = None
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = None
        self.before_dispatch = None
        self.after_response = None
        self.skip_capture = False

    async def plan(self, problem, evidence, *, request_context=None):
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.before_dispatch is not None:
            self.before_dispatch()
        guard = getattr(self, 'before_model_call', None)
        if guard is not None:
            guard()
        request = self.request_body(problem, evidence, request_context=request_context)
        self.calls += 1
        item = evidence[0]
        result = OwnedSkillPlan(decision='invoke_selected_skill', evidence_refs=['admitted-skill'],
            case_key=item['requested_case_key'], parameter_value=item['requested_value'],
            steps=item['ordered_steps'], reason_code='skill_match')
        if not self.skip_capture:
            self.last_request = json.loads(canonical(request))
            self.last_response = result.model_dump(mode='json')
        if self.after_response is not None:
            self.after_response()
        return result


class FixtureKnowledgeScheduler:
    def __init__(self, root, authority, evidence):
        self.settings = SimpleNamespace(workspace=root / 'workspace')
        self.settings.workspace.mkdir(mode=0o700)
        self.authority = copy.deepcopy(authority)
        self.evidence = copy.deepcopy(evidence)
        self.control = {'session_id': authority['desktop_session_id'], 'runtime_id': authority['runtime_id'],
                        'lease_id': authority['lease_id'], 'generation': authority['generation'],
                        'owner': 'AGENT', 'status': 'running'}
        self.controller = SimpleNamespace(state=lambda: copy.deepcopy(self.control))
        self.closed = False
        self.restart_quiesced = False
        self.busy = False
        self.owned_skill_planning = None

    @property
    def reserved(self):
        return self.busy or self.owned_skill_planning is not None and self.owned_skill_planning.reserved

    def prepare(self, case_key, value, lease_id, generation):
        if (self.busy or self.control['owner'] != 'AGENT' or self.control['status'] != 'running'
                or (lease_id, generation) != (self.control['lease_id'], self.control['generation'])):
            raise ValueError('fixture_control_changed')
        evidence = copy.deepcopy(self.evidence)
        evidence[0].update(requested_case_key=case_key, requested_value=value)
        return copy.deepcopy(self.authority), evidence


def make_fixture(root, planner, *, yield_gpu=None):
    fixture = json.loads((REPO_ROOT / 'examples/owned_skill_knowledge.json').read_text())['responses']['preview']
    scheduler = FixtureKnowledgeScheduler(root, fixture['authority'], fixture['evidence'])

    async def idle_yield():
        await asyncio.sleep(0)

    planning = OwnedSkillPlanning(planner, root / 'plans', scheduler.prepare, yield_gpu or idle_yield)
    scheduler.owned_skill_planning = planning
    store = KnowledgeStore(root / 'knowledge')
    service = OwnedSkillKnowledgeService(scheduler, store)
    planning.knowledge = service
    publication = store.publish_preview(scope=SCOPE, source_id='manual', title='Synthetic planning manual',
        text=TEXT, previous_sha256=None, expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        rights_attested=True, storage_consent=True, synthetic=True)
    document = store.publish(preview=publication, confirm_sha256=publication['preview_sha256'])
    review = store.review_preview(scope=SCOPE, document_sha256=document['document_sha256'], decision='accept')
    store.review(preview=review, confirm_sha256=review['preview_sha256'])
    scheduler.synthetic_document = document
    arguments = {'schema_version': '1.0', 'goal': GOAL, 'scope': SCOPE, 'query': 'planning recipe',
                 'top_k': 4, 'context_chars': 2048, 'lease_id': scheduler.control['lease_id'],
                 'generation': scheduler.control['generation']}
    return scheduler, store, service, arguments


class OwnedSkillKnowledgeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.planner = FixtureKnowledgePlanner()
        self.yield_entered = asyncio.Event()
        self.yield_release = None
        self.scheduler, self.store, self.service, self.arguments = make_fixture(
            self.root, self.planner, yield_gpu=self.yield_gpu)
        self.planning = self.scheduler.owned_skill_planning
        self.document = self.scheduler.synthetic_document

    async def asyncTearDown(self):
        await self.planning.close()
        self.temporary.cleanup()

    async def yield_gpu(self):
        self.yield_entered.set()
        if self.yield_release is not None:
            await self.yield_release.wait()

    def publish(self):
        preview = self.store.publish_preview(scope=SCOPE, source_id='manual', title='Synthetic planning manual',
            text=TEXT, previous_sha256=None, expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        return self.store.publish(preview=preview, confirm_sha256=preview['preview_sha256'])

    def review(self, decision):
        preview = self.store.review_preview(scope=SCOPE, document_sha256=self.document['document_sha256'],
                                             decision=decision)
        return self.store.review(preview=preview, confirm_sha256=preview['preview_sha256'])

    def preview_arguments(self):
        return {'schema_version': '1.0', 'goal': GOAL, 'scope': SCOPE, 'query': 'planning recipe',
                'top_k': 4, 'context_chars': 2048, 'lease_id': self.scheduler.control['lease_id'],
                'generation': self.scheduler.control['generation']}

    def preview(self, **changes):
        return self.service.preview(**(self.preview_arguments() | changes))

    def start_arguments(self, preview):
        return {'schema_version': '1.0', 'preview': preview, 'confirm_sha256': preview['confirm_sha256'],
                'inference_consent': True, 'storage_consent': True,
                'lease_id': preview['authority']['lease_id'], 'generation': preview['authority']['generation']}

    def begin(self, preview=None, **changes):
        return self.service.begin(**(self.start_arguments(preview or self.preview()) | changes))

    async def ready(self):
        preview = self.preview()
        started = self.begin(preview)
        await self.planning.task
        self.assertEqual(self.planning.status()['status'], 'ready')
        return preview, started, self.planning.status()['bundle_sha256']

    async def test_preview_is_readonly_and_fixture_dispatch_is_not_real_model_proof(self):
        preview = self.preview()
        self.assertFalse((self.root / 'plans').exists())
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.planning.status()['status'], 'idle')
        validate_planning_knowledge_preview(preview)
        self.begin(preview)
        await self.planning.task
        self.assertEqual(self.planning.status()['status'], 'ready')
        checksum = self.planning.status()['bundle_sha256']
        report = self.service.report(checksum)
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['dispatch_recorded'])
        self.assertTrue(report['current_source_valid'])
        self.assertTrue(report['current_authority_valid'])
        self.assertFalse(report['real_model'])
        self.assertFalse(report['model_request_verified'])
        self.assertFalse(report['knowledge_applied'])
        self.assertFalse(report['downstream_verified'])
        self.assertFalse(report['semantic_relevance_verified'])
        self.assertEqual(report['bundle_canonical'], canonical(report['bundle']))
        self.assertEqual(digest(report['bundle']), checksum)
        self.assertEqual(report['bundle']['model_request'], self.planner.last_request)
        self.assertEqual(report['bundle']['model_response'], self.planner.last_response)

    async def test_revoke_at_native_dispatch_boundary_records_no_dispatch_or_bundle(self):
        self.planner.before_dispatch = lambda: self.review('revoke')
        previous = Mock()
        self.planner.before_model_call = previous
        self.begin()
        await self.planning.task
        previous.assert_called_once_with()
        self.assertIs(self.planner.before_model_call, previous)
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.planning.status()['status'], 'failed')
        self.assertEqual(list((self.root / 'plans').glob('knowledge-dispatch-*')), [])
        self.assertIsNone(self.planning.status()['bundle_sha256'])

    async def test_revoke_after_response_prevents_bundle_publication(self):
        self.planner.after_response = lambda: self.review('revoke')
        self.begin()
        await self.planning.task
        self.assertEqual(self.planner.calls, 1)
        self.assertEqual(self.planning.status()['status'], 'failed')
        self.assertIsNone(self.planning.status()['bundle_sha256'])
        self.assertEqual(len(list((self.root / 'plans').glob('knowledge-dispatch-*'))), 1)
        self.assertFalse(hasattr(self.planner, 'before_model_call'))

    async def test_historical_report_survives_revocation_but_binding_and_effect_gate_fail(self):
        preview, started, checksum = await self.ready()
        bundle = self.planning.load(checksum)
        self.review('revoke')
        report = self.service.report(checksum)
        self.assertTrue(report['historical_binding_verified'])
        self.assertFalse(report['current_source_valid'])
        self.assertTrue(report['current_authority_valid'])
        with self.assertRaises(ValueError):
            self.service.check_binding(bundle)
        with self.assertRaises(ValueError):
            self.planning.select(checksum, preview['authority']['lease_id'], preview['authority']['generation'])
        self.scheduler.busy = True
        with self.assertRaises(ValueError):
            self.service.check_binding(bundle, check_plan=False)
        self.assertEqual(self.planner.calls, 1)

    async def test_effect_check_works_while_busy_without_replanning(self):
        preview, started, checksum = await self.ready()
        bundle = self.planning.load(checksum)
        self.scheduler.busy = True
        self.service.check_binding(bundle, check_plan=False)
        with self.assertRaises(ValueError):
            self.service.check_binding(bundle)
        self.scheduler.control['generation'] += 1
        with self.assertRaises(ValueError):
            self.service.check_binding(bundle, check_plan=False)

    async def test_missing_actual_request_capture_cannot_publish_context_bundle(self):
        self.planner.skip_capture = True
        self.begin()
        await self.planning.task
        self.assertEqual(self.planning.status()['status'], 'failed')
        self.assertIsNone(self.planning.status()['bundle_sha256'])
        self.assertEqual(self.planner.calls, 1)

    async def test_consents_nonce_expiry_and_learning_lane_are_separate(self):
        preview = self.preview()
        for changes in ({'inference_consent': False}, {'storage_consent': False},
                        {'inference_consent': 1}, {'collect_learning': True}):
            with self.assertRaises(ValueError):
                self.begin(preview, **changes)
        expired = copy.deepcopy(preview)
        expired['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        expired['confirm_sha256'] = digest({key: item for key, item in expired.items() if key != 'confirm_sha256'})
        with self.assertRaises(ValueError):
            self.begin(expired)
        self.assertFalse((self.root / 'plans').exists())
        self.begin(preview)
        await self.planning.task
        with self.assertRaises(ValueError):
            self.begin(preview)
        self.assertEqual(self.planner.calls, 1)

    async def test_current_control_workspace_store_and_skill_identity_drift_fail_before_call(self):
        preview = self.preview()
        self.scheduler.control['owner'] = 'HUMAN'
        with self.assertRaises(ValueError):
            self.begin(preview)
        self.scheduler.control['owner'] = 'AGENT'
        original_workspace = self.scheduler.settings.workspace
        moved = self.root / 'other-workspace'
        moved.mkdir(mode=0o700)
        self.scheduler.settings.workspace = moved
        with self.assertRaises(ValueError):
            self.begin(preview)
        self.scheduler.settings.workspace = original_workspace
        self.scheduler.authority['review_sha256'] = 'f' * 64
        with self.assertRaises(ValueError):
            self.begin(preview)
        self.scheduler.authority = copy.deepcopy(preview['authority'])
        alternate = KnowledgeStore(self.root / 'other-store')
        self.service.store = alternate
        with self.assertRaises((ValueError, OSError)):
            self.begin(preview)
        self.service.store = self.store
        self.assertEqual(self.planner.calls, 0)

    async def test_cancellation_restores_existing_guard_during_model_wait(self):
        previous = Mock()
        self.planner.before_model_call = previous
        self.planner.release = asyncio.Event()
        self.begin()
        await self.planner.entered.wait()
        self.assertIsNot(self.planner.before_model_call, previous)
        await self.planning.cancel()
        self.assertIs(self.planner.before_model_call, previous)
        previous.assert_not_called()
        self.assertEqual(self.planning.status()['status'], 'cancelled')
        self.assertEqual(self.planner.calls, 0)

    async def test_cancellation_before_gpu_yield_does_not_remove_previous_guard(self):
        previous = Mock()
        self.planner.before_model_call = previous
        self.yield_release = asyncio.Event()
        self.begin()
        await self.yield_entered.wait()
        await self.planning.cancel()
        self.assertIs(self.planner.before_model_call, previous)
        previous.assert_not_called()
        self.assertEqual(self.planner.calls, 0)

    async def test_existing_none_guard_presence_survives_cancellation_before_yield(self):
        self.planner.before_model_call = None
        self.yield_release = asyncio.Event()
        self.begin()
        await self.yield_entered.wait()
        await self.planning.cancel()
        self.assertTrue(hasattr(self.planner, 'before_model_call'))
        self.assertIsNone(self.planner.before_model_call)
        self.assertEqual(self.planner.calls, 0)

    async def test_previous_guard_failure_prevents_dispatch_and_restores_guard(self):
        previous = Mock(side_effect=ValueError('synthetic_previous_guard_denial'))
        self.planner.before_model_call = previous
        self.begin()
        await self.planning.task
        self.assertIs(self.planner.before_model_call, previous)
        previous.assert_called_once_with()
        self.assertEqual(self.planning.status()['status'], 'failed')
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(list((self.root / 'plans').glob('knowledge-dispatch-*')), [])

    async def test_direct_planning_cannot_mix_context_with_episode_collection(self):
        preview = self.preview()
        intent = {'schema_version': '1.0', 'preview': preview, 'inference_consent': True, 'storage_consent': True}
        with self.assertRaises(ValueError):
            self.planning.begin(GOAL, preview['authority']['lease_id'], preview['authority']['generation'],
                                collect_learning=True, knowledge={'intent': intent, 'intent_sha256': digest(intent)})
        self.assertEqual(self.planner.calls, 0)
        self.assertFalse((self.root / 'plans').exists())

    async def test_private_dispatch_tamper_breaks_historical_audit(self):
        preview, started, checksum = await self.ready()
        path = next((self.root / 'plans').glob('knowledge-dispatch-*'))
        dispatch = json.loads(path.read_text())
        dispatch['request_sha256'] = '0' * 64
        path.write_text(canonical(dispatch))
        with self.assertRaises(ValueError):
            self.service.report(checksum)

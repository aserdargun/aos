from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aos.contracts import canonical, digest
from aos.knowledge import KnowledgeStore
from aos.web_goal_knowledge_service import WebGoalKnowledgeService
from aos.web_goal_planning import WebGoalPlanning
from test_web_goal_knowledge import knowledge_inputs
from test_web_goal_planner import response_for


class WebGoalKnowledgeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.case, self.catalog, self.planner, self.authority, _review = knowledge_inputs()
        self.scope = {'application_id': self.catalog.application_key,
                      'tenant_id': self.catalog.tenant_key, 'account_role': self.catalog.account_role}
        self.store = KnowledgeStore(self.root / 'knowledge')
        self.planning = WebGoalPlanning(self.planner, self.root / 'proposals',
            lambda lease, generation: (deepcopy(self.authority), self.catalog), AsyncMock())
        self.scheduler = SimpleNamespace(web_goal_planning=self.planning, reserved=False,
            closed=False, restart_quiesced=False, settings=SimpleNamespace(workspace=self.workspace))
        self.service = WebGoalKnowledgeService(self.scheduler, self.store)
        self.planning.knowledge_current = self.service.check_current
        self.document = self.publish(self.scope)
        self.review_document(self.document)

    def publish(self, scope):
        preview = self.store.publish_preview(scope=scope, source_id='synthetic-manual',
            title='Synthetic notes manual', text='Synthetic notes manual describes reviewing exact note text.',
            previous_sha256=None, expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        return self.store.publish(preview=preview, confirm_sha256=preview['preview_sha256'])

    def review_document(self, document, decision='accept'):
        preview = self.store.review_preview(scope=document['document']['scope'],
            document_sha256=document['document_sha256'], decision=decision)
        return self.store.review(preview=preview, confirm_sha256=preview['preview_sha256'])

    def preview(self, **overrides):
        return self.service.preview(**({'goal': self.case['goal'], 'scope': self.scope,
            'query': 'notes manual', 'top_k': 4, 'context_chars': 2048,
            'confirm_catalog_sha256': digest(self.catalog.model_dump(mode='json')),
            'lease_id': self.authority['lease_id'], 'generation': self.authority['generation']} | overrides))

    async def test_actual_reviewed_store_private_dispatch_and_revocation_blocks_start(self):
        review = self.preview()
        self.assertFalse(self.planning.directory.exists())
        self.assertEqual(review['retrieval']['hits'][0]['document_sha256'], self.document['document_sha256'])
        self.planning.begin(self.case['goal'], self.authority['lease_id'], self.authority['generation'],
            confirm_catalog_sha256=review['catalog_sha256'], inference_consent=True,
            knowledge_review=review, confirm_knowledge_sha256=review['confirm_sha256'], storage_consent=True)
        self.review_document(self.document, 'revoke')
        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', AsyncMock()) as native:
            await self.planning.task
        self.assertEqual(self.planning.status()['status'], 'failed')
        self.assertFalse(self.planning.status()['model_called'])
        native.assert_not_called()

    async def test_actual_reviewed_store_context_flows_into_private_model_request(self):
        review = self.preview()

        async def native(instance, goal, evidence, *, request_context):
            instance.before_model_call()
            instance.last_request = instance.request_body(goal, evidence, request_context=request_context)
            instance.last_response = response_for(self.case, self.catalog)
            return instance.parse_response(canonical(instance.last_response), evidence)

        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', native):
            self.planning.begin(self.case['goal'], self.authority['lease_id'], self.authority['generation'],
                confirm_catalog_sha256=review['catalog_sha256'], inference_consent=True,
                knowledge_review=review, confirm_knowledge_sha256=review['confirm_sha256'], storage_consent=True)
            await self.planning.task
        self.assertEqual(self.planning.status()['status'], 'ready')
        bundle = self.planning.load(self.planning.status()['bundle_sha256'])
        self.assertEqual(bundle['knowledge']['review'], review)
        self.assertFalse(bundle['deployment']['real_model'])
        self.review_document(self.document, 'revoke')
        with self.assertRaises(ValueError):
            self.planning.select(self.planning.status()['bundle_sha256'], self.authority['lease_id'], 2)

    async def test_foreign_scope_workspace_control_and_source_cannot_be_reused(self):
        foreign = self.scope | {'tenant_id': 'foreign-tenant'}
        self.review_document(self.publish(foreign))
        with self.assertRaises(ValueError):
            self.preview(scope=foreign)
        review = self.preview()
        self.authority['generation'] += 1
        with self.assertRaises(ValueError):
            self.service.check_current(review)
        self.authority['generation'] -= 1
        self.workspace.rename(self.root / 'old-workspace')
        self.workspace.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.service.check_current(review)

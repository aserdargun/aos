from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.knowledge import KnowledgeStore
from aos.owned_form_candidate_execution import _write_private_child
from aos.web_goal_knowledge_service import (
    WebGoalKnowledgeAcknowledgement, WebGoalKnowledgeReport,
    WebGoalKnowledgeReportRequest, WebGoalKnowledgeService,
)
from aos.web_goal_planning import WebGoalPlanning
from test_web_goal_knowledge import knowledge_inputs
from test_web_goal_planner import response_for


class WebGoalKnowledgeReportTests(unittest.IsolatedAsyncioTestCase):
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
        publication = self.store.publish_preview(scope=self.scope, source_id='synthetic-notes',
            title='Synthetic notes manual', text='Synthetic notes manual describes exact note text.',
            previous_sha256=None, expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        self.document = self.store.publish(preview=publication, confirm_sha256=publication['preview_sha256'])
        self.review_source('accept')

    def review_source(self, decision):
        preview = self.store.review_preview(scope=self.scope,
            document_sha256=self.document['document_sha256'], decision=decision)
        return self.store.review(preview=preview, confirm_sha256=preview['preview_sha256'])

    async def propose(self, *, fail_before_dispatch=False):
        review = self.service.preview(goal=self.case['goal'], scope=self.scope,
            query='notes manual', top_k=4, context_chars=2048,
            confirm_catalog_sha256=digest(self.catalog.model_dump(mode='json')),
            lease_id=self.authority['lease_id'], generation=self.authority['generation'])

        async def native(instance, goal, evidence, *, request_context):
            instance.before_model_call()
            instance.last_request = instance.request_body(goal, evidence, request_context=request_context)
            instance.last_response = response_for(self.case, self.catalog)
            return instance.parse_response(canonical(instance.last_response), evidence)

        if fail_before_dispatch:
            self.planning.yield_gpu.side_effect = ValueError('synthetic rejection before dispatch')
        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', native):
            self.planning.begin(self.case['goal'], self.authority['lease_id'], self.authority['generation'],
                confirm_catalog_sha256=review['catalog_sha256'], inference_consent=True,
                knowledge_review=review, confirm_knowledge_sha256=review['confirm_sha256'], storage_consent=True)
            await self.planning.task
        if fail_before_dispatch:
            self.assertEqual(self.planning.status()['status'], 'failed')
            return next(self.planning.directory.glob('*.json')).stem
        self.assertEqual(self.planning.status()['status'], 'ready')
        return self.planning.status()['bundle_sha256']

    def acknowledgement_path(self, checksum):
        return self.planning.directory / (checksum + '.knowledge-ack.json')

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*.json')}

    async def test_fixture_acknowledgement_is_inspectable_readonly_and_never_real_use(self):
        checksum = await self.propose()
        before = self.snapshot()
        with patch.object(self.planner, 'plan', AsyncMock()) as model, \
                patch.object(self.store, 'search', side_effect=AssertionError('no retrieval rerun')):
            report = self.service.report(bundle_sha256=checksum)
            repeated = self.service.report(bundle_sha256=checksum)
        self.assertEqual(report, repeated)
        self.assertEqual(self.snapshot(), before)
        model.assert_not_called()
        self.planning.yield_gpu.assert_awaited_once()
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['model_input_bound'])
        self.assertTrue(report['model_acknowledgement_recorded'])
        self.assertTrue(report['current_review_valid'])
        self.assertFalse(report['real_model'])
        self.assertFalse(report['actual_model_use_verified'])
        self.assertEqual(digest(json.loads(report['bundle_canonical'])), checksum)
        self.assertEqual(digest(json.loads(report['acknowledgement_canonical'])), report['acknowledgement_sha256'])
        self.assertEqual(report['acknowledgement']['model_request_sha256'], digest(report['bundle']['model_request']))
        for flag in ('semantic_relevance_verified', 'downstream_verified', 'training_ready',
                     'execution_authorized', 'activation_authorized', 'gpu_release_verified'):
            self.assertIs(report[flag], False)
        validator('web_goal_knowledge_report_response').validate(report)

    async def test_legacy_proposal_without_acknowledgement_never_claims_actual_use(self):
        checksum = await self.propose()
        self.acknowledgement_path(checksum).unlink()
        report = self.service.report(bundle_sha256=checksum)
        self.assertTrue(report['proposal_recorded'])
        self.assertTrue(report['model_input_bound'])
        self.assertFalse(report['model_acknowledgement_recorded'])
        self.assertFalse(report['actual_model_use_verified'])
        self.assertIsNone(report['acknowledgement'])

    async def test_proposed_input_before_dispatch_is_not_acknowledged_model_use(self):
        checksum = await self.propose(fail_before_dispatch=True)
        report = self.service.report(bundle_sha256=checksum)
        self.assertEqual(report['stage'], 'intent')
        self.assertTrue(report['historical_binding_verified'])
        self.assertFalse(report['proposal_recorded'])
        self.assertFalse(report['model_acknowledgement_recorded'])
        self.assertFalse(report['actual_model_use_verified'])
        self.assertIsNone(report['model_response_sha256'])
        self.assertIsNone(report['intent_sha256'])

    async def test_synthetic_native_metadata_without_receipt_does_not_prove_actual_use(self):
        checksum = await self.propose()
        bundle = deepcopy(self.service.report(bundle_sha256=checksum)['bundle'])
        deployment = {'kind': 'bonsai_native_web_goal_planner', 'real_model': True,
            'pins': deepcopy(self.planner.pins), 'deployment_id': 'bonsai-' + digest(self.planner.pins)}
        bundle['deployment'] = deployment
        bundle['model_request']['model'] = deployment['deployment_id']
        review = bundle['knowledge']['review']
        review['deployment_sha256'] = digest(deployment)
        review['confirm_sha256'] = digest({key: value for key, value in review.items() if key != 'confirm_sha256'})
        intent = bundle | {'stage': 'intent', 'intent_sha256': None, 'model_response': None}
        bundle['intent_sha256'] = digest(intent)
        descriptor = self.planning._open_directory()
        try:
            for record in (intent, bundle):
                _write_private_child(descriptor, digest(record) + '.json', canonical(record).encode())
        finally:
            os.close(descriptor)
        report = self.service.report(bundle_sha256=digest(bundle))
        self.assertTrue(report['real_model'])
        self.assertTrue(report['proposal_recorded'])
        self.assertTrue(report['model_input_bound'])
        self.assertFalse(report['model_acknowledgement_recorded'])
        self.assertFalse(report['actual_model_use_verified'])

    async def test_schema_errors_do_not_include_private_record_content(self):
        checksum = await self.propose()
        bundle = deepcopy(self.service.report(bundle_sha256=checksum)['bundle'])
        secret = 'synthetic-private-document-payload'
        bundle['unauthorized_key'] = secret
        descriptor = self.planning._open_directory()
        try:
            _write_private_child(descriptor, digest(bundle) + '.json', canonical(bundle).encode())
        finally:
            os.close(descriptor)
        with self.assertRaisesRegex(ValueError, '^web_goal_knowledge_report_record_invalid$') as error:
            self.service.report(bundle_sha256=digest(bundle))
        self.assertNotIn(secret, str(error.exception))

    async def test_revocation_preserves_historical_inspection_and_reports_current_invalid(self):
        checksum = await self.propose()
        historical = self.service.report(bundle_sha256=checksum)
        revoked = self.review_source('revoke')
        report = self.service.report(bundle_sha256=checksum)
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['model_acknowledgement_recorded'])
        self.assertTrue(report['current_authority_valid'])
        self.assertFalse(report['current_source_valid'])
        self.assertFalse(report['current_review_valid'])
        self.assertEqual(report['bundle'], historical['bundle'])
        self.assertEqual(report['sources'][0]['review_sha256'], historical['sources'][0]['review_sha256'])
        self.assertEqual(report['sources'][0]['current_review_status'], 'revoked')
        self.assertEqual(report['sources'][0]['current_review_sha256'], revoked['review_sha256'])
        self.assertEqual(report['errors'], ['current_source_invalid'])

    async def test_source_expiry_and_review_expiry_are_distinct(self):
        checksum = await self.propose()
        self.store.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=2)
        report = self.service.report(bundle_sha256=checksum)
        self.assertFalse(report['current_source_valid'])
        self.assertTrue(report['sources'][0]['expired'])
        self.assertFalse(report['review_expired'])
        self.store.clock = lambda: datetime.now(timezone.utc)
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        with patch('aos.web_goal_knowledge_service.datetime') as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = future
            report = self.service.report(bundle_sha256=checksum)
        self.assertTrue(report['current_source_valid'])
        self.assertTrue(report['review_expired'])
        self.assertFalse(report['current_review_valid'])

    async def test_stale_authority_workspace_and_deployment_preserve_historical_binding(self):
        checksum = await self.propose()
        self.authority['generation'] += 1
        report = self.service.report(bundle_sha256=checksum)
        self.assertTrue(report['historical_binding_verified'])
        self.assertFalse(report['current_authority_valid'])
        self.authority['generation'] -= 1
        self.workspace.rename(self.root / 'old-workspace')
        self.workspace.mkdir(mode=0o700)
        report = self.service.report(bundle_sha256=checksum)
        self.assertFalse(report['current_authority_valid'])
        self.planner.identity = self.planner.identity | {'deployment_id': 'changed-fixture'}
        report = self.service.report(bundle_sha256=checksum)
        self.assertTrue(report['historical_binding_verified'])
        self.assertFalse(report['current_authority_valid'])

    async def test_record_acknowledgement_hash_mismatch_and_symlink_fail_closed(self):
        checksum = await self.propose()
        path = self.acknowledgement_path(checksum)
        original = path.read_bytes()
        acknowledgement = json.loads(original)
        acknowledgement['context_sha256'] = '0' * 64
        path.write_bytes(canonical(acknowledgement).encode())
        with self.assertRaises(ValueError):
            self.service.report(bundle_sha256=checksum)
        path.write_bytes(original + b'\n')
        with self.assertRaises(ValueError):
            self.service.report(bundle_sha256=checksum)
        path.unlink()
        target = self.root / 'copied-ack.json'
        target.write_bytes(original)
        path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.service.report(bundle_sha256=checksum)

    async def test_corrupt_original_intent_is_not_a_historical_binding(self):
        checksum = await self.propose()
        report = self.service.report(bundle_sha256=checksum)
        intent_path = self.planning.directory / (report['intent_sha256'] + '.json')
        intent_path.write_bytes(intent_path.read_bytes() + b'\n')
        with self.assertRaises(ValueError):
            self.service.report(bundle_sha256=checksum)

    async def test_canonical_schemas_and_strict_acknowledgement_booleans(self):
        for name, model in (
                ('web_goal_knowledge_report_request', WebGoalKnowledgeReportRequest),
                ('web_goal_knowledge_report_response', WebGoalKnowledgeReport),
                ('web_goal_knowledge_acknowledgement', WebGoalKnowledgeAcknowledgement)):
            expected = model.model_json_schema()
            expected['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
            expected['$id'] = name + '.schema.json'
            self.assertEqual(json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text()), expected)
        checksum = await self.propose()
        acknowledgement = json.loads(self.acknowledgement_path(checksum).read_bytes())
        acknowledgement['real_model'] = 1
        with self.assertRaises(ValidationError):
            WebGoalKnowledgeAcknowledgement.model_validate(acknowledgement)
        with self.assertRaises(ValidationError):
            WebGoalKnowledgeReportRequest.model_validate({'schema_version': '1.0', 'bundle_sha256': '../escape'})

    async def test_report_schema_and_typed_contract_reject_inconsistent_evidence_claims(self):
        checksum = await self.propose()
        report = self.service.report(bundle_sha256=checksum)
        for changes in (
                {'actual_model_use_verified': True},
                {'real_model': True},
                {'model_acknowledgement_recorded': False},
                {'acknowledgement': None},
                {'acknowledgement_canonical': None},
                {'acknowledgement_sha256': None},
                {'proposal_recorded': False},
                {'stage': 'intent'},
                {'current_review_valid': False},
                {'current_source_valid': False},
                {'current_authority_valid': False},
                {'review_expired': True}):
            with self.subTest(changes=changes):
                contradictory = report | changes
                self.assertFalse(validator('web_goal_knowledge_report_response').is_valid(contradictory))
                with self.assertRaises(ValidationError):
                    WebGoalKnowledgeReport.model_validate(contradictory)

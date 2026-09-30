import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.knowledge import KnowledgeStore
from aos.knowledge_answer import (KnowledgeAnswerService, REQUESTS, RESPONSES,
                                  validate_answer_bundle)
from aos.knowledge_answer_model import KnowledgeAnswerSelection


SCOPE = {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant', 'account_role': 'reader'}
TEXT = 'Synthetic password reset instructions: open Settings and select Reset password.'


class FixtureAnswerer:
    def __init__(self):
        self.identity = {'kind': 'synthetic_knowledge_answerer', 'real_model': False,
                         'deployment_id': 'synthetic-answerer-v1', 'pins': {}}
        self.calls = 0
        self.started = asyncio.Event()
        self.release = None
        self.response = {'schema_version': '1.0', 'needs_human': False,
                         'quotes': [{'citation_id': 'c1', 'text': 'open Settings and select Reset password.'}]}
        self.callback = None
        self.cleaned = False

    def request_body(self, question, evidence):
        return {'question': question, 'evidence': copy.deepcopy(evidence), 'model': self.identity['deployment_id']}

    def parse_response(self, content, evidence):
        result = KnowledgeAnswerSelection.model_validate(json.loads(content))
        result.validate_evidence(evidence)
        return result

    async def plan(self, question, evidence):
        try:
            if self.callback:
                self.callback()
            if getattr(self, 'before_model_call', None):
                self.before_model_call()
            self.calls += 1
            self.started.set()
            if self.release is not None:
                await self.release.wait()
            return self.parse_response(canonical(self.response), evidence)
        finally:
            self.cleaned = True


class KnowledgeAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.instant = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.store = KnowledgeStore(self.parent / 'knowledge', clock=lambda: self.instant)
        self.answerer = FixtureAnswerer()
        self.control = {'session_id': 'synthetic-session', 'lease_id': 'synthetic-lease', 'generation': 1}
        self.yields = 0
        self.yield_callback = None
        self.service = KnowledgeAnswerService(self.store, self.answerer,
            self.parent / 'answers', self.authority, self.yield_gpu)
        self.addAsyncCleanup(self.service.close)
        self.document = self.publish()
        self.review()

    def authority(self, lease_id, generation):
        if (lease_id, generation) != (self.control['lease_id'], self.control['generation']):
            raise ValueError('private control detail must not leak')
        return copy.deepcopy(self.control)

    async def yield_gpu(self):
        self.yields += 1
        if self.yield_callback:
            self.yield_callback()
        await asyncio.sleep(0)

    def publish(self, text=TEXT, previous_sha256=None, scope=None, source_id='manual'):
        preview = self.store.publish_preview(scope=scope or SCOPE, source_id=source_id, title='Synthetic manual',
            text=text, previous_sha256=previous_sha256,
            expires_at=(self.instant + timedelta(hours=1)).isoformat(),
            rights_attested=True, storage_consent=True, synthetic=True)
        return self.store.publish(preview=preview, confirm_sha256=preview['preview_sha256'])

    def review(self, decision='accept'):
        preview = self.store.review_preview(scope=SCOPE,
            document_sha256=self.document['document_sha256'], decision=decision)
        return self.store.review(preview=preview, confirm_sha256=preview['preview_sha256'])

    def preview(self, **changes):
        return self.service.preview(**({'scope': SCOPE, 'question': 'password reset', 'top_k': 8,
            'context_chars': 8192, 'lease_id': 'synthetic-lease', 'generation': 1} | changes))

    def begin(self, preview=None, **changes):
        preview = preview or self.preview()
        return self.service.begin(**({'preview': preview, 'confirm_sha256': preview['confirm_sha256'],
            'consent': True, 'lease_id': 'synthetic-lease', 'generation': 1} | changes))

    async def finish(self):
        await self.service.task
        return self.service.status()

    async def test_preview_no_inference_or_writes_and_exact_consent(self):
        preview = self.preview()
        self.assertEqual(self.answerer.calls, 0)
        self.assertFalse(self.service.directory.exists())
        self.assertEqual(preview['confirm_sha256'], digest({key: value for key, value in preview.items()
                                                         if key != 'confirm_sha256'}))
        for consent in (False, 1, 'true', None):
            with self.assertRaises(ValueError):
                self.begin(preview, consent=consent)
        for changed in (preview | {'extra': True}, preview | {'question': 'changed'}):
            with self.assertRaises(ValueError):
                self.begin(changed)
        with self.assertRaises(ValueError):
            self.begin(preview, confirm_sha256='0' * 64)
        with self.assertRaises(ValueError):
            self.preview(question='unrelated')
        with self.assertRaises(ValueError):
            self.preview(scope=SCOPE | {'tenant_id': 'foreign'})
        self.assertEqual(self.answerer.calls, 0)

    async def test_bound_private_bundle_and_current_vs_historical(self):
        started = self.begin()
        self.assertTrue(self.service.reserved)
        status = await self.finish()
        self.assertEqual(status['status'], 'ready')
        self.assertFalse(status['real_model'])
        self.assertTrue(status['citation_binding_verified'])
        report = self.service.report(started['answer_id'])
        self.assertEqual(digest(report['bundle']), status['bundle_sha256'])
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['current_source_valid'])
        self.assertTrue(report['current_authority_valid'])
        self.assertFalse(report['semantic_relevance_verified'])
        self.assertEqual(report['bundle']['consent'], {'inference_consent': True, 'storage_consent': True,
                                                     'confirm_sha256': report['preview']['confirm_sha256']})
        self.assertEqual(self.yields, 1)
        self.assertEqual(self.service.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(next(self.service.directory.iterdir()).stat().st_mode & 0o777, 0o600)
        self.review('revoke')
        self.control['generation'] = 2
        report = self.service.report(started['answer_id'])
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['citation_binding_verified'])
        self.assertFalse(report['current_source_valid'])
        self.assertFalse(report['current_authority_valid'])

    async def test_revoked_expired_superseded_and_changed_review_reject_begin(self):
        preview = self.preview()
        self.review('accept')
        with self.assertRaises(ValueError):
            self.begin(preview)
        preview = self.preview()
        self.instant += timedelta(hours=2)
        with self.assertRaises(ValueError):
            self.begin(preview)
        self.instant -= timedelta(hours=2)
        self.publish('Synthetic password successor pending review.', self.document['document_sha256'])
        with self.assertRaises(ValueError):
            self.begin(preview)
        self.assertEqual(self.answerer.calls, 0)

    async def test_revocation_before_gpu_yield_and_during_gpu_yield(self):
        preview = self.preview()
        self.begin(preview)
        self.review('revoke')
        self.assertEqual((await self.finish())['status'], 'failed')
        self.assertEqual(self.yields, 0)
        self.assertEqual(self.answerer.calls, 0)

    async def test_control_drift_during_gpu_yield_calls_no_model(self):
        self.yield_callback = lambda: self.control.update(generation=2)
        self.begin()
        status = await self.finish()
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['error_code'], 'knowledge_answer_failed')
        self.assertEqual(self.answerer.calls, 0)
        self.assertFalse(self.service.directory.exists())

    async def test_revocation_during_gpu_yield_calls_no_model(self):
        self.yield_callback = lambda: self.review('revoke')
        self.begin()
        status = await self.finish()
        self.assertEqual(status['status'], 'failed')
        self.assertFalse(status['model_called'])
        self.assertEqual(self.answerer.calls, 0)

    async def test_dispatch_guard_after_startup_blocks_control_drift(self):
        self.answerer.callback = lambda: self.control.update(generation=2)
        self.begin()
        status = await self.finish()
        self.assertEqual(status['status'], 'failed')
        self.assertFalse(status['model_called'])
        self.assertFalse(status['real_model'])
        self.assertEqual(self.answerer.calls, 0)
        self.assertFalse(self.service.directory.exists())

    async def test_fixture_cannot_claim_real_model(self):
        self.answerer.identity['real_model'] = True
        with self.assertRaisesRegex(ValueError, '^knowledge_answer_deployment_invalid$'):
            self.preview()
        self.assertEqual(self.answerer.calls, 0)

    async def test_thirty_two_sources_bounded_retrieval_and_invalid_limits(self):
        for index in range(31):
            document = self.publish(text='Synthetic password reset source ' + str(index),
                                    source_id='source-' + str(index))
            review = self.store.review_preview(scope=SCOPE, document_sha256=document['document_sha256'],
                                              decision='accept')
            self.store.review(preview=review, confirm_sha256=review['preview_sha256'])
        self.assertEqual(len(self.store.catalog(scope=SCOPE)['documents']), 32)
        preview = self.preview(top_k=4, context_chars=256)
        self.assertLessEqual(len(preview['retrieval']['hits']), 4)
        self.assertLessEqual(preview['retrieval']['used_context_chars'], 256)
        for changes in ({'top_k': 9}, {'top_k': True}, {'context_chars': 255},
                        {'context_chars': 8193}, {'question': ' '}, {'question': 'x' * 513}):
            with self.assertRaises(ValueError):
                self.preview(**changes)

    async def test_revocation_after_model_start_never_persists_output(self):
        self.answerer.release = asyncio.Event()
        self.begin()
        await self.answerer.started.wait()
        self.review('revoke')
        self.answerer.release.set()
        status = await self.finish()
        self.assertEqual(status['status'], 'failed')
        self.assertTrue(status['model_called'])
        self.assertIsNone(status['bundle_sha256'])
        self.assertFalse(self.service.directory.exists())

    async def test_singleflight_cancellation_and_prior_report(self):
        first = self.begin()
        await self.finish()
        self.answerer.started.clear()
        self.answerer.release = asyncio.Event()
        second = self.begin()
        self.assertNotEqual(first['answer_id'], second['answer_id'])
        self.assertEqual(self.service.status(first['answer_id'])['status'], 'ready')
        with self.assertRaises(ValueError):
            self.begin()
        await self.answerer.started.wait()
        cancelled = await self.service.cancel(second['answer_id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertTrue(self.answerer.cleaned)
        self.assertFalse(self.service.reserved)
        self.assertTrue(self.service.report(first['answer_id'])['historical_binding_verified'])
        self.assertIsNone(self.service.report(second['answer_id'])['bundle'])

    async def test_max_eight_answers_and_close(self):
        for index in range(8):
            self.begin()
            self.assertEqual((await self.finish())['status'], 'ready')
        with self.assertRaises(ValueError):
            self.begin()
        self.assertEqual(self.answerer.calls, 8)
        await self.service.close()
        with self.assertRaises(ValueError):
            self.preview()

    async def test_needs_human_and_invalid_model_output(self):
        self.answerer.response = {'schema_version': '1.0', 'needs_human': True, 'quotes': []}
        self.begin()
        self.assertEqual((await self.finish())['status'], 'needs_human')
        self.answerer.response = {'schema_version': '1.0', 'needs_human': False,
                                 'quotes': [{'citation_id': 'c1', 'text': 'fabricated answer'}]}
        started = self.begin()
        self.assertEqual((await self.finish())['status'], 'failed')
        self.assertFalse(self.service.report(started['answer_id'])['historical_binding_verified'])

    async def test_store_directory_replacement_rejected_even_identical_contents(self):
        preview = self.preview()
        old = self.parent / 'old'
        self.store.root.rename(old)
        self.store.root.mkdir(mode=0o700)
        for path in old.iterdir():
            target = self.store.root / path.name
            target.write_bytes(path.read_bytes())
            target.chmod(0o600)
        with self.assertRaises(ValueError):
            self.begin(preview)
        self.assertEqual(self.answerer.calls, 0)

    async def test_bundle_tampering_extra_fields_and_symlinks_fail_closed(self):
        started = self.begin()
        status = await self.finish()
        report = self.service.report(started['answer_id'])
        with self.assertRaises(ValueError):
            validate_answer_bundle(report['bundle'] | {'extra': True}, self.answerer)
        filename = self.service.directory / (status['bundle_sha256'] + '.json')
        raw = filename.read_bytes()
        filename.write_bytes(raw + b' ')
        with self.assertRaisesRegex(ValueError, '^knowledge_answer_bundle_invalid$'):
            self.service.report(started['answer_id'])
        filename.write_bytes(raw)
        target = self.parent / 'private-copy.json'
        target.write_bytes(raw)
        target.chmod(0o600)
        filename.unlink()
        filename.symlink_to(target)
        with self.assertRaises(ValueError):
            self.service.report(started['answer_id'])

    async def test_schema_and_fixture_contracts(self):
        fixture = json.loads((REPO_ROOT / 'examples' / 'knowledge_answer.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertFalse(fixture['responses']['report']['real_model'])
        for operation, request in fixture['requests'].items():
            REQUESTS[operation].model_validate(request)
            validator('knowledge_answer_' + operation + '_request').validate(request)
            schema = json.loads((REPO_ROOT / 'schemas' /
                ('knowledge_answer_' + operation + '_request.schema.json')).read_text())
            self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                             REQUESTS[operation].model_json_schema())
        for operation, response in fixture['responses'].items():
            RESPONSES[operation].model_validate(response)
            validator('knowledge_answer_' + operation + '_response').validate(response)
            schema = json.loads((REPO_ROOT / 'schemas' /
                ('knowledge_answer_' + operation + '_response.schema.json')).read_text())
            self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                             RESPONSES[operation].model_json_schema())
        report = fixture['responses']['report']
        self.assertEqual(digest(report['bundle']), report['bundle_sha256'])
        validate_answer_bundle(report['bundle'], FixtureAnswerer())

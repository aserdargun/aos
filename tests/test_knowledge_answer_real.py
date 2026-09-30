import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from aos.contracts import REPO_ROOT, canonical
from aos.knowledge import KnowledgeStore
from aos.knowledge_answer import KnowledgeAnswerService
from aos.knowledge_answer_model import BonsaiKnowledgeAnswerer


@unittest.skipUnless(os.environ.get('AOS_KNOWLEDGE_ANSWER_REAL_TESTS') == '1',
                     'Requires an explicitly isolated idle GPU window and pinned local Bonsai; synthetic corpus only')
class KnowledgeAnswerRealTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_bonsai_extracts_independent_english_turkish_facts_and_abstains(self):
        root = Path(tempfile.mkdtemp(prefix='knowledge-answer-real-', dir=REPO_ROOT / 'data'))
        os.chmod(root, 0o700)
        store = KnowledgeStore(root / 'knowledge')
        scope = {'application_id': 'synthetic-evaluation', 'tenant_id': 'synthetic', 'account_role': 'reader'}
        cases = [
            {'source': 'password', 'text': 'Synthetic application manual. To reset a password, open Settings and select Reset password.',
             'question': 'How do I reset a password?', 'expected': 'open Settings and select Reset password', 'abstain': False},
            {'source': 'iade', 'text': 'Sentetik uygulama kılavuzu. İade talebi, teslim tarihinden sonraki 14 gün içinde açılmalıdır.',
             'question': 'İade talebi kaç gün içinde açılmalıdır?', 'expected': '14 gün', 'abstain': False},
            {'source': 'billing', 'text': 'Synthetic billing manual. Invoice copies are available on the Billing page. No refund timing is documented.',
             'question': 'How many days does a billing refund take?', 'expected': None, 'abstain': True},
        ]
        for case in cases:
            preview = store.publish_preview(scope=scope, source_id=case['source'], title='Synthetic ' + case['source'],
                text=case['text'], previous_sha256=None, expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                rights_attested=True, storage_consent=True, synthetic=True)
            inspection = store.publish(preview=preview, confirm_sha256=preview['preview_sha256'])
            review = store.review_preview(scope=scope, document_sha256=inspection['document_sha256'], decision='accept')
            store.review(preview=review, confirm_sha256=review['preview_sha256'])
        answerer = BonsaiKnowledgeAnswerer(REPO_ROOT / 'models/bonsai-manifest.json')
        authority = {'session_id': 'synthetic-real-knowledge', 'lease_id': 'synthetic-lease', 'generation': 1}

        def current(lease_id, generation):
            if (lease_id, generation) != (authority['lease_id'], authority['generation']):
                raise ValueError('synthetic_control_changed')
            return dict(authority)

        async def yield_gpu():
            await asyncio.sleep(0)

        service = KnowledgeAnswerService(store, answerer, root / 'answers', current, yield_gpu)
        results = []
        started = time.perf_counter()
        try:
            for case in cases:
                preview = service.preview(scope, case['question'], 4, 4096, authority['lease_id'], authority['generation'])
                status = service.begin(preview, preview['confirm_sha256'], True, authority['lease_id'], authority['generation'])
                await service.task
                report = service.report(status['answer_id'])
                self.assertIn(report['status'], ('ready', 'needs_human'), report['error_code'])
                self.assertTrue(report['real_model'])
                self.assertTrue(report['model_called'])
                self.assertTrue(report['historical_binding_verified'])
                self.assertTrue(report['current_source_valid'])
                self.assertTrue(report['citation_binding_verified'])
                self.assertFalse(report['semantic_relevance_verified'])
                self.assertFalse(report['execution_authorized'])
                self.assertFalse(report['gold'])
                self.assertFalse(report['training_ready'])
                selection = report['model_response']
                if case['abstain']:
                    self.assertTrue(selection['needs_human'])
                    self.assertEqual(selection['quotes'], [])
                else:
                    self.assertFalse(selection['needs_human'])
                    self.assertTrue(any(case['expected'] in quote['text'] for quote in selection['quotes']))
                    source_ids = [preview['retrieval']['hits'][int(quote['citation_id'][1:]) - 1]['source_id'] for quote in selection['quotes']]
                    self.assertIn(case['source'], source_ids)
                results.append({'source': case['source'], 'answer_id': status['answer_id'],
                    'bundle_sha256': report['bundle_sha256'], 'status': report['status'], 'real_model': True,
                    'citation_binding_verified': True, 'independent_synthetic_case_passed': True})
            summary = {'schema_version': '1.0', 'synthetic': True, 'deployment': answerer.identity,
                'cases': results, 'elapsed_seconds': time.perf_counter() - started,
                'execution_authorized': False, 'gold': False, 'training_ready': False,
                'general_quality_verified': False, 'real_site_acceptance': False}
            descriptor = os.open(root / 'acceptance.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as output:
                output.write(canonical(summary))
            print(json.dumps({'private_acceptance': str(root), 'cases_passed': len(results), 'real_bonsai': True}))
        finally:
            await service.close()

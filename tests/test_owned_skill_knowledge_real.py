import json
import os
from pathlib import Path
import time
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.owned_skill_planner import BonsaiOwnedSkillPlanner


@unittest.skipUnless(os.environ.get('AOS_OWNED_SKILL_KNOWLEDGE_REAL_TESTS') == '1',
                     'Requires explicitly reserved idle GPU and pinned Bonsai; synthetic admitted skill, no execution')
class OwnedSkillKnowledgeRealTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_s2_context_dispatch_english_turkish_and_revocation(self):
        import tempfile
        from test_owned_skill_knowledge_service import make_fixture

        root = Path(tempfile.mkdtemp(prefix='owned-skill-knowledge-real-', dir=REPO_ROOT / 'data'))
        root.chmod(0o700)
        planner = BonsaiOwnedSkillPlanner(REPO_ROOT / 'models/bonsai-manifest.json', knowledge_context=True)
        scheduler, store, service, arguments = make_fixture(root, planner)
        planning = scheduler.owned_skill_planning
        results = []
        reports = []
        started = time.perf_counter()
        try:
            for goal in ('Save message "gamma"', 'Mesaj alanına "delta" kaydet'):
                arguments = {**arguments, 'goal': goal}
                preview = service.preview(**arguments)
                before = planning.calls
                status = service.begin(schema_version='1.0', preview=preview,
                    confirm_sha256=preview['confirm_sha256'], inference_consent=True, storage_consent=True,
                    lease_id=arguments['lease_id'], generation=arguments['generation'])
                await planning.task
                self.assertEqual(planning.calls, before + 1)
                self.assertEqual(planning.status()['status'], 'ready', planning.status())
                report = service.report(planning.status()['bundle_sha256'])
                reports.append(report)
                bundle = report['bundle']
                self.assertEqual(bundle['planning_id'], status['planning_id'])
                self.assertEqual(bundle['model_request'], planner.last_request)
                self.assertEqual(bundle['model_response'], planner.last_response)
                self.assertEqual(digest(bundle), report['planning_bundle_sha256'])
                self.assertTrue(report['real_model'] and report['model_request_verified'] and report['knowledge_applied'])
                self.assertTrue(report['historical_binding_verified'] and report['current_source_valid'])
                self.assertTrue(report['current_authority_valid'] and report['dispatch_recorded'])
                self.assertFalse(report['downstream_verified'] or report['training_ready'] or report['gold'])
                self.assertFalse(report['execution_authorized'] or report['causality_verified'])
                self.assertEqual(bundle['model_response']['parameter_value'], 'gamma' if 'gamma' in goal else 'delta')
                self.assertEqual(bundle['model_response']['steps'], bundle['evidence'][0]['ordered_steps'])
                payload = json.loads(bundle['model_request']['messages'][1]['content'])
                self.assertEqual(payload['reviewed_documents']['context_sha256'], preview['context_sha256'])
                self.assertEqual(payload['reviewed_documents']['context_text'], preview['context_text'])
                path = root / ('audit-' + str(len(results) + 1) + '.json')
                with path.open('x') as stream:
                    os.chmod(path, 0o600)
                    stream.write(canonical(report))
                results.append({'goal_language': 'en' if 'gamma' in goal else 'tr',
                                'bundle_sha256': report['planning_bundle_sha256'],
                                'real_model': True, 'request_verified': True,
                                'decision': bundle['model_response']['decision'],
                                'metrics': bundle['metrics']})
            if os.environ.get('AOS_UI_TESTS') == '1':
                from owned_skill_knowledge_ui_validator import validate_native_reports
                await validate_native_reports(reports)
            review = store.review_preview(scope=arguments['scope'],
                document_sha256=scheduler.synthetic_document['document_sha256'], decision='revoke')
            store.review(preview=review, confirm_sha256=review['preview_sha256'])
            historical = service.report(planning.status()['bundle_sha256'])
            self.assertTrue(historical['knowledge_applied'] and historical['historical_binding_verified'])
            self.assertFalse(historical['current_source_valid'])
            self.assertTrue(historical['current_authority_valid'])
            with self.assertRaises(ValueError):
                planning.select(planning.status()['bundle_sha256'], arguments['lease_id'], arguments['generation'])
        finally:
            await planning.close()
        result = {'synthetic': True, 'scope': 'native Bonsai/model-service; synthetic controller/admitted skill, no S1/task effect',
                  'cases': results, 'revocation_denied_selection': True,
                  'browser_record_validation': os.environ.get('AOS_UI_TESTS') == '1',
                  'wall_seconds': time.perf_counter() - started, 'execution_verified': False,
                  'training_ready': False, 'causality_verified': False}
        path = root / 'acceptance.json'
        with path.open('x') as stream:
            os.chmod(path, 0o600)
            stream.write(canonical(result))
        print('Native S2 context acceptance: ' + str(path.relative_to(REPO_ROOT)), flush=True)

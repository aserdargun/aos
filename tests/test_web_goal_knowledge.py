from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aos.contracts import AOSFault, REPO_ROOT, canonical, digest
from aos.owned_skill_knowledge import (CONTEXT_PROTOCOL_PIN, CONTEXT_SCHEMA_PIN,
    CONTEXT_VERSION, planning_knowledge_schema_sha256, build_planning_knowledge_context)
from aos.knowledge import text_sha256
from aos.web_goal_planner import WebGoalCatalog
from aos.web_goal_planning import WebGoalPlanning, validate_web_goal_knowledge_review
from test_web_goal_planner import fixture_planner, fixtures, response_for


def knowledge_inputs():
    case = fixtures()['cases'][0]
    catalog = WebGoalCatalog.model_validate(fixtures()['catalogs'][case['catalog']])
    planner = fixture_planner()
    planner.pins.update({CONTEXT_PROTOCOL_PIN: CONTEXT_VERSION,
                        CONTEXT_SCHEMA_PIN: planning_knowledge_schema_sha256()})
    planner.identity = {'deployment_id': 'fixture-' + digest(planner.pins),
                        'pins': deepcopy(planner.pins), 'real_model': False}
    original = json.loads((REPO_ROOT / 'examples/owned_skill_knowledge.json').read_text())['responses']['preview']
    retrieval = deepcopy(original['retrieval'])
    context = build_planning_knowledge_context(retrieval, original['query'])
    authority = {'runtime_id': 'synthetic-runtime', 'lease_id': 'synthetic-lease', 'generation': 2}
    review = {'schema_version': '1.0', 'kind': 'web_goal_knowledge_review',
        'goal': case['goal'], 'authority': authority, 'catalog_sha256': digest(catalog.model_dump(mode='json')),
        'deployment_sha256': digest(planner.identity), 'query': original['query'], 'retrieval': retrieval,
        'workspace_identity': original['workspace_identity'], 'store_identity': original['store_identity'],
        'payload': {'context_version': CONTEXT_VERSION, 'context_sha256': text_sha256(context),
                    'context_text': context, 'untrusted': True},
        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    review['confirm_sha256'] = digest(review)
    return case, catalog, planner, authority, validate_web_goal_knowledge_review(review)


class WebGoalKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_review_intent_actual_dispatch_and_source_revalidation(self):
        case, catalog, planner, authority, review = knowledge_inputs()
        current = [True]
        checks = []

        def check(supplied):
            checks.append(deepcopy(supplied))
            if not current[0]:
                raise ValueError('synthetic source revoked')

        with tempfile.TemporaryDirectory() as directory:
            service = WebGoalPlanning(planner, Path(directory) / 'proposals',
                lambda lease, generation: (deepcopy(authority), catalog), AsyncMock())
            service.knowledge_current = check

            async def native(instance, goal, evidence, *, request_context):
                instance.before_model_call()
                instance.last_request = instance.request_body(goal, evidence, request_context=request_context)
                instance.last_response = response_for(case, catalog)
                return instance.parse_response(canonical(instance.last_response), evidence)

            with patch('aos.web_goal_planner.BonsaiSupervisor.plan', native):
                service.begin(case['goal'], authority['lease_id'], 2,
                    confirm_catalog_sha256=review['catalog_sha256'], inference_consent=True,
                    knowledge_review=review, confirm_knowledge_sha256=review['confirm_sha256'], storage_consent=True)
                await service.task
            self.assertEqual(service.status()['status'], 'ready')
            checksum = service.status()['bundle_sha256']
            bundle = service.load(checksum)
            self.assertEqual(bundle['schema_version'], '1.1')
            self.assertEqual(bundle['knowledge']['review'], review)
            self.assertEqual(service.load(bundle['intent_sha256'])['stage'], 'intent')
            self.assertEqual(json.loads(bundle['model_request']['messages'][1]['content'])['reviewed_documents'], review['payload'])
            self.assertGreaterEqual(len(checks), 5)
            self.assertFalse(bundle['execution_authorized'] or bundle['training_ready'])
            service.select(checksum, authority['lease_id'], 2)
            current[0] = False
            with self.assertRaises(ValueError):
                service.select(checksum, authority['lease_id'], 2)
            self.assertEqual(service.load(checksum)['knowledge']['review'], review)

    async def test_missing_permission_checker_or_capability_never_calls_model(self):
        case, catalog, planner, authority, review = knowledge_inputs()
        with tempfile.TemporaryDirectory() as directory:
            service = WebGoalPlanning(planner, Path(directory) / 'proposals',
                lambda lease, generation: (authority, catalog), AsyncMock())
            arguments = dict(confirm_catalog_sha256=review['catalog_sha256'], inference_consent=True,
                knowledge_review=review, confirm_knowledge_sha256=review['confirm_sha256'], storage_consent=True)
            with self.assertRaises(ValueError):
                service.begin(case['goal'], authority['lease_id'], 2, **arguments)
            service.knowledge_current = lambda supplied: None
            for overrides in ({'storage_consent': False}, {'storage_consent': 1},
                              {'confirm_knowledge_sha256': '0' * 64}):
                with self.assertRaises(ValueError):
                    service.begin(case['goal'], authority['lease_id'], 2, **(arguments | overrides))
            planner.pins.pop(CONTEXT_PROTOCOL_PIN)
            with self.assertRaises(ValueError):
                service.begin(case['goal'], authority['lease_id'], 2, **arguments)
            self.assertEqual(service.calls, 0)
            self.assertFalse(service.directory.exists())

    async def test_revocation_before_dispatch_and_missing_dispatch_acknowledgement_fail(self):
        case, catalog, planner, authority, review = knowledge_inputs()
        evidence = [catalog.model_dump(mode='json')]
        valid = [True]

        def current():
            if not valid[0]:
                raise ValueError('synthetic revoked source')
            return review['payload']

        async def revoked(instance, goal, supplied, *, request_context):
            valid[0] = False
            instance.before_model_call()
            self.fail('revoked source cannot dispatch')

        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', revoked), self.assertRaises(ValueError):
            await planner.plan(case['goal'], evidence, inference_consent=True,
                current_catalog=lambda: catalog, request_context=review['payload'], current_knowledge=current)
        self.assertFalse(planner._planning)
        self.assertFalse(hasattr(planner, 'before_model_call'))
        valid[0] = True

        async def missing(instance, goal, supplied, *, request_context):
            instance.before_model_call()
            instance.last_request = None
            instance.last_response = None
            return instance.parse_response(canonical(response_for(case, catalog)), supplied)

        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', missing), self.assertRaises(AOSFault):
            await planner.plan(case['goal'], evidence, inference_consent=True,
                current_catalog=lambda: catalog, request_context=review['payload'], current_knowledge=current)

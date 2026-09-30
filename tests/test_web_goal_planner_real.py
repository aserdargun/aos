import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.web_goal_planner import BonsaiWebGoalPlanner, WebGoalCatalog


@unittest.skipUnless(os.environ.get('AOS_WEB_GOAL_PLANNER_REAL_TESTS') == '1',
                     'Explicit idle GPU reservation; real Bonsai, synthetic proposal-only catalogs')
class WebGoalPlannerRealTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_application_goals_parameters_and_negation(self):
        example = json.loads((REPO_ROOT / 'examples/web_goal_planning.json').read_text())
        planner = BonsaiWebGoalPlanner(REPO_ROOT / 'models/bonsai-manifest.json')
        directory = Path(tempfile.mkdtemp(prefix='web-goal-planner-real-', dir=REPO_ROOT / 'data'))
        directory.chmod(0o700)
        print('Private native goal proposal evidence: ' + str(directory.relative_to(REPO_ROOT)), flush=True)
        cases = []
        started = time.perf_counter()
        for index, case in enumerate(example['cases']):
            catalog = WebGoalCatalog.model_validate(example['catalogs'][case['catalog']])
            evidence = [catalog.model_dump(mode='json')]
            plan = await planner.plan(case['goal'], evidence, inference_consent=True,
                current_catalog=lambda: catalog)
            record = {'synthetic': True, 'goal': case['goal'], 'deployment': planner.identity,
                'catalog': evidence[0], 'request': planner.last_request,
                'response': planner.last_response, 'metrics': planner.last_metrics,
                'execution_verified': False, 'source_admission_verified': False,
                'training_ready': False, 'causality_verified': False}
            with (directory / ('case-' + str(index + 1) + '.json')).open('x') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(canonical(record))
            self.assertEqual(plan.decision, case['decision'], plan)
            self.assertEqual(plan.skill_ref, case['skill_ref'])
            self.assertEqual(plan.parameters, case['parameters'])
            self.assertEqual(plan.catalog_sha256, digest(evidence[0]))
            self.assertFalse(plan.execution_authorized or plan.scope_authorization_verified)
            self.assertEqual(planner.last_request, planner.request_body(case['goal'], evidence))
            self.assertEqual(planner.last_response, plan.model_dump(mode='json'))
            cases.append({'application': catalog.application_key, 'decision': plan.decision,
                          'skill_ref': plan.skill_ref, 'record_sha256': digest(record)})
        result = {'synthetic': True, 'scope': 'native Bonsai proposals; synthetic catalogs, no executor or profile admission',
            'cases': cases, 'wall_seconds': time.perf_counter() - started,
            'execution_verified': False, 'training_ready': False, 'causality_verified': False}
        with (directory / 'acceptance.json').open('x') as stream:
            os.chmod(stream.name, 0o600)
            stream.write(canonical(result))
        print('Native generic web-goal proposal acceptance: ' + str(directory.relative_to(REPO_ROOT) / 'acceptance.json'), flush=True)

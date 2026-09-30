import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from aos.contracts import AOSFault, REPO_ROOT, canonical, digest
from aos.web_goal_planner import BonsaiWebGoalPlanner, WebGoalCatalog, WebGoalParameter, WebGoalPlan, _prompt, proposal_schema


def fixtures():
    return json.loads((REPO_ROOT / 'examples/web_goal_planning.json').read_text())


def fixture_planner():
    planner = BonsaiWebGoalPlanner.__new__(BonsaiWebGoalPlanner)
    planner.pins = {'temperature': 0.0, 'max_output_tokens': 1024,
        'web_goal_prompt_sha256': digest(_prompt()),
        'web_goal_plan_schema_sha256': digest(json.loads((REPO_ROOT / 'schemas/web_goal_plan.schema.json').read_text()))}
    planner.identity = {'deployment_id': 'fixture-' + digest(planner.pins), 'real_model': False}
    planner._planning = False
    return planner


def response_for(case, catalog):
    return {'schema_version': '1.0', 'catalog_sha256': digest(catalog.model_dump(mode='json')),
        'decision': case['decision'], 'skill_ref': case['skill_ref'], 'parameters': deepcopy(case['parameters']),
        'reason_code': 'skill_match' if case['decision'] == 'propose_skill' else 'unsafe_goal',
        'execution_authorized': False, 'activation_authorized': False, 'training_ready': False,
        'scope_authorization_verified': False}


class WebGoalPlannerTests(unittest.TestCase):
    def test_two_application_catalogs_and_five_synthetic_oracles(self):
        example = fixtures()
        planner = fixture_planner()
        for case in example['cases']:
            with self.subTest(goal=case['goal']):
                catalog = WebGoalCatalog.model_validate(example['catalogs'][case['catalog']])
                plan = response_for(case, catalog)
                Draft202012Validator(proposal_schema(catalog)).validate(plan)
                parsed = planner.parse_response(canonical(plan), [catalog.model_dump(mode='json')])
                self.assertEqual(parsed.parameters, case['parameters'])
                request = planner.request_body(case['goal'], [catalog.model_dump(mode='json')])
                self.assertEqual(json.loads(request['messages'][1]['content'])['goal'], case['goal'])
                self.assertFalse(parsed.execution_authorized or parsed.training_ready)

    def test_foreign_skill_scope_hash_parameters_and_authority_fail_closed(self):
        example = fixtures()
        catalog = WebGoalCatalog.model_validate(example['catalogs']['contact_notes'])
        other = WebGoalCatalog.model_validate(example['catalogs']['inventory_notes'])
        original = response_for(example['cases'][0], catalog)
        for changed in (original | {'catalog_sha256': digest(other.model_dump(mode='json'))},
                        original | {'skill_ref': 'annotate_item'},
                        original | {'parameters': {'contact_name': 'Ada'}},
                        original | {'parameters': original['parameters'] | {'url': 'https://unapproved.invalid'}},
                        original | {'parameters': original['parameters'] | {'note': 'a' * 513}},
                        original | {'execution_authorized': True}, original | {'training_ready': 0},
                        original | {'decision': 'needs_human'},
                        original | {'parameters': original['parameters'] | {'note': 'bad\nvalue'}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                fixture_planner().parse_response(canonical(changed), [catalog.model_dump(mode='json')])
        priority = response_for(example['cases'][1], catalog)
        priority['parameters']['priority'] = 'urgent'
        with self.assertRaises(ValueError):
            fixture_planner().parse_response(canonical(priority), [catalog.model_dump(mode='json')])

    def test_duplicate_response_keys_and_missing_false_flags_are_rejected(self):
        example = fixtures()
        catalog = WebGoalCatalog.model_validate(example['catalogs']['contact_notes'])
        plan = response_for(example['cases'][0], catalog)
        for value in ('{"decision":"needs_human",' + canonical(plan)[1:],
                      canonical({key: value for key, value in plan.items() if key != 'execution_authorized'})):
            with self.assertRaises(ValueError):
                fixture_planner().parse_response(value, [catalog.model_dump(mode='json')])

    def test_catalog_bounds_duplicates_and_schema_pin(self):
        example = fixtures()
        catalog = deepcopy(example['catalogs']['contact_notes'])
        for changed in (catalog | {'skills': []}, catalog | {'skills': catalog['skills'] * 2},
                        catalog | {'skills': list(reversed(catalog['skills']))},
                        catalog | {'profile_sha256': 'bad'}, catalog | {'scope_authorization_verified': 0}):
            with self.assertRaises(ValueError):
                WebGoalCatalog.model_validate(changed)
        with self.assertRaises(ValueError):
            WebGoalParameter(description='Exact value', min_chars=9, max_chars=3)
        planner = fixture_planner()
        planner.pins['web_goal_plan_schema_sha256'] = '0' * 64
        with self.assertRaises(AOSFault):
            planner.request_body('Any text goal', [catalog])
        planner = fixture_planner()
        planner.pins['web_goal_prompt_sha256'] = '0' * 64
        with self.assertRaises(AOSFault):
            planner.request_body('Any text goal', [catalog])
        for goal in ('', ' ', 'a' * 4097, 'unsafe\ntext', '\ud800'):
            with self.assertRaises(ValueError):
                fixture_planner().request_body(goal, [catalog])


class WebGoalPlannerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_mutation_cannot_replace_dispatched_catalog(self):
        example = fixtures()
        catalog = WebGoalCatalog.model_validate(example['catalogs']['contact_notes'])
        evidence = [catalog.model_dump(mode='json')]
        planner = fixture_planner()

        async def native(instance, goal, supplied):
            evidence[0]['application_key'] = 'foreign-app'
            evidence[0]['skills'][0]['description'] = 'Changed by another caller'
            instance.before_model_call()
            self.assertEqual(supplied, [catalog.model_dump(mode='json')])
            return instance.parse_response(canonical(response_for(example['cases'][0], catalog)), supplied)

        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', native):
            result = await planner.plan(example['cases'][0]['goal'], evidence,
                inference_consent=True, current_catalog=lambda: catalog)
        self.assertEqual(result.catalog_sha256, digest(catalog.model_dump(mode='json')))

    async def test_consent_source_checks_and_original_hook_are_preserved(self):
        example = fixtures()
        catalog = WebGoalCatalog.model_validate(example['catalogs']['contact_notes'])
        evidence = [catalog.model_dump(mode='json')]
        planner = fixture_planner()
        calls = []
        planner.before_model_call = lambda: calls.append('original')

        def current():
            calls.append('fresh')
            return catalog

        async def native(instance, goal, supplied):
            calls.append('native')
            instance.before_model_call()
            return instance.parse_response(canonical(response_for(example['cases'][0], catalog)), supplied)

        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', native):
            for consent in (False, 1, None):
                with self.assertRaises(AOSFault):
                    await planner.plan(example['cases'][0]['goal'], evidence,
                                       inference_consent=consent, current_catalog=current)
            result = await planner.plan(example['cases'][0]['goal'], evidence,
                                       inference_consent=True, current_catalog=current)
        self.assertEqual(result.skill_ref, 'add_note')
        self.assertEqual(calls, ['fresh', 'native', 'original', 'fresh', 'fresh'])
        self.assertFalse(planner._planning)
        planner.before_model_call()
        self.assertEqual(calls[-1], 'original')

    async def test_drift_before_dispatch_after_response_and_cancellation_release(self):
        example = fixtures()
        catalog = WebGoalCatalog.model_validate(example['catalogs']['contact_notes'])
        foreign = WebGoalCatalog.model_validate(example['catalogs']['inventory_notes'])
        evidence = [catalog.model_dump(mode='json')]
        for drift_at in (1, 2, 3):
            with self.subTest(drift_at=drift_at):
                planner = fixture_planner()
                checks = []

                def current():
                    checks.append(True)
                    return foreign if len(checks) == drift_at else catalog

                async def native(instance, goal, supplied):
                    instance.before_model_call()
                    return WebGoalPlan.model_validate(response_for(example['cases'][0], catalog))

                with patch('aos.web_goal_planner.BonsaiSupervisor.plan', native), self.assertRaises(AOSFault):
                    await planner.plan(example['cases'][0]['goal'], evidence,
                                       inference_consent=True, current_catalog=current)
                self.assertFalse(planner._planning)
                self.assertFalse(hasattr(planner, 'before_model_call'))
        entered = asyncio.Event()
        planner = fixture_planner()
        planner.before_model_call = None

        async def waiting(instance, goal, supplied):
            entered.set()
            await asyncio.Future()

        with patch('aos.web_goal_planner.BonsaiSupervisor.plan', waiting):
            task = asyncio.create_task(planner.plan(example['cases'][0]['goal'], evidence,
                inference_consent=True, current_catalog=lambda: catalog))
            await entered.wait()
            with self.assertRaises(AOSFault):
                await planner.plan(example['cases'][0]['goal'], evidence,
                    inference_consent=True, current_catalog=lambda: catalog)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(planner._planning)
        self.assertTrue(hasattr(planner, 'before_model_call'))
        self.assertIsNone(planner.before_model_call)

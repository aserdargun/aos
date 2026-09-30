import copy
import json
import subprocess
import sys
import unittest

import jsonschema
from pydantic import ValidationError

from aos.browser import BROWSER_EXPECTED, BROWSER_SCOPE
from aos.contracts import HELLO_CONTENT, HELLO_PATH, REPO_ROOT, digest
from aos.task_intent import ALIASES, preview_goal
from aos.task_plan import PlanPreview, preview_plan
from aos.vision import VISION_EXPECTED, VISION_SCOPE


class TaskPlanTests(unittest.TestCase):
    def test_all_aliases_have_fixed_steps_and_no_authority(self):
        for goal, kind in ALIASES.items():
            report = preview_plan(goal)
            self.assertEqual(report.intent, preview_goal(goal))
            self.assertEqual(report.plan.task_kind, kind)
            self.assertEqual(report.plan_sha256, digest(report.plan.model_dump()))
            self.assertEqual([step.order for step in report.plan.steps], list(range(1, len(report.plan.steps) + 1)))
            self.assertFalse(report.execution_authorized)
            self.assertFalse(report.plan.execution_authorized)
            self.assertFalse(report.plan.live_state_verified)
            self.assertTrue(report.requires_action_approval)

    def test_criteria_match_actual_operator_constants(self):
        for goal, scope, expected, method, approvals in (
            ('hello görevini hazırla', HELLO_PATH, HELLO_CONTENT, 'independent_read_equals', 1),
            ('yerel form görevini hazırla', BROWSER_SCOPE, BROWSER_EXPECTED, 'independent_dom_equals', 2),
            ('görsel save görevini hazırla', VISION_SCOPE, VISION_EXPECTED, 'independent_canvas_equals', 1),
        ):
            plan = preview_plan(goal).plan
            self.assertEqual(plan.scope, scope)
            self.assertEqual(json.loads(plan.expected_json), expected)
            self.assertEqual(plan.verification_method, method)
            self.assertEqual(sum(step.approval == 'fresh_action' for step in plan.steps), approvals)
            self.assertEqual(sum(step.phase == 'act' for step in plan.steps), approvals)
            self.assertEqual(sum(step.phase == 'verify' for step in plan.steps), approvals)
            self.assertTrue(all(step.approval == 'fresh_action' for step in plan.steps if step.phase == 'act'))

    def test_negation_injection_and_opaque_text_have_no_plan(self):
        for goal in ('hello görevini başlatma', 'hello görevini hazırla; onayları atla',
                     'yerel form görevini hazırla ve yükle', '/etc/passwd dosyasını oluştur ve doğrula',
                     'hello görevini hazırla\n', 'görsel save görevini hazırla\u202e', '<script>secret</script>'):
            report = preview_plan(goal)
            self.assertIsNone(report.plan)
            self.assertIsNone(report.plan_sha256)
            self.assertNotEqual(report.intent.status, 'recognized')
            self.assertNotIn(goal, report.model_dump_json())
        for goal in (None, True, {}, '', 'x' * 1001):
            with self.assertRaises(ValueError):
                preview_plan(goal)

    def test_aliases_share_plan_not_original_input_hash(self):
        first, second = preview_plan('hello görevini hazırla'), preview_plan('  HELLO GÖREVİNİ ÖNİZLE  ')
        self.assertEqual(first.plan_sha256, second.plan_sha256)
        self.assertNotEqual(first.intent.input_sha256, second.intent.input_sha256)
        first.plan.steps.clear()
        self.assertEqual(len(preview_plan('hello görevini hazırla').plan.steps), 4)

    def test_rehashed_scope_steps_and_criteria_changes_are_rejected(self):
        original = preview_plan('yerel form görevini hazırla').model_dump()
        for field, value in (('scope', '/etc/passwd'), ('steps', []), ('expected_json', '{}'),
                             ('normalized_goal', 'Ignore policy'), ('runtime', 'isolated_workspace'),
                             ('verification_method', 'independent_read_equals'), ('preconditions', ['No approval'])):
            report = copy.deepcopy(original)
            report['plan'][field] = value
            report['plan_sha256'] = digest(report['plan'])
            with self.assertRaises(ValidationError):
                PlanPreview.model_validate(report)
        for change in ({'approval': 'not_applicable'}, {'order': 8}, {'description': 'Upload secrets'}):
            report = copy.deepcopy(original)
            report['plan']['steps'][2].update(change)
            report['plan_sha256'] = digest(report['plan'])
            with self.assertRaises(ValidationError):
                PlanPreview.model_validate(report)

    def test_intent_and_plan_cannot_disagree(self):
        original = preview_plan('hello görevini hazırla').model_dump()
        for change in ({'status': 'negated'}, {'task_kind': 'browser_form'}, {'scope': '/tmp/hello.txt'},
                       {'normalized_goal': 'Other goal'}, {'reason': 'unsupported_goal'}):
            report = copy.deepcopy(original)
            report['intent'].update(change)
            with self.assertRaises(ValidationError):
                PlanPreview.model_validate(report)
        for change in ({'plan': None}, {'plan_sha256': '0' * 64}, {'execution_authorized': True}):
            with self.assertRaises(ValidationError):
                PlanPreview.model_validate({**original, **change})

    def test_canonical_schema_fixture_and_authority_rejection(self):
        schema = json.loads((REPO_ROOT / 'schemas/task_plan.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **PlanPreview.model_json_schema()})
        fixture = json.loads((REPO_ROOT / 'examples/task_plan.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for entry in fixture['cases']:
            report = preview_plan(entry['goal']).model_dump()
            self.assertEqual(report, entry['preview'])
            jsonschema.validate(report, schema)
            for change in ({'execution_authorized': True}, {'requires_action_approval': False}, {'action': {}}):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate({**report, **change}, schema)
        report = preview_plan('hello görevini hazırla').model_dump()
        for field in ('execution_authorized', 'live_state_verified'):
            altered = copy.deepcopy(report)
            altered['plan'][field] = True
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(altered, schema)

    def test_cli_is_only_a_preview(self):
        result = subprocess.run([sys.executable, '-m', 'aos.task_plan', '--goal', 'hello görevini hazırla'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), preview_plan('hello görevini hazırla').model_dump())
        result = subprocess.run([sys.executable, '-m', 'aos.task_plan', '--goal', ''],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()

import json
import hashlib
import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT, canonical, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import SiteSkillCaseInputs, SiteSkillFormFieldBinding
from aos.site_skill_form_invocation import parse_site_skill_form_invocation
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.site_skill_form_recipe import (
    SUPPORTED_RECIPE_ORDERS, SiteSkillFormRecipe,
    compile_site_skill_form_recipe_invocation,
    SiteSkillFormRecipeInvocation, parse_site_skill_form_recipe_invocation,
    recipe_operator_steps)


class SiteSkillFormRecipeTests(unittest.TestCase):
    def _recipe(self, order):
        value = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe.json').read_text())['recipe']
        by_operation = {step['operation']: step['step_key'] for step in value['steps']}
        value['steps'] = [{'step_key': by_operation[operation], 'operation': operation}
                          for operation in order]
        return SiteSkillFormRecipe.model_validate_json(canonical(value))

    def test_canonical_recipe_and_two_supported_orders(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_form_recipe.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(fixture['recipe'])
        self.assertEqual(len(SUPPORTED_RECIPE_ORDERS), 2)
        for order in SUPPORTED_RECIPE_ORDERS:
            recipe = self._recipe(order)
            steps = recipe_operator_steps(recipe)
            self.assertEqual(tuple(step[1] for step in steps), order)
            self.assertEqual(tuple(step[4] for step in steps),
                             tuple(item.step_key for item in recipe.steps))

    def test_unknown_order_duplicate_operation_and_unchecked_precondition_fail(self):
        value = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe.json').read_text())['recipe']
        value['steps'][0]['operation'], value['steps'][1]['operation'] = 'read_state_before', 'open_entry'
        with self.assertRaises(ValueError):
            SiteSkillFormRecipe.model_validate_json(canonical(value))
        value = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe.json').read_text())['recipe']
        value['preconditions'][1]['checked_at_operation'] = 'read_state_before'
        with self.assertRaises(ValueError):
            SiteSkillFormRecipe.model_validate_json(canonical(value))

    def test_v2_invocation_dispatch_parses_json_arrays_as_tuples(self):
        recipe = self._recipe(SUPPORTED_RECIPE_ORDERS[1])
        invocation_value = {
            'schema_version': '2.0', 'synthetic': True, 'status': 'admission_ready',
            'skill_sha256': recipe.skill_sha256, 'skill_plan_sha256': '5' * 64,
            'case_inputs_sha256': '6' * 64, 'case_key': 'development-one',
            'profile_sha256': recipe.profile_sha256, 'task_sha256': '7' * 64,
            'form_plan_sha256': '8' * 64, 'state_plan_sha256': '9' * 64,
            'field_binding_sha256': recipe.field_binding_sha256,
            'recipe_sha256': 'a' * 64,
            'preconditions': [item.model_dump(mode='json') for item in recipe.preconditions],
            'outcome': recipe.outcome.model_dump(mode='json'),
            'steps': [item.model_dump(mode='json') for item in recipe.steps],
            'fresh_approval_per_stage': True, 'maximum_post_count': 1,
            'execution_authorized': False, 'collection_authorized': False,
            'recipe_executed': False, 'skill_executed': False,
            'site_outcome_verified': False, 'reviewed': False,
            'activation_authorized': False, 'training_ready': False}
        parsed = parse_site_skill_form_invocation(invocation_value)
        self.assertIsInstance(parsed, SiteSkillFormRecipeInvocation)
        self.assertEqual(tuple(step.operation for step in parsed.steps), SUPPORTED_RECIPE_ORDERS[1])
        self.assertEqual(parse_site_skill_form_recipe_invocation(invocation_value), parsed)
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_form_recipe_invocation.schema.json').read_text())
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_invocation.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(fixture['invocation'])
        self.assertIsInstance(parse_site_skill_form_invocation(fixture['invocation']),
                              SiteSkillFormRecipeInvocation)
        with self.assertRaises(ValueError):
            parse_site_skill_form_invocation(parsed.model_copy(update={'execution_authorized': 0}))

    def test_cli_admits_recipe_sources_and_rejects_missing_pin_or_changed_bytes(self):
        from aos.site_skill_validation import SiteSkillValidationPlan
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract
        from aos.web_https_form_state_probe import WebHTTPSFormStatePlan, form_state_marker_sha256
        from aos.web_https_form_transport import WebHTTPSFormPlan, form_body

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root / 'data'
        data.mkdir(mode=0o700)
        profiles_root = data / 'web-applications'
        profiles_root.mkdir(mode=0o700)
        profiles = WebApplicationProfiles(profiles_root)
        profile_fixture = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())
        profile = WebApplicationProfile.model_validate(profile_fixture['profile'])
        profile_sha256 = profile_report(profile).profile_sha256
        profiles.register(profile, confirm_sha256=profile_sha256)
        task_fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_case_binding.json').read_text())
        task = WebTaskContract.model_validate(task_fixture['task'])

        page_value = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
        page = SitePageDraft.model_validate({**page_value, 'profile_sha256': profile_sha256,
            'origin': profile.allowed_origins[0], 'route_template': '/entry',
            'outgoing_page_keys': []})
        pages = SiteKnowledgeStore(data / 'site-knowledge', profiles)
        page_sha256 = digest(page.model_dump())
        pages.register(page, confirm_sha256=page_sha256)
        skill_value = json.loads((REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill']
        step_keys = {operation: operation.replace('_', '-')
                     for operation in SUPPORTED_RECIPE_ORDERS[0]}
        skill = SiteSkillDraft.model_validate({**skill_value,
            'profile_sha256': profile_sha256, 'page_draft_sha256': page_sha256,
            'task_key': task.task_key, 'step_keys': sorted(step_keys.values()),
            'precondition_keys': ['baseline-known', 'form-available']})
        skill_store = SiteSkillStore(data / 'site-skills', profiles, pages)
        skill_sha256 = digest(skill.model_dump())
        skill_store.register(skill, confirm_sha256=skill_sha256)
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
        plan = SiteSkillValidationPlan.model_validate({**fixture['plan'],
            'profile_sha256': profile_sha256, 'page_draft_sha256': page_sha256,
            'skill_sha256': skill_sha256, 'task_key': task.task_key})
        inputs = SiteSkillCaseInputs.model_validate({**fixture['case_inputs'],
            'plan_sha256': digest(plan.model_dump())})
        bindings = [SiteSkillFormFieldBinding(parameter_key='record-query', form_field_name='message')]
        selected = inputs.cases[0].parameters['record-query']
        fields = (('message', selected),)
        body = form_body(fields)
        form_plan = WebHTTPSFormPlan.model_validate({**task_fixture['form_plan'],
            'profile_sha256': profile_sha256, 'task_sha256': digest(task.model_dump()),
            'body_sha256': hashlib.sha256(body).hexdigest(), 'body_bytes': len(body)})
        before = b'<html><h1 id="outcome">Empty</h1></html>'
        after = f'<html><h1 id="outcome">{selected}</h1></html>'.encode()
        state_plan = WebHTTPSFormStatePlan(
            profile_sha256=profile_sha256, task_sha256=digest(task.model_dump()),
            form_plan_sha256=digest(form_plan.model_dump()),
            state_url='https://crm.example.invalid/app/state',
            expected_before_sha256=hashlib.sha256(before).hexdigest(),
            expected_after_sha256=hashlib.sha256(after).hexdigest(), marker_id='outcome',
            expected_before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
            expected_after_marker_sha256=form_state_marker_sha256(after, 'outcome'),
            submitted_field_name='message')
        recipe_value = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe.json').read_text())['recipe']
        recipe_value.update({'skill_sha256': skill_sha256, 'profile_sha256': profile_sha256,
            'page_draft_sha256': page_sha256, 'task_key': task.task_key,
            'task_sha256': digest(task.model_dump()),
            'field_binding_sha256': digest([binding.model_dump() for binding in bindings]),
            'outcome': {**recipe_value['outcome'], 'outcome_key': skill.expected_outcome_key}})
        recipe_value['steps'] = [{'step_key': step_keys[operation], 'operation': operation}
                                 for operation in SUPPORTED_RECIPE_ORDERS[0]]
        recipe = SiteSkillFormRecipe.model_validate_json(canonical(recipe_value))
        invocation = compile_site_skill_form_recipe_invocation(
            skill_store, plan, inputs, 'dev-query', profiles, task, form_plan,
            state_plan, bindings, recipe)

        sources = {
            'task': canonical(task.model_dump(mode='json')).encode(),
            'form_plan': canonical(form_plan.model_dump(mode='json')).encode(),
            'form_value': selected.encode(),
            'state_plan': canonical(state_plan.model_dump(mode='json')).encode(),
            'skill_plan': canonical(plan.model_dump()).encode(),
            'case_inputs': canonical(inputs.model_dump()).encode(),
            'field_bindings': canonical([binding.model_dump(mode='json') for binding in bindings]).encode(),
            'recipe': canonical(recipe.model_dump(mode='json')).encode()}
        source_paths = {}
        for name, content in sources.items():
            path = data / f'{name}.json'
            path.write_bytes(content)
            path.chmod(0o600)
            source_paths[name] = path
        run_root = root / 'runs'
        run_root.mkdir(mode=0o700)
        captured = {}
        runtime_constructions = []

        class StopAfterAdmission(Exception):
            pass

        class Runtime:
            pins = {'image_id': 'desktop:test'}
            container_id = 'fixture-container'

            def __init__(self, *_args, **_kwargs):
                runtime_constructions.append(True)

            def start(self):
                pass

            def stop(self):
                pass

            def docker(self, _arguments):
                pass

        class Store:
            def __init__(self, *_args, **_kwargs):
                pass

            def close(self):
                pass

        class Controller:
            def __init__(self, *_args, **_kwargs):
                pass

            def state(self):
                return {'status': 'stopped'}

        def scheduler(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(engine=None, close=lambda: None)

        app = SimpleNamespace(router=SimpleNamespace())
        spec = importlib.util.spec_from_file_location(
            'aos_test_recipe_serve_desktop', REPO_ROOT / 'scripts/serve_desktop.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        arguments = [
            '--workspace', str(data / 'workspace'), '--database', str(data / 'store.sqlite'),
            '--engine', 'fixture', '--browser-tasks', '--desktop-browser',
            '--desktop-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
            '--remote-entry-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
            '--remote-entry-profile-sha256', profile_sha256,
            '--remote-entry-task-file', str(source_paths['task']),
            '--web-profiles-root', str(profiles_root),
            '--remote-form-plan-file', str(source_paths['form_plan']),
            '--remote-form-plan-sha256', hashlib.sha256(sources['form_plan']).hexdigest(),
            '--remote-form-field-name', 'message', '--remote-form-value-file', str(source_paths['form_value']),
            '--remote-form-state-plan-file', str(source_paths['state_plan']),
            '--remote-form-state-plan-sha256', hashlib.sha256(sources['state_plan']).hexdigest(),
            '--remote-form-skill-plan-file', str(source_paths['skill_plan']),
            '--remote-form-skill-plan-sha256', hashlib.sha256(sources['skill_plan']).hexdigest(),
            '--remote-form-skill-case-inputs-file', str(source_paths['case_inputs']),
            '--remote-form-skill-case-inputs-sha256', hashlib.sha256(sources['case_inputs']).hexdigest(),
            '--remote-form-skill-field-bindings-file', str(source_paths['field_bindings']),
            '--remote-form-skill-field-bindings-sha256', hashlib.sha256(sources['field_bindings']).hexdigest(),
            '--remote-form-skill-case-key', 'dev-query',
            '--remote-form-skill-recipe-file', str(source_paths['recipe']),
            '--remote-form-skill-recipe-file-sha256', hashlib.sha256(sources['recipe']).hexdigest(),
            '--remote-form-skill-invocation-sha256', digest(invocation)]

        def stop(_app, **_kwargs):
            raise StopAfterAdmission

        def run_cli(cli_arguments):
            with (patch.object(module, 'REPO_ROOT', root), patch.object(module, 'DesktopRuntime', Runtime),
                  patch.object(module, 'TrajectoryStore', Store), patch.object(module, 'DesktopController', Controller),
                  patch.object(module, 'DesktopScheduler', side_effect=scheduler),
                  patch.object(module, 'create_console', return_value=app),
                  patch.object(module.uvicorn, 'run', side_effect=stop),
                  patch.object(sys, 'argv', ['serve_desktop.py', *cli_arguments])):
                module.main()

        try:
            with self.assertRaises(StopAfterAdmission):
                run_cli(arguments)
            self.assertEqual(captured['remote_form_skill_invocation']['schema_version'], '2.0')
            self.assertEqual(digest(captured['remote_form_skill_invocation']), digest(invocation))
            with patch.object(module, 'REPO_ROOT', root):
                self.assertEqual(captured['remote_form_skill_revalidator'](),
                                 parse_site_skill_form_invocation(invocation))
            invalids = [
                [item for index, item in enumerate(arguments)
                 if item != '--remote-form-skill-recipe-file-sha256'
                 and not (index and arguments[index - 1] == '--remote-form-skill-recipe-file-sha256')],
            ]
            source_paths['recipe'].write_bytes(sources['recipe'] + b' ')
            with patch.object(module, 'REPO_ROOT', root), self.assertRaises(ValueError):
                captured['remote_form_skill_revalidator']()
            for rejected in invalids + [arguments]:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    run_cli(rejected)
                self.assertEqual(len(runtime_constructions), 1)
        finally:
            sys.modules.pop(spec.name, None)


if __name__ == '__main__':
    unittest.main()

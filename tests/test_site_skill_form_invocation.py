import json
import hashlib
import importlib.util
import io
from pathlib import Path
import ssl
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import jsonschema

from aos.contracts import AOSFault, REPO_ROOT, State, canonical, digest
from aos.desktop_tasks import DesktopScheduler
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding)
from aos.site_skill_form_invocation import (FORM_SKILL_STAGES,
                                            SiteSkillFormInvocation,
                                            compile_site_skill_form_invocation,
                                            revalidate_site_skill_form_invocation,
                                            verify_site_skill_form_invocation)
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.web_application import (WebApplicationProfile, WebApplicationProfiles,
                                 profile_report)
from aos.web_application_binding import WebTaskContract
from aos.web_https_form_state_probe import WebHTTPSFormStatePlan
from aos.web_https_form_transport import WebHTTPSFormPlan


class SiteSkillFormInvocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        profile = WebApplicationProfile.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
        self.profile_sha256 = profile_report(profile).profile_sha256
        self.profiles.register(profile, confirm_sha256=self.profile_sha256)
        pages = SiteKnowledgeStore(self.root / 'pages', self.profiles)
        page = SitePageDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_page_draft.json').read_text())['page'])
        pages.register(page, confirm_sha256=digest(page.model_dump()))
        self.store = SiteSkillStore(self.root / 'skills', self.profiles, pages)
        skill = SiteSkillDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill'])
        self.skill_sha256 = digest(skill.model_dump())
        self.store.register(skill, confirm_sha256=self.skill_sha256)
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_case_binding.json').read_text())
        self.plan = SiteSkillValidationPlan.model_validate(fixture['plan'])
        self.inputs = SiteSkillCaseInputs.model_validate(fixture['case_inputs'])
        form_fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_case_binding.json').read_text())
        self.task = WebTaskContract.model_validate(form_fixture['task'])
        self.form_plan = WebHTTPSFormPlan.model_validate(form_fixture['form_plan'])
        self.bindings = [SiteSkillFormFieldBinding.model_validate(item)
                         for item in form_fixture['field_bindings']]
        self.state_plan = WebHTTPSFormStatePlan(
            profile_sha256=self.profile_sha256,
            task_sha256=digest(self.task.model_dump()),
            form_plan_sha256=digest(self.form_plan.model_dump()),
            state_url='https://crm.example.invalid/app/state',
            expected_before_sha256='1' * 64,
            expected_after_sha256='2' * 64,
            marker_id='outcome', expected_before_marker_sha256=digest({'text': 'empty'}),
            expected_after_marker_sha256=digest({'text': 'alpha'}),
            submitted_field_name='message')

    def test_synthetic_fixture_matches_canonical_schema_and_model(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_invocation.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/site_skill_form_invocation.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SiteSkillFormInvocation.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(fixture)
        self.assertEqual(SiteSkillFormInvocation.model_validate_json(json.dumps(fixture)).model_dump(mode='json'), fixture)
        self.assertEqual(tuple(fixture['stages']), FORM_SKILL_STAGES)
        for flag in ('execution_authorized', 'collection_authorized', 'skill_executed',
                     'site_outcome_verified', 'reviewed', 'activation_authorized',
                     'training_ready'):
            self.assertFalse(fixture[flag])

    def test_stage_mutation_and_changed_pins_fail_closed(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_invocation.json').read_text())
        with self.assertRaises(ValueError):
            SiteSkillFormInvocation.model_validate({**fixture, 'stages': list(reversed(FORM_SKILL_STAGES))})

        class Model:
            def __init__(self, **values):
                self.__dict__.update(values)

            def model_dump(self):
                return dict(self.__dict__)

        task = Model(entry_url='https://demo.invalid/entry')
        form_plan = Model(form='bound')
        state_plan = Model(state='bound')
        valid = {
            **fixture,
            'task_sha256': digest(task.model_dump()),
            'form_plan_sha256': digest(form_plan.model_dump()),
            'state_plan_sha256': digest(state_plan.model_dump()),
        }
        checked = verify_site_skill_form_invocation(
            valid, skill_sha256=valid['skill_sha256'], task=task,
            form_plan=form_plan, state_plan=state_plan)
        self.assertEqual(checked.task_sha256, valid['task_sha256'])
        with self.assertRaises(ValueError):
            verify_site_skill_form_invocation(
                valid, skill_sha256='f' * 64, task=task,
                form_plan=form_plan, state_plan=state_plan)
        real_task = Model(entry_url='https://example.com/entry')
        with self.assertRaises(ValueError):
            verify_site_skill_form_invocation(
                valid, skill_sha256=valid['skill_sha256'], task=real_task,
                form_plan=form_plan, state_plan=state_plan)

    def test_invocation_hash_is_part_of_persisted_run_state(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_skill_form_invocation.json').read_text())
        invocation_hash = digest(fixture)
        state = State(task_id='task-test', run_id='run-test', step_id='step-test',
                      runtime_id='runtime-test', deployment_id='deployment-test',
                      owner_lease_id='lease-test', skill_invocation_sha256=invocation_hash)
        restored = State.model_validate_json(state.model_dump_json())
        self.assertEqual(restored.skill_invocation_sha256, invocation_hash)

    def test_real_sources_compile_readmit_and_mutations_fail_closed(self):
        invocation = compile_site_skill_form_invocation(
            self.store, self.plan, self.inputs, 'dev-query', self.profiles,
            self.task, self.form_plan, self.state_plan, self.bindings)
        checked = revalidate_site_skill_form_invocation(
            invocation, self.store, self.plan, self.inputs, 'dev-query',
            self.profiles, self.task, self.form_plan, self.state_plan, self.bindings)
        self.assertEqual(checked, SiteSkillFormInvocation.model_validate_json(json.dumps(invocation)))
        self.assertFalse(checked.skill_executed)

        changed_inputs = self.inputs.model_copy(update={
            'cases': [self.inputs.cases[0].model_copy(update={
                'parameters': {'record-query': 'changed'}}), *self.inputs.cases[1:]]})
        changed_bindings = [SiteSkillFormFieldBinding(
            parameter_key='record-query', form_field_name='other')]
        changed_state = self.state_plan.model_copy(update={'expected_after_sha256': '5' * 64})
        for inputs, bindings, state in (
                (changed_inputs, self.bindings, self.state_plan),
                (self.inputs, changed_bindings, self.state_plan),
                (self.inputs, self.bindings, changed_state)):
            with self.subTest(inputs=inputs, bindings=bindings, state=state), self.assertRaises(ValueError):
                revalidate_site_skill_form_invocation(
                    invocation, self.store, self.plan, inputs, 'dev-query',
                    self.profiles, self.task, self.form_plan, state, bindings)

    def test_cli_admits_exact_private_sources_without_public_state_grant(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        data = root / 'data'
        data.mkdir(mode=0o700)
        profiles_root = data / 'web-applications'
        profiles_root.mkdir(mode=0o700)
        profiles = WebApplicationProfiles(profiles_root)
        profile = WebApplicationProfile.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
        profiles.register(profile, confirm_sha256=self.profile_sha256)
        pages = SiteKnowledgeStore(data / 'site-knowledge', profiles)
        page = SitePageDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_page_draft.json').read_text())['page'])
        pages.register(page, confirm_sha256=digest(page.model_dump()))
        store = SiteSkillStore(data / 'site-skills', profiles, pages)
        skill = SiteSkillDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_skill_draft.json').read_text())['skill'])
        store.register(skill, confirm_sha256=self.skill_sha256)

        state_plan = self.state_plan
        invocation = compile_site_skill_form_invocation(
            store, self.plan, self.inputs, 'dev-query', profiles, self.task,
            self.form_plan, state_plan, self.bindings)
        sources = {
            'task': canonical(self.task.model_dump(mode='json')).encode(),
            'form_plan': canonical(self.form_plan.model_dump(mode='json')).encode(),
            'form_value': b'alpha',
            'state_plan': canonical(state_plan.model_dump(mode='json')).encode(),
            'skill_plan': canonical(self.plan.model_dump()).encode(),
            'case_inputs': canonical(self.inputs.model_dump()).encode(),
            'field_bindings': canonical([binding.model_dump(mode='json')
                                         for binding in self.bindings]).encode(),
        }
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
            'aos_test_serve_desktop', REPO_ROOT / 'scripts/serve_desktop.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        arguments = [
            '--workspace', str(data / 'workspace'), '--database', str(data / 'store.sqlite'),
            '--engine', 'fixture', '--browser-tasks', '--desktop-browser',
            '--desktop-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
            '--remote-entry-mcp-manifest', str(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'),
            '--remote-entry-profile-sha256', self.profile_sha256,
            '--remote-entry-task-file', str(source_paths['task']),
            '--web-profiles-root', str(profiles_root),
            '--remote-form-plan-file', str(source_paths['form_plan']),
            '--remote-form-plan-sha256', hashlib.sha256(sources['form_plan']).hexdigest(),
            '--remote-form-field-name', 'message',
            '--remote-form-value-file', str(source_paths['form_value']),
            '--remote-form-state-plan-file', str(source_paths['state_plan']),
            '--remote-form-state-plan-sha256', hashlib.sha256(sources['state_plan']).hexdigest(),
            '--remote-form-skill-plan-file', str(source_paths['skill_plan']),
            '--remote-form-skill-plan-sha256', hashlib.sha256(sources['skill_plan']).hexdigest(),
            '--remote-form-skill-case-inputs-file', str(source_paths['case_inputs']),
            '--remote-form-skill-case-inputs-sha256', hashlib.sha256(sources['case_inputs']).hexdigest(),
            '--remote-form-skill-field-bindings-file', str(source_paths['field_bindings']),
            '--remote-form-skill-field-bindings-sha256', hashlib.sha256(sources['field_bindings']).hexdigest(),
            '--remote-form-skill-case-key', 'dev-query',
            '--remote-form-skill-invocation-sha256', digest(invocation),
        ]

        def stop(_app, **_kwargs):
            raise StopAfterAdmission

        def run_cli(cli_arguments):
            with (patch.object(module, 'REPO_ROOT', root),
                  patch.object(module, 'DesktopRuntime', Runtime),
                  patch.object(module, 'TrajectoryStore', Store),
                  patch.object(module, 'DesktopController', Controller),
                  patch.object(module, 'DesktopScheduler', side_effect=scheduler),
                  patch.object(module, 'create_console', return_value=app),
                  patch.object(module.uvicorn, 'run', side_effect=stop),
                  patch.object(sys, 'argv', ['serve_desktop.py', *cli_arguments])):
                module.main()

        try:
            with self.assertRaises(StopAfterAdmission):
                run_cli(arguments)
            negative_arguments = [
                [item for index, item in enumerate(arguments)
                 if item not in {'--remote-form-skill-case-inputs-sha256'}
                 and not (index and arguments[index - 1] == '--remote-form-skill-case-inputs-sha256')],
                [*arguments, '--remote-form-cookie-file', str(source_paths['form_value']),
                 '--remote-form-cookie-sha256', hashlib.sha256(b'alpha').hexdigest()],
                [*arguments, '--remote-form-public-state-plan-sha256',
                 digest(state_plan.model_dump())],
            ]
            for rejected_arguments in negative_arguments:
                with self.subTest(arguments=rejected_arguments[-4:]), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        run_cli(rejected_arguments)
                self.assertEqual(len(runtime_constructions), 1)
        finally:
            sys.modules.pop(spec.name, None)

        self.assertEqual(digest(captured['remote_form_skill_invocation']), digest(invocation))
        self.assertIs(captured['remote_form_public_state_plan_sha256'], None)
        self.assertTrue(callable(captured['remote_form_skill_revalidator']))
        self.assertEqual(captured['remote_form_skill_revalidator'](),
                         SiteSkillFormInvocation.model_validate_json(json.dumps(invocation)))

        changed_inputs = self.inputs.model_copy(update={
            'cases': [self.inputs.cases[0].model_copy(update={
                'parameters': {'record-query': 'changed'}}), *self.inputs.cases[1:]]})
        source_paths['case_inputs'].write_bytes(canonical(changed_inputs.model_dump()).encode())
        source_paths['case_inputs'].chmod(0o600)
        with self.assertRaises(ValueError):
            captured['remote_form_skill_revalidator']()

        scheduler = object.__new__(DesktopScheduler)
        scheduler.web_goal_planning = None
        scheduler.restart_quiesced = False
        scheduler.sequences = SimpleNamespace(reserved=False)
        scheduler.synthetic_learning_stream_dir = None
        scheduler.controller = SimpleNamespace(state=lambda: {
            'owner': 'AGENT', 'status': 'running', 'lease_id': 'lease',
            'generation': 1, 'runtime_id': 'runtime'})
        scheduler.kinds = lambda: ['browser_remote_form']
        scheduler.closed = False
        scheduler.task = None
        scheduler.store = SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_args: SimpleNamespace(fetchone=lambda: None)))
        scheduler.remote_entry_profiles = profiles
        scheduler.remote_entry_profile_sha256 = self.profile_sha256
        scheduler.remote_entry_task = self.task
        scheduler.remote_form_plan = self.form_plan
        scheduler.remote_form_cookie = None
        scheduler.remote_form_state_plan = state_plan
        scheduler.remote_form_tls_context = ssl.create_default_context()
        scheduler.remote_form_owned_target = None
        scheduler.remote_form_owned_fixture = None
        scheduler._owned_skill_reuse = None
        scheduler.owned_skill_planning = None
        scheduler._owned_candidate_execution = None
        scheduler.remote_form_public_plan_sha256 = None
        scheduler.remote_form_public_state_plan_sha256 = None
        scheduler.remote_form_skill_invocation = captured['remote_form_skill_invocation']
        scheduler.remote_form_skill_invocation_sha256 = digest(invocation)
        scheduler.remote_form_skill_revalidator = captured['remote_form_skill_revalidator']
        scheduler.job_id = 'not-admitted'
        with self.assertRaises(AOSFault):
            scheduler.start('lease', 1, 'browser_remote_form')
        self.assertEqual(scheduler.job_id, 'not-admitted')


if __name__ == '__main__':
    unittest.main()

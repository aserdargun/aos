import hashlib
import html
import json
from pathlib import Path
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                         parameter_variant_sha256)
from aos.site_skill_form_recipe import SUPPORTED_RECIPE_ORDERS, SiteSkillFormRecipe
from aos.site_skill_validation import SiteSkillValidationPlan
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import WebTaskContract
from aos.web_goal_execution_binding import (WebGoalExecutionAuthority, WebGoalExecutionSource,
    WebGoalWholeRecordOracle, build_web_goal_execution_binding, confirm_web_goal_execution_binding,
    fields_for_parameters, read_web_goal_whole_record)
from aos.web_goal_planner import WebGoalCatalog, WebGoalPlan
from aos.web_https_form_state_probe import WebHTTPSFormStatePlan, form_state_marker_sha256
from aos.web_https_form_transport import form_body, plan_web_https_form


def example(name):
    return json.loads((REPO_ROOT / 'examples' / name).read_text())


def source_fixture(root, application, identity_field, record_id, note):
    root.mkdir(mode=0o700)
    origin = 'https://' + application + '.aos.invalid'
    profile = WebApplicationProfile.model_validate(example('web_application_profile.json')['profile'] | {
        'application_key': application, 'entry_url': origin + '/app/', 'allowed_origins': [origin]})
    profile_sha256 = profile_report(profile).profile_sha256
    profiles = WebApplicationProfiles(root / 'profiles')
    profiles.register(profile, confirm_sha256=profile_sha256)
    task = WebTaskContract.model_validate(example('site_skill_form_case_binding.json')['task'] | {
        'profile_sha256': profile_sha256, 'entry_url': profile.entry_url, 'allowed_origins': [origin]})
    page = SitePageDraft.model_validate(example('site_page_draft.json')['page'] | {
        'profile_sha256': profile_sha256, 'application_key': application, 'origin': origin,
        'route_template': '/app', 'outgoing_page_keys': []})
    pages = SiteKnowledgeStore(root / 'pages', profiles)
    page_sha256 = digest(page.model_dump(mode='json'))
    pages.register(page, confirm_sha256=page_sha256)
    steps = [{'step_key': operation.replace('_', '-'), 'operation': operation}
             for operation in SUPPORTED_RECIPE_ORDERS[0]]
    skill = SiteSkillDraft.model_validate(example('site_skill_draft.json')['skill'] | {
        'application_key': application, 'profile_sha256': profile_sha256, 'page_draft_sha256': page_sha256,
        'task_key': task.task_key, 'skill_key': 'annotate-record', 'parameter_keys': ['note-text', 'record-id'],
        'step_keys': sorted(step['step_key'] for step in steps),
        'precondition_keys': ['baseline-known', 'form-available']})
    store = SiteSkillStore(root / 'skills', profiles, pages)
    skill_sha256 = digest(skill.model_dump(mode='json'))
    store.register(skill, confirm_sha256=skill_sha256)
    original_plan = example('site_skill_case_binding.json')['plan']
    cases, input_cases = [], []
    for index, original in enumerate(original_plan['cases']):
        parameters = {'record-id': record_id if index == 0 else f'held-out-{index}',
                      'note-text': note if index == 0 else f'held-out-note-{index}'}
        cases.append(original | {'parameter_keys': sorted(parameters),
            'parameter_variant_sha256': parameter_variant_sha256(parameters), 'expected_outcome_key': skill.expected_outcome_key})
        input_cases.append({'case_key': original['case_key'], 'parameters': parameters})
    plan = SiteSkillValidationPlan.model_validate(original_plan | {'profile_sha256': profile_sha256,
        'page_draft_sha256': page_sha256, 'skill_sha256': skill_sha256, 'task_key': task.task_key,
        'source_variant_sha256': [digest({'synthetic': 'unexecuted-source'})], 'cases': cases})
    inputs = SiteSkillCaseInputs.model_validate({'schema_version': '1.0', 'synthetic': True,
        'plan_sha256': digest(plan.model_dump(mode='json')), 'cases': input_cases})
    bindings = [SiteSkillFormFieldBinding(parameter_key='record-id', form_field_name=identity_field),
                SiteSkillFormFieldBinding(parameter_key='note-text', form_field_name='note')]
    selected = inputs.cases[0]
    fields = fields_for_parameters(selected.parameters, bindings)
    body = form_body(fields)
    form_plan = plan_web_https_form(profiles, task, submit_url=origin + '/app/submit',
        receipt_url=origin + '/app/receipt', body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
    before = b'<html><h1 id="outcome">Empty</h1></html>'
    after = ('<html><h1 id="outcome">' + html.escape(note, quote=True) + '</h1></html>').encode()
    state_plan = WebHTTPSFormStatePlan(profile_sha256=profile_sha256, task_sha256=digest(task.model_dump(mode='json')),
        form_plan_sha256=digest(form_plan.model_dump(mode='json')), state_url=origin + '/app/state',
        expected_before_sha256=hashlib.sha256(before).hexdigest(), expected_after_sha256=hashlib.sha256(after).hexdigest(),
        marker_id='outcome', expected_before_marker_sha256=form_state_marker_sha256(before, 'outcome'),
        expected_after_marker_sha256=form_state_marker_sha256(after, 'outcome'), submitted_field_name='note')
    recipe = SiteSkillFormRecipe.model_validate_json(canonical(example('site_skill_form_recipe.json')['recipe'] | {
        'profile_sha256': profile_sha256, 'skill_sha256': skill_sha256, 'page_draft_sha256': page_sha256,
        'task_key': task.task_key, 'task_sha256': digest(task.model_dump(mode='json')),
        'field_binding_sha256': digest([binding.model_dump(mode='json') for binding in bindings]), 'steps': steps,
        'outcome': {'outcome_key': skill.expected_outcome_key, 'kind': 'submitted_field_state_transition',
            'parameter_key': 'note-text', 'form_field_name': 'note', 'verified_at_operation': 'read_state_after'}}))
    source = WebGoalExecutionSource(**{key: digest({'synthetic': key, 'application': application}) for key in (
        'source_run_ref', 'source_invocation_sha256', 'candidate_sha256', 'review_sha256',
        'release_sha256', 'selection_sha256', 'reuse_admission_sha256')}, profile_sha256=profile_sha256,
        task_sha256=digest(task.model_dump(mode='json')), skill_sha256=skill_sha256,
        recipe_sha256=digest(recipe.model_dump(mode='json')), skill_plan_sha256=digest(plan.model_dump(mode='json')),
        case_inputs_sha256=digest(inputs.model_dump(mode='json')))
    catalog = WebGoalCatalog.model_validate({'schema_version': '1.0', 'synthetic': True,
        'application_key': application, 'tenant_key': profile.tenant_key, 'account_role': profile.account_role,
        'profile_sha256': profile_sha256, 'execution_authorized': False, 'activation_authorized': False,
        'training_ready': False, 'scope_authorization_verified': False, 'skills': [{
            'skill_ref': skill.skill_key, 'skill_sha256': skill_sha256, 'task_sha256': source.task_sha256,
            'recipe_sha256': source.recipe_sha256, 'release_sha256': source.release_sha256,
            'selection_sha256': source.selection_sha256, 'description': 'Synthetic record annotation',
            'parameters': {key: {'description': key, 'min_chars': 1, 'max_chars': 128, 'allowed_values': None}
                for key in selected.parameters}}]})
    proposal = WebGoalPlan.model_validate({'schema_version': '1.0', 'catalog_sha256': digest(catalog.model_dump(mode='json')),
        'decision': 'propose_skill', 'skill_ref': skill.skill_key, 'parameters': selected.parameters,
        'reason_code': 'skill_match', 'execution_authorized': False, 'activation_authorized': False,
        'training_ready': False, 'scope_authorization_verified': False})
    authority = WebGoalExecutionAuthority(manager_session='synthetic-manager', desktop_session_id='synthetic-session',
        runtime_id='synthetic-runtime', lease_id='synthetic-lease', generation=2, owner='AGENT', status='running')
    oracle = WebGoalWholeRecordOracle(schema_version='1.0', kind='web_goal_whole_record_contract',
        scope={'application_id': application, 'tenant_id': profile.tenant_key, 'account_role': profile.account_role},
        profile_sha256=profile_sha256, task_sha256=source.task_sha256, source_run_ref=source.source_run_ref,
        case_key=selected.case_key, readback_url=state_plan.state_url, expected_fields=dict(fields),
        expected_record_sha256=digest(dict(fields)), maximum_reported_posts=1, ambiguous_effect='stop_without_retry')
    return {'store': store, 'profiles': profiles, 'task': task, 'plan': plan, 'inputs': inputs,
        'case_key': selected.case_key, 'form_plan': form_plan, 'state_plan': state_plan,
        'field_bindings': bindings, 'recipe': recipe, 'proposal': proposal, 'catalog': catalog,
        'source': source, 'authority': authority, 'oracle': oracle}


class WebGoalExecutionBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.arguments = source_fixture(self.root / 'crm', 'synthetic-crm-note', 'contact_name', 'Ada', 'Call tomorrow')

    def confirmation(self, binding):
        return confirm_web_goal_execution_binding(binding, confirm_sha256=binding.confirm_sha256, human_confirmation=True,
            current_source=lambda: binding.source, current_authority=lambda: binding.authority)

    def observation(self, binding, **changes):
        return canonical({'schema_version': '1.0', 'scope': binding.oracle.scope.model_dump(mode='json'),
            'record': binding.oracle.expected_fields, 'reported_post_count': 1, 'effect_status': 'committed', **changes}).encode()

    def test_two_profiles_multiple_parameters_reuse_real_generic_compiler_without_execution_claims(self):
        bindings = []
        for application, field, cases in (
                ('synthetic-crm-note', 'contact_name', [('Ada', 'Call tomorrow'), ('Grace', 'Send brief')]),
                ('synthetic-inventory-note', 'item_code', [('SKU-42', 'Inspect stock'), ('SKU-73', 'Check shelf')])):
            for index, (record_id, note) in enumerate(cases):
                arguments = source_fixture(self.root / (application + '-' + str(index)), application, field, record_id, note)
                binding = build_web_goal_execution_binding(**arguments)
                bindings.append(binding)
                self.assertEqual(len(binding.parameters), 2)
                self.assertEqual(len(binding.invocation.steps), 6)
                self.assertEqual(binding.oracle.expected_fields,
                                 dict(fields_for_parameters(binding.parameters, binding.field_bindings)))
                report = read_web_goal_whole_record(binding, self.confirmation(binding),
                    current_source=lambda: binding.source, current_authority=lambda: binding.authority,
                    host_readback=lambda request: self.observation(binding))
                self.assertTrue(report.record_values_matched)
                self.assertFalse(report.execution_performed or report.site_outcome_verified
                                 or report.execution_receipt_bound or report.real_model_verified)
        self.assertNotEqual(bindings[0].source.profile_sha256, bindings[2].source.profile_sha256)
        self.assertNotEqual(bindings[0].oracle.scope.application_id, bindings[2].oracle.scope.application_id)
        self.assertNotEqual(set(bindings[0].oracle.expected_fields), set(bindings[2].oracle.expected_fields))
        self.assertNotEqual(bindings[0].parameter_variant_sha256, bindings[1].parameter_variant_sha256)
        self.assertNotEqual(bindings[2].parameter_variant_sha256, bindings[3].parameter_variant_sha256)

    def test_missing_extra_wrong_values_or_foreign_scope_are_not_proposal_authority(self):
        for parameters in ({'record-id': 'Ada'}, {'record-id': 'Ada', 'note-text': 'Call tomorrow', 'extra': 'bad'},
                           {'record-id': 'Other', 'note-text': 'Call tomorrow'}):
            arguments = self.arguments | {'proposal': self.arguments['proposal'].model_copy(update={'parameters': parameters})}
            with self.assertRaises(ValueError):
                build_web_goal_execution_binding(**arguments)
        oracle = self.arguments['oracle'].model_dump(mode='json')
        oracle['scope']['tenant_id'] = 'foreign-tenant'
        with self.assertRaises(ValueError):
            build_web_goal_execution_binding(**(self.arguments | {'oracle': oracle}))
        with self.assertRaises(ValueError):
            build_web_goal_execution_binding(**(self.arguments | {'source': self.arguments['source'].model_copy(
                update={'release_sha256': '0' * 64})}))

    def test_exact_human_review_and_fresh_source_and_authority_required(self):
        binding = build_web_goal_execution_binding(**self.arguments)
        for confirmed in (False, 1, 'yes'):
            with self.assertRaises(ValueError):
                confirm_web_goal_execution_binding(binding, confirm_sha256=binding.confirm_sha256,
                    human_confirmation=confirmed, current_source=lambda: binding.source, current_authority=lambda: binding.authority)
        for changes in ({'confirm_sha256': '0' * 64},
                        {'current_authority': lambda: binding.authority.model_copy(update={'generation': 3})},
                        {'current_source': lambda: binding.source.model_copy(update={'candidate_sha256': '0' * 64})}):
            with self.assertRaises(ValueError):
                confirm_web_goal_execution_binding(binding, **({'confirm_sha256': binding.confirm_sha256,
                    'human_confirmation': True, 'current_source': lambda: binding.source,
                    'current_authority': lambda: binding.authority} | changes))

    def test_all_fields_oracle_and_ambiguous_results_fail_without_retry(self):
        binding = build_web_goal_execution_binding(**self.arguments)
        confirmation = self.confirmation(binding)
        wrong = dict(binding.oracle.expected_fields, contact_name='Other')
        for response in (self.observation(binding, record=wrong), self.observation(binding, effect_status='unknown'),
                         self.observation(binding, reported_post_count=2),
                         b'{"schema_version":"1.0","schema_version":"1.0"}'):
            requests = []
            with self.assertRaises(ValueError):
                read_web_goal_whole_record(binding, confirmation, current_source=lambda: binding.source,
                    current_authority=lambda: binding.authority,
                    host_readback=lambda request: requests.append(request) or response)
            self.assertEqual(len(requests), 1)

    def test_authority_change_during_readback_cannot_return_match(self):
        binding = build_web_goal_execution_binding(**self.arguments)
        authority = [binding.authority]
        requests = []

        def readback(request):
            requests.append(request)
            authority[0] = binding.authority.model_copy(update={'generation': 3})
            return self.observation(binding)

        with self.assertRaises(ValueError):
            read_web_goal_whole_record(binding, self.confirmation(binding), current_source=lambda: binding.source,
                current_authority=lambda: authority[0], host_readback=readback)
        self.assertEqual(len(requests), 1)

    def test_source_change_during_readback_cannot_return_match(self):
        binding = build_web_goal_execution_binding(**self.arguments)
        source = [binding.source]
        requests = []

        def readback(request):
            requests.append(request)
            source[0] = binding.source.model_copy(update={'reuse_admission_sha256': '0' * 64})
            return self.observation(binding)

        with self.assertRaises(ValueError):
            read_web_goal_whole_record(binding, self.confirmation(binding), current_source=lambda: source[0],
                current_authority=lambda: binding.authority, host_readback=readback)
        self.assertEqual(len(requests), 1)

    def test_stale_control_or_altered_confirmation_does_not_call_reader(self):
        binding = build_web_goal_execution_binding(**self.arguments)
        confirmation = self.confirmation(binding)
        altered = confirmation.model_dump(mode='json')
        altered['binding_sha256'] = '0' * 64
        altered['confirm_sha256'] = digest({key: value for key, value in altered.items() if key != 'confirm_sha256'})
        for authority, review in ((binding.authority.model_copy(update={'generation': 3}), confirmation),
                (binding.authority.model_copy(update={'owner': 'HUMAN'}), confirmation),
                (binding.authority.model_copy(update={'status': 'paused'}), confirmation),
                (binding.authority, altered)):
            requests = []
            with self.assertRaises(ValueError):
                read_web_goal_whole_record(binding, review, current_source=lambda: binding.source,
                    current_authority=lambda: authority,
                    host_readback=lambda request: requests.append(request) or self.observation(binding))
            self.assertEqual(requests, [])

    def test_generated_canonical_schemas_match_typed_models(self):
        from aos.web_goal_execution_binding import (WebGoalExecutionBinding, WebGoalExecutionConfirmation,
            WebGoalWholeRecordObservation, WebGoalWholeRecordReadback)

        for name, model in (('web_goal_execution_binding', WebGoalExecutionBinding),
                ('web_goal_execution_confirmation', WebGoalExecutionConfirmation),
                ('web_goal_whole_record_oracle', WebGoalWholeRecordOracle),
                ('web_goal_whole_record_observation', WebGoalWholeRecordObservation),
                ('web_goal_whole_record_readback', WebGoalWholeRecordReadback)):
            schema = model.model_json_schema()
            schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
            schema['$id'] = name + '.schema.json'
            self.assertEqual(example_schema(name), schema)


def example_schema(name):
    return json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())

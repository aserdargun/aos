"""Host-reviewed parameter maps and whole-record readback; no task execution."""

import hashlib
import json
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .knowledge import KnowledgeScope
from .site_skill_case_binding import (SiteSkillCaseInput, SiteSkillCaseInputs,
                                      SiteSkillFormFieldBinding, parameter_variant_sha256)
from .site_skill_form_recipe import (SiteSkillFormRecipe, SiteSkillFormRecipeInvocation,
                                     compile_site_skill_form_recipe_invocation)
from .site_skill_validation import SiteSkillValidationPlan
from .web_application import Checksum, Key, WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskContract
from .web_goal_planner import WebGoalCatalog, WebGoalPlan
from .web_https_form_state_probe import WebHTTPSFormStatePlan
from .web_https_form_transport import WebHTTPSFormPlan, exact_form_fields, form_body


Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')]
FieldName = Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')]


def _check(condition):
    if not condition:
        raise ValueError('web_goal_execution_binding_invalid')


def _typed(model, value):
    value = value.model_dump(mode='json') if isinstance(value, TypedModel) else value
    try:
        parsed = model.model_validate_json(canonical(value))
        _check(canonical(parsed.model_dump(mode='json')) == canonical(value))
        return parsed
    except Exception:
        raise ValueError('web_goal_execution_binding_invalid') from None


class WebGoalExecutionFlags(TypedModel):
    execution_authorized: Literal[False] = False
    execution_performed: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    activation_authorized: Literal[False] = False
    training_ready: Literal[False] = False
    gpu_release_verified: Literal[False] = False

    @field_validator('execution_authorized', 'execution_performed', 'site_outcome_verified',
                     'activation_authorized', 'training_ready', 'gpu_release_verified', mode='before')
    @classmethod
    def exact_false(cls, value):
        _check(value is False)
        return value


class WebGoalExecutionAuthority(TypedModel):
    manager_session: Identifier
    desktop_session_id: Identifier
    runtime_id: Identifier
    lease_id: Identifier
    generation: int = Field(ge=0)
    owner: Literal['AGENT']
    status: Literal['running']


class WebGoalExecutionSource(TypedModel):
    source_run_ref: Checksum
    source_invocation_sha256: Checksum
    candidate_sha256: Checksum
    review_sha256: Checksum
    release_sha256: Checksum
    selection_sha256: Checksum
    reuse_admission_sha256: Checksum
    profile_sha256: Checksum
    task_sha256: Checksum
    skill_sha256: Checksum
    recipe_sha256: Checksum
    skill_plan_sha256: Checksum
    case_inputs_sha256: Checksum


class WebGoalWholeRecordOracle(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_whole_record_contract']
    scope: KnowledgeScope
    profile_sha256: Checksum
    task_sha256: Checksum
    source_run_ref: Checksum
    case_key: Key
    readback_url: str = Field(min_length=1, max_length=2048)
    expected_fields: dict[FieldName, str] = Field(min_length=1, max_length=8)
    expected_record_sha256: Checksum
    maximum_reported_posts: Literal[1]
    ambiguous_effect: Literal['stop_without_retry']

    @field_validator('expected_fields')
    @classmethod
    def bounded_record(cls, fields):
        _check(all(value and value.isprintable() and len(value.encode('utf-8')) <= 256
                   for value in fields.values()))
        return fields

    @field_validator('readback_url')
    @classmethod
    def canonical_url(cls, value):
        canonical_origin(value)
        return value

    @model_validator(mode='after')
    def record_hash(self):
        _check(self.expected_record_sha256 == digest(self.expected_fields))
        return self


class WebGoalExecutionBinding(WebGoalExecutionFlags):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_parameter_execution_binding']
    synthetic: Literal[True]
    authority: WebGoalExecutionAuthority
    source: WebGoalExecutionSource
    catalog_sha256: Checksum
    proposal_sha256: Checksum
    skill_ref: Key
    case_key: Key
    parameters: dict[Key, str] = Field(min_length=1, max_length=8)
    parameter_variant_sha256: Checksum
    field_bindings: list[SiteSkillFormFieldBinding] = Field(min_length=1, max_length=8)
    form_body_sha256: Checksum
    form_body_bytes: int = Field(ge=1, le=4096)
    invocation: SiteSkillFormRecipeInvocation
    oracle: WebGoalWholeRecordOracle
    confirm_sha256: Checksum

    @model_validator(mode='after')
    def exact_binding(self):
        value = self.model_dump(mode='json')
        fields = fields_for_parameters(self.parameters, self.field_bindings)
        body = form_body(fields)
        invocation = self.invocation
        _check(self.parameter_variant_sha256 == parameter_variant_sha256(self.parameters)
               and self.form_body_sha256 == hashlib.sha256(body).hexdigest()
               and self.form_body_bytes == len(body)
               and self.oracle.expected_fields == dict(fields)
               and self.oracle.case_key == self.case_key
               and self.oracle.source_run_ref == self.source.source_run_ref
               and self.oracle.profile_sha256 == self.source.profile_sha256
               and self.oracle.task_sha256 == self.source.task_sha256
               and invocation.case_key == self.case_key
               and invocation.field_binding_sha256 == digest(value['field_bindings']))
        for field in ('profile_sha256', 'task_sha256', 'skill_sha256', 'recipe_sha256',
                      'skill_plan_sha256', 'case_inputs_sha256'):
            _check(getattr(invocation, field) == getattr(self.source, field))
        _check(self.confirm_sha256 == digest({key: item for key, item in value.items() if key != 'confirm_sha256'}))
        return self


class WebGoalExecutionConfirmation(WebGoalExecutionFlags):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_parameter_execution_confirmation']
    binding_sha256: Checksum
    source_sha256: Checksum
    authority: WebGoalExecutionAuthority
    human_confirmation: Literal[True]
    fresh_approval_per_stage: Literal[True]
    confirm_sha256: Checksum

    @field_validator('human_confirmation', 'fresh_approval_per_stage', mode='before')
    @classmethod
    def exact_true(cls, value):
        _check(value is True)
        return value

    @model_validator(mode='after')
    def confirmation_hash(self):
        value = self.model_dump(mode='json')
        _check(self.confirm_sha256 == digest({key: item for key, item in value.items() if key != 'confirm_sha256'}))
        return self


class WebGoalWholeRecordObservation(TypedModel):
    schema_version: Literal['1.0']
    scope: KnowledgeScope
    record: dict[FieldName, str] = Field(min_length=1, max_length=8)
    reported_post_count: int = Field(ge=0, le=2)
    effect_status: Literal['committed', 'unknown', 'not_committed']


class WebGoalWholeRecordReadback(WebGoalExecutionFlags):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_whole_record_readback']
    binding_sha256: Checksum
    confirmation_sha256: Checksum
    oracle_sha256: Checksum
    source_sha256: Checksum
    request_sha256: Checksum
    response_sha256: Checksum
    observed_record_sha256: Checksum
    record_values_matched: Literal[True]
    verification_scope: Literal['host_reader_record_binding_only']
    execution_receipt_bound: Literal[False]
    real_model_verified: Literal[False]


def fields_for_parameters(parameters, bindings):
    parameters = _typed(SiteSkillCaseInput, {'case_key': 'bound-parameters', 'parameters': parameters}).parameters
    bindings = [_typed(SiteSkillFormFieldBinding, binding) for binding in bindings]
    _check(1 <= len(bindings) <= 8
           and len({binding.parameter_key for binding in bindings}) == len(bindings)
           and len({binding.form_field_name for binding in bindings}) == len(bindings)
           and set(parameters) == {binding.parameter_key for binding in bindings})
    entries = [{'name': binding.form_field_name, 'value': parameters[binding.parameter_key]}
               for binding in bindings]
    return (exact_form_fields(entries[0]['name'], entries[0]['value']) if len(entries) == 1
            else exact_form_fields(None, None, entries))


def build_web_goal_execution_binding(*, store, profiles: WebApplicationProfiles,
                                     task: WebTaskContract, plan: SiteSkillValidationPlan,
                                     inputs: SiteSkillCaseInputs, case_key: str,
                                     form_plan: WebHTTPSFormPlan, state_plan: WebHTTPSFormStatePlan,
                                     field_bindings, recipe: SiteSkillFormRecipe,
                                     proposal: WebGoalPlan, catalog: WebGoalCatalog,
                                     source: WebGoalExecutionSource,
                                     authority: WebGoalExecutionAuthority,
                                     oracle: WebGoalWholeRecordOracle):
    source = _typed(WebGoalExecutionSource, source)
    authority = _typed(WebGoalExecutionAuthority, authority)
    oracle = _typed(WebGoalWholeRecordOracle, oracle)
    proposal = _typed(WebGoalPlan, proposal)
    catalog = _typed(WebGoalCatalog, catalog)
    inputs = _typed(SiteSkillCaseInputs, inputs)
    plan = _typed(SiteSkillValidationPlan, plan)
    task = _typed(WebTaskContract, task)
    recipe = _typed(SiteSkillFormRecipe, recipe)
    field_bindings = [_typed(SiteSkillFormFieldBinding, binding) for binding in field_bindings]
    proposal.validate_catalog(catalog)
    _check(proposal.decision == 'propose_skill' and catalog.synthetic is True)
    selected = next((case for case in inputs.cases if case.case_key == case_key), None)
    authored = next((case for case in plan.cases if case.case_key == case_key), None)
    _check(selected is not None and authored is not None and authored.cohort == 'development'
           and proposal.parameters == selected.parameters)
    skill = store.get(plan.skill_sha256)
    catalog_skill = next(item for item in catalog.skills if item.skill_ref == proposal.skill_ref)
    profile = profiles.get(source.profile_sha256)
    scope = {'application_id': profile.application_key, 'tenant_id': profile.tenant_key,
             'account_role': profile.account_role}
    _check(catalog.profile_sha256 == source.profile_sha256
           and (catalog.application_key, catalog.tenant_key, catalog.account_role)
           == (profile.application_key, profile.tenant_key, profile.account_role)
           and oracle.scope.model_dump(mode='json') == scope
           and catalog_skill.skill_ref == skill.skill_key
           and catalog_skill.skill_sha256 == source.skill_sha256
           and catalog_skill.task_sha256 == source.task_sha256
           and catalog_skill.recipe_sha256 == source.recipe_sha256
           and catalog_skill.release_sha256 == source.release_sha256
           and catalog_skill.selection_sha256 == source.selection_sha256
           and set(skill.parameter_keys) == set(selected.parameters)
           and source.skill_sha256 == digest(skill.model_dump(mode='json'))
           and source.task_sha256 == digest(task.model_dump(mode='json'))
           and source.recipe_sha256 == digest(recipe.model_dump(mode='json'))
           and source.skill_plan_sha256 == digest(plan.model_dump(mode='json'))
           and source.case_inputs_sha256 == digest(inputs.model_dump(mode='json'))
           and canonical_origin(oracle.readback_url)[0] in task.allowed_origins
           and canonical_origin(oracle.readback_url)[0] in profile.allowed_origins
           and oracle.readback_url == state_plan.state_url)
    invocation = compile_site_skill_form_recipe_invocation(
        store, plan, inputs, case_key, profiles, task, form_plan, state_plan, field_bindings, recipe)
    fields = fields_for_parameters(selected.parameters, field_bindings)
    body = form_body(fields)
    value = {'schema_version': '1.0', 'kind': 'web_goal_parameter_execution_binding', 'synthetic': True,
             'authority': authority.model_dump(mode='json'), 'source': source.model_dump(mode='json'),
             'catalog_sha256': digest(catalog.model_dump(mode='json')),
             'proposal_sha256': digest(proposal.model_dump(mode='json')), 'skill_ref': proposal.skill_ref,
             'case_key': case_key, 'parameters': selected.parameters,
             'parameter_variant_sha256': parameter_variant_sha256(selected.parameters),
             'field_bindings': [binding.model_dump(mode='json') for binding in field_bindings],
             'form_body_sha256': hashlib.sha256(body).hexdigest(), 'form_body_bytes': len(body),
             'invocation': invocation, 'oracle': oracle.model_dump(mode='json'),
             **WebGoalExecutionFlags().model_dump(mode='json')}
    value['confirm_sha256'] = digest(value)
    return _typed(WebGoalExecutionBinding, value)


def _current(binding, current_source, current_authority):
    _check(callable(current_source) and callable(current_authority)
           and _typed(WebGoalExecutionSource, current_source()) == binding.source
           and _typed(WebGoalExecutionAuthority, current_authority()) == binding.authority)


def confirm_web_goal_execution_binding(binding, *, confirm_sha256, human_confirmation,
                                       current_source: Callable, current_authority: Callable):
    binding = _typed(WebGoalExecutionBinding, binding)
    _check(human_confirmation is True and confirm_sha256 == binding.confirm_sha256)
    _current(binding, current_source, current_authority)
    value = {'schema_version': '1.0', 'kind': 'web_goal_parameter_execution_confirmation',
             'binding_sha256': digest(binding.model_dump(mode='json')),
             'source_sha256': digest(binding.source.model_dump(mode='json')),
             'authority': binding.authority.model_dump(mode='json'), 'human_confirmation': True,
             'fresh_approval_per_stage': True, **WebGoalExecutionFlags().model_dump(mode='json')}
    value['confirm_sha256'] = digest(value)
    return _typed(WebGoalExecutionConfirmation, value)


def _unique_pairs(entries):
    result = {}
    for key, value in entries:
        _check(key not in result)
        result[key] = value
    return result


def read_web_goal_whole_record(binding, confirmation, *, current_source: Callable,
                              current_authority: Callable, host_readback: Callable):
    binding = _typed(WebGoalExecutionBinding, binding)
    confirmation = _typed(WebGoalExecutionConfirmation, confirmation)
    _check(confirmation.binding_sha256 == digest(binding.model_dump(mode='json'))
           and confirmation.source_sha256 == digest(binding.source.model_dump(mode='json'))
           and confirmation.authority == binding.authority and callable(host_readback))
    _current(binding, current_source, current_authority)
    request = {'method': 'GET', 'url': binding.oracle.readback_url,
               'profile_sha256': binding.source.profile_sha256,
               'task_sha256': binding.source.task_sha256,
               'source_run_ref': binding.source.source_run_ref,
               'binding_sha256': confirmation.binding_sha256,
               'oracle_sha256': digest(binding.oracle.model_dump(mode='json'))}
    response = host_readback(dict(request))
    _check(type(response) is bytes and 1 <= len(response) <= 16384)
    try:
        value = json.loads(response.decode('utf-8', 'strict'), object_pairs_hook=_unique_pairs)
        observed = _typed(WebGoalWholeRecordObservation, value)
    except Exception:
        raise ValueError('web_goal_whole_record_unverified_no_retry') from None
    _current(binding, current_source, current_authority)
    _check(observed.scope == binding.oracle.scope
           and observed.record == binding.oracle.expected_fields
           and observed.effect_status == 'committed'
           and observed.reported_post_count == binding.oracle.maximum_reported_posts)
    return WebGoalWholeRecordReadback(
        schema_version='1.0', kind='web_goal_whole_record_readback',
        binding_sha256=confirmation.binding_sha256,
        confirmation_sha256=digest(confirmation.model_dump(mode='json')),
        oracle_sha256=request['oracle_sha256'], source_sha256=confirmation.source_sha256,
        request_sha256=digest(request), response_sha256=hashlib.sha256(response).hexdigest(),
        observed_record_sha256=digest(observed.record), record_values_matched=True,
        verification_scope='host_reader_record_binding_only', execution_receipt_bound=False,
        real_model_verified=False)

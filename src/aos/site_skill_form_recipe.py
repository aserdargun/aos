"""Executable, bounded recipes for the synthetic HTTPS form operator."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .site_skill import SiteSkillDraft, SiteSkillStore
from .site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                     bind_site_skill_form_case)
from .site_skill_validation import SiteSkillValidationPlan
from .web_application import Checksum, Key, WebApplicationProfiles
from .web_application import canonical_origin
from .web_application_binding import WebTaskContract
from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                         verify_submitted_field_binding)
from .web_https_form_transport import WebHTTPSFormPlan, exact_form_fields


RecipeOperation = Literal[
    'open_entry', 'read_state_before', 'fill_form', 'submit_form',
    'read_receipt', 'read_state_after',
]
SUPPORTED_RECIPE_ORDERS = (
    ('open_entry', 'read_state_before', 'fill_form', 'submit_form',
     'read_receipt', 'read_state_after'),
    ('open_entry', 'fill_form', 'read_state_before', 'submit_form',
     'read_receipt', 'read_state_after'),
)


class SiteSkillFormRecipeStep(TypedModel):
    step_key: Key
    operation: RecipeOperation


class SiteSkillFormRecipePrecondition(TypedModel):
    precondition_key: Key
    kind: Literal['entry_form_available', 'declared_state_before']
    checked_at_operation: Literal['fill_form', 'read_state_before']


class SiteSkillFormRecipeOutcome(TypedModel):
    outcome_key: Key
    kind: Literal['submitted_field_state_transition']
    parameter_key: Key
    form_field_name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    verified_at_operation: Literal['read_state_after']


class SiteSkillFormRecipe(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    skill_sha256: Checksum
    profile_sha256: Checksum
    page_draft_sha256: Checksum
    task_key: Key
    task_sha256: Checksum
    model_role: Literal['system1']
    field_binding_sha256: Checksum
    preconditions: tuple[SiteSkillFormRecipePrecondition, ...] = Field(min_length=2, max_length=2)
    outcome: SiteSkillFormRecipeOutcome
    steps: tuple[SiteSkillFormRecipeStep, ...] = Field(min_length=6, max_length=6)
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @field_validator('execution_authorized', 'collection_authorized', 'reviewed',
                     'activation_authorized', 'training_ready', mode='before')
    @classmethod
    def false_claims_are_booleans(cls, value):
        if value is not False:
            raise ValueError('site_skill_form_recipe_cannot_claim_authority')
        return value

    @model_validator(mode='after')
    def fixed_bounded_recipe(self):
        precondition_keys = [item.precondition_key for item in self.preconditions]
        operations = tuple(item.operation for item in self.steps)
        step_keys = [item.step_key for item in self.steps]
        if (precondition_keys != sorted(set(precondition_keys))
                or {item.kind for item in self.preconditions}
                != {'entry_form_available', 'declared_state_before'}
                or any(item.checked_at_operation != {
                    'entry_form_available': 'fill_form',
                    'declared_state_before': 'read_state_before'}[item.kind]
                       for item in self.preconditions)
                or len(step_keys) != len(set(step_keys))
                or len({item.operation for item in self.steps}) != 6
                or operations not in SUPPORTED_RECIPE_ORDERS):
            raise ValueError('site_skill_form_recipe_contract_invalid')
        return self


class SiteSkillFormRecipeInvocation(TypedModel):
    schema_version: Literal['2.0']
    synthetic: Literal[True]
    status: Literal['admission_ready']
    skill_sha256: Checksum
    skill_plan_sha256: Checksum
    case_inputs_sha256: Checksum
    case_key: Key
    profile_sha256: Checksum
    task_sha256: Checksum
    form_plan_sha256: Checksum
    state_plan_sha256: Checksum
    field_binding_sha256: Checksum
    recipe_sha256: Checksum
    preconditions: tuple[SiteSkillFormRecipePrecondition, ...] = Field(min_length=2, max_length=2)
    outcome: SiteSkillFormRecipeOutcome
    steps: tuple[SiteSkillFormRecipeStep, ...] = Field(min_length=6, max_length=6)
    fresh_approval_per_stage: Literal[True]
    maximum_post_count: Literal[1]
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    recipe_executed: Literal[False]
    skill_executed: Literal[False]
    site_outcome_verified: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @field_validator('execution_authorized', 'collection_authorized', 'recipe_executed',
                     'skill_executed', 'site_outcome_verified', 'reviewed',
                     'activation_authorized', 'training_ready', mode='before')
    @classmethod
    def false_claims_are_booleans(cls, value):
        if value is not False:
            raise ValueError('site_skill_form_recipe_invocation_cannot_claim_execution')
        return value

    @model_validator(mode='after')
    def fixed_bounded_recipe_invocation(self):
        precondition_keys = [item.precondition_key for item in self.preconditions]
        if (tuple(item.operation for item in self.steps) not in SUPPORTED_RECIPE_ORDERS
                or {item.kind for item in self.preconditions}
                != {'entry_form_available', 'declared_state_before'}
                or precondition_keys != sorted(set(precondition_keys))
                or any(item.checked_at_operation != {
                    'entry_form_available': 'fill_form',
                    'declared_state_before': 'read_state_before'}[item.kind]
                       for item in self.preconditions)
                or len({item.step_key for item in self.steps}) != 6
                or len({item.operation for item in self.steps}) != 6):
            raise ValueError('site_skill_form_recipe_invocation_invalid')
        return self


def recipe_operator_steps(recipe: SiteSkillFormRecipe | SiteSkillFormRecipeInvocation):
    if isinstance(recipe, dict):
        recipe = parse_site_skill_form_recipe_invocation(recipe)
    operations = []
    for item in recipe.steps:
        tool, stage, label = {
            'open_entry': ('browser.form.open', 0, 'entry GET'),
            'read_state_before': ('browser.form.state_before', None, 'state-before GET'),
            'fill_form': ('browser.form.fill', 1, 'form fill'),
            'submit_form': ('browser.form.submit', 2, 'one form POST'),
            'read_receipt': ('browser.form.receipt', 3, 'receipt GET'),
            'read_state_after': ('browser.form.state_after', None, 'state-after GET'),
        }[item.operation]
        operations.append((tool, item.operation, stage, label, item.step_key))
    return tuple(operations)


def verify_site_skill_form_recipe(recipe: SiteSkillFormRecipe | dict,
                                  skill: SiteSkillDraft,
                                  field_bindings: list[SiteSkillFormFieldBinding],
                                  state_plan: WebHTTPSFormStatePlan) -> SiteSkillFormRecipe:
    recipe = (recipe if isinstance(recipe, SiteSkillFormRecipe) else
              SiteSkillFormRecipe.model_validate_json(canonical(recipe)))
    skill = SiteSkillDraft.model_validate(skill.model_dump())
    field_bindings = [SiteSkillFormFieldBinding.model_validate(item.model_dump())
                      for item in field_bindings]
    if (skill.model_role != 'system1'
            or recipe.skill_sha256 != digest(skill.model_dump())
            or recipe.profile_sha256 != skill.profile_sha256
            or recipe.page_draft_sha256 != skill.page_draft_sha256
            or recipe.task_key != skill.task_key
            or recipe.task_sha256 != state_plan.task_sha256
            or recipe.model_role != skill.model_role
            or {item.step_key for item in recipe.steps} != set(skill.step_keys)
            or len(recipe.steps) != len(skill.step_keys)
            or {item.precondition_key for item in recipe.preconditions}
            != set(skill.precondition_keys)
            or {item.parameter_key for item in field_bindings}
            != set(skill.parameter_keys)
            or recipe.outcome.outcome_key != skill.expected_outcome_key
            or recipe.outcome.verified_at_operation != 'read_state_after'
            or recipe.field_binding_sha256 != digest(
                [item.model_dump() for item in field_bindings])
            or recipe.outcome.form_field_name != state_plan.submitted_field_name
            or not any(binding.parameter_key == recipe.outcome.parameter_key
                       and binding.form_field_name == recipe.outcome.form_field_name
                       for binding in field_bindings)):
        raise ValueError('site_skill_form_recipe_source_mismatch')
    return recipe


def compile_site_skill_form_recipe_invocation(
        store: SiteSkillStore, plan: SiteSkillValidationPlan,
        inputs: SiteSkillCaseInputs, case_key: str,
        profiles: WebApplicationProfiles, task: WebTaskContract,
        form_plan: WebHTTPSFormPlan, state_plan: WebHTTPSFormStatePlan,
        field_bindings: list[SiteSkillFormFieldBinding],
        recipe: SiteSkillFormRecipe | dict) -> dict:
    plan = SiteSkillValidationPlan.model_validate(plan.model_dump())
    inputs = SiteSkillCaseInputs.model_validate(inputs.model_dump())
    skill = store.get(plan.skill_sha256)
    profile = profiles.get(plan.profile_sha256)
    host = urlsplit(profile.entry_url).hostname
    if (skill.model_role != 'system1' or plan.model_role != 'system1'
            or plan.profile_sha256 != skill.profile_sha256
            or plan.page_draft_sha256 != skill.page_draft_sha256
            or plan.task_key != skill.task_key
            or task.profile_sha256 != plan.profile_sha256
            or task.task_key != plan.task_key or not host or not host.endswith('.invalid')
            or state_plan.submitted_field_name is None):
        raise ValueError('site_skill_form_recipe_scope_invalid')
    recipe = verify_site_skill_form_recipe(recipe, skill, field_bindings, state_plan)
    case_binding = bind_site_skill_form_case(
        store, plan, inputs, case_key, profiles, task, form_plan, field_bindings)
    if (state_plan.profile_sha256 != plan.profile_sha256
            or state_plan.task_sha256 != digest(task.model_dump())
            or state_plan.form_plan_sha256 != digest(form_plan.model_dump())
            or canonical_origin(state_plan.state_url)[0] not in task.allowed_origins):
        raise ValueError('site_skill_form_recipe_state_scope_mismatch')
    selected = next(item for item in inputs.cases if item.case_key == case_key)
    outcome_field = next(binding.form_field_name for binding in field_bindings
                         if binding.parameter_key == recipe.outcome.parameter_key)
    if outcome_field != recipe.outcome.form_field_name:
        raise ValueError('site_skill_form_recipe_outcome_binding_mismatch')
    ordered_fields = tuple((binding.form_field_name,
                            selected.parameters[binding.parameter_key])
                           for binding in field_bindings)
    fields = (exact_form_fields(*ordered_fields[0]) if len(ordered_fields) == 1 else
              exact_form_fields(None, None, [
                  {'name': name, 'value': value} for name, value in ordered_fields]))
    verify_submitted_field_binding(state_plan, form_plan, fields)
    return SiteSkillFormRecipeInvocation.model_validate({
        'schema_version': '2.0', 'synthetic': True, 'status': 'admission_ready',
        'skill_sha256': plan.skill_sha256,
        'skill_plan_sha256': digest(plan.model_dump()),
        'case_inputs_sha256': case_binding['case_inputs_sha256'],
        'case_key': case_key, 'profile_sha256': plan.profile_sha256,
        'task_sha256': digest(task.model_dump()),
        'form_plan_sha256': digest(form_plan.model_dump()),
        'state_plan_sha256': digest(state_plan.model_dump()),
        'field_binding_sha256': recipe.field_binding_sha256,
        'recipe_sha256': digest(recipe.model_dump(mode='json')),
        'preconditions': recipe.preconditions, 'outcome': recipe.outcome,
        'steps': recipe.steps, 'fresh_approval_per_stage': True,
        'maximum_post_count': 1, 'execution_authorized': False,
        'collection_authorized': False, 'recipe_executed': False,
        'skill_executed': False, 'site_outcome_verified': False,
        'reviewed': False, 'activation_authorized': False,
        'training_ready': False,
    }).model_dump(mode='json')


def revalidate_site_skill_form_recipe_invocation(
        invocation: SiteSkillFormRecipeInvocation | dict,
        store: SiteSkillStore, plan: SiteSkillValidationPlan,
        inputs: SiteSkillCaseInputs, case_key: str,
        profiles: WebApplicationProfiles, task: WebTaskContract,
        form_plan: WebHTTPSFormPlan, state_plan: WebHTTPSFormStatePlan,
        field_bindings: list[SiteSkillFormFieldBinding],
        recipe: SiteSkillFormRecipe | dict) -> SiteSkillFormRecipeInvocation:
    checked = parse_site_skill_form_recipe_invocation(invocation)
    rebuilt = compile_site_skill_form_recipe_invocation(
        store, plan, inputs, case_key, profiles, task, form_plan, state_plan,
        field_bindings, recipe)
    if digest(checked.model_dump(mode='json')) != digest(rebuilt):
        raise ValueError('site_skill_form_recipe_invocation_sources_changed')
    return checked


def parse_site_skill_form_recipe_invocation(value: SiteSkillFormRecipeInvocation | dict
                                            ) -> SiteSkillFormRecipeInvocation:
    if isinstance(value, SiteSkillFormRecipeInvocation):
        value = value.model_dump(mode='json')
    if not isinstance(value, dict):
        raise ValueError('site_skill_form_recipe_invocation_invalid')
    return SiteSkillFormRecipeInvocation.model_validate_json(canonical(value))


def verify_site_skill_form_recipe_invocation(
        invocation: SiteSkillFormRecipeInvocation | dict, *, skill_sha256: str,
        task: WebTaskContract, form_plan: WebHTTPSFormPlan,
        state_plan: WebHTTPSFormStatePlan) -> SiteSkillFormRecipeInvocation:
    from urllib.parse import urlsplit

    invocation = parse_site_skill_form_recipe_invocation(invocation)
    if (not (urlsplit(task.entry_url).hostname or '').endswith('.invalid')
            or invocation.skill_sha256 != skill_sha256
            or invocation.task_sha256 != digest(task.model_dump())
            or invocation.form_plan_sha256 != digest(form_plan.model_dump())
            or invocation.state_plan_sha256 != digest(state_plan.model_dump())):
        raise ValueError('site_skill_form_recipe_invocation_changed')
    return invocation

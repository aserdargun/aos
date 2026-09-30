"""Compile one synthetic S1 skill case into a fixed, typed form invocation."""

import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator

from .contracts import TypedModel, digest
from .site_skill import SiteSkillStore
from .site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                     bind_site_skill_form_case)
from .site_skill_validation import SiteSkillValidationPlan
from .web_application import Checksum, Key, WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskContract
from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                         verify_submitted_field_binding)
from .web_https_form_transport import WebHTTPSFormPlan


FORM_SKILL_STAGES = (
    'open_entry', 'read_state_before', 'fill_form', 'submit_form',
    'read_receipt', 'read_state_after',
)


class SiteSkillFormInvocation(TypedModel):
    schema_version: Literal['1.0']
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
    stages: tuple[Literal['open_entry', 'read_state_before', 'fill_form',
                          'submit_form', 'read_receipt', 'read_state_after'], ...]
    fresh_approval_per_stage: Literal[True]
    maximum_post_count: Literal[1]
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    skill_executed: Literal[False]
    site_outcome_verified: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @model_validator(mode='after')
    def fixed_workflow(self):
        if self.stages != FORM_SKILL_STAGES:
            raise ValueError('skill_form_invocation_stage_order_invalid')
        return self


def compile_site_skill_form_invocation(
    store: SiteSkillStore,
    plan: SiteSkillValidationPlan,
    inputs: SiteSkillCaseInputs,
    case_key: str,
    profiles: WebApplicationProfiles,
    task: WebTaskContract,
    form_plan: WebHTTPSFormPlan,
    state_plan: WebHTTPSFormStatePlan,
    field_bindings: list[SiteSkillFormFieldBinding],
) -> dict:
    plan = SiteSkillValidationPlan.model_validate(plan.model_dump())
    inputs = SiteSkillCaseInputs.model_validate(inputs.model_dump())
    skill = store.get(plan.skill_sha256)
    profile = profiles.get(plan.profile_sha256)
    host = urlsplit(profile.entry_url).hostname
    if (skill.model_role != 'system1' or task.profile_sha256 != plan.profile_sha256
            or task.task_key != plan.task_key or not host or not host.endswith('.invalid')
            or state_plan.submitted_field_name is None):
        raise ValueError('skill_form_invocation_scope_invalid')
    case_binding = bind_site_skill_form_case(
        store, plan, inputs, case_key, profiles, task, form_plan, field_bindings)
    if (state_plan.profile_sha256 != plan.profile_sha256
            or state_plan.task_sha256 != digest(task.model_dump())
            or state_plan.form_plan_sha256 != digest(form_plan.model_dump())
            or canonical_origin(state_plan.state_url)[0] not in task.allowed_origins):
        raise ValueError('skill_form_invocation_state_scope_mismatch')
    selected_case = next(item for item in inputs.cases if item.case_key == case_key)
    ordered = tuple((binding.form_field_name, selected_case.parameters[binding.parameter_key])
                    for binding in field_bindings)
    from .web_https_form_transport import exact_form_fields

    fields = (exact_form_fields(*ordered[0]) if len(ordered) == 1 else
              exact_form_fields(None, None, [{'name': name, 'value': value}
                                             for name, value in ordered]))
    verify_submitted_field_binding(state_plan, form_plan, fields)
    invocation = SiteSkillFormInvocation.model_validate({
        'schema_version': '1.0', 'synthetic': True, 'status': 'admission_ready',
        'skill_sha256': plan.skill_sha256,
        'skill_plan_sha256': digest(plan.model_dump()),
        'case_inputs_sha256': case_binding['case_inputs_sha256'],
        'case_key': case_key, 'profile_sha256': plan.profile_sha256,
        'task_sha256': digest(task.model_dump()),
        'form_plan_sha256': digest(form_plan.model_dump()),
        'state_plan_sha256': digest(state_plan.model_dump()),
        'field_binding_sha256': digest([binding.model_dump() for binding in field_bindings]),
        'stages': FORM_SKILL_STAGES, 'fresh_approval_per_stage': True,
        'maximum_post_count': 1, 'execution_authorized': False,
        'collection_authorized': False, 'skill_executed': False,
        'site_outcome_verified': False, 'reviewed': False,
        'activation_authorized': False, 'training_ready': False,
    })
    return invocation.model_dump(mode='json')


def verify_site_skill_form_invocation(invocation: SiteSkillFormInvocation | dict,
                                      *, skill_sha256: str, task: WebTaskContract,
                                      form_plan: WebHTTPSFormPlan,
                                      state_plan: WebHTTPSFormStatePlan) -> SiteSkillFormInvocation:
    invocation = (invocation if isinstance(invocation, SiteSkillFormInvocation) else
                  SiteSkillFormInvocation.model_validate_json(json.dumps(invocation)))
    if (not (urlsplit(task.entry_url).hostname or '').endswith('.invalid')
            or invocation.skill_sha256 != skill_sha256
            or invocation.task_sha256 != digest(task.model_dump())
            or invocation.form_plan_sha256 != digest(form_plan.model_dump())
            or invocation.state_plan_sha256 != digest(state_plan.model_dump())):
        raise ValueError('skill_form_invocation_changed')
    return invocation


def parse_site_skill_form_invocation(value):
    if isinstance(value, SiteSkillFormInvocation):
        return SiteSkillFormInvocation.model_validate_json(
            json.dumps(value.model_dump(mode='json')))
    if not isinstance(value, dict):
        from .site_skill_form_recipe import SiteSkillFormRecipeInvocation

        if isinstance(value, SiteSkillFormRecipeInvocation):
            value = value.model_dump(mode='json')
    if not isinstance(value, dict):
        raise ValueError('skill_form_invocation_invalid')
    if value.get('schema_version') == '1.0':
        return SiteSkillFormInvocation.model_validate_json(json.dumps(value))
    if value.get('schema_version') == '2.0':
        from .site_skill_form_recipe import parse_site_skill_form_recipe_invocation

        return parse_site_skill_form_recipe_invocation(value)
    raise ValueError('skill_form_invocation_version_unsupported')


def verify_site_skill_form_invocation_input(
        invocation, *, skill_sha256: str, task: WebTaskContract,
        form_plan: WebHTTPSFormPlan, state_plan: WebHTTPSFormStatePlan):
    parsed = parse_site_skill_form_invocation(invocation)
    if isinstance(parsed, SiteSkillFormInvocation):
        return verify_site_skill_form_invocation(
            parsed, skill_sha256=skill_sha256, task=task,
            form_plan=form_plan, state_plan=state_plan)
    from .site_skill_form_recipe import verify_site_skill_form_recipe_invocation

    return verify_site_skill_form_recipe_invocation(
        parsed, skill_sha256=skill_sha256, task=task,
        form_plan=form_plan, state_plan=state_plan)


def revalidate_site_skill_form_invocation(
    invocation: SiteSkillFormInvocation | dict,
    store: SiteSkillStore,
    plan: SiteSkillValidationPlan,
    inputs: SiteSkillCaseInputs,
    case_key: str,
    profiles: WebApplicationProfiles,
    task: WebTaskContract,
    form_plan: WebHTTPSFormPlan,
    state_plan: WebHTTPSFormStatePlan,
    field_bindings: list[SiteSkillFormFieldBinding],
) -> SiteSkillFormInvocation:
    checked = verify_site_skill_form_invocation(
        invocation, skill_sha256=plan.skill_sha256, task=task,
        form_plan=form_plan, state_plan=state_plan)
    rebuilt = compile_site_skill_form_invocation(
        store, plan, inputs, case_key, profiles, task, form_plan,
        state_plan, field_bindings)
    if (digest(checked.model_dump(mode='json')) != digest(rebuilt)
            or checked.profile_sha256 != plan.profile_sha256
            or checked.skill_plan_sha256 != digest(plan.model_dump())
            or checked.case_key != case_key):
        raise ValueError('skill_form_invocation_sources_changed')
    return checked

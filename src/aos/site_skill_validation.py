"""Structural, non-executing synthetic variation checks for one skill draft."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, digest
from .site_skill import SiteSkillStore
from .web_application import Checksum, Key


class SiteSkillValidationCase(TypedModel):
    case_key: Key
    cohort: Literal['development', 'held_out']
    parameter_keys: list[Key] = Field(max_length=16)
    parameter_variant_sha256: Checksum
    expected_outcome_key: Key

    @field_validator('parameter_keys')
    @classmethod
    def canonical_parameter_keys(cls, values):
        if values != sorted(set(values)):
            raise ValueError('skill_case_parameters_not_canonical')
        return values


class SiteSkillValidationPlan(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    skill_sha256: Checksum
    profile_sha256: Checksum
    page_draft_sha256: Checksum
    task_key: Key
    model_role: Literal['system1', 'system2']
    source_variant_sha256: list[Checksum] = Field(min_length=1, max_length=16)
    cases: list[SiteSkillValidationCase] = Field(min_length=3, max_length=16)

    @field_validator('source_variant_sha256')
    @classmethod
    def canonical_sources(cls, values):
        if values != sorted(set(values)):
            raise ValueError('skill_validation_sources_not_canonical')
        return values

    @field_validator('cases')
    @classmethod
    def canonical_cases(cls, cases):
        case_keys = [item.case_key for item in cases]
        if case_keys != sorted(set(case_keys)):
            raise ValueError('skill_validation_cases_not_canonical')
        return cases


class SiteSkillValidationReport(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['structure_only']
    plan_sha256: Checksum
    skill_sha256: Checksum
    profile_sha256: Checksum
    page_draft_sha256: Checksum
    task_key: Key
    model_role: Literal['system1', 'system2']
    case_count: int = Field(ge=3, le=16)
    development_count: int = Field(ge=1, le=14)
    held_out_count: int = Field(ge=2, le=15)
    source_variant_count: int = Field(ge=1, le=16)
    execution_performed: Literal[False]
    outcomes_verified: Literal[False]
    skill_validated: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @model_validator(mode='after')
    def complete_case_counts(self):
        if self.development_count + self.held_out_count != self.case_count:
            raise ValueError('skill_validation_case_count_mismatch')
        return self


def preview_skill_validation(store: SiteSkillStore, plan: SiteSkillValidationPlan) -> dict:
    plan = SiteSkillValidationPlan.model_validate(plan.model_dump())
    skill = store.get(plan.skill_sha256)
    if (plan.profile_sha256 != skill.profile_sha256
            or plan.page_draft_sha256 != skill.page_draft_sha256
            or plan.task_key != skill.task_key or plan.model_role != skill.model_role):
        raise ValueError('skill_validation_scope_mismatch')
    if not skill.parameter_keys:
        raise ValueError('skill_validation_requires_parameter_variation')
    if any(case.parameter_keys != skill.parameter_keys
           or case.expected_outcome_key != skill.expected_outcome_key for case in plan.cases):
        raise ValueError('skill_validation_case_contract_mismatch')
    variants = [case.parameter_variant_sha256 for case in plan.cases]
    if len(variants) != len(set(variants)) or set(variants) & set(plan.source_variant_sha256):
        raise ValueError('skill_validation_duplicate_or_source_variant')
    development_count = sum(case.cohort == 'development' for case in plan.cases)
    held_out_count = sum(case.cohort == 'held_out' for case in plan.cases)
    if development_count < 1 or held_out_count < 2:
        raise ValueError('skill_validation_insufficient_variation')
    return SiteSkillValidationReport.model_validate({'schema_version': '1.0', 'synthetic': True,
            'status': 'structure_only',
            'plan_sha256': digest(plan.model_dump()), 'skill_sha256': plan.skill_sha256,
            'profile_sha256': plan.profile_sha256, 'page_draft_sha256': plan.page_draft_sha256,
            'task_key': plan.task_key, 'model_role': plan.model_role,
            'case_count': len(plan.cases), 'development_count': development_count,
            'held_out_count': held_out_count, 'source_variant_count': len(plan.source_variant_sha256),
            'execution_performed': False, 'outcomes_verified': False,
            'skill_validated': False, 'reviewed': False,
            'activation_authorized': False, 'training_ready': False}).model_dump()

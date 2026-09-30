"""Bind private synthetic skill case inputs to an exact structural plan."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Literal

from pydantic import Field, field_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .lifecycle import private_directory
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillStore
from .site_skill_validation import SiteSkillValidationPlan, preview_skill_validation
from .site_skill_validation_cli import read_plan_source
from .web_application import Checksum, Key, WebApplicationProfiles
from .web_application_binding import WebTaskContract
from .web_https_form_transport import (FIELD_NAME, WebHTTPSFormPlan, exact_form_fields,
                                       form_body, verify_web_https_form_plan)


MAX_CASE_INPUT_BYTES = 16384
ERROR = 'Site skill case binding unavailable: missing, invalid or unsafe source.'


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, _message):
        self.exit(2, 'Site skill case binding unavailable: invalid arguments.\n')


class SiteSkillCaseInput(TypedModel):
    case_key: Key
    parameters: dict[Key, str] = Field(min_length=1, max_length=16)

    @field_validator('parameters')
    @classmethod
    def bounded_values(cls, values):
        if any(not value or len(value.encode('utf-8')) > 256 or not value.isprintable()
               for value in values.values()):
            raise ValueError('invalid_skill_case_value')
        return values


class SiteSkillCaseInputs(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    plan_sha256: Checksum
    cases: list[SiteSkillCaseInput] = Field(min_length=3, max_length=16)

    @field_validator('cases')
    @classmethod
    def canonical_cases(cls, cases):
        keys = [case.case_key for case in cases]
        if keys != sorted(set(keys)):
            raise ValueError('skill_case_input_keys_not_canonical')
        return cases


class SiteSkillCaseBindingReport(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['case_inputs_bound_source_unverified']
    plan_sha256: Checksum
    case_inputs_sha256: Checksum
    skill_sha256: Checksum
    model_role: Literal['system1', 'system2']
    case_count: int = Field(ge=3, le=16)
    development_count: int = Field(ge=1, le=14)
    held_out_count: int = Field(ge=2, le=15)
    case_inputs_bound: Literal[True]
    source_parameters_bound: Literal[False]
    held_out_independence_verified: Literal[False]
    execution_performed: Literal[False]
    outcomes_verified: Literal[False]
    skill_validated: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]


class SiteSkillFormFieldBinding(TypedModel):
    parameter_key: Key
    form_field_name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')


class SiteSkillFormCaseReport(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['exact_form_plan_bound_execution_unverified']
    skill_sha256: Checksum
    skill_plan_sha256: Checksum
    case_inputs_sha256: Checksum
    case_key: Key
    form_plan_sha256: Checksum
    task_sha256: Checksum
    field_binding_sha256: Checksum
    field_count: int = Field(ge=1, le=8)
    parameter_values_bound_to_plan: Literal[True]
    source_parameters_bound: Literal[False]
    page_readback_bound: Literal[False]
    execution_performed: Literal[False]
    outcomes_verified: Literal[False]
    skill_validated: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]


def parameter_variant_sha256(parameters: dict[str, str]) -> str:
    return digest({'domain': 'site_skill_parameter_variant_v1', 'parameters': parameters})


def bind_site_skill_cases(store: SiteSkillStore, plan: SiteSkillValidationPlan,
                          inputs: SiteSkillCaseInputs) -> dict:
    plan = SiteSkillValidationPlan.model_validate(plan.model_dump())
    inputs = SiteSkillCaseInputs.model_validate(inputs.model_dump())
    structure = preview_skill_validation(store, plan)
    if inputs.plan_sha256 != structure['plan_sha256']:
        raise ValueError('skill_case_plan_mismatch')
    if [case.case_key for case in inputs.cases] != [case.case_key for case in plan.cases]:
        raise ValueError('skill_case_inventory_mismatch')
    for expected, actual in zip(plan.cases, inputs.cases, strict=True):
        if (sorted(actual.parameters) != expected.parameter_keys
                or parameter_variant_sha256(actual.parameters) != expected.parameter_variant_sha256):
            raise ValueError('skill_case_input_mismatch')
    return SiteSkillCaseBindingReport.model_validate({
        'schema_version': '1.0', 'synthetic': True,
        'status': 'case_inputs_bound_source_unverified',
        'plan_sha256': structure['plan_sha256'],
        'case_inputs_sha256': digest(inputs.model_dump()),
        'skill_sha256': plan.skill_sha256, 'model_role': plan.model_role,
        'case_count': structure['case_count'],
        'development_count': structure['development_count'],
        'held_out_count': structure['held_out_count'],
        'case_inputs_bound': True, 'source_parameters_bound': False,
        'held_out_independence_verified': False,
        'execution_performed': False, 'outcomes_verified': False,
        'skill_validated': False, 'reviewed': False,
        'activation_authorized': False, 'training_ready': False,
    }).model_dump()


def bind_site_skill_form_case(store: SiteSkillStore, plan: SiteSkillValidationPlan,
                              inputs: SiteSkillCaseInputs, case_key: str,
                              profiles: WebApplicationProfiles, task: WebTaskContract,
                              form_plan: WebHTTPSFormPlan,
                              field_bindings: list[SiteSkillFormFieldBinding]) -> dict:
    binding = bind_site_skill_cases(store, plan, inputs)
    task = WebTaskContract.model_validate(task.model_dump())
    form_plan = WebHTTPSFormPlan.model_validate(form_plan.model_dump())
    field_bindings = [SiteSkillFormFieldBinding.model_validate(item.model_dump())
                      for item in field_bindings]
    if (binding['model_role'] != 'system1'
            or task.profile_sha256 != plan.profile_sha256
            or task.task_key != plan.task_key
            or len(field_bindings) not in range(1, 9)
            or len({item.parameter_key for item in field_bindings}) != len(field_bindings)
            or len({item.form_field_name for item in field_bindings}) != len(field_bindings)
            or any(FIELD_NAME.fullmatch(item.form_field_name) is None for item in field_bindings)):
        raise ValueError('skill_form_case_scope_mismatch')
    matching = [case for case in inputs.cases if case.case_key == case_key]
    if len(matching) != 1 or set(matching[0].parameters) != {
            item.parameter_key for item in field_bindings}:
        raise ValueError('skill_form_case_selection_mismatch')
    selected = matching[0]
    ordered = [(item.form_field_name, selected.parameters[item.parameter_key])
               for item in field_bindings]
    fields = (exact_form_fields(*ordered[0]) if len(ordered) == 1 else
              exact_form_fields(None, None, [{'name': name, 'value': value}
                                             for name, value in ordered]))
    body = form_body(fields)
    if (form_plan.body_sha256 != hashlib.sha256(body).hexdigest()
            or form_plan.body_bytes != len(body)
            or verify_web_https_form_plan(profiles, task, form_plan) != form_plan):
        raise ValueError('skill_form_case_body_or_plan_mismatch')
    return SiteSkillFormCaseReport.model_validate({
        'schema_version': '1.0', 'synthetic': True,
        'status': 'exact_form_plan_bound_execution_unverified',
        'skill_sha256': plan.skill_sha256,
        'skill_plan_sha256': binding['plan_sha256'],
        'case_inputs_sha256': binding['case_inputs_sha256'],
        'case_key': case_key, 'form_plan_sha256': digest(form_plan.model_dump()),
        'task_sha256': digest(task.model_dump()),
        'field_binding_sha256': digest([item.model_dump() for item in field_bindings]),
        'field_count': len(fields), 'parameter_values_bound_to_plan': True,
        'source_parameters_bound': False, 'page_readback_bound': False,
        'execution_performed': False, 'outcomes_verified': False,
        'skill_validated': False, 'reviewed': False,
        'activation_authorized': False, 'training_ready': False,
    }).model_dump()


def read_case_inputs(path: Path) -> SiteSkillCaseInputs:
    directory = private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_CASE_INPUT_BYTES):
                raise ValueError('invalid_private_skill_case_inputs')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_CASE_INPUT_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
                      'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) > MAX_CASE_INPUT_BYTES or len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('skill_case_inputs_changed')

            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate_skill_case_input_key')
                    result[key] = value
                return result

            value = json.loads(payload, object_pairs_hook=unique_pairs)
            inputs = SiteSkillCaseInputs.model_validate(value)
            if payload != canonical(inputs.model_dump()).encode():
                raise ValueError('skill_case_inputs_not_canonical')
            return inputs
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Bind private synthetic skill case inputs')
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-skills')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--cases-sha256', required=True)
    parser.add_argument('--skill-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        plan = read_plan_source(arguments.plan)
        inputs = read_case_inputs(arguments.cases)
        if (plan.skill_sha256 != arguments.skill_sha256
                or digest(plan.model_dump()) != arguments.plan_sha256
                or digest(inputs.model_dump()) != arguments.cases_sha256):
            raise ValueError('skill_case_selection_mismatch')
        profiles = WebApplicationProfiles(arguments.profiles)
        pages = SiteKnowledgeStore(arguments.pages, profiles)
        report = bind_site_skill_cases(SiteSkillStore(arguments.store, profiles, pages),
                                       plan, inputs)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, ERROR + '\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

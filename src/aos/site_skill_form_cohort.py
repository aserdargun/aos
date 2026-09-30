"""Audit distinct synthetic form runs for one declared skill variation cohort."""

import argparse
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .local_app import private_read
from .remote_form_learning_source import inspect_remote_form_learning_source
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillStore
from .site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                      bind_site_skill_form_case, read_case_inputs)
from .site_skill_validation import SiteSkillValidationPlan
from .site_skill_validation_cli import read_plan_source
from .web_application import Checksum, Key, WebApplicationProfiles
from .web_application_binding import WebTaskContract
from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                         verify_submitted_field_binding)
from .web_https_form_transport import WebHTTPSFormPlan, exact_form_fields


RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
ERROR = 'Site skill form cohort unavailable: missing, changed or unsafe source.'


class SiteSkillFormCohortRun(TypedModel):
    case_key: Key
    run_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    task: WebTaskContract
    form_plan: WebHTTPSFormPlan
    state_plan: WebHTTPSFormStatePlan

    @field_validator('run_id')
    @classmethod
    def valid_run_id(cls, value):
        if RUN_ID.fullmatch(value) is None:
            raise ValueError('invalid_skill_cohort_run_id')
        return value


class SiteSkillFormCohortSelection(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    skill_sha256: Checksum
    plan_sha256: Checksum
    case_inputs_sha256: Checksum
    field_binding_sha256: Checksum
    runs: list[SiteSkillFormCohortRun] = Field(min_length=3, max_length=16)

    @field_validator('runs')
    @classmethod
    def canonical_runs(cls, values):
        keys = [item.case_key for item in values]
        run_ids = [item.run_id for item in values]
        if keys != sorted(set(keys)) or len(run_ids) != len(set(run_ids)):
            raise ValueError('skill_cohort_runs_not_distinct')
        return values


class SiteSkillFormCohortCaseReport(TypedModel):
    case_key: Key
    cohort: Literal['development', 'held_out']
    run_ref: Checksum
    form_plan_sha256: Checksum
    state_plan_sha256: Checksum
    parameter_values_bound_to_plan: Literal[True]
    submitted_value_readback_bound: Literal[True]
    transport_readback_verified: Literal[True]
    declared_state_readback_verified: Literal[True]


class SiteSkillFormCohortReport(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['audited_form_cohort_transport_only']
    skill_sha256: Checksum
    plan_sha256: Checksum
    case_inputs_sha256: Checksum
    selection_sha256: Checksum
    snapshot_sha256: Checksum
    case_count: int = Field(ge=3, le=16)
    development_count: int = Field(ge=1, le=14)
    held_out_count: int = Field(ge=2, le=15)
    cases: list[SiteSkillFormCohortCaseReport] = Field(min_length=3, max_length=16)
    source_ids_separated_from_held_out: Literal[True]
    source_parameters_bound: Literal[False]
    skill_executed: Literal[False]
    site_outcome_verified: Literal[False]
    held_out_independence_verified: Literal[False]
    skill_validated: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @model_validator(mode='after')
    def complete_case_inventory(self):
        if (len(self.cases) != self.case_count
                or sum(item.cohort == 'development' for item in self.cases)
                != self.development_count
                or sum(item.cohort == 'held_out' for item in self.cases)
                != self.held_out_count
                or [item.case_key for item in self.cases]
                != sorted(set(item.case_key for item in self.cases))):
            raise ValueError('skill_form_cohort_report_count_mismatch')
        return self


def audit_site_skill_form_cohort(store: SiteSkillStore, plan: SiteSkillValidationPlan,
                                 inputs: SiteSkillCaseInputs,
                                 field_bindings: list[SiteSkillFormFieldBinding],
                                 selection: SiteSkillFormCohortSelection,
                                 database: Path) -> dict:
    plan = SiteSkillValidationPlan.model_validate(plan.model_dump())
    inputs = SiteSkillCaseInputs.model_validate(inputs.model_dump())
    selection = SiteSkillFormCohortSelection.model_validate(selection.model_dump())
    field_bindings = [SiteSkillFormFieldBinding.model_validate(item.model_dump())
                      for item in field_bindings]
    if (plan.model_role != 'system1'
            or selection.skill_sha256 != plan.skill_sha256
            or selection.plan_sha256 != digest(plan.model_dump())
            or selection.case_inputs_sha256 != digest(inputs.model_dump())
            or selection.field_binding_sha256 != digest([item.model_dump() for item in field_bindings])
            or [item.case_key for item in selection.runs] != [item.case_key for item in plan.cases]):
        raise ValueError('skill_form_cohort_selection_mismatch')
    skill = store.get(plan.skill_sha256)
    if not skill.source_verification_ids:
        raise ValueError('skill_form_cohort_requires_source_verification')
    reports = []
    profile = store.profiles.get(plan.profile_sha256)
    if not (urlsplit(profile.entry_url).hostname or '').endswith('.invalid'):
        raise ValueError('skill_form_cohort_requires_synthetic_target')
    development_events = set()
    held_out_events = set()
    development_verifications = set()
    held_out_verifications = set()
    run_refs = set()
    form_plans = set()
    with audit_snapshot(database) as (snapshot, identity):
        for declared, selected, case_inputs in zip(plan.cases, selection.runs, inputs.cases,
                                                    strict=True):
            bound = bind_site_skill_form_case(
                store, plan, inputs, selected.case_key, store.profiles, selected.task,
                selected.form_plan, field_bindings)
            ordered = [(item.form_field_name, case_inputs.parameters[item.parameter_key])
                       for item in field_bindings]
            fields = (exact_form_fields(*ordered[0]) if len(ordered) == 1 else
                      exact_form_fields(None, None, [{'name': name, 'value': value}
                                                     for name, value in ordered]))
            if selected.state_plan.submitted_field_name is None:
                raise ValueError('skill_form_cohort_requires_submitted_marker_binding')
            verify_submitted_field_binding(selected.state_plan, selected.form_plan, fields)
            source = inspect_remote_form_learning_source(
                database, selected.run_id, profiles=store.profiles.root,
                selected_profile_sha256=plan.profile_sha256,
                selected_plan_sha256=bound['form_plan_sha256'],
                selected_state_plan_sha256=digest(selected.state_plan.model_dump()),
                _audited_snapshot=(snapshot, identity))
            events = set(source['source_event_ids_by_role']['system1'])
            if (source['snapshot_sha256'] != identity['sha256']
                    or 'system1' not in source['requested_roles'] or not events
                    or not source['transport_readback_verified']
                    or not source['declared_state_readback_verified']
                    or source['run_ref'] in run_refs
                    or bound['form_plan_sha256'] in form_plans):
                raise ValueError('skill_form_cohort_run_unverified_or_reused')
            run_refs.add(source['run_ref'])
            form_plans.add(bound['form_plan_sha256'])
            verifications = {row['verification_id'] for row in snapshot.execute(
                'SELECT verification_id FROM verifications WHERE run_id=?',
                (selected.run_id,)).fetchall()}
            if len(verifications) != 2:
                raise ValueError('skill_form_cohort_verification_missing')
            if declared.cohort == 'development':
                development_events.update(events)
                development_verifications.update(verifications)
            else:
                held_out_events.update(events)
                held_out_verifications.update(verifications)
            reports.append(SiteSkillFormCohortCaseReport.model_validate({
                'case_key': selected.case_key, 'cohort': declared.cohort,
                'run_ref': source['run_ref'],
                'form_plan_sha256': bound['form_plan_sha256'],
                'state_plan_sha256': digest(selected.state_plan.model_dump()),
                'parameter_values_bound_to_plan': True,
                'submitted_value_readback_bound': True,
                'transport_readback_verified': True,
                'declared_state_readback_verified': True}).model_dump())
        if (not set(skill.source_event_ids).issubset(development_events)
                or not set(skill.source_verification_ids).issubset(development_verifications)
                or development_events & held_out_events
                or development_verifications & held_out_verifications):
            raise ValueError('skill_form_cohort_held_out_source_overlap')
    report = SiteSkillFormCohortReport.model_validate({
        'schema_version': '1.0', 'synthetic': True,
        'status': 'audited_form_cohort_transport_only',
        'skill_sha256': plan.skill_sha256, 'plan_sha256': selection.plan_sha256,
        'case_inputs_sha256': selection.case_inputs_sha256,
        'selection_sha256': digest(selection.model_dump()),
        'snapshot_sha256': identity['sha256'],
        'case_count': len(reports),
        'development_count': sum(item.cohort == 'development' for item in plan.cases),
        'held_out_count': sum(item.cohort == 'held_out' for item in plan.cases),
        'cases': reports,
        'source_ids_separated_from_held_out': True,
        'source_parameters_bound': False, 'skill_executed': False,
        'site_outcome_verified': False, 'held_out_independence_verified': False,
        'skill_validated': False, 'reviewed': False,
        'activation_authorized': False, 'training_ready': False}).model_dump()
    validator('site_skill_form_cohort_report').validate(report)
    return report


def _private_canonical(path: Path, model, *, limit: int = 131072):
    content = private_read(path, limit=limit)
    checked = model.model_validate_json(content)
    if content != canonical(checked.model_dump(mode='json')).encode():
        raise ValueError('skill_form_cohort_source_not_canonical')
    return checked


def main(argv: list[str] | None = None) -> None:
    class PrivateArgumentParser(argparse.ArgumentParser):
        def error(self, _message):
            self.exit(2, ERROR + '\n')

    parser = PrivateArgumentParser(description='Audit synthetic skill form cohort transport',
                                   allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-skills')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--field-bindings-file', type=Path, required=True)
    parser.add_argument('--selection', type=Path, required=True)
    parser.add_argument('--selection-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        plan = read_plan_source(arguments.plan)
        inputs = read_case_inputs(arguments.cases)
        selection = _private_canonical(arguments.selection, SiteSkillFormCohortSelection)
        binding_content = private_read(arguments.field_bindings_file, limit=16384)
        field_bindings = [SiteSkillFormFieldBinding.model_validate(item)
                          for item in json.loads(binding_content)]
        if (binding_content != canonical([item.model_dump() for item in field_bindings]).encode()
                or digest(selection.model_dump()) != arguments.selection_sha256):
            raise ValueError('skill_form_cohort_exact_selection_required')
        profiles = WebApplicationProfiles(arguments.profiles)
        pages = SiteKnowledgeStore(arguments.pages, profiles)
        store = SiteSkillStore(arguments.store, profiles, pages)
        report = audit_site_skill_form_cohort(
            store, plan, inputs, field_bindings, selection, arguments.database)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError):
        parser.exit(1, ERROR + '\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

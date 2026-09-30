"""Audit an exact synthetic skill case against one completed HTTPS form run."""

import argparse
import json
from pathlib import Path
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .learning_events import review_learning_events_snapshot
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
from .web_https_form_transport import WebHTTPSFormPlan
from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                         verify_submitted_field_binding)
from .web_https_form_transport import exact_form_fields


ERROR = 'Site skill form execution unavailable: missing, changed or unsafe source.'


class SiteSkillFormExecutionReport(TypedModel):
    model_config = ConfigDict(json_schema_extra={
        'if': {'properties': {'status': {'const': 'audited_form_state_readback_candidate'}}},
        'then': {
            'required': ['state_plan_sha256', 'state_verification_ref',
                         'submitted_value_readback_bound'],
            'properties': {
                'state_plan_sha256': {'type': 'string'},
                'state_verification_ref': {'type': 'string'},
                'submitted_value_readback_bound': {'const': True}}},
        'else': {'not': {'anyOf': [
            {'required': ['state_plan_sha256']},
            {'required': ['state_verification_ref']},
            {'required': ['submitted_value_readback_bound']}]}}})
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['audited_form_transport_candidate', 'audited_form_state_readback_candidate']
    skill_sha256: Checksum
    skill_plan_sha256: Checksum
    case_inputs_sha256: Checksum
    case_key: Key
    form_plan_sha256: Checksum
    task_sha256: Checksum
    field_binding_sha256: Checksum
    run_ref: Checksum
    source_snapshot_sha256: Checksum
    submit_event_id: str = Field(pattern=r'^learning-[a-f0-9]{64}$')
    verification_ref: Checksum
    state_plan_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    state_verification_ref: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    submitted_value_readback_bound: Literal[True] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    parameter_values_bound_to_plan: Literal[True]
    submit_decision_source_bound: Literal[True]
    transport_execution_verified: Literal[True]
    transport_readback_verified: Literal[True]
    source_parameters_bound: Literal[False]
    page_readback_bound: Literal[False]
    skill_executed: Literal[False]
    site_outcome_verified: Literal[False]
    held_out_independence_verified: Literal[False]
    skill_validated: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @model_validator(mode='after')
    def state_evidence_complete(self):
        state_fields = (self.state_plan_sha256, self.state_verification_ref,
                        self.submitted_value_readback_bound)
        if (any(value is None for value in state_fields) != all(value is None for value in state_fields)
                or (self.status == 'audited_form_state_readback_candidate')
                != (self.state_plan_sha256 is not None)):
            raise ValueError('skill_form_state_evidence_incomplete')
        return self


def audit_site_skill_form_execution(store: SiteSkillStore, plan: SiteSkillValidationPlan,
                                    inputs: SiteSkillCaseInputs, case_key: str,
                                    profiles: WebApplicationProfiles, task: WebTaskContract,
                                    form_plan: WebHTTPSFormPlan,
                                    field_bindings: list[SiteSkillFormFieldBinding],
                                    database: Path, run_id: str,
                                    state_plan: WebHTTPSFormStatePlan | None = None) -> dict:
    case_report = bind_site_skill_form_case(
        store, plan, inputs, case_key, profiles, task, form_plan, field_bindings)
    profile = profiles.get(plan.profile_sha256)
    selected_case = next(case for case in plan.cases if case.case_key == case_key)
    if (selected_case.cohort != 'development'
            or not (urlsplit(profile.entry_url).hostname or '').endswith('.invalid')):
        raise ValueError('skill_form_execution_requires_development_case')
    if state_plan is not None:
        state_plan = WebHTTPSFormStatePlan.model_validate(state_plan.model_dump())
        if state_plan.submitted_field_name is None:
            raise ValueError('skill_form_execution_requires_submitted_marker_binding')
        selected_inputs = next(case for case in inputs.cases if case.case_key == case_key)
        ordered = tuple((item.form_field_name, selected_inputs.parameters[item.parameter_key])
                        for item in field_bindings)
        fields = (exact_form_fields(*ordered[0]) if len(ordered) == 1 else
                  exact_form_fields(None, None, [{'name': name, 'value': value}
                                                 for name, value in ordered]))
        verify_submitted_field_binding(state_plan, form_plan, fields)
    source = inspect_remote_form_learning_source(
        database, run_id, profiles=profiles.root,
        selected_profile_sha256=plan.profile_sha256,
        selected_plan_sha256=case_report['form_plan_sha256'],
        selected_state_plan_sha256=(digest(state_plan.model_dump()) if state_plan is not None else None))
    if ('system1' not in source['requested_roles']
            or not source['transport_readback_verified']
            or (state_plan is not None and not source['declared_state_readback_verified'])
            or form_plan.task_sha256 != case_report['task_sha256']):
        raise ValueError('skill_form_execution_source_scope_mismatch')
    with audit_snapshot(database) as (snapshot, identity):
        if identity['sha256'] != source['snapshot_sha256']:
            raise ValueError('skill_form_execution_source_changed')
        submit_actions = snapshot.execute('''SELECT step_id,decision_id FROM actions
            WHERE run_id=? AND tool='browser.form.submit' ''', (run_id,)).fetchall()
        verifications = snapshot.execute('''SELECT verification_id,method FROM verifications
            WHERE run_id=?''', (run_id,)).fetchall()
        expected_methods = ({'independent_https_form_transport_readback',
                             'declared_https_form_state_readback'} if state_plan is not None else
                            {'independent_https_form_transport_readback'})
        if (len(submit_actions) != 1 or len(verifications) != len(expected_methods)
                or {row['method'] for row in verifications} != expected_methods):
            raise ValueError('skill_form_execution_evidence_missing')
        verification_by_method = {row['method']: row['verification_id'] for row in verifications}
        submit = submit_actions[0]
        review = review_learning_events_snapshot(snapshot, identity, run_id)
        submit_events = [event['event_id'] for event in review['events']
                         if event['role'] == 'system1'
                         and event['source']['step_id'] == submit['step_id']
                         and event['source']['decision_id'] == submit['decision_id']]
        skill = store.get(plan.skill_sha256)
        source_events = set(source['source_event_ids_by_role']['system1'])
        if (len(submit_events) != 1 or submit_events[0] not in source_events
                or submit_events[0] not in skill.source_event_ids
                or not set(skill.source_event_ids).issubset(source_events)
                or skill.source_verification_ids != sorted(verification_by_method.values())):
            raise ValueError('skill_form_execution_skill_source_mismatch')
        report = {
            'schema_version': '1.0', 'synthetic': True,
            'status': ('audited_form_state_readback_candidate' if state_plan is not None else
                       'audited_form_transport_candidate'),
            'skill_sha256': case_report['skill_sha256'],
            'skill_plan_sha256': case_report['skill_plan_sha256'],
            'case_inputs_sha256': case_report['case_inputs_sha256'],
            'case_key': case_key, 'form_plan_sha256': case_report['form_plan_sha256'],
            'task_sha256': case_report['task_sha256'],
            'field_binding_sha256': case_report['field_binding_sha256'],
            'run_ref': source['run_ref'],
            'source_snapshot_sha256': source['snapshot_sha256'],
            'submit_event_id': submit_events[0],
            'verification_ref': digest({'verification_id': verification_by_method[
                'independent_https_form_transport_readback']}),
            'parameter_values_bound_to_plan': True,
            'submit_decision_source_bound': True,
            'transport_execution_verified': True,
            'transport_readback_verified': True,
            'source_parameters_bound': False, 'page_readback_bound': False,
            'skill_executed': False, 'site_outcome_verified': False,
            'held_out_independence_verified': False, 'skill_validated': False,
            'reviewed': False, 'activation_authorized': False,
            'training_ready': False}
        if state_plan is not None:
            report.update(state_plan_sha256=digest(state_plan.model_dump()),
                          state_verification_ref=digest({'verification_id': verification_by_method[
                              'declared_https_form_state_readback']}),
                          submitted_value_readback_bound=True)
        checked = SiteSkillFormExecutionReport.model_validate(report).model_dump()
        validator('site_skill_form_execution').validate(checked)
        return checked


def _private_canonical(path: Path, model):
    content = private_read(path, limit=16384)
    checked = model.model_validate_json(content)
    if content != canonical(checked.model_dump(mode='json')).encode():
        raise ValueError('skill_form_execution_source_not_canonical')
    return checked


def main(argv: list[str] | None = None) -> None:
    class PrivateArgumentParser(argparse.ArgumentParser):
        def error(self, _message):
            self.exit(2, ERROR + '\n')

    parser = PrivateArgumentParser(description='Audit one synthetic skill case against an HTTPS form run',
                                   allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-skills')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--plan-sha256', required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--cases-sha256', required=True)
    parser.add_argument('--skill-sha256', required=True)
    parser.add_argument('--case-key', required=True)
    parser.add_argument('--task-file', type=Path, required=True)
    parser.add_argument('--task-sha256', required=True)
    parser.add_argument('--form-plan-file', type=Path, required=True)
    parser.add_argument('--form-plan-sha256', required=True)
    parser.add_argument('--field-bindings-file', type=Path, required=True)
    parser.add_argument('--field-binding-sha256', required=True)
    parser.add_argument('--state-plan-file', type=Path)
    parser.add_argument('--state-plan-sha256')
    arguments = parser.parse_args(argv)
    try:
        plan = read_plan_source(arguments.plan)
        inputs = read_case_inputs(arguments.cases)
        task = _private_canonical(arguments.task_file, WebTaskContract)
        form_plan = _private_canonical(arguments.form_plan_file, WebHTTPSFormPlan)
        if (arguments.state_plan_file is None) != (arguments.state_plan_sha256 is None):
            raise ValueError('skill_form_execution_state_selection_incomplete')
        state_plan = (_private_canonical(arguments.state_plan_file, WebHTTPSFormStatePlan)
                      if arguments.state_plan_file is not None else None)
        field_content = private_read(arguments.field_bindings_file, limit=16384)
        field_bindings = [SiteSkillFormFieldBinding.model_validate(item)
                          for item in json.loads(field_content)]
        if (field_content != canonical([item.model_dump() for item in field_bindings]).encode()
                or plan.skill_sha256 != arguments.skill_sha256
                or digest(plan.model_dump()) != arguments.plan_sha256
                or digest(inputs.model_dump()) != arguments.cases_sha256
                or digest(task.model_dump()) != arguments.task_sha256
                or digest(form_plan.model_dump()) != arguments.form_plan_sha256
                or state_plan is not None and digest(state_plan.model_dump()) != arguments.state_plan_sha256
                or digest([item.model_dump() for item in field_bindings])
                != arguments.field_binding_sha256):
            raise ValueError('skill_form_execution_selection_mismatch')
        profiles = WebApplicationProfiles(arguments.profiles)
        pages = SiteKnowledgeStore(arguments.pages, profiles)
        store = SiteSkillStore(arguments.store, profiles, pages)
        report = audit_site_skill_form_execution(
            store, plan, inputs, arguments.case_key, profiles, task, form_plan,
            field_bindings, arguments.database, arguments.run_id, state_plan)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError):
        parser.exit(1, ERROR + '\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

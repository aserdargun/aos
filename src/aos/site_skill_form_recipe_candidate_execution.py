"""Source-bound development execution of quarantined recipe candidates."""

from contextlib import nullcontext
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .dataset_audit import audit_snapshot
from .learning_events import review_learning_events_snapshot
from .site_skill_case_binding import parameter_variant_sha256
from .site_skill_form_invocation_audit import _timestamp
from .site_skill_form_recipe import (
    SiteSkillFormRecipeInvocation, revalidate_site_skill_form_recipe_invocation)
from .site_skill_form_recipe_audit import audit_site_skill_form_recipe_execution
from .web_application import Checksum


class CandidateExecutionAdmission(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    candidate_sha256: Checksum
    source_group_sha256: Checksum
    source_run_ref: Checksum
    source_parameter_variant_sha256: Checksum
    parameter_variant_sha256: Checksum
    invocation_sha256: Checksum
    recipe_sha256: Checksum
    skill_sha256: Checksum
    profile_sha256: Checksum
    purpose: Literal['development_variation']
    independent_held_out: Literal[False]
    dataset_ingestion_authorized: Literal[False]
    execution_authorized: Literal[False]

    @field_validator('synthetic', mode='before')
    @classmethod
    def exact_true(cls, value):
        if value is not True:
            raise ValueError('candidate_execution_boolean_invalid')
        return value

    @field_validator('independent_held_out', 'dataset_ingestion_authorized',
                     'execution_authorized', mode='before')
    @classmethod
    def exact_false(cls, value):
        if value is not False:
            raise ValueError('candidate_execution_cannot_grant_authority')
        return value


def verify_candidate_admission(admission, invocation) -> CandidateExecutionAdmission:
    admission = CandidateExecutionAdmission.model_validate_json(canonical(
        admission.model_dump(mode='json') if isinstance(admission, CandidateExecutionAdmission)
        else admission))
    invocation = SiteSkillFormRecipeInvocation.model_validate_json(canonical(invocation))
    if (admission.invocation_sha256 != digest(invocation.model_dump(mode='json'))
            or any(getattr(admission, key) != getattr(invocation, key)
                   for key in ('recipe_sha256', 'skill_sha256', 'profile_sha256'))
            or admission.parameter_variant_sha256 == admission.source_parameter_variant_sha256):
        raise ValueError('candidate_execution_admission_mismatch')
    return admission


def candidate_admission_payload(admission) -> dict:
    payload = admission.model_dump(mode='json')
    return {'admission_sha256': digest(payload), 'admission': payload}


def prepare_candidate_execution(candidate, candidate_sha256, invocation, plan, inputs,
                                case_key, *, requested_partition='development_variation'):
    from .site_skill_form_recipe_candidate import parse_site_skill_form_recipe_candidate

    candidate = parse_site_skill_form_recipe_candidate(candidate).model_dump(mode='json')
    invocation = SiteSkillFormRecipeInvocation.model_validate_json(canonical(invocation))
    if requested_partition != 'development_variation':
        raise ValueError('candidate_descendants_are_not_independent_held_out')
    case = next((item for item in inputs.cases if item.case_key == case_key), None)
    planned = next((item for item in plan.cases if item.case_key == case_key), None)
    if case is None or planned is None:
        raise ValueError('candidate_execution_case_missing')
    variant = parameter_variant_sha256(case.parameters)
    if (digest(candidate) != candidate_sha256
            or digest(candidate['recipe']) != invocation.recipe_sha256
            or digest(candidate['skill']) != invocation.skill_sha256
            or invocation.case_key != case_key
            or invocation.skill_plan_sha256 != digest(plan.model_dump(mode='json'))
            or invocation.case_inputs_sha256 != digest(inputs.model_dump(mode='json'))
            or inputs.plan_sha256 != invocation.skill_plan_sha256
            or planned.cohort != 'development'
            or planned.parameter_variant_sha256 != variant
            or candidate['source_parameter_variant_sha256'] not in plan.source_variant_sha256
            or variant == candidate['source_parameter_variant_sha256']):
        raise ValueError('candidate_execution_case_or_source_mismatch')
    return verify_candidate_admission({
        'schema_version': '1.0', 'synthetic': True,
        'candidate_sha256': candidate_sha256,
        'source_group_sha256': candidate['source_group_sha256'],
        'source_run_ref': candidate['source_run_ref'],
        'source_parameter_variant_sha256': candidate['source_parameter_variant_sha256'],
        'parameter_variant_sha256': variant,
        'invocation_sha256': digest(invocation.model_dump(mode='json')),
        'recipe_sha256': invocation.recipe_sha256, 'skill_sha256': invocation.skill_sha256,
        'profile_sha256': invocation.profile_sha256,
        'purpose': 'development_variation', 'independent_held_out': False,
        'dataset_ingestion_authorized': False, 'execution_authorized': False,
    }, invocation.model_dump(mode='json'))


def revalidate_candidate_execution(
        admission, candidate_directory: Path, *, database, profiles, pages,
        source_parameters, store, plan, inputs, case_key, task, form_plan, state_plan,
        field_bindings, recipe, invocation, _audited_snapshot=None,
        owned_source_directory: Path | None = None,
        owned_source_manifest_sha256: str | None = None):
    from .site_skill_form_recipe_candidate import load_site_skill_form_recipe_candidate

    admission = verify_candidate_admission(admission, invocation)
    candidate = load_site_skill_form_recipe_candidate(
        candidate_directory, admission.candidate_sha256, database=database,
        profiles=profiles, pages=pages, source_parameters=source_parameters,
        owned_source_directory=owned_source_directory,
        owned_source_manifest_sha256=owned_source_manifest_sha256,
        _audited_snapshot=_audited_snapshot)
    expected = prepare_candidate_execution(
        candidate, admission.candidate_sha256, invocation, plan, inputs, case_key)
    if expected != admission or form_plan.body_sha256 == candidate['source_body_sha256']:
        raise ValueError('candidate_execution_source_or_body_reused')
    return revalidate_site_skill_form_recipe_invocation(
        invocation, store, plan, inputs, case_key, profiles, task, form_plan,
        state_plan, field_bindings, recipe)


class CandidateExecutionReport(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['source_bound_development_execution']
    admission: CandidateExecutionAdmission
    admission_sha256: Checksum
    source_group_sha256: Checksum
    source_run_ref: Checksum
    execution_run_ref: Checksum
    source_snapshot_sha256: Checksum
    recipe_audit_sha256: Checksum
    admission_observation_ref: Checksum
    source_bound: Literal[True]
    executable_recipe_executed: Literal[True]
    source_and_execution_events_disjoint: Literal[True]
    held_out_independence_verified: Literal[False]
    site_outcome_verified: Literal[False]
    skill_validated: Literal[False]
    dataset_ingestion_authorized: Literal[False]
    training_ready: Literal[False]
    activation_authorized: Literal[False]

    @field_validator('synthetic', 'source_bound', 'executable_recipe_executed',
                     'source_and_execution_events_disjoint', mode='before')
    @classmethod
    def exact_true(cls, value):
        if value is not True:
            raise ValueError('candidate_execution_report_boolean_invalid')
        return value

    @field_validator('held_out_independence_verified', 'site_outcome_verified',
                     'skill_validated', 'dataset_ingestion_authorized',
                     'training_ready', 'activation_authorized', mode='before')
    @classmethod
    def exact_false(cls, value):
        if value is not False:
            raise ValueError('candidate_execution_report_cannot_grant_authority')
        return value

    @model_validator(mode='after')
    def consistent_lineage(self):
        if (self.admission_sha256 != digest(self.admission.model_dump(mode='json'))
                or self.source_group_sha256 != self.admission.source_group_sha256
                or self.source_run_ref != self.admission.source_run_ref
                or self.execution_run_ref == self.source_run_ref):
            raise ValueError('candidate_execution_report_lineage_mismatch')
        return self


def audit_candidate_execution(admission, candidate_directory: Path, *, run_id,
                              _audited_snapshot=None, **sources):
    from .site_skill_form_recipe_candidate import load_site_skill_form_recipe_candidate

    admission = verify_candidate_admission(admission, sources['invocation'])
    if digest({'run_id': run_id}) == admission.source_run_ref:
        raise ValueError('candidate_source_run_is_not_new_execution')
    context = (audit_snapshot(sources['database']) if _audited_snapshot is None
               else nullcontext(_audited_snapshot))
    with context as (snapshot, identity):
        revalidate_candidate_execution(admission, candidate_directory,
                                       **sources, _audited_snapshot=(snapshot, identity))
        candidate = load_site_skill_form_recipe_candidate(
            candidate_directory, admission.candidate_sha256,
            **{key: sources[key] for key in ('database', 'profiles', 'pages', 'source_parameters')},
            **{key: sources[key] for key in ('owned_source_directory', 'owned_source_manifest_sha256')
               if key in sources},
            _audited_snapshot=(snapshot, identity))
        report = audit_site_skill_form_recipe_execution(
            *(sources[key] for key in ('store', 'plan', 'inputs', 'case_key', 'profiles',
                                      'task', 'form_plan', 'state_plan', 'field_bindings',
                                      'recipe', 'invocation', 'database')),
            run_id, _audited_snapshot=(snapshot, identity))
        rows = snapshot.execute("SELECT * FROM observations WHERE run_id=? "
                                "AND kind='skill.recipe_candidate_admission'", (run_id,)).fetchall()
        states = snapshot.execute('SELECT * FROM state_snapshots WHERE run_id=? '
                                  'AND state_version IN (0,1) ORDER BY state_version',
                                  (run_id,)).fetchall()
        payload = candidate_admission_payload(admission)
        if (len(rows) != 1 or len(states) != 2
                or rows[0]['action_id'] is not None
                or rows[0]['step_id'] != states[0]['step_id']
                or rows[0]['payload_json'] != canonical(payload)
                or not _timestamp(states[0]['created_at']) <= _timestamp(rows[0]['created_at'])
                <= _timestamp(states[1]['created_at'])):
            raise ValueError('candidate_execution_admission_not_recorded_before_actions')
        events = review_learning_events_snapshot(snapshot, identity, run_id)['events']
        source_events = set(candidate['skill']['source_event_ids'])
        execution_events = {event['event_id'] for event in events if event['role'] == 'system1'}
        if len(execution_events) != 6 or source_events & execution_events:
            raise ValueError('candidate_execution_event_lineage_reused')
        return CandidateExecutionReport.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'status': 'source_bound_development_execution',
            'admission': admission, 'admission_sha256': digest(admission.model_dump(mode='json')),
            'source_group_sha256': admission.source_group_sha256,
            'source_run_ref': admission.source_run_ref,
            'execution_run_ref': digest({'run_id': run_id}),
            'source_snapshot_sha256': identity['sha256'],
            'recipe_audit_sha256': digest(report),
            'admission_observation_ref': digest({'observation_id': rows[0]['observation_id']}),
            'source_bound': True, 'executable_recipe_executed': True,
            'source_and_execution_events_disjoint': True,
            'held_out_independence_verified': False, 'site_outcome_verified': False,
            'skill_validated': False, 'dataset_ingestion_authorized': False,
            'training_ready': False, 'activation_authorized': False,
        }).model_dump(mode='json')

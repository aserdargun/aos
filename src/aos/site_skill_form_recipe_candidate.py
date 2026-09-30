"""Derive immutable, unreviewed executable-recipe candidates from audited form runs."""

import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Literal
from uuid import uuid4
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import Option, Prediction, State, TypedModel, canonical, digest
from .dataset_audit import audit_snapshot
from .dataset_reviews import source_fingerprint as run_source_fingerprint
from .decision import decision_request
from .desktop_tasks import Approval
from .learning_events import review_learning_events_snapshot
from .lifecycle import private_directory
from .remote_form_learning_source import inspect_remote_form_learning_source
from .site_knowledge import SiteKnowledgeStore, SitePageDraft
from .site_skill import SiteSkillDraft, SiteSkillStore
from .site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                      parameter_variant_sha256)
from .site_skill_form_invocation_audit import _state_history, _verify_stage_chain
from .site_skill_form_recipe import (SiteSkillFormRecipe, SiteSkillFormRecipePrecondition,
                                     SiteSkillFormRecipeOutcome, SiteSkillFormRecipeStep,
                                     SUPPORTED_RECIPE_ORDERS)
from .site_skill_validation import SiteSkillValidationPlan
from .web_application import Checksum, Key, WebApplicationProfiles
from .web_application_binding import WebTaskAdmissionDraft
from .web_application_binding import WebTaskContract
from .web_https_form_state_probe import WebHTTPSFormStatePlan
from .web_https_form_transport import WebHTTPSFormPlan, form_body
from .workspace_identity import open_existing_workspace


DERIVER_VERSION = 'audited-form-recipe-candidate-v1'
MAX_CANDIDATE_BYTES = 65536
MAX_CANDIDATES = 1000
RECIPE_OPERATIONS = ('open_entry', 'read_state_before', 'fill_form', 'submit_form',
                     'read_receipt', 'read_state_after')
TOOL_OPERATIONS = {
    'browser.form.open': 'open_entry',
    'browser.form.state_before': 'read_state_before',
    'browser.form.fill': 'fill_form',
    'browser.form.submit': 'submit_form',
    'browser.form.receipt': 'read_receipt',
    'browser.form.state_after': 'read_state_after',
}


class CandidatePrecondition(TypedModel):
    precondition_key: Key
    kind: Literal['entry_form_available', 'declared_state_before']


class CandidateStepName(TypedModel):
    operation: Literal['open_entry', 'read_state_before', 'fill_form', 'submit_form',
                       'read_receipt', 'read_state_after']
    step_key: Key


class CandidateOutcomeName(TypedModel):
    outcome_key: Key
    parameter_key: Key


class SiteSkillFormRecipeCandidateSourceContext(TypedModel):
    kind: Literal['owned_fixed_template_v1']
    manifest_sha256: Checksum
    invocation_sha256: Checksum


class SiteSkillFormRecipeCandidateAnnotation(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    skill_key: Key
    page_key: Key
    page_draft_sha256: Checksum
    task_key: Key
    parameter_bindings: tuple[SiteSkillFormFieldBinding, ...] = Field(min_length=1, max_length=8)
    preconditions: tuple[CandidatePrecondition, ...] = Field(min_length=2, max_length=2)
    outcome: CandidateOutcomeName
    operation_step_keys: tuple[CandidateStepName, ...] = Field(min_length=6, max_length=6)

    @field_validator('synthetic', mode='before')
    @classmethod
    def require_synthetic_true(cls, value):
        if value is not True:
            raise ValueError('recipe_candidate_annotation_must_be_synthetic')
        return value

    @model_validator(mode='after')
    def exact_semantic_mapping(self):
        parameter_keys = [item.parameter_key for item in self.parameter_bindings]
        field_names = [item.form_field_name for item in self.parameter_bindings]
        precondition_keys = [item.precondition_key for item in self.preconditions]
        step_keys = [item.step_key for item in self.operation_step_keys]
        operations = [item.operation for item in self.operation_step_keys]
        if (len(parameter_keys) != len(set(parameter_keys))
                or len(field_names) != len(set(field_names))
                or precondition_keys != sorted(set(precondition_keys))
                or {item.kind for item in self.preconditions}
                != {'entry_form_available', 'declared_state_before'}
                or operations != sorted(RECIPE_OPERATIONS)
                or len(step_keys) != len(set(step_keys))
                or self.outcome.parameter_key not in parameter_keys):
            raise ValueError('recipe_candidate_annotation_mapping_invalid')
        return self


class CandidateStageLineage(TypedModel):
    operation: Literal['open_entry', 'read_state_before', 'fill_form', 'submit_form',
                       'read_receipt', 'read_state_after']
    step_key: Key
    action_ref: Checksum
    decision_ref: Checksum
    approval_ref: Checksum
    decision_state_ref: Checksum
    execute_state_ref: Checksum
    call_ref: Checksum
    input_sha256: Checksum
    prediction_sha256: Checksum
    source_event_id: str = Field(pattern=r'^learning-[a-f0-9]{64}$')
    source_verification_refs: tuple[Checksum, ...] = Field(max_length=16)


class SiteSkillFormRecipeCandidate(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    derivation_kind: Literal['audited_form_trajectory']
    deriver_version: Literal['audited-form-recipe-candidate-v1']
    status: Literal['unreviewed_executable_recipe_candidate']
    source_run_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    source_run_ref: Checksum
    source_group_sha256: Checksum
    source_fingerprint_sha256: Checksum
    source_body_sha256: Checksum
    source_parameter_variant_sha256: Checksum
    profile_sha256: Checksum
    task_sha256: Checksum
    form_plan_sha256: Checksum
    state_plan_sha256: Checksum
    field_binding_sha256: Checksum
    deployment_sha256: Checksum
    source_event_ids_by_role: dict[Literal['system1', 'system2'], list[str]]
    source_verification_ids: list[Key]
    source_stage_refs: tuple[CandidateStageLineage, ...] = Field(min_length=6, max_length=6)
    annotation: SiteSkillFormRecipeCandidateAnnotation
    field_bindings: tuple[SiteSkillFormFieldBinding, ...] = Field(min_length=1, max_length=8)
    skill: SiteSkillDraft
    recipe: SiteSkillFormRecipe
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]
    site_outcome_verified: Literal[False]
    skill_executed: Literal[False]

    @field_validator('synthetic', mode='before')
    @classmethod
    def synthetic_must_be_boolean_true(cls, value):
        if value is not True:
            raise ValueError('recipe_candidate_must_be_synthetic')
        return value

    @field_validator('reviewed', 'activation_authorized', 'training_ready',
                     'site_outcome_verified', 'skill_executed', mode='before')
    @classmethod
    def claims_must_be_boolean_false(cls, value):
        if value is not False:
            raise ValueError('recipe_candidate_cannot_claim_execution_or_authority')
        return value

    @model_validator(mode='after')
    def candidate_matches_artifacts(self):
        if (self.source_event_ids_by_role.get('system2') != []
                or len(self.source_event_ids_by_role.get('system1', [])) != 6
                or len(set(self.source_event_ids_by_role.get('system1', []))) != 6
                or self.source_event_ids_by_role['system1']
                != sorted(self.source_event_ids_by_role['system1'])
                or set(self.source_event_ids_by_role['system1'])
                != {item.source_event_id for item in self.source_stage_refs}
                or self.source_verification_ids != sorted(set(self.source_verification_ids))
                or [item.operation for item in self.source_stage_refs]
                not in [list(order) for order in SUPPORTED_RECIPE_ORDERS]
                or len({item.source_event_id for item in self.source_stage_refs}) != 6
                or self.skill.profile_sha256 != self.profile_sha256
                or self.skill.page_draft_sha256 != self.annotation.page_draft_sha256
                or self.skill.task_key != self.annotation.task_key
                or self.skill.skill_key != self.annotation.skill_key
                or digest(self.skill.model_dump()) != self.recipe.skill_sha256
                or digest([item.model_dump() for item in self.field_bindings])
                != self.field_binding_sha256
                or self.recipe.profile_sha256 != self.profile_sha256
                or self.recipe.task_sha256 != self.task_sha256
                or self.recipe.page_draft_sha256 != self.annotation.page_draft_sha256
                or self.recipe.field_binding_sha256 != self.field_binding_sha256):
            raise ValueError('recipe_candidate_artifact_binding_invalid')
        expected_keys = {item.operation: item.step_key for item in self.annotation.operation_step_keys}
        if (tuple(item.operation for item in self.recipe.steps)
                != tuple(item.operation for item in self.source_stage_refs)
                or any(step.step_key != expected_keys[step.operation] for step in self.recipe.steps)
                or tuple(item.model_dump() for item in self.field_bindings)
                != tuple(item.model_dump() for item in self.annotation.parameter_bindings)
                or self.recipe.outcome.outcome_key != self.skill.expected_outcome_key):
            raise ValueError('recipe_candidate_source_binding_invalid')
        expected_group = digest({'domain': 'site_skill_form_recipe_candidate_lineage_v1',
                                 'source_run_ref': self.source_run_ref,
                                 'profile_sha256': self.profile_sha256,
                                 'task_sha256': self.task_sha256})
        if self.source_group_sha256 != expected_group:
            raise ValueError('recipe_candidate_source_group_invalid')
        return self


class OwnedSiteSkillFormRecipeCandidate(SiteSkillFormRecipeCandidate):
    schema_version: Literal['1.1']
    source_context: SiteSkillFormRecipeCandidateSourceContext


def _parameter_variant_sha256(parameters: dict[str, str], bindings) -> str:
    if (not isinstance(parameters, dict)
            or set(parameters) != {item.parameter_key for item in bindings}
            or any(not isinstance(value, str) for value in parameters.values())):
        raise ValueError('recipe_candidate_source_parameters_invalid')
    return parameter_variant_sha256(parameters)


def _validate_source_parameters(parameters: dict[str, str], bindings, plan: WebHTTPSFormPlan):
    _parameter_variant_sha256(parameters, bindings)
    ordered = tuple((binding.form_field_name, parameters[binding.parameter_key])
                    for binding in bindings)
    body = form_body(ordered)
    if len(body) != plan.body_bytes or hashlib.sha256(body).hexdigest() != plan.body_sha256:
        raise ValueError('recipe_candidate_source_parameters_do_not_match_form_plan')
    return hashlib.sha256(body).hexdigest()


def _reference(value: str) -> str:
    return digest({'reference': value})


def _source_context(database: Path, audited_snapshot):
    return (audit_snapshot(database) if audited_snapshot is None
            else nullcontext(audited_snapshot))


def _canonical_source(directory: Path, name: str, model, *, limit=20000):
    from .local_app import private_read

    content = private_read(directory / name, limit)
    checked = model.model_validate_json(content)
    if content != canonical(checked.model_dump(mode='json')).encode('utf-8'):
        raise ValueError('owned_recipe_candidate_source_not_canonical')
    return checked


def _owned_v1_source(directory: Path, profiles: WebApplicationProfiles,
                     annotation: SiteSkillFormRecipeCandidateAnnotation,
                     expected_manifest_sha256: str | None,
                     pages: SiteKnowledgeStore | None = None):
    from .local_app import private_read
    from .owned_form_invocation_session import verify_owned_form_invocation_manifest
    from .site_skill_form_invocation import (SiteSkillFormInvocation,
                                             compile_site_skill_form_invocation)
    from .web_https_form_state_probe import WebHTTPSFormStatePlan
    from .web_https_form_transport import WebHTTPSFormPlan

    manifest_bytes = private_read(directory / 'manifest.json', 16384)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (expected_manifest_sha256 is not None
            and manifest_sha256 != expected_manifest_sha256):
        raise ValueError('owned_recipe_candidate_manifest_changed')
    manifest = verify_owned_form_invocation_manifest(directory, manifest_sha256)
    if (manifest['mode'] != 'owned_synthetic_form_invocation'
            or manifest['schema_version'] != '1.0' or 'recipe_sha256' in manifest):
        raise ValueError('owned_recipe_candidate_requires_v1_invocation_source')
    if (profiles.root.absolute() != directory.absolute() / 'profiles'
            or pages is not None and (pages.root.absolute() != directory.absolute() / 'site-knowledge'
                                      or pages.profiles.root.absolute() != profiles.root.absolute())):
        raise ValueError('owned_recipe_candidate_sources_must_be_fixed')
    task = _canonical_source(directory, 'remote-entry-task.json', WebTaskContract)
    form_plan = _canonical_source(directory, 'remote-form-plan.json', WebHTTPSFormPlan)
    state_plan = _canonical_source(directory, 'remote-form-state-plan.json', WebHTTPSFormStatePlan)
    plan = _canonical_source(directory, 'remote-form-skill-plan.json', SiteSkillValidationPlan)
    inputs = _canonical_source(directory, 'remote-form-skill-case-inputs.json', SiteSkillCaseInputs)
    page = _canonical_source(directory, 'site-page-draft.json', SitePageDraft)
    skill = _canonical_source(directory, 'site-skill-draft.json', SiteSkillDraft)
    bindings_bytes = private_read(directory / 'remote-form-skill-field-bindings.json', 8192)
    bindings_value = json.loads(bindings_bytes)
    if (not isinstance(bindings_value, list)
            or canonical(bindings_value).encode('utf-8') != bindings_bytes):
        raise ValueError('owned_recipe_candidate_field_bindings_invalid')
    bindings = tuple(SiteSkillFormFieldBinding.model_validate_json(canonical(item))
                     for item in bindings_value)
    from .web_https_form_state_probe import verify_submitted_field_binding
    from .web_https_form_transport import exact_form_fields

    raw_value = private_read(directory / 'remote-form-value.txt', 4096).decode('utf-8')
    if not raw_value or not raw_value.isprintable():
        raise ValueError('owned_recipe_candidate_source_value_invalid')
    configured_fields = {item.form_field_name for item in bindings}
    if ({item.form_field_name for item in annotation.parameter_bindings} != configured_fields
            or len(annotation.parameter_bindings) != len(configured_fields)):
        raise ValueError('owned_recipe_candidate_annotation_field_scope_invalid')
    values_by_field = {name: raw_value for name in configured_fields}
    source_parameters = {item.parameter_key: values_by_field[item.form_field_name]
                         for item in annotation.parameter_bindings}
    ordered = tuple((item.form_field_name, source_parameters[item.parameter_key])
                    for item in annotation.parameter_bindings)
    fields = (exact_form_fields(*ordered[0]) if len(ordered) == 1 else
              exact_form_fields(None, None, [
                  {'name': name, 'value': value} for name, value in ordered]))
    verify_submitted_field_binding(state_plan, form_plan, fields)
    skill_store = SiteSkillStore(directory / 'site-skills', profiles,
                                 SiteKnowledgeStore(directory / 'site-knowledge', profiles))
    invocation = compile_site_skill_form_invocation(
        skill_store, plan, inputs, 'dev-query', profiles, task, form_plan,
        state_plan, list(bindings))
    invocation = SiteSkillFormInvocation.model_validate_json(canonical(invocation))
    invocation_sha256 = digest(invocation.model_dump(mode='json'))
    if (invocation_sha256 != manifest['invocation_sha256']
            or manifest['profile_sha256'] != task.profile_sha256
            or manifest['skill_sha256'] != plan.skill_sha256
            or manifest['form_plan_sha256'] != digest(form_plan.model_dump())
            or manifest['state_plan_sha256'] != digest(state_plan.model_dump())
            or page.page_key != annotation.page_key
            or digest(page.model_dump()) != annotation.page_draft_sha256
            or task.task_key != annotation.task_key):
        raise ValueError('owned_recipe_candidate_invocation_binding_changed')
    return {'manifest_sha256': manifest_sha256, 'manifest': manifest,
            'invocation_sha256': invocation_sha256, 'source_parameters': source_parameters,
            'task': task, 'form_plan': form_plan, 'state_plan': state_plan,
            'profile_sha256': manifest['profile_sha256'], 'page': page,
            'skill': skill, 'bindings': bindings, 'skill_store': skill_store,
            'plan': plan, 'inputs': inputs, 'invocation': invocation}


class OwnedSiteSkillFormRecipeCandidateSession:
    def __init__(self, *, directory: Path, manifest_sha256: str,
                 candidate_directory: Path, database: Path,
                 profiles: WebApplicationProfiles, pages: SiteKnowledgeStore):
        if (not isinstance(directory, Path) or not isinstance(candidate_directory, Path)
                or not isinstance(database, Path) or not isinstance(profiles, WebApplicationProfiles)
                or not isinstance(pages, SiteKnowledgeStore)
                or re.fullmatch(r'[a-f0-9]{64}', str(manifest_sha256)) is None):
            raise ValueError('owned_recipe_candidate_session_invalid')
        directory = directory.absolute()
        if (profiles.root.absolute() != directory / 'profiles'
                or pages.root.absolute() != directory / 'site-knowledge'
                or pages.profiles.root.absolute() != profiles.root.absolute()):
            raise ValueError('owned_recipe_candidate_session_sources_must_be_fixed')
        self.directory = directory
        self.manifest_sha256 = manifest_sha256
        self.candidate_directory = candidate_directory
        self.database = database
        self.profiles = profiles
        self.pages = pages

    def annotation_seed(self) -> SiteSkillFormRecipeCandidateAnnotation:
        from .web_application_binding import WebTaskContract
        from .local_app import private_read
        from .owned_form_invocation_session import verify_owned_form_invocation_manifest

        manifest = verify_owned_form_invocation_manifest(self.directory, self.manifest_sha256)
        if manifest['mode'] != 'owned_synthetic_form_invocation':
            raise ValueError('owned_recipe_candidate_requires_v1_invocation_source')
        task = _canonical_source(self.directory, 'remote-entry-task.json', WebTaskContract)
        page = _canonical_source(self.directory, 'site-page-draft.json', SitePageDraft)
        skill = _canonical_source(self.directory, 'site-skill-draft.json', SiteSkillDraft)
        raw_bindings_bytes = private_read(
            self.directory / 'remote-form-skill-field-bindings.json', 8192)
        raw_bindings = json.loads(raw_bindings_bytes)
        if canonical(raw_bindings).encode('utf-8') != raw_bindings_bytes:
            raise ValueError('owned_recipe_candidate_field_bindings_invalid')
        bindings = tuple(SiteSkillFormFieldBinding.model_validate_json(canonical(item))
                         for item in raw_bindings)
        step_by_operation = {operation: operation.replace('_', '-')
                             for operation in sorted(RECIPE_OPERATIONS)}
        return SiteSkillFormRecipeCandidateAnnotation.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'skill_key': (skill.skill_key + '-derived')[:64],
            'page_key': page.page_key, 'page_draft_sha256': digest(page.model_dump()),
            'task_key': task.task_key,
            'parameter_bindings': tuple(item.model_dump(mode='json') for item in bindings),
            'preconditions': (
                {'precondition_key': 'baseline-known', 'kind': 'declared_state_before'},
                {'precondition_key': 'form-available', 'kind': 'entry_form_available'}),
            'outcome': {'outcome_key': skill.expected_outcome_key,
                        'parameter_key': bindings[0].parameter_key},
            'operation_step_keys': tuple(
                {'operation': operation, 'step_key': step_by_operation[operation]}
                for operation in sorted(RECIPE_OPERATIONS))})

    def _check_pins(self, run_id: str, source_run_ref: str, invocation_sha256: str):
        if (not isinstance(source_run_ref, str) or re.fullmatch(r'[a-f0-9]{64}', source_run_ref) is None
                or not isinstance(invocation_sha256, str)
                or re.fullmatch(r'[a-f0-9]{64}', invocation_sha256) is None
                or source_run_ref != digest({'run_id': run_id})):
            raise ValueError('owned_recipe_candidate_source_pin_changed')

    def context(self, run_id: str, invocation_sha256: str) -> dict:
        annotation = self.annotation_seed()
        source = _owned_v1_source(self.directory, self.profiles, annotation,
                                  self.manifest_sha256, self.pages)
        self._check_pins(run_id, digest({'run_id': run_id}), invocation_sha256)
        if source['invocation_sha256'] != invocation_sha256:
            raise ValueError('owned_recipe_candidate_invocation_pin_changed')
        return {'schema_version': '1.0', 'available': True,
                'mode': 'owned_synthetic_form_invocation', 'lifecycle': 'audited',
                'source_run_ref': digest({'run_id': run_id}),
                'invocation_sha256': invocation_sha256,
                'profile_sha256': source['profile_sha256'],
                'task_sha256': digest(source['task'].model_dump()),
                'page_draft_sha256': annotation.page_draft_sha256,
                'task_key': source['task'].task_key,
                'form_fields': sorted(item.form_field_name for item in source['bindings']),
                'annotation_seed': annotation.model_dump(mode='json')}

    def preview(self, run_id: str, invocation_sha256: str, annotation) -> tuple[dict, str]:
        self._check_pins(run_id, digest({'run_id': run_id}), invocation_sha256)
        candidate = derive_site_skill_form_recipe_candidate(
            self.database, run_id, profiles=self.profiles, pages=self.pages,
            annotation=annotation, owned_source_directory=self.directory,
            owned_source_manifest_sha256=self.manifest_sha256)
        parsed = parse_site_skill_form_recipe_candidate(candidate)
        if (parsed.schema_version != '1.1'
                or parsed.source_context.invocation_sha256 != invocation_sha256
                or parsed.source_context.manifest_sha256 != self.manifest_sha256):
            raise ValueError('owned_recipe_candidate_context_changed')
        return parsed.model_dump(mode='json'), digest(parsed.model_dump(mode='json'))

    def publish(self, run_id: str, invocation_sha256: str, annotation,
                confirm_sha256: str) -> tuple[dict, str]:
        candidate, checksum = self.preview(run_id, invocation_sha256, annotation)
        if confirm_sha256 != checksum:
            raise ValueError('exact_recipe_candidate_confirmation_required')
        persist_site_skill_form_recipe_candidate(
            self.candidate_directory, candidate, confirm_sha256=confirm_sha256)
        return candidate, checksum

    def inspect(self, run_id: str, invocation_sha256: str, checksum: str) -> tuple[dict, str]:
        self._check_pins(run_id, digest({'run_id': run_id}), invocation_sha256)
        candidate = load_site_skill_form_recipe_candidate(
            self.candidate_directory, checksum, database=self.database,
            profiles=self.profiles, pages=self.pages,
            owned_source_directory=self.directory,
            owned_source_manifest_sha256=self.manifest_sha256)
        parsed = parse_site_skill_form_recipe_candidate(candidate)
        if (parsed.schema_version != '1.1'
                or parsed.source_run_ref != digest({'run_id': run_id})
                or parsed.source_context.invocation_sha256 != invocation_sha256):
            raise ValueError('owned_recipe_candidate_context_changed')
        return parsed.model_dump(mode='json'), checksum

    def execution_source(self, run_id: str, invocation_sha256: str,
                         checksum: str) -> tuple[dict, dict]:
        candidate, checked = self.inspect(run_id, invocation_sha256, checksum)
        parsed = parse_site_skill_form_recipe_candidate(candidate)
        source = _owned_v1_source(
            self.directory, self.profiles, parsed.annotation,
            self.manifest_sha256, self.pages)
        if (checked != checksum or source['invocation_sha256'] != invocation_sha256
                or source['manifest_sha256'] != self.manifest_sha256):
            raise ValueError('owned_recipe_candidate_execution_source_changed')
        source['profiles'] = self.profiles
        source['pages'] = self.pages
        return candidate, source

    def reinspect(self, source_run_ref: str, invocation_sha256: str,
                  checksum: str) -> tuple[dict, str]:
        stored = _read_candidate(self.candidate_directory, checksum)
        if (stored.schema_version != '1.1'
                or stored.source_run_ref != source_run_ref
                or stored.source_context.invocation_sha256 != invocation_sha256):
            raise ValueError('owned_recipe_candidate_context_changed')
        candidate = load_site_skill_form_recipe_candidate(
            self.candidate_directory, checksum, database=self.database,
            profiles=self.profiles, pages=self.pages,
            owned_source_directory=self.directory,
            owned_source_manifest_sha256=self.manifest_sha256)
        return candidate, checksum


def derive_site_skill_form_recipe_candidate(
        database: Path, run_id: str, *, profiles: WebApplicationProfiles,
        pages: SiteKnowledgeStore, annotation: SiteSkillFormRecipeCandidateAnnotation | dict,
        source_parameters: dict[str, str] | None = None,
        owned_source_directory: Path | None = None,
        owned_source_manifest_sha256: str | None = None,
        _audited_snapshot=None) -> dict:
    annotation = (annotation if isinstance(annotation, SiteSkillFormRecipeCandidateAnnotation)
                  else SiteSkillFormRecipeCandidateAnnotation.model_validate_json(canonical(annotation)))
    owned_source = (_owned_v1_source(owned_source_directory, profiles, annotation,
                                     owned_source_manifest_sha256, pages)
                    if owned_source_directory is not None else None)
    if owned_source is not None:
        if source_parameters is not None and source_parameters != owned_source['source_parameters']:
            raise ValueError('owned_recipe_candidate_parameters_are_server_bound')
        source_parameters = owned_source['source_parameters']
    if source_parameters is None:
        raise ValueError('recipe_candidate_source_parameters_invalid')
    with _source_context(database, _audited_snapshot) as (snapshot, identity):
        binding = snapshot.execute('''SELECT * FROM desktop_remote_form_bindings WHERE run_id=?''',
                                   (run_id,)).fetchone()
        state_binding = snapshot.execute('''SELECT * FROM desktop_remote_form_state_bindings WHERE run_id=?''',
                                         (run_id,)).fetchone()
        if binding is None or state_binding is None:
            raise ValueError('recipe_candidate_requires_audited_state_form_run')
        draft = WebTaskAdmissionDraft.model_validate_json(binding['draft_json'])
        plan = WebHTTPSFormPlan.model_validate_json(binding['plan_json'])
        state_plan = WebHTTPSFormStatePlan.model_validate_json(state_binding['state_plan_json'])
        if (canonical(draft.model_dump(mode='json')) != binding['draft_json']
                or canonical(plan.model_dump(mode='json')) != binding['plan_json']
                or canonical(state_plan.model_dump(mode='json')) != state_binding['state_plan_json']
                or annotation.task_key != draft.task.task_key
                or annotation.page_key == ''
                or annotation.page_draft_sha256 == ''):
            raise ValueError('recipe_candidate_source_contract_invalid')
        if owned_source is not None and (
                canonical(owned_source['task'].model_dump(mode='json'))
                != canonical(draft.task.model_dump(mode='json'))
                or canonical(owned_source['form_plan'].model_dump(mode='json'))
                != canonical(plan.model_dump(mode='json'))
                or canonical(owned_source['state_plan'].model_dump(mode='json'))
                != canonical(state_plan.model_dump(mode='json'))
                or owned_source['profile_sha256'] != binding['profile_sha256']):
            raise ValueError('owned_recipe_candidate_trajectory_binding_changed')
        host = urlsplit(draft.task.entry_url).hostname
        if not host or not host.endswith('.invalid'):
            raise ValueError('recipe_candidate_requires_owned_synthetic_origin')
        if snapshot.execute("SELECT count(*) FROM model_calls WHERE run_id=? AND role='system2'",
                            (run_id,)).fetchone()[0] != 0:
            raise ValueError('recipe_candidate_system2_calls_unsupported')
        page = pages.get(annotation.page_draft_sha256)
        profile = profiles.get(binding['profile_sha256'])
        if (page.profile_sha256 != binding['profile_sha256']
                or page.page_key != annotation.page_key
                or annotation.task_key not in profile.task_keys
                or page.origin not in profile.allowed_origins
                or draft.task.profile_sha256 != binding['profile_sha256']
                or state_binding['state_plan_sha256'] != digest(state_plan.model_dump())
                or state_plan.form_plan_sha256 != digest(plan.model_dump())
                or state_plan.submitted_field_name is None):
            raise ValueError('recipe_candidate_page_task_scope_mismatch')
        bindings = annotation.parameter_bindings
        outcome_binding = next((item for item in bindings
                                if item.parameter_key == annotation.outcome.parameter_key), None)
        if (outcome_binding is None
                or outcome_binding.form_field_name != state_plan.submitted_field_name):
            raise ValueError('recipe_candidate_field_mapping_scope_invalid')
        source_body_sha256 = _validate_source_parameters(source_parameters, bindings, plan)
        parameter_variant_sha256 = _parameter_variant_sha256(source_parameters, bindings)

        action_rows = snapshot.execute('''SELECT * FROM actions WHERE run_id=?''', (run_id,)).fetchall()
        stage_rows = [row for row in action_rows if row['tool'] in TOOL_OPERATIONS]
        if len(stage_rows) != 6:
            raise ValueError('recipe_candidate_requires_six_distinct_form_actions')
        approvals = snapshot.execute('''SELECT * FROM desktop_approvals WHERE job_id=?''',
                                     (binding['job_id'],)).fetchall()
        source_job = snapshot.execute('SELECT * FROM desktop_tasks WHERE job_id=?',
                                      (binding['job_id'],)).fetchone()
        if source_job is None:
            raise ValueError('recipe_candidate_source_job_missing')
        approvals_by_action = {}
        for row in approvals:
            envelope = Approval.model_validate_json(row['envelope_json'])
            if envelope.action.action_id in approvals_by_action:
                raise ValueError('recipe_candidate_duplicate_approval')
            approvals_by_action[envelope.action.action_id] = (row, envelope)
        ordered_stages = []
        for action in stage_rows:
            pair = approvals_by_action.get(action['action_id'])
            if pair is None:
                raise ValueError('recipe_candidate_stage_approval_missing')
            ordered_stages.append((pair[1].action.state_version, action))
        if len({version for version, _action in ordered_stages}) != 6:
            raise ValueError('recipe_candidate_stage_order_ambiguous')
        ordered_stages.sort(key=lambda pair: pair[0])
        operations = tuple(TOOL_OPERATIONS[action['tool']] for _version, action in ordered_stages)
        if operations not in SUPPORTED_RECIPE_ORDERS:
            raise ValueError('recipe_candidate_source_stage_order_unsupported')

        source = inspect_remote_form_learning_source(
            database, run_id, profiles=profiles.root,
            selected_profile_sha256=binding['profile_sha256'],
            selected_plan_sha256=digest(plan.model_dump()),
            selected_state_plan_sha256=digest(state_plan.model_dump()),
            _audited_snapshot=(snapshot, identity), _recipe_stage_order=operations)
        if (source.get('declared_state_readback_verified') is not True
                or source.get('transport_readback_verified') is not True
                or source.get('site_outcome_verified') is not False
                or source.get('source_event_ids_by_role', {}).get('system2') != []):
            raise ValueError('recipe_candidate_source_audit_failed')
        invocation_sha256 = (owned_source['invocation_sha256']
                             if owned_source is not None else None)
        states = _state_history(snapshot, run_id, invocation_sha256,
                                digest(plan.model_dump()))
        if states[0][1].skill_invocation_sha256 != invocation_sha256:
            raise ValueError('recipe_candidate_source_must_not_be_candidate_derived')
        if snapshot.execute('''SELECT count(*) FROM observations WHERE run_id=?
            AND kind='skill.recipe_candidate_admission' ''', (run_id,)).fetchone()[0]:
            raise ValueError('recipe_candidate_descendant_source_forbidden')
        if owned_source is not None:
            from .site_skill_form_invocation_audit import (
                audit_site_skill_form_invocation_execution)

            audit_site_skill_form_invocation_execution(
                owned_source['skill_store'], owned_source['plan'], owned_source['inputs'],
                'dev-query', profiles, owned_source['task'], plan, state_plan,
                list(owned_source['bindings']), owned_source['invocation'], database, run_id,
                _audited_snapshot=(snapshot, identity))
        _verify_stage_chain(snapshot, run_id, source, states, invocation_sha256,
                            stage_order=operations)
        run_events = review_learning_events_snapshot(snapshot, identity, run_id)['events']
        events_by_decision = {}
        for event in run_events:
            src = event['source']
            if event['role'] == 'system1' and src['decision_id'] is not None:
                if src['decision_id'] in events_by_decision:
                    raise ValueError('recipe_candidate_source_decision_event_ambiguous')
                events_by_decision[src['decision_id']] = event
        operation_keys = {item.operation: item.step_key for item in annotation.operation_step_keys}
        stage_refs = []
        ordered_event_ids = []
        source_verification_ids = set()
        decisions = []
        for operation, (_version, action) in zip(operations, ordered_stages, strict=True):
            decision = snapshot.execute('''SELECT * FROM decisions WHERE decision_id=? AND run_id=?
                AND step_id=?''', (action['decision_id'], run_id, action['step_id'])).fetchone()
            approval_row, approval = approvals_by_action[action['action_id']]
            event = events_by_decision.get(action['decision_id'])
            if (decision is None or event is None or event['source']['call_id'] != decision['call_id']
                    or event['verified_outcome'] is not bool(event['source']['verification_ids'])):
                raise ValueError('recipe_candidate_source_decider_call_unbound')
            decision_state_row, decision_state = states[2 + 5 * len(stage_refs)]
            call = snapshot.execute('''SELECT * FROM model_calls WHERE call_id=? AND run_id=?
                AND step_id=?''', (decision['call_id'], run_id, action['step_id'])).fetchone()
            options_value = json.loads(decision['options_json'])
            if (operation == 'open_entry'):
                label = 'entry GET'
            elif operation == 'read_state_before':
                label = 'state-before GET'
            elif operation == 'fill_form':
                label = 'form fill'
            elif operation == 'submit_form':
                label = 'one form POST'
            elif operation == 'read_receipt':
                label = 'receipt GET'
            else:
                label = 'state-after GET'
            expected_options = [Option(id=operation,
                label='Request separate approval for exact ' + label),
                Option(id='ask_human', label='Ask the user for help')]
            options = [Option.model_validate(option) for option in options_value]
            prediction = Prediction(selected_option=decision['selected_option'],
                                    probabilities=json.loads(decision['probabilities_json']))
            prediction.validate_options(options)
            expected_request = decision_request(decision_state, expected_options)
            if (call is None or options != expected_options
                    or decision['selected_option'] != operation
                    or decision['question'] != expected_request['question']
                    or decision['policy_result'] != 'allow'
                    or decision['confidence'] != prediction.probabilities[operation]
                    or call['role'] != 'system1' or call['status'] != 'ok'
                    or call['deployment_id'] != decision_state.deployment_id
                    or call['request_json'] != canonical(expected_request)
                    or call['response_json'] != prediction.model_dump_json()):
                raise ValueError('recipe_candidate_model_decision_input_or_output_changed')
            source_event_id = event['event_id']
            ordered_event_ids.append(source_event_id)
            source_verification_ids.update(event['source']['verification_ids'])
            decisions.append(decision)
            stage_refs.append(CandidateStageLineage(
                operation=operation, step_key=operation_keys[operation],
                action_ref=_reference(action['action_id']),
                decision_ref=_reference(action['decision_id']),
                approval_ref=_reference(approval.approval_id),
                decision_state_ref=digest({'snapshot_id': states[2 + 5 * len(stage_refs)][0]['snapshot_id']}),
                execute_state_ref=digest({'snapshot_id': states[4 + 5 * len(stage_refs)][0]['snapshot_id']}),
                call_ref=_reference(event['source']['call_id']), source_event_id=source_event_id,
                input_sha256=digest(expected_request),
                prediction_sha256=digest(prediction.model_dump()),
                source_verification_refs=tuple(sorted(_reference(item)
                                                      for item in event['source']['verification_ids']))))
        if (len(set(ordered_event_ids)) != 6
                or sorted(ordered_event_ids) != source['source_event_ids_by_role']['system1']
                or source['requested_roles'].count('system1') != 1):
            raise ValueError('recipe_candidate_requires_six_real_s1_events')
        field_names = {item.form_field_name for item in bindings}
        actual_args = json.loads(next(action for _version, action in ordered_stages
                                      if action['tool'] == 'browser.form.fill')['arguments_json'])
        declared_names = (actual_args.get('field_names', '').split(',')
                          if 'field_names' in actual_args else [actual_args.get('field_name')])
        if set(declared_names) != field_names or len(declared_names) != len(field_names):
            raise ValueError('recipe_candidate_source_fields_changed')

        task_sha256 = digest(draft.task.model_dump())
        field_binding_sha256 = digest([item.model_dump() for item in bindings])
        step_names = operation_keys
        skill = SiteSkillDraft(
            schema_version='1.0', profile_sha256=binding['profile_sha256'],
            application_key=page.application_key, tenant_key=page.tenant_key,
            account_role=page.account_role, task_key=draft.task.task_key,
            page_key=page.page_key, page_draft_sha256=annotation.page_draft_sha256,
            skill_key=annotation.skill_key, model_role='system1',
            candidate_kind='finite_action_choice', revision=1, previous_sha256=None,
            parameter_keys=sorted(item.parameter_key for item in bindings),
            precondition_keys=sorted(item.precondition_key for item in annotation.preconditions),
            step_keys=sorted(item.step_key for item in annotation.operation_step_keys),
            expected_outcome_key=annotation.outcome.outcome_key,
            source_event_ids=sorted(ordered_event_ids),
            source_verification_ids=sorted(source_verification_ids),
            source_kind='manual_candidate', status='draft', execution_authorized=False,
            collection_authorized=False, training_ready=False, activation_authorized=False)
        preconditions = tuple(SiteSkillFormRecipePrecondition(
            precondition_key=item.precondition_key, kind=item.kind,
            checked_at_operation=('fill_form' if item.kind == 'entry_form_available'
                                  else 'read_state_before'))
            for item in annotation.preconditions)
        outcome_binding = next(item for item in bindings
                               if item.parameter_key == annotation.outcome.parameter_key)
        recipe = SiteSkillFormRecipe(
            schema_version='1.0', synthetic=True, skill_sha256=digest(skill.model_dump()),
            profile_sha256=binding['profile_sha256'], page_draft_sha256=annotation.page_draft_sha256,
            task_key=draft.task.task_key, task_sha256=task_sha256, model_role='system1',
            field_binding_sha256=field_binding_sha256, preconditions=preconditions,
            outcome=SiteSkillFormRecipeOutcome(
                outcome_key=annotation.outcome.outcome_key,
                kind='submitted_field_state_transition',
                parameter_key=annotation.outcome.parameter_key,
                form_field_name=outcome_binding.form_field_name,
                verified_at_operation='read_state_after'),
            steps=tuple(SiteSkillFormRecipeStep(step_key=step_names[operation], operation=operation)
                        for operation in operations), execution_authorized=False,
            collection_authorized=False, reviewed=False, activation_authorized=False,
            training_ready=False)

        source_verification_ids = sorted(source_verification_ids)
        source_fingerprint = digest({
            'version': DERIVER_VERSION, 'source_run_ref': source['run_ref'],
            'run_content_sha256': run_source_fingerprint(snapshot, run_id),
            'form_binding_sha256': digest(dict(binding)),
            'state_binding_sha256': digest(dict(state_binding)),
            'source_job': dict(source_job),
            'approval_records': sorted((dict(row) for row in approvals), key=canonical),
            'profile_sha256': binding['profile_sha256'], 'task_sha256': task_sha256,
            'form_plan_sha256': digest(plan.model_dump()),
            'state_plan_sha256': digest(state_plan.model_dump()),
            'body_sha256': source_body_sha256,
            'parameter_variant_sha256': parameter_variant_sha256,
            'deployment_sha256': events_by_decision[decisions[0]['decision_id']]['source']['deployment_sha256'],
            'stages': [{key: value for key, value in item.model_dump(mode='json').items()
                        if key != 'step_key'} for item in stage_refs],
            'source_verification_ids': source_verification_ids,
            **({'owned_source_context': {
                    'manifest_sha256': owned_source['manifest_sha256'],
                    'invocation_sha256': owned_source['invocation_sha256']}}
               if owned_source is not None else {}),
        })
        source_group = digest({'domain': 'site_skill_form_recipe_candidate_lineage_v1',
                               'source_run_ref': source['run_ref'],
                               'profile_sha256': binding['profile_sha256'],
                               'task_sha256': task_sha256})
        candidate_data = {
            'schema_version': '1.1' if owned_source is not None else '1.0', 'synthetic': True,
            'derivation_kind': 'audited_form_trajectory', 'deriver_version': DERIVER_VERSION,
            'status': 'unreviewed_executable_recipe_candidate', 'source_run_id': run_id,
            'source_run_ref': source['run_ref'], 'source_group_sha256': source_group,
            'source_fingerprint_sha256': source_fingerprint,
            'source_body_sha256': source_body_sha256,
            'source_parameter_variant_sha256': parameter_variant_sha256,
            'profile_sha256': binding['profile_sha256'], 'task_sha256': task_sha256,
            'form_plan_sha256': digest(plan.model_dump()),
            'state_plan_sha256': digest(state_plan.model_dump()),
            'field_binding_sha256': field_binding_sha256,
            'deployment_sha256': events_by_decision[decisions[0]['decision_id']]['source']['deployment_sha256'],
            'source_event_ids_by_role': source['source_event_ids_by_role'],
            'source_verification_ids': source_verification_ids,
            'source_stage_refs': tuple(stage_refs), 'annotation': annotation,
            'field_bindings': tuple(bindings), 'skill': skill, 'recipe': recipe,
            'reviewed': False, 'activation_authorized': False,
            'training_ready': False, 'site_outcome_verified': False, 'skill_executed': False,
        }
        if owned_source is not None:
            candidate_data['source_context'] = {
                'kind': 'owned_fixed_template_v1',
                'manifest_sha256': owned_source['manifest_sha256'],
                'invocation_sha256': owned_source['invocation_sha256']}
            candidate = OwnedSiteSkillFormRecipeCandidate.model_validate(candidate_data)
        else:
            candidate = SiteSkillFormRecipeCandidate.model_validate(candidate_data)
        return candidate.model_dump(mode='json')


def candidate_parameter_variant_sha256(candidate: SiteSkillFormRecipeCandidate | dict,
                                       inputs, case_key: str) -> str:
    checked = _parse_candidate(candidate)
    case = next((item for item in inputs.cases if item.case_key == case_key), None)
    if case is None:
        raise ValueError('recipe_candidate_case_missing')
    variant = _parameter_variant_sha256(case.parameters, checked.field_bindings)
    if variant == checked.source_parameter_variant_sha256:
        raise ValueError('recipe_candidate_source_case_reuse_forbidden')
    return variant


def _parse_candidate(value: SiteSkillFormRecipeCandidate | dict) -> SiteSkillFormRecipeCandidate:
    return parse_site_skill_form_recipe_candidate(value)


def parse_site_skill_form_recipe_candidate(value):
    if isinstance(value, OwnedSiteSkillFormRecipeCandidate):
        value = value.model_dump(mode='json')
    if isinstance(value, SiteSkillFormRecipeCandidate):
        value = value.model_dump(mode='json')
    if not isinstance(value, dict):
        raise ValueError('recipe_candidate_invalid')
    version = value.get('schema_version')
    if version == '1.0':
        return SiteSkillFormRecipeCandidate.model_validate_json(canonical(value))
    if version == '1.1':
        return OwnedSiteSkillFormRecipeCandidate.model_validate_json(canonical(value))
    raise ValueError('recipe_candidate_version_unsupported')


def persist_site_skill_form_recipe_candidate(directory: Path,
                                             candidate: SiteSkillFormRecipeCandidate | dict,
                                             *, confirm_sha256: str) -> str:
    checked = _parse_candidate(candidate)
    content = canonical(checked.model_dump(mode='json')).encode()
    checksum = hashlib.sha256(content).hexdigest()
    if checksum != confirm_sha256:
        raise ValueError('exact_recipe_candidate_confirmation_required')
    if len(content) > MAX_CANDIDATE_BYTES:
        raise ValueError('recipe_candidate_too_large')
    parent = open_existing_workspace(directory.parent)
    try:
        try:
            os.mkdir(directory.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    descriptor = private_directory(directory)
    temporary = '.candidate-' + uuid4().hex
    try:
        target = checksum + '.json'
        try:
            existing = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=descriptor)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            os.close(existing)
            prior = _read_candidate(directory, checksum)
            if canonical(prior.model_dump(mode='json')).encode() != content:
                raise ValueError('recipe_candidate_content_address_collision')
            return checksum
        if len(os.listdir(descriptor)) >= MAX_CANDIDATES:
            raise ValueError('recipe_candidate_store_limit_exceeded')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target, src_dir_fd=descriptor,
                    dst_dir_fd=descriptor, follow_symlinks=False)
        except FileExistsError:
            pass
        os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
        final = _read_candidate(directory, checksum)
        if canonical(final.model_dump(mode='json')).encode() != content:
            raise ValueError('recipe_candidate_content_address_changed')
        return checksum
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _read_candidate(directory: Path, checksum: str) -> SiteSkillFormRecipeCandidate:
    if not isinstance(checksum, str) or re.fullmatch('[a-f0-9]{64}', checksum) is None:
        raise ValueError('invalid_recipe_candidate_sha256')
    parent = private_directory(directory)
    try:
        descriptor = os.open(checksum + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_CANDIDATE_BYTES):
                raise ValueError('invalid_private_recipe_candidate')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                content = stream.read(MAX_CANDIDATE_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(checksum + '.json', dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            if (len(content) > MAX_CANDIDATE_BYTES or len(content) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))
                    or hashlib.sha256(content).hexdigest() != checksum):
                raise ValueError('recipe_candidate_file_changed')
            candidate = parse_site_skill_form_recipe_candidate(json.loads(content))
            if content != canonical(candidate.model_dump(mode='json')).encode():
                raise ValueError('recipe_candidate_not_canonical')
            return candidate
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def load_site_skill_form_recipe_candidate(
        directory: Path, checksum: str, *, database: Path, profiles: WebApplicationProfiles,
        pages: SiteKnowledgeStore, source_parameters: dict[str, str] | None = None,
        owned_source_directory: Path | None = None,
        owned_source_manifest_sha256: str | None = None,
        _audited_snapshot=None) -> dict:
    stored = _read_candidate(directory, checksum)
    if stored.schema_version == '1.1':
        if owned_source_directory is None:
            raise ValueError('owned_recipe_candidate_source_directory_required')
        source_context = stored.source_context
        if (owned_source_manifest_sha256 is not None
                and owned_source_manifest_sha256 != source_context.manifest_sha256):
            raise ValueError('owned_recipe_candidate_manifest_pin_mismatch')
        owned_source_manifest_sha256 = source_context.manifest_sha256
    elif owned_source_directory is not None:
        raise ValueError('unbound_recipe_candidate_rejects_owned_source')
    rebuilt = derive_site_skill_form_recipe_candidate(
        database, stored.source_run_id, profiles=profiles, pages=pages,
        annotation=stored.annotation, source_parameters=source_parameters,
        owned_source_directory=owned_source_directory,
        owned_source_manifest_sha256=owned_source_manifest_sha256,
        _audited_snapshot=_audited_snapshot)
    if digest(rebuilt) != checksum:
        raise ValueError('recipe_candidate_source_changed')
    return rebuilt


def _main(argv=None):
    parser = argparse.ArgumentParser(description='Derive or revalidate an audited form recipe candidate',
                                     allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)

    def source_arguments(command, *, include_annotation=False):
        command.add_argument('--database', type=Path, required=True)
        command.add_argument('--profiles', type=Path, required=True)
        command.add_argument('--pages', type=Path, required=True)
        command.add_argument('--source-parameters-file', type=Path)
        command.add_argument('--owned-source-directory', type=Path)
        command.add_argument('--owned-source-manifest-sha256')
        if include_annotation:
            command.add_argument('--run-id', required=True)
            command.add_argument('--annotation-file', type=Path, required=True)

    preview = commands.add_parser('preview', allow_abbrev=False)
    source_arguments(preview, include_annotation=True)
    publish = commands.add_parser('publish', allow_abbrev=False)
    source_arguments(publish, include_annotation=True)
    publish.add_argument('--directory', type=Path, required=True)
    publish.add_argument('--confirm-sha256', required=True)
    inspect = commands.add_parser('inspect', allow_abbrev=False)
    source_arguments(inspect)
    inspect.add_argument('--directory', type=Path, required=True)
    inspect.add_argument('--sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        from .local_app import private_read

        if ((arguments.owned_source_directory is None)
                != (arguments.owned_source_manifest_sha256 is None)
                or arguments.owned_source_directory is not None
                and (arguments.source_parameters_file is not None
                     or re.fullmatch(r'[a-f0-9]{64}', arguments.owned_source_manifest_sha256) is None)
                or arguments.owned_source_directory is None
                and arguments.source_parameters_file is None):
            raise ValueError('recipe_candidate_cli_source_selection_invalid')
        owned_directory = (arguments.owned_source_directory.absolute()
                           if arguments.owned_source_directory is not None else None)
        if owned_directory is not None and (
                arguments.profiles.absolute() != owned_directory / 'profiles'
                or arguments.pages.absolute() != owned_directory / 'site-knowledge'):
            raise ValueError('owned_recipe_candidate_cli_paths_must_be_fixed')
        candidate_directory = (owned_directory / 'site-skill-recipe-candidates'
                               if owned_directory is not None else None)
        if (candidate_directory is not None
                and arguments.command in {'publish', 'inspect'}
                and arguments.directory.absolute() != candidate_directory):
            raise ValueError('owned_recipe_candidate_store_must_be_fixed')

        profile_store = WebApplicationProfiles(arguments.profiles)
        pages = SiteKnowledgeStore(arguments.pages, profile_store)
        parameters = None
        if arguments.source_parameters_file is not None:
            parameters_content = private_read(arguments.source_parameters_file, 8192)
            parameters = json.loads(parameters_content)
            if canonical(parameters).encode() != parameters_content or not isinstance(parameters, dict):
                raise ValueError('source_parameter_file_invalid')
        if arguments.command in {'preview', 'publish'}:
            annotation_content = private_read(arguments.annotation_file, 16384)
            annotation = SiteSkillFormRecipeCandidateAnnotation.model_validate_json(annotation_content)
            if annotation_content != canonical(annotation.model_dump(mode='json')).encode():
                raise ValueError('candidate_annotation_file_not_canonical')
            candidate = derive_site_skill_form_recipe_candidate(
                arguments.database, arguments.run_id, profiles=profile_store, pages=pages,
                annotation=annotation, source_parameters=parameters,
                owned_source_directory=owned_directory,
                owned_source_manifest_sha256=arguments.owned_source_manifest_sha256)
            checksum = digest(candidate)
            if arguments.command == 'publish':
                persist_site_skill_form_recipe_candidate(
                    arguments.directory, candidate, confirm_sha256=arguments.confirm_sha256)
            print(canonical(_candidate_summary(candidate, checksum)))
            return
        candidate = load_site_skill_form_recipe_candidate(
            arguments.directory, arguments.sha256, database=arguments.database,
            profiles=profile_store, pages=pages, source_parameters=parameters,
            owned_source_directory=owned_directory,
            owned_source_manifest_sha256=arguments.owned_source_manifest_sha256)
        print(canonical(_candidate_summary(candidate, arguments.sha256)))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
            json.JSONDecodeError, RecursionError):
        parser.exit(1, 'Form recipe candidate unavailable: missing, changed or unsafe source.\n')


def _candidate_summary(candidate: dict, checksum: str) -> dict:
    return {'schema_version': '1.0', 'candidate_sha256': checksum,
            'source_group_sha256': candidate['source_group_sha256'],
            'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
            'source_parameter_variant_sha256': candidate['source_parameter_variant_sha256'],
            'skill_sha256': digest(candidate['skill']),
            'recipe_sha256': digest(candidate['recipe']),
            'steps': candidate['recipe']['steps'], 'status': candidate['status'],
            'reviewed': False, 'activation_authorized': False,
            'training_ready': False, 'site_outcome_verified': False,
            'skill_executed': False}


if __name__ == '__main__':
    _main()

"""Read-only execution proof for an explicitly bound synthetic form recipe."""

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from .contracts import canonical, digest
from .dataset_audit import audit_snapshot
from .remote_form_learning_source import inspect_remote_form_learning_source
from .site_skill_form_invocation_audit import (
    SiteSkillFormInvocationAuditReport, _state_history, _timestamp, _verify_stage_chain)
from .site_skill_form_recipe import revalidate_site_skill_form_recipe_invocation
from .web_application import Checksum, Key


class SiteSkillFormRecipeAuditReport(SiteSkillFormInvocationAuditReport):
    schema_version: Literal['2.0']
    status: Literal['executable_recipe_execution_verified']
    recipe_sha256: Checksum
    symbolic_step_keys: tuple[Key, ...]
    step_observation_refs: tuple[Checksum, ...]
    executable_recipe_executed: Literal[True]
    symbolic_skill_steps_executed: Literal[True]
    skill_executed: Literal[True]
    skill_validated: Literal[False]

    @field_validator('synthetic', 'transport_readback_verified',
                     'declared_state_readback_verified', 'invocation_persisted_before_actions',
                     'invocation_consistent_across_states', 'invocation_execution_verified',
                     'executable_recipe_executed', 'symbolic_skill_steps_executed',
                     'skill_executed', mode='before')
    @classmethod
    def exact_true_claim(cls, value):
        if value is not True:
            raise ValueError('skill_form_recipe_boolean_claim_invalid')
        return value

    @field_validator('skill_validated', 'site_outcome_verified', 'held_out_independence_verified',
                     'reviewed', 'activation_authorized', 'training_ready', mode='before')
    @classmethod
    def exact_false_claim(cls, value):
        if value is not False:
            raise ValueError('skill_form_recipe_boolean_claim_invalid')
        return value

    @model_validator(mode='after')
    def stage_order_is_fixed(self):
        supported = (
            ('open_entry', 'read_state_before', 'fill_form', 'submit_form',
             'read_receipt', 'read_state_after'),
            ('open_entry', 'fill_form', 'read_state_before', 'submit_form',
             'read_receipt', 'read_state_after'))
        if (tuple(stage.stage for stage in self.stages) not in supported
                or len(self.symbolic_step_keys) != 6
                or len(set(self.symbolic_step_keys)) != 6
                or len(self.step_observation_refs) != 6
                or len(set(self.step_observation_refs)) != 6):
            raise ValueError('skill_form_recipe_evidence_invalid')
        return self


def _recipe_observations(snapshot, run_id, invocation, states):
    binding = snapshot.execute('''SELECT binding_sha256 FROM desktop_remote_form_bindings
        WHERE run_id=?''', (run_id,)).fetchone()
    if binding is None:
        raise ValueError('skill_form_recipe_observation_binding_missing')
    rows = snapshot.execute('''SELECT * FROM observations
        WHERE run_id=? AND kind='browser.https_form' ''', (run_id,)).fetchall()
    candidates = []
    for row in rows:
        payload = json.loads(row['payload_json'])
        if not isinstance(payload, dict) or canonical(payload) != row['payload_json']:
            raise ValueError('skill_form_recipe_observation_noncanonical')
        if 'recipe_step' in payload:
            candidates.append((row, payload))
    if len(candidates) != len(invocation.steps):
        raise ValueError('skill_form_recipe_observations_incomplete')
    refs = []
    for ordinal, step in enumerate(invocation.steps):
        expected = {'recipe_sha256': invocation.recipe_sha256, 'step_key': step.step_key,
                    'operation': step.operation, 'ordinal': ordinal}
        matches = [(row, payload) for row, payload in candidates
                   if canonical(payload.get('recipe_step')) == canonical(expected)]
        if len(matches) != 1:
            raise ValueError('skill_form_recipe_step_observation_changed')
        row, payload = matches[0]
        observed, observed_state = states[1 + 5 * ordinal]
        decided, _decision_state = states[2 + 5 * ordinal]
        expected_payload = {
            'profile_sha256': invocation.profile_sha256,
            'binding_sha256': binding['binding_sha256'],
            'plan_sha256': invocation.form_plan_sha256,
            'next_stage': ordinal, 'recipe_step': expected}
        if (row['action_id'] is not None or row['step_id'] != observed_state.step_id
                or canonical(payload) != canonical(expected_payload)
                or not _timestamp(observed['created_at']) <= _timestamp(row['created_at'])
                <= _timestamp(decided['created_at'])):
            raise ValueError('skill_form_recipe_step_observation_unbound')
        refs.append(digest({'observation_id': row['observation_id']}))
    return tuple(refs)


def audit_site_skill_form_recipe_execution(
        store, plan, inputs, case_key, profiles, task, form_plan, state_plan,
        field_bindings, recipe, invocation, database: Path, run_id: str, *,
        _audited_snapshot=None) -> dict:
    invocation = revalidate_site_skill_form_recipe_invocation(
        invocation, store, plan, inputs, case_key, profiles, task,
        form_plan, state_plan, field_bindings, recipe)
    invocation_sha256 = digest(invocation.model_dump(mode='json'))
    stage_order = tuple(step.operation for step in invocation.steps)
    with (audit_snapshot(database) if _audited_snapshot is None else
          nullcontext(_audited_snapshot)) as (snapshot, identity):
        source = inspect_remote_form_learning_source(
            database, run_id, profiles=profiles.root,
            selected_profile_sha256=invocation.profile_sha256,
            selected_plan_sha256=invocation.form_plan_sha256,
            selected_state_plan_sha256=invocation.state_plan_sha256,
            _audited_snapshot=(snapshot, identity), _recipe_stage_order=stage_order)
        if (source['snapshot_sha256'] != identity['sha256']
                or not source['transport_readback_verified']
                or not source['declared_state_readback_verified']):
            raise ValueError('skill_form_recipe_source_unverified')
        states = _state_history(snapshot, run_id, invocation_sha256,
                                invocation.form_plan_sha256)
        evidence = _verify_stage_chain(snapshot, run_id, source, states,
                                       invocation_sha256, stage_order=stage_order)
        observations = _recipe_observations(snapshot, run_id, invocation, states)
        report = SiteSkillFormRecipeAuditReport.model_validate({
            'schema_version': '2.0', 'synthetic': True,
            'status': 'executable_recipe_execution_verified',
            'invocation_sha256': invocation_sha256,
            **{key: getattr(invocation, key) for key in (
                'skill_sha256', 'skill_plan_sha256', 'case_inputs_sha256',
                'case_key', 'profile_sha256', 'task_sha256', 'form_plan_sha256',
                'state_plan_sha256', 'field_binding_sha256', 'recipe_sha256')},
            'run_ref': source['run_ref'], 'source_snapshot_sha256': identity['sha256'],
            'initial_state_ref': digest({'snapshot_id': states[0][0]['snapshot_id']}),
            'terminal_state_ref': digest({'snapshot_id': states[31][0]['snapshot_id']}),
            'stages': evidence,
            'symbolic_step_keys': tuple(step.step_key for step in invocation.steps),
            'step_observation_refs': observations,
            'consumed_approval_count': 6, 'submit_count': 1,
            'transport_readback_verified': True, 'declared_state_readback_verified': True,
            'invocation_persisted_before_actions': True,
            'invocation_consistent_across_states': True,
            'invocation_execution_verified': True, 'executable_recipe_executed': True,
            'symbolic_skill_steps_executed': True, 'skill_executed': True,
            'skill_validated': False, 'site_outcome_verified': False,
            'held_out_independence_verified': False, 'reviewed': False,
            'activation_authorized': False, 'training_ready': False})
        return report.model_dump(mode='json')

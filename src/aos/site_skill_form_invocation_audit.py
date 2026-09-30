"""Audit one completed fixed-template skill form invocation."""

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import Field, model_validator

from .contracts import REPO_ROOT, REMOTE_FORM_SCOPE, State, TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .desktop_tasks import Approval
from .local_app import private_read
from .remote_form_learning_source import inspect_remote_form_learning_source
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillStore
from .site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                      read_case_inputs)
from .site_skill_form_invocation import (FORM_SKILL_STAGES, SiteSkillFormInvocation,
                                         compile_site_skill_form_invocation,
                                         revalidate_site_skill_form_invocation)
from .site_skill_validation import SiteSkillValidationPlan
from .site_skill_validation_cli import read_plan_source
from .web_application import Checksum, Key, WebApplicationProfiles
from .web_application_binding import WebTaskContract
from .web_https_form_state_probe import WebHTTPSFormStatePlan
from .web_https_form_transport import WebHTTPSFormPlan


ERROR = 'Site skill form invocation audit unavailable: missing, changed or unsafe source.'
FORM_TOOL_BY_STAGE = dict(zip(FORM_SKILL_STAGES,
                              ('browser.form.open', 'browser.form.state_before',
                               'browser.form.fill', 'browser.form.submit',
                               'browser.form.receipt', 'browser.form.state_after'),
                              strict=True))


class SiteSkillFormInvocationStageEvidence(TypedModel):
    stage: Literal['open_entry', 'read_state_before', 'fill_form', 'submit_form',
                   'read_receipt', 'read_state_after']
    action_ref: Checksum
    decision_ref: Checksum
    approval_ref: Checksum
    decision_state_ref: Checksum
    execute_state_ref: Checksum


class SiteSkillFormInvocationAuditReport(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    status: Literal['fixed_template_invocation_execution_verified']
    invocation_sha256: Checksum
    skill_sha256: Checksum
    skill_plan_sha256: Checksum
    case_inputs_sha256: Checksum
    case_key: Key
    profile_sha256: Checksum
    task_sha256: Checksum
    form_plan_sha256: Checksum
    state_plan_sha256: Checksum
    field_binding_sha256: Checksum
    run_ref: Checksum
    source_snapshot_sha256: Checksum
    initial_state_ref: Checksum
    terminal_state_ref: Checksum
    stages: tuple[SiteSkillFormInvocationStageEvidence, ...]
    consumed_approval_count: Literal[6]
    submit_count: Literal[1]
    transport_readback_verified: Literal[True]
    declared_state_readback_verified: Literal[True]
    invocation_persisted_before_actions: Literal[True]
    invocation_consistent_across_states: Literal[True]
    invocation_execution_verified: Literal[True]
    symbolic_skill_steps_executed: Literal[False]
    skill_executed: Literal[False]
    site_outcome_verified: Literal[False]
    held_out_independence_verified: Literal[False]
    reviewed: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @model_validator(mode='after')
    def stage_order_is_fixed(self):
        if tuple(stage.stage for stage in self.stages) != FORM_SKILL_STAGES:
            raise ValueError('skill_form_invocation_audit_stage_order_invalid')
        return self


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError('skill_form_invocation_timestamp_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('skill_form_invocation_timestamp_invalid')
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError('skill_form_invocation_timestamp_invalid') from error


def _state_history(snapshot: sqlite3.Connection, run_id: str,
                   invocation_sha256: str, form_plan_sha256: str):
    rows = snapshot.execute('''SELECT * FROM state_snapshots WHERE run_id=?
        ORDER BY state_version,created_at,snapshot_id''', (run_id,)).fetchall()
    if len(rows) != 32 or [row['state_version'] for row in rows] != list(range(32)):
        raise ValueError('skill_form_invocation_state_history_incomplete')
    expected_phases = ['CREATED', *(phase for _ in FORM_SKILL_STAGES for phase in (
        'OBSERVE', 'DECIDE', 'POLICY', 'EXECUTE', 'VERIFY')), 'SUCCEEDED']
    states = {}
    initial_state = None
    previous_created = None
    for row in rows:
        state = State.model_validate_json(row['state_json'])
        created = _timestamp(row['created_at'])
        if (state.model_dump_json() != row['state_json']
                or state.run_id != run_id or state.step_id != row['step_id']
                or state.state_version != row['state_version']
                or digest(state.model_dump(mode='json')) != row['content_sha256']
                or state.task_kind != 'browser_remote_form'
                or state.phase.value != expected_phases[row['state_version']]
                or state.skill_invocation_sha256 != invocation_sha256
                or state.owner != 'AGENT'
                or state.authorized_path != REMOTE_FORM_SCOPE
                or state.authorized_content != form_plan_sha256):
            raise ValueError('skill_form_invocation_state_changed')
        if initial_state is None:
            initial_state = state
        elif (state.task_id != initial_state.task_id or state.step_id != initial_state.step_id
              or state.runtime_id != initial_state.runtime_id
              or state.deployment_id != initial_state.deployment_id
              or state.owner_lease_id != initial_state.owner_lease_id
              or state.authorized_path != initial_state.authorized_path
              or state.authorized_content != initial_state.authorized_content):
            raise ValueError('skill_form_invocation_state_scope_changed')
        if previous_created is not None and created < previous_created:
            raise ValueError('skill_form_invocation_state_order_changed')
        previous_created = created
        states[state.state_version] = (row, state)
    first_row, first_state = states[0]
    last_row, last_state = states[31]
    if (first_state.phase.value != 'CREATED' or last_state.phase.value != 'SUCCEEDED'
            or first_state.task_id != last_state.task_id
            or first_state.runtime_id != last_state.runtime_id
            or first_state.deployment_id != last_state.deployment_id
            or first_state.owner_lease_id != last_state.owner_lease_id
            or _timestamp(first_row['created_at']) > _timestamp(last_row['created_at'])):
        raise ValueError('skill_form_invocation_state_bounds_changed')
    runtime = snapshot.execute('SELECT state_version,state_json FROM runtime_states WHERE run_id=?',
                               (run_id,)).fetchone()
    if (runtime is None or runtime['state_version'] != last_state.state_version
            or runtime['state_json'] != last_row['state_json']):
        raise ValueError('skill_form_invocation_terminal_state_changed')
    run = snapshot.execute('''SELECT task_id,status,outcome,policy_version,ended_at,
            deployment_snapshot_json FROM runs WHERE run_id=?''',
                           (run_id,)).fetchone()
    deployment = json.loads(run['deployment_snapshot_json']) if run is not None else {}
    if not isinstance(deployment, dict):
        raise ValueError('skill_form_invocation_deployment_binding_changed')
    if (run is None or run['task_id'] != first_state.task_id or run['status'] != 'succeeded'
            or run['outcome'] != 'passed' or run['policy_version'] != 'browser-remote-form-policy-v1'
            or run['ended_at'] is None
            or deployment.get('deployment_id') != first_state.deployment_id
            or _timestamp(run['ended_at']) < _timestamp(last_row['created_at'])):
        raise ValueError('skill_form_invocation_run_binding_changed')
    return states


def _verify_stage_chain(snapshot: sqlite3.Connection, run_id: str,
                        source: dict, states: dict,
                        invocation_sha256: str, *,
                        stage_order: tuple[str, ...] = FORM_SKILL_STAGES
                        ) -> tuple[SiteSkillFormInvocationStageEvidence, ...]:
    if stage_order not in (FORM_SKILL_STAGES,
                           ('open_entry', 'fill_form', 'read_state_before', 'submit_form',
                            'read_receipt', 'read_state_after')):
        raise ValueError('skill_form_recipe_stage_order_invalid')
    source_binding = snapshot.execute('''SELECT job_id,browser_runtime_id FROM desktop_remote_form_bindings
        WHERE run_id=?''', (run_id,)).fetchone()
    job = snapshot.execute('''SELECT run_id,kind,status,runtime_id,lease_id FROM desktop_tasks
        WHERE job_id=?''', (source_binding['job_id'],)).fetchone()
    initial_state = states[0][1]
    if (job is None or job['run_id'] != run_id or job['kind'] != 'browser_remote_form'
            or job['status'] != 'succeeded' or job['runtime_id'] != source_binding['browser_runtime_id']
            or job['lease_id'] != initial_state.owner_lease_id
            or initial_state.runtime_id != source_binding['browser_runtime_id']):
        raise ValueError('skill_form_invocation_job_binding_changed')
    actions = snapshot.execute('SELECT * FROM actions WHERE run_id=?', (run_id,)).fetchall()
    by_tool = {row['tool']: row for row in actions}
    approvals = snapshot.execute('SELECT * FROM desktop_approvals WHERE job_id=?',
                                 (source_binding['job_id'],)).fetchall()
    approval_by_action = {}
    for row in approvals:
        approval = Approval.model_validate_json(row['envelope_json'])
        if approval.action.action_id in approval_by_action:
            raise ValueError('skill_form_invocation_approval_duplicate')
        approval_by_action[approval.action.action_id] = (row, approval)

    evidence = []
    previous_completion = None
    initial_created = _timestamp(states[0][0]['created_at'])
    for stage in stage_order:
        tool = FORM_TOOL_BY_STAGE[stage]
        action = by_tool[tool]
        decision = snapshot.execute('''SELECT * FROM decisions WHERE decision_id=?
            AND run_id=? AND step_id=?''',
            (action['decision_id'], run_id, action['step_id'])).fetchone()
        decision_state = next((entry for entry in states.values()
                               if entry[0]['snapshot_id'] == decision['snapshot_id']), None)
        approval_entry = approval_by_action.get(action['action_id'])
        if decision_state is None or approval_entry is None:
            raise ValueError('skill_form_invocation_stage_state_missing')
        decision_snapshot, decision_state_value = decision_state
        approval_row, approval = approval_entry
        execute_state = states.get(approval.action.state_version)
        if execute_state is None:
            raise ValueError('skill_form_invocation_approval_state_missing')
        execute_snapshot, execute_state_value = execute_state
        created = _timestamp(approval_row['created_at'])
        human_rows = snapshot.execute('''SELECT actor,created_at,payload_json FROM human_interventions
            WHERE run_id=? AND step_id=? AND kind='approve' ''',
            (run_id, action['step_id'])).fetchall()
        human_times = [_timestamp(row['created_at']) for row in human_rows
                       if row['actor'] == 'local_authenticated_user'
                       and row['payload_json'] == canonical({
                           'approval_id': approval.approval_id,
                           'action_sha256': approval.action_sha256})]
        if len(human_times) != 1:
            raise ValueError('skill_form_invocation_human_approval_missing')
        human_time = human_times[0]
        consumed = _timestamp(approval_row['updated_at'])
        if action['created_at'] is None or action['completed_at'] is None:
            raise ValueError('skill_form_invocation_action_incomplete')
        action_created = _timestamp(action['created_at'])
        action_completed = _timestamp(action['completed_at'])
        try:
            deadline = datetime.fromtimestamp(approval.action.deadline, timezone.utc)
            expires = datetime.fromtimestamp(approval_row['expires_at'], timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise ValueError('skill_form_invocation_deadline_invalid') from error
        if (approval_row['status'] != 'consumed'
                or approval_row['expires_at'] != approval.expires_at
                or expires > deadline or created < initial_created
                or not created <= human_time <= consumed < expires
                or action_created >= deadline
                or consumed > action_created or action_created > action_completed
                or action_completed > deadline
                or decision_state_value.phase.value != 'DECIDE'
                or execute_state_value.phase.value != 'EXECUTE'
                or action['status'] != 'ok' or action['completed_at'] is None
                or decision_state_value.state_version != 2 + 5 * stage_order.index(stage)
                or execute_state_value.state_version != 4 + 5 * stage_order.index(stage)
                or _timestamp(decision_snapshot['created_at']) > created
                or _timestamp(decision_snapshot['created_at']) > action_created
                or _timestamp(execute_snapshot['created_at']) > created
                or _timestamp(execute_snapshot['created_at']) > action_created
                or action_completed > _timestamp(states[5 + 5 * stage_order.index(stage)][0]['created_at'])
                or decision_state_value.skill_invocation_sha256 != invocation_sha256
                or execute_state_value.skill_invocation_sha256 != invocation_sha256
                or approval.action.state_version != execute_state_value.state_version
                or approval.action.step_id != execute_state_value.step_id
                or approval.action.task_id != initial_state.task_id
                or approval.action.runtime_id != execute_state_value.runtime_id
                or approval.action.owner_lease_id != execute_state_value.owner_lease_id
                or action['step_id'] != execute_state_value.step_id
                or decision_state_value.step_id != action['step_id']):
            raise ValueError('skill_form_invocation_approval_order_changed')
        if previous_completion is not None and created < previous_completion:
            raise ValueError('skill_form_invocation_stage_order_changed')
        previous_completion = action_completed
        evidence.append(SiteSkillFormInvocationStageEvidence(
            stage=stage,
            action_ref=digest({'action_id': action['action_id']}),
            decision_ref=digest({'decision_id': action['decision_id']}),
            approval_ref=digest({'approval_id': approval.approval_id}),
            decision_state_ref=digest({'snapshot_id': decision_snapshot['snapshot_id']}),
            execute_state_ref=digest({'snapshot_id': execute_snapshot['snapshot_id']})))

    observation = by_tool['browser.form.observe']
    receipt = by_tool['browser.form.receipt']
    state_after = by_tool['browser.form.state_after']
    if (observation['completed_at'] is None or state_after['completed_at'] is None
            or receipt['completed_at'] is None
            or _timestamp(receipt['completed_at']) > _timestamp(observation['created_at'])
            or _timestamp(observation['created_at']) > _timestamp(observation['completed_at'])
            or _timestamp(observation['completed_at']) > _timestamp(state_after['created_at'])
            or _timestamp(observation['completed_at'])
            > _timestamp(states[5 + 5 * stage_order.index('read_receipt')][0]['created_at'])
            or _timestamp(state_after['completed_at'])
            > _timestamp(states[31][0]['created_at'])):
        raise ValueError('skill_form_invocation_readback_order_changed')
    return tuple(evidence)


def audit_site_skill_form_invocation_execution(
        store: SiteSkillStore, plan: SiteSkillValidationPlan,
        inputs: SiteSkillCaseInputs, case_key: str,
        profiles: WebApplicationProfiles, task: WebTaskContract,
        form_plan: WebHTTPSFormPlan, state_plan: WebHTTPSFormStatePlan,
        field_bindings: list[SiteSkillFormFieldBinding], invocation: SiteSkillFormInvocation,
        database: Path, run_id: str, *, _audited_snapshot=None) -> dict:
    invocation = revalidate_site_skill_form_invocation(
        invocation, store, plan, inputs, case_key, profiles, task,
        form_plan, state_plan, field_bindings)
    invocation_sha256 = digest(invocation.model_dump(mode='json'))
    if (invocation.status != 'admission_ready'
            or invocation.skill_executed or invocation.site_outcome_verified
            or invocation.reviewed or invocation.activation_authorized
            or invocation.training_ready):
        raise ValueError('skill_form_invocation_not_admission_ready')

    snapshot_context = (audit_snapshot(database) if _audited_snapshot is None
                        else nullcontext(_audited_snapshot))
    with snapshot_context as (snapshot, identity):
        source = inspect_remote_form_learning_source(
            database, run_id, profiles=profiles.root,
            selected_profile_sha256=invocation.profile_sha256,
            selected_plan_sha256=invocation.form_plan_sha256,
            selected_state_plan_sha256=invocation.state_plan_sha256,
            _audited_snapshot=(snapshot, identity))
        if (source['snapshot_sha256'] != identity['sha256']
                or not source['transport_readback_verified']
                or not source['declared_state_readback_verified']):
            raise ValueError('skill_form_invocation_source_unverified')
        states = _state_history(snapshot, run_id, invocation_sha256,
                                invocation.form_plan_sha256)
        stage_evidence = _verify_stage_chain(snapshot, run_id, source, states,
                                             invocation_sha256)
        report = SiteSkillFormInvocationAuditReport.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'status': 'fixed_template_invocation_execution_verified',
            'invocation_sha256': invocation_sha256,
            'skill_sha256': invocation.skill_sha256,
            'skill_plan_sha256': invocation.skill_plan_sha256,
            'case_inputs_sha256': invocation.case_inputs_sha256,
            'case_key': case_key, 'profile_sha256': invocation.profile_sha256,
            'task_sha256': invocation.task_sha256,
            'form_plan_sha256': invocation.form_plan_sha256,
            'state_plan_sha256': invocation.state_plan_sha256,
            'field_binding_sha256': invocation.field_binding_sha256,
            'run_ref': source['run_ref'], 'source_snapshot_sha256': identity['sha256'],
            'initial_state_ref': digest({'snapshot_id': states[0][0]['snapshot_id']}),
            'terminal_state_ref': digest({'snapshot_id': states[31][0]['snapshot_id']}),
            'stages': tuple(item.model_dump(mode='json') for item in stage_evidence),
            'consumed_approval_count': 6, 'submit_count': 1,
            'transport_readback_verified': True,
            'declared_state_readback_verified': True,
            'invocation_persisted_before_actions': True,
            'invocation_consistent_across_states': True,
            'invocation_execution_verified': True,
            'symbolic_skill_steps_executed': False, 'skill_executed': False,
            'site_outcome_verified': False, 'held_out_independence_verified': False,
            'reviewed': False, 'activation_authorized': False,
            'training_ready': False}).model_dump(mode='json')
        validator('site_skill_form_invocation_audit').validate(report)
        return report


def _canonical_private(path: Path, model):
    content = private_read(path, 20000)
    checked = model.model_validate_json(content)
    if content != canonical(checked.model_dump(mode='json')).encode():
        raise ValueError('skill_form_invocation_audit_source_not_canonical')
    return checked


def main(argv: list[str] | None = None) -> None:
    class PrivateArgumentParser(argparse.ArgumentParser):
        def error(self, _message):
            self.exit(2, ERROR + '\n')

    parser = PrivateArgumentParser(description='Audit one completed typed synthetic form invocation',
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
    parser.add_argument('--state-plan-file', type=Path, required=True)
    parser.add_argument('--state-plan-sha256', required=True)
    parser.add_argument('--field-bindings-file', type=Path, required=True)
    parser.add_argument('--field-binding-sha256', required=True)
    parser.add_argument('--invocation-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        plan = read_plan_source(arguments.plan)
        inputs = read_case_inputs(arguments.cases)
        task = _canonical_private(arguments.task_file, WebTaskContract)
        form_plan = _canonical_private(arguments.form_plan_file, WebHTTPSFormPlan)
        state_plan = _canonical_private(arguments.state_plan_file, WebHTTPSFormStatePlan)
        field_content = private_read(arguments.field_bindings_file, 8192)
        fields = json.loads(field_content)
        bindings = [SiteSkillFormFieldBinding.model_validate(item) for item in fields]
        expected_hashes = (
            (digest(plan.model_dump()), arguments.plan_sha256),
            (digest(inputs.model_dump()), arguments.cases_sha256),
            (digest(task.model_dump()), arguments.task_sha256),
            (digest(form_plan.model_dump()), arguments.form_plan_sha256),
            (digest(state_plan.model_dump()), arguments.state_plan_sha256),
            (digest([item.model_dump() for item in bindings]), arguments.field_binding_sha256))
        if (any(actual != expected for actual, expected in expected_hashes)
                or plan.skill_sha256 != arguments.skill_sha256
                or field_content != canonical([item.model_dump(mode='json')
                                               for item in bindings]).encode()):
            raise ValueError('skill_form_invocation_audit_selection_mismatch')
        profiles = WebApplicationProfiles(arguments.profiles)
        skill_store = SiteSkillStore(
            arguments.store, profiles,
            SiteKnowledgeStore(arguments.pages, profiles))
        invocation = SiteSkillFormInvocation.model_validate_json(json.dumps(
            compile_site_skill_form_invocation(
                skill_store, plan, inputs, arguments.case_key, profiles,
                task, form_plan, state_plan, bindings)))
        if digest(invocation.model_dump(mode='json')) != arguments.invocation_sha256:
            raise ValueError('skill_form_invocation_audit_hash_mismatch')
        report = audit_site_skill_form_invocation_execution(
            skill_store, plan, inputs, arguments.case_key, profiles, task,
            form_plan, state_plan, bindings, invocation, arguments.database,
            arguments.run_id)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError, OverflowError):
        parser.exit(1, ERROR + '\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

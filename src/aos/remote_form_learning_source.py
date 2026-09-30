"""Read-only source audit for one completed, approved HTTPS form task."""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import re
import sqlite3
import ssl
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .desktop_tasks import Approval
from .learning_events import review_learning_events_snapshot
from .web_application import Checksum, WebApplicationProfiles
from .web_application_binding import WebTaskAdmissionDraft, verify_web_task_binding
from .web_https_form_transport import (WebHTTPSFormPlan, form_stage_arguments,
                                       verify_web_https_form_plan)
from .web_https_form_state_probe import (WebHTTPSFormStatePlan, WebHTTPSFormStateReport,
                                         form_state_action_arguments,
                                         verify_web_https_form_state_plan)


CHECKSUM = re.compile(r'[a-f0-9]{64}\Z')
RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
STAGES = (('browser.form.open', 'open_entry'), ('browser.form.fill', 'fill_form'),
          ('browser.form.submit', 'submit_form'), ('browser.form.receipt', 'read_receipt'))
STATE_STAGES = (STAGES[0], ('browser.form.state_before', 'read_state_before'),
                *STAGES[1:], ('browser.form.state_after', 'read_state_after'))
EventId = Annotated[str, Field(pattern=r'^learning-[a-f0-9]{64}$')]


class RemoteFormLearningSourceReport(TypedModel):
    schema_version: Literal['1.0']
    mode: Literal['read_only_remote_form_source']
    status: Literal['unreviewed_transport_candidate']
    profile_sha256: Checksum
    plan_sha256: Checksum
    run_ref: Checksum
    snapshot_sha256: Checksum
    requested_roles: list[Literal['system1', 'system2']] = Field(max_length=2)
    source_event_ids_by_role: dict[Literal['system1', 'system2'], list[EventId]]
    approved_stage_count: Literal[4]
    submit_count: Literal[1]
    transport_readback_verified: Literal[True]
    site_outcome_verified: Literal[False]
    account_verified: Literal[False]
    rights_reviewed: Literal[False]
    redaction_reviewed: Literal[False]
    collection_authorized: Literal[False]
    training_ready: Literal[False]

    @model_validator(mode='after')
    def canonical_roles(self):
        if (self.requested_roles != sorted(set(self.requested_roles))
                or set(self.source_event_ids_by_role) != {'system1', 'system2'}
                or any(values != sorted(set(values)) or len(values) > 32
                       for values in self.source_event_ids_by_role.values())):
            raise ValueError('remote_form_source_roles_invalid')
        return self


class RemoteFormStateLearningSourceReport(RemoteFormLearningSourceReport):
    mode: Literal['read_only_remote_form_state_source']
    state_plan_sha256: Checksum
    approved_stage_count: Literal[6]
    declared_state_readback_verified: Literal[True]


class RemoteFormCookieLearningSourceReport(RemoteFormLearningSourceReport):
    mode: Literal['read_only_remote_form_cookie_source']
    cookie_sha256: Checksum
    cookie_bound: Literal[True]

    @model_validator(mode='after')
    def no_learning_event_projection(self):
        if self.source_event_ids_by_role != {'system1': [], 'system2': []}:
            raise ValueError('cookie_source_learning_events_unavailable')
        return self


def _canonical_object(value: str) -> dict:
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or canonical(decoded) != value:
        raise ValueError('remote_form_source_noncanonical_json')
    return decoded


def inspect_remote_form_learning_source(database: Path, run_id: str, *, profiles: Path,
                                        selected_profile_sha256: str,
                                        selected_plan_sha256: str,
                                        selected_state_plan_sha256: str | None = None,
                                        selected_cookie_sha256: str | None = None,
                                        _audited_snapshot: tuple[sqlite3.Connection, dict] | None = None,
                                        _recipe_stage_order: tuple[str, ...] | None = None) -> dict:
    if _recipe_stage_order is not None:
        supported = (tuple(choice for _tool, choice in STATE_STAGES),
                     ('open_entry', 'fill_form', 'read_state_before', 'submit_form',
                      'read_receipt', 'read_state_after'))
        if (type(_recipe_stage_order) is not tuple or _recipe_stage_order not in supported
                or selected_state_plan_sha256 is None or selected_cookie_sha256 is not None):
            raise ValueError('remote_form_recipe_stage_order_invalid')
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not isinstance(selected_profile_sha256, str)
            or CHECKSUM.fullmatch(selected_profile_sha256) is None
            or not isinstance(selected_plan_sha256, str)
            or CHECKSUM.fullmatch(selected_plan_sha256) is None
            or selected_state_plan_sha256 is not None
            and (not isinstance(selected_state_plan_sha256, str)
                 or CHECKSUM.fullmatch(selected_state_plan_sha256) is None)
            or selected_cookie_sha256 is not None
            and (not isinstance(selected_cookie_sha256, str)
                 or CHECKSUM.fullmatch(selected_cookie_sha256) is None)
            or selected_cookie_sha256 is not None
            and selected_state_plan_sha256 is not None):
        raise ValueError('remote_form_source_selection_invalid')
    profile_store = WebApplicationProfiles(profiles)
    profile = profile_store.get(selected_profile_sha256)
    requested = [role for role in ('system1', 'system2')
                 if getattr(profile.learning, role) == 'requested']
    with (audit_snapshot(database) if _audited_snapshot is None
          else nullcontext(_audited_snapshot)) as (snapshot, identity):
        binding = snapshot.execute('''SELECT binding.*, job.kind, job.status AS job_status,
                job.runtime_id AS job_runtime_id, run.status AS run_status,
                run.outcome, run.policy_version
            FROM desktop_remote_form_bindings AS binding
            JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
            JOIN runs AS run ON run.run_id=binding.run_id
            WHERE binding.run_id=?''', (run_id,)).fetchone()
        if (binding is None or binding['kind'] != 'browser_remote_form'
                or binding['job_status'] != 'succeeded'
                or binding['run_status'] != 'succeeded' or binding['outcome'] != 'passed'
                or binding['policy_version'] != 'browser-remote-form-policy-v1'
                or binding['profile_sha256'] != selected_profile_sha256
                or binding['plan_sha256'] != selected_plan_sha256
                or binding['job_runtime_id'] != binding['browser_runtime_id']):
            raise ValueError('remote_form_source_run_unverified')
        draft = WebTaskAdmissionDraft.model_validate_json(binding['draft_json'])
        plan = WebHTTPSFormPlan.model_validate_json(binding['plan_json'])
        if (canonical(draft.model_dump(mode='json')) != binding['draft_json']
                or canonical(plan.model_dump(mode='json')) != binding['plan_json']
                or draft.profile_sha256 != selected_profile_sha256
                or draft.binding_sha256 != binding['binding_sha256']
                or draft.runtime_sha256 != binding['runtime_sha256']
                or draft.runtime.runtime_id != binding['browser_runtime_id']
                or plan.profile_sha256 != selected_profile_sha256
                or digest(plan.model_dump()) != selected_plan_sha256):
            raise ValueError('remote_form_source_binding_changed')
        verify_web_task_binding(profile_store, draft)
        verify_web_https_form_plan(profile_store, draft.task, plan)
        has_cookie_table = snapshot.execute('''SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='desktop_remote_form_cookie_bindings' ''').fetchone()
        cookie_bindings = (snapshot.execute('''SELECT * FROM desktop_remote_form_cookie_bindings
                WHERE job_id=? OR run_id=?''', (binding['job_id'], run_id)).fetchall()
                if has_cookie_table else [])
        if selected_cookie_sha256 is None:
            if cookie_bindings:
                raise ValueError('remote_form_source_cookie_scope_unsupported')
        elif (len(cookie_bindings) != 1
              or cookie_bindings[0]['job_id'] != binding['job_id']
              or cookie_bindings[0]['run_id'] != run_id
              or cookie_bindings[0]['form_plan_sha256'] != selected_plan_sha256
              or cookie_bindings[0]['browser_runtime_id'] != binding['browser_runtime_id']
              or cookie_bindings[0]['cookie_sha256'] != selected_cookie_sha256
              or urlsplit(plan.entry_url).hostname.endswith('.invalid')):
            raise ValueError('remote_form_source_cookie_binding_changed')
        has_state_table = snapshot.execute('''SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='desktop_remote_form_state_bindings' ''').fetchone()
        state_binding = (snapshot.execute('''SELECT * FROM desktop_remote_form_state_bindings
            WHERE job_id=? AND run_id=?''', (binding['job_id'], run_id)).fetchone()
            if has_state_table else None)
        if (state_binding is None) != (selected_state_plan_sha256 is None):
            raise ValueError('remote_form_source_state_selection_mismatch')
        state_plan = None
        if state_binding is not None:
            state_plan = WebHTTPSFormStatePlan.model_validate_json(state_binding['state_plan_json'])
            state_sha256 = digest(state_plan.model_dump())
            if (canonical(state_plan.model_dump(mode='json')) != state_binding['state_plan_json']
                    or state_binding['form_plan_sha256'] != selected_plan_sha256
                    or state_binding['state_plan_sha256'] != selected_state_plan_sha256
                    or state_sha256 != selected_state_plan_sha256
                    or state_binding['browser_runtime_id'] != binding['browser_runtime_id']):
                raise ValueError('remote_form_source_state_binding_changed')
            public = not urlsplit(plan.entry_url).hostname.endswith('.invalid')
            verify_web_https_form_state_plan(
                profile_store, draft.task, plan, state_plan, ssl.create_default_context(),
                confirm_public_form_plan_sha256=(selected_plan_sha256 if public else None),
                confirm_public_state_plan_sha256=(state_sha256 if public else None))
        stages = STATE_STAGES if state_plan is not None else STAGES
        if _recipe_stage_order is not None:
            tools_by_stage = {choice: tool for tool, choice in STATE_STAGES}
            stages = tuple((tools_by_stage[choice], choice) for choice in _recipe_stage_order)
        actions = snapshot.execute('SELECT * FROM actions WHERE run_id=?', (run_id,)).fetchall()
        approvals = snapshot.execute('SELECT * FROM desktop_approvals WHERE job_id=?',
                                     (binding['job_id'],)).fetchall()
        verifications = snapshot.execute('SELECT * FROM verifications WHERE run_id=?',
                                         (run_id,)).fetchall()
        if (len(actions) != len(stages) + 1 or len(approvals) != len(stages)
                or len(verifications) != (2 if state_plan is not None else 1)
                or {action['tool'] for action in actions}
                != {tool for tool, _choice in stages} | {'browser.form.observe'}):
            raise ValueError('remote_form_source_count_changed')
        by_tool = {action['tool']: action for action in actions}
        state_versions = []
        field_arguments = _canonical_object(by_tool['browser.form.fill']['arguments_json'])
        field_names = (field_arguments.get('field_name') if 'field_name' in field_arguments
                       else tuple(field_arguments.get('field_names', '').split(',')))
        if isinstance(field_names, tuple) and any(not name for name in field_names):
            raise ValueError('remote_form_source_field_names_invalid')
        form_stage = {'browser.form.open': 0, 'browser.form.fill': 1,
                      'browser.form.submit': 2, 'browser.form.receipt': 3}
        for tool, choice in stages:
            action = by_tool[tool]
            expected_arguments = (form_state_action_arguments(
                state_plan, draft.binding_sha256,
                'before' if tool.endswith('before') else 'after', selected_cookie_sha256)
                if tool.startswith('browser.form.state_') else
                form_stage_arguments(plan, draft.binding_sha256,
                                     field_names, form_stage[tool], selected_cookie_sha256))
            decision = snapshot.execute('''SELECT * FROM decisions WHERE decision_id=?
                AND run_id=? AND step_id=?''',
                (action['decision_id'], run_id, action['step_id'])).fetchone()
            if (action['status'] != 'ok' or action['actual_option'] != choice
                    or _canonical_object(action['arguments_json']) != expected_arguments
                    or decision is None or decision['selected_option'] != choice
                    or decision['policy_result'] != 'allow'):
                raise ValueError('remote_form_source_action_changed')
            matching = [row for row in approvals
                        if Approval.model_validate_json(row['envelope_json']).action.action_id
                        == action['action_id']]
            if len(matching) != 1:
                raise ValueError('remote_form_source_approval_missing')
            approval_row = matching[0]
            approval = Approval.model_validate_json(approval_row['envelope_json'])
            if (approval_row['status'] != 'consumed'
                    or approval.model_dump_json() != approval_row['envelope_json']
                    or approval_row['approval_id'] != approval.approval_id
                    or approval.job_id != binding['job_id']
                    or approval.action_sha256 != approval_row['action_sha256']
                    or digest(approval.action.model_dump(mode='json')) != approval_row['action_sha256']
                    or approval.action.run_id != run_id
                    or approval.action.step_id != action['step_id']
                    or approval.action.runtime_id != binding['browser_runtime_id']
                    or approval.action.tool != tool
                    or approval.action.arguments != expected_arguments
                    or approval.action.idempotency_key != action['idempotency_key']
                    or approval.action.selected_option != choice):
                raise ValueError('remote_form_source_approval_changed')
            if state_plan is not None:
                state_versions.append(approval.action.state_version)
            human = snapshot.execute('''SELECT actor,payload_json FROM human_interventions
                WHERE run_id=? AND step_id=? AND kind='approve' ''',
                (run_id, action['step_id'])).fetchall()
            if not any(entry['actor'] == 'local_authenticated_user'
                       and _canonical_object(entry['payload_json']) == {
                           'approval_id': approval.approval_id,
                           'action_sha256': approval.action_sha256}
                       for entry in human):
                raise ValueError('remote_form_source_human_approval_missing')
        if state_plan is not None and state_versions != sorted(set(state_versions)):
            raise ValueError('remote_form_source_state_order_changed')
        if (state_plan is not None and snapshot.execute('''SELECT count(*)
                FROM human_interventions WHERE run_id=? AND kind='approve' ''',
                (run_id,)).fetchone()[0] != len(stages)):
            raise ValueError('remote_form_source_state_human_count_changed')
        receipt = by_tool['browser.form.receipt']
        observed = by_tool['browser.form.observe']
        expected_observe_arguments = form_stage_arguments(plan, draft.binding_sha256,
                                                          field_names, 4, selected_cookie_sha256)
        if (observed['status'] != 'ok' or observed['actual_option'] != 'read_receipt'
                or observed['step_id'] != receipt['step_id']
                or observed['decision_id'] != receipt['decision_id']
                or _canonical_object(observed['arguments_json']) != expected_observe_arguments):
            raise ValueError('remote_form_source_observation_action_changed')
        verification_rows = {row['method']: row for row in verifications}
        if len(verification_rows) != len(verifications):
            raise ValueError('remote_form_source_verification_count_changed')
        verification = verification_rows.get('independent_https_form_transport_readback')
        if verification is None:
            raise ValueError('remote_form_source_transport_verification_missing')
        expected = _canonical_object(verification['expected_json'])
        actual = _canonical_object(verification['actual_json'])
        receipt_result = _canonical_object(receipt['result_json'])
        readback = _canonical_object(observed['result_json'])
        references = json.loads(verification['evidence_refs_json'])
        submit_request_sha256 = digest({'method': 'POST', 'url': plan.submit_url,
                                        'body_sha256': plan.body_sha256})
        if (verification['step_id'] != receipt['step_id']
                or verification['action_id'] != receipt['action_id']
                or verification['result'] != 'passed'
                or verification['method'] != 'independent_https_form_transport_readback'
                or verification['verifier'] != 'aos-remote-form-v1'
                or set(expected) != {'url', 'title_sha256', 'heading_sha256',
                                     'plan_sha256', 'submit_request_sha256'}
                or expected != actual or expected['url'] != plan.receipt_url
                or expected['plan_sha256'] != selected_plan_sha256
                or expected['submit_request_sha256'] != submit_request_sha256
                or any(expected[key] != receipt_result.get(key)
                       or expected[key] != readback.get(key)
                       for key in ('url', 'title_sha256', 'heading_sha256'))
                or len(references) != 1 or not isinstance(references[0], str)):
            raise ValueError('remote_form_source_readback_changed')
        observation = snapshot.execute('''SELECT * FROM observations WHERE observation_id=?
            AND run_id=? AND step_id=?''',
            (references[0], run_id, receipt['step_id'])).fetchone()
        if (observation is None or observation['action_id'] != observed['action_id']
                or observation['kind'] != 'browser.https_form'
                or _canonical_object(observation['payload_json']) != readback):
            raise ValueError('remote_form_source_evidence_changed')
        events = {'system1': [], 'system2': []}
        review = review_learning_events_snapshot(snapshot, identity, run_id)
        if review['snapshot_sha256'] != identity['sha256']:
            raise ValueError('remote_form_source_snapshot_changed')
        if state_plan is not None:
            before = _canonical_object(by_tool['browser.form.state_before']['result_json'])
            after = WebHTTPSFormStateReport.model_validate(
                _canonical_object(by_tool['browser.form.state_after']['result_json']))
            state_verification = verification_rows.get('declared_https_form_state_readback')
            if (before != {'status': 'before_response_matched',
                           'state_plan_sha256': state_sha256,
                           'response_sha256': state_plan.expected_before_sha256}
                    or after.state_plan_sha256 != state_sha256
                    or after.form_plan_sha256 != selected_plan_sha256
                    or after.profile_sha256 != selected_profile_sha256
                    or after.task_sha256 != plan.task_sha256
                    or after.before_response_sha256 != state_plan.expected_before_sha256
                    or after.after_response_sha256 != state_plan.expected_after_sha256
                    or after.state_url_sha256 != digest({'url': state_plan.state_url})
                    or after.marker_id_sha256 != (digest({'marker_id': state_plan.marker_id})
                                                  if state_plan.marker_id is not None else None)
                    or after.before_marker_sha256 != state_plan.expected_before_marker_sha256
                    or after.after_marker_sha256 != state_plan.expected_after_marker_sha256
                    or after.submitted_field_name_sha256 != (digest({'field_name': state_plan.submitted_field_name})
                                                               if state_plan.submitted_field_name is not None else None)
                    or after.submitted_value_readback_verified != (True if state_plan.submitted_field_name is not None
                                                                   else None)
                    or state_verification is None):
                raise ValueError('remote_form_source_state_readback_changed')
            state_expected = {
                'state_plan_sha256': state_sha256,
                'form_plan_sha256': selected_plan_sha256,
                'before_response_sha256': state_plan.expected_before_sha256,
                'after_response_sha256': state_plan.expected_after_sha256,
                'receipt_response_sha256': after.receipt_response_sha256}
            state_references = json.loads(state_verification['evidence_refs_json'])
            if (state_verification['step_id'] != by_tool['browser.form.state_after']['step_id']
                    or state_verification['action_id'] != by_tool['browser.form.state_after']['action_id']
                    or state_verification['result'] != 'passed'
                    or state_verification['criterion'] != 'Declared HTTPS state response transition'
                    or state_verification['verifier'] != 'aos-remote-form-state-v1'
                    or _canonical_object(state_verification['expected_json']) != state_expected
                    or _canonical_object(state_verification['actual_json']) != state_expected
                    or len(state_references) != 1 or not isinstance(state_references[0], str)):
                raise ValueError('remote_form_source_state_verification_changed')
            state_observation = snapshot.execute('''SELECT * FROM observations
                WHERE observation_id=? AND run_id=? AND step_id=?''',
                (state_references[0], run_id,
                 by_tool['browser.form.state_after']['step_id'])).fetchone()
            if (state_observation is None
                    or state_observation['action_id'] != by_tool['browser.form.state_after']['action_id']
                    or state_observation['kind'] != 'browser.https_form'
                    or _canonical_object(state_observation['payload_json']) != after.model_dump()):
                raise ValueError('remote_form_source_state_evidence_changed')
        decisions = {by_tool[tool]['decision_id'] for tool, _choice in stages}
        for event in review['events']:
            source = event['source']
            if event['role'] not in requested:
                continue
            if event['role'] == 'system1' and source['decision_id'] in decisions:
                events['system1'].append(event['event_id'])
            elif event['role'] == 'system2' and source['escalation_id'] is not None:
                events['system2'].append(event['event_id'])
        if any(len(ids) > 32 for ids in events.values()):
            raise ValueError('remote_form_source_event_limit')
        for ids in events.values():
            ids.sort()
        if selected_cookie_sha256 is not None:
            events = {'system1': [], 'system2': []}
        report = {'schema_version': '1.0', 'mode': 'read_only_remote_form_source',
                  'status': 'unreviewed_transport_candidate',
                  'profile_sha256': selected_profile_sha256,
                  'plan_sha256': selected_plan_sha256,
                  'run_ref': digest({'run_id': run_id}),
                  'snapshot_sha256': identity['sha256'],
                  'requested_roles': requested,
                  'source_event_ids_by_role': events,
                  'approved_stage_count': len(stages), 'submit_count': 1,
                  'transport_readback_verified': True,
                  'site_outcome_verified': False, 'account_verified': False,
                  'rights_reviewed': False, 'redaction_reviewed': False,
                  'collection_authorized': False, 'training_ready': False}
        if state_plan is not None:
            report.update(mode='read_only_remote_form_state_source',
                          state_plan_sha256=state_sha256,
                          declared_state_readback_verified=True)
            checked = RemoteFormStateLearningSourceReport.model_validate(report).model_dump()
            validator('remote_form_state_learning_source').validate(checked)
        elif selected_cookie_sha256 is not None:
            report.update(mode='read_only_remote_form_cookie_source',
                          cookie_sha256=selected_cookie_sha256, cookie_bound=True)
            checked = RemoteFormCookieLearningSourceReport.model_validate(report).model_dump()
            validator('remote_form_cookie_learning_source').validate(checked)
        else:
            checked = RemoteFormLearningSourceReport.model_validate(report).model_dump()
            validator('remote_form_learning_source').validate(checked)
        return checked


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Inspect one approved HTTPS form source',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--selected-state-plan-sha256')
    parser.add_argument('--selected-cookie-sha256')
    arguments = parser.parse_args(argv)
    try:
        report = inspect_remote_form_learning_source(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256,
            selected_state_plan_sha256=arguments.selected_state_plan_sha256,
            selected_cookie_sha256=arguments.selected_cookie_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError,
            RecursionError):
        parser.exit(1, 'Remote form source unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

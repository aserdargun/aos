"""Read-only learning source audit for one completed exact static-bundle task."""

import argparse
from pathlib import Path
import json
import re
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .desktop_tasks import Approval
from .learning_events import review_learning_events_snapshot
from .web_application import WebApplicationProfiles
from .web_application_binding import WebTaskAdmissionDraft, verify_web_task_binding
from .web_static_assets import WebStaticAssetPlan
from .web_readonly_data import WebReadOnlyDataBundlePlan, verify_web_bundle_plan


CHECKSUM = re.compile(r'[a-f0-9]{64}\Z')
RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
PAGE_KEYS = {'url', 'title_sha256', 'heading_sha256'}
VERIFICATION_KEYS = PAGE_KEYS | {'responses'}


def inspect_remote_static_learning_source(database: Path, run_id: str, *, profiles: Path,
                                          selected_profile_sha256: str,
                                          selected_plan_sha256: str) -> dict:
    return _inspect_bundle_learning_source(
        database, run_id, profiles=profiles,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256, data_bundle=False)


def inspect_remote_readonly_data_source(database: Path, run_id: str, *, profiles: Path,
                                        selected_profile_sha256: str,
                                        selected_plan_sha256: str) -> dict:
    return _inspect_bundle_learning_source(
        database, run_id, profiles=profiles,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256, data_bundle=True)


def _inspect_bundle_learning_source(database: Path, run_id: str, *, profiles: Path,
                                    selected_profile_sha256: str,
                                    selected_plan_sha256: str,
                                    data_bundle: bool, source=None,
                                    include_verified_responses: bool = False) -> dict | tuple[dict, dict]:
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not isinstance(selected_profile_sha256, str)
            or CHECKSUM.fullmatch(selected_profile_sha256) is None
            or not isinstance(selected_plan_sha256, str)
            or CHECKSUM.fullmatch(selected_plan_sha256) is None):
        raise ValueError('remote_static_learning_selection_invalid')
    profile_store = WebApplicationProfiles(profiles)
    profile = profile_store.get(selected_profile_sha256)
    requested = [role for role in ('system1', 'system2')
                 if getattr(profile.learning, role) == 'requested']
    with (audit_snapshot(database) if source is None else source) as (snapshot, identity):
        row = snapshot.execute('''SELECT binding.*, job.kind, job.status AS job_status,
                job.runtime_id AS job_runtime_id, run.status AS run_status,
                run.outcome, run.policy_version
            FROM desktop_remote_static_asset_bindings AS binding
            JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
            JOIN runs AS run ON run.run_id=binding.run_id
            WHERE binding.run_id=?''', (run_id,)).fetchone()
        if (row is None or row['kind'] != 'browser_remote_static_assets'
                or row['job_status'] != 'succeeded' or row['run_status'] != 'succeeded'
                or row['outcome'] != 'passed'
                or row['policy_version'] != 'browser-remote-static-assets-policy-v1'
                or row['profile_sha256'] != selected_profile_sha256
                or row['plan_sha256'] != selected_plan_sha256
                or row['job_runtime_id'] != row['browser_runtime_id']):
            raise ValueError('remote_static_learning_run_unverified')
        draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
        plan = (WebReadOnlyDataBundlePlan if data_bundle else WebStaticAssetPlan
                ).model_validate_json(row['plan_json'])
        if (canonical(draft.model_dump(mode='json')) != row['draft_json']
                or canonical(plan.model_dump(mode='json')) != row['plan_json']
                or draft.profile_sha256 != selected_profile_sha256
                or draft.binding_sha256 != row['binding_sha256']
                or draft.runtime_sha256 != row['runtime_sha256']
                or draft.runtime.runtime_id != row['browser_runtime_id']
                or plan.profile_sha256 != selected_profile_sha256
                or digest(plan.model_dump()) != selected_plan_sha256):
            raise ValueError('remote_static_learning_binding_changed')
        verify_web_task_binding(profile_store, draft)
        verify_web_bundle_plan(profile_store, draft.task, plan)
        actions = snapshot.execute('SELECT * FROM actions WHERE run_id=?', (run_id,)).fetchall()
        approvals = snapshot.execute('SELECT * FROM desktop_approvals WHERE job_id=?',
                                     (row['job_id'],)).fetchall()
        verifications = snapshot.execute('SELECT * FROM verifications WHERE run_id=?',
                                         (run_id,)).fetchall()
        if (len(actions) != 2 or len(approvals) != 1 or len(verifications) != 1
                or {action['tool'] for action in actions}
                != {'browser.static.open', 'browser.static.observe'}):
            raise ValueError('remote_static_learning_count_changed')
        opened = next(action for action in actions if action['tool'] == 'browser.static.open')
        observed = next(action for action in actions if action['tool'] == 'browser.static.observe')
        expected_arguments = {'profile_sha256': selected_profile_sha256,
                              'binding_sha256': draft.binding_sha256,
                              'plan_sha256': selected_plan_sha256,
                              'entry_url': draft.task.entry_url}
        if (opened['status'] != 'ok' or observed['status'] != 'ok'
                or opened['actual_option'] != 'open_entry'
                or observed['actual_option'] != 'open_entry'
                or opened['step_id'] != observed['step_id']
                or opened['decision_id'] != observed['decision_id']
                or json.loads(opened['arguments_json']) != expected_arguments
                or json.loads(observed['arguments_json']) != {}):
            raise ValueError('remote_static_learning_action_changed')
        decision = snapshot.execute('''SELECT * FROM decisions WHERE decision_id=?
            AND run_id=? AND step_id=?''',
            (opened['decision_id'], run_id, opened['step_id'])).fetchone()
        if (decision is None or decision['selected_option'] != 'open_entry'
                or decision['policy_result'] != 'allow'):
            raise ValueError('remote_static_learning_decision_changed')
        approval_row = approvals[0]
        approval = Approval.model_validate_json(approval_row['envelope_json'])
        if (approval_row['status'] != 'consumed'
                or approval.job_id != row['job_id']
                or approval.action.action_id != opened['action_id']
                or approval.action.run_id != run_id
                or approval.action.step_id != opened['step_id']
                or approval.action.runtime_id != row['browser_runtime_id']
                or approval.action.tool != 'browser.static.open'
                or approval.action.arguments != expected_arguments
                or approval.action.idempotency_key != opened['idempotency_key']
                or approval.action.selected_option != decision['selected_option']
                or approval.action_sha256 != approval_row['action_sha256']
                or digest(approval.action.model_dump(mode='json')) != approval_row['action_sha256']):
            raise ValueError('remote_static_learning_approval_changed')
        human = snapshot.execute('''SELECT actor,payload_json FROM human_interventions
            WHERE run_id=? AND step_id=? AND kind='approve' ''',
            (run_id, opened['step_id'])).fetchall()
        if not any(entry['actor'] == 'local_authenticated_user'
                   and json.loads(entry['payload_json']) == {
                       'approval_id': approval.approval_id,
                       'action_sha256': approval.action_sha256}
                   for entry in human):
            raise ValueError('remote_static_learning_human_approval_missing')
        verification = verifications[0]
        expected = json.loads(verification['expected_json'])
        actual = json.loads(verification['actual_json'])
        open_result = json.loads(opened['result_json'])
        readback = json.loads(observed['result_json'])
        evidence_refs = json.loads(verification['evidence_refs_json'])
        response = expected.get('responses') if isinstance(expected, dict) else None
        response_keys = ({'entry_response_sha256', 'asset_response_sha256',
                          'data_response_sha256'} if data_bundle else
                         {'entry_response_sha256', 'asset_response_sha256'})
        if (verification['step_id'] != opened['step_id']
                or verification['action_id'] != opened['action_id']
                or verification['result'] != 'passed'
                or verification['method'] != ('independent_readonly_data_bundle_readback'
                                              if data_bundle else
                                              'independent_static_bundle_readback')
                or verification['verifier'] != ('aos-remote-readonly-data-v1'
                                                if data_bundle else
                                                'aos-remote-static-assets-v1')
                or not isinstance(expected, dict) or set(expected) != VERIFICATION_KEYS
                or expected != actual or expected['url'] != draft.task.entry_url
                or not isinstance(open_result, dict) or not isinstance(readback, dict)
                or not PAGE_KEYS <= set(open_result) or not PAGE_KEYS <= set(readback)
                or any(open_result[key] != readback[key] or open_result[key] != expected[key]
                       for key in PAGE_KEYS)
                or any(not isinstance(expected[key], str)
                       or CHECKSUM.fullmatch(expected[key]) is None
                       for key in ('title_sha256', 'heading_sha256'))
                or not isinstance(response, dict)
                or set(response) != response_keys
                or not isinstance(response['entry_response_sha256'], str)
                or CHECKSUM.fullmatch(response['entry_response_sha256']) is None
                or not isinstance(response['asset_response_sha256'], list)
                or len(response['asset_response_sha256']) != len(plan.assets)
                or any(not isinstance(item, str) or CHECKSUM.fullmatch(item) is None
                       for item in response['asset_response_sha256'])
                or (data_bundle and (
                    not isinstance(response['data_response_sha256'], list)
                    or len(response['data_response_sha256']) != len(plan.data_resources)
                    or any(not isinstance(item, str) or CHECKSUM.fullmatch(item) is None
                           for item in response['data_response_sha256'])))
                or not isinstance(evidence_refs, list) or len(evidence_refs) != 1
                or not isinstance(evidence_refs[0], str)):
            raise ValueError('remote_static_learning_readback_changed')
        observation = snapshot.execute('''SELECT * FROM observations WHERE observation_id=?
            AND run_id=? AND step_id=?''',
            (evidence_refs[0], run_id, opened['step_id'])).fetchone()
        if (observation is None or observation['action_id'] != observed['action_id']
                or observation['kind'] != ('browser.remote_readonly_data' if data_bundle else
                                           'browser.remote_static_assets')
                or json.loads(observation['payload_json']) != readback):
            raise ValueError('remote_static_learning_observation_changed')
        review = review_learning_events_snapshot(snapshot, identity, run_id)
        if review['snapshot_sha256'] != identity['sha256']:
            raise ValueError('remote_static_learning_snapshot_changed')
        events = {'system1': [], 'system2': []}
        for event in review['events']:
            role = event['role']
            source = event['source']
            if role not in requested or source['step_id'] != opened['step_id']:
                continue
            if role == 'system1' and source['decision_id'] != opened['decision_id']:
                continue
            if role == 'system2' and source['escalation_id'] is None:
                continue
            events[role].append(event['event_id'])
        if any(len(references) > 32 for references in events.values()):
            raise ValueError('remote_static_learning_event_limit')
        report = {'schema_version': '1.0',
                  'mode': ('read_only_remote_json_bundle_source' if data_bundle else
                           'read_only_remote_static_learning_source'),
                  'status': 'unreviewed_metadata_candidate',
                  'profile_sha256': selected_profile_sha256,
                  'plan_sha256': selected_plan_sha256,
                  'run_ref': digest({'run_id': run_id}),
                  'snapshot_sha256': identity['sha256'],
                  'asset_count': len(plan.assets),
                  **({'data_count': len(plan.data_resources)} if data_bundle else {}),
                  'requested_roles': requested,
                  'source_event_ids_by_role': events,
                  'transport_readback_verified': True,
                  'site_outcome_verified': False, 'account_verified': False,
                  'rights_reviewed': False, 'redaction_reviewed': False,
                  'collection_authorized': False, 'training_ready': False}
        validator('remote_readonly_data_source' if data_bundle else
                  'remote_static_learning_source').validate(report)
        if include_verified_responses:
            if not data_bundle:
                raise ValueError('remote_static_learning_response_scope_invalid')
            return report, {'title_sha256': expected['title_sha256'],
                            'heading_sha256': expected['heading_sha256'],
                            'responses': response}
        return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Inspect one approved static-bundle learning source',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = inspect_remote_static_learning_source(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote static learning source unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

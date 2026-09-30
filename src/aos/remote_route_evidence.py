"""Read-only metadata evidence from a completed, explicitly bound HTTPS route task."""

import argparse
import json
from pathlib import Path
import re
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .desktop_tasks import Approval
from .web_application import WebApplicationProfiles
from .web_application_binding import (WebReadOnlyRoutePlan, WebTaskAdmissionDraft,
                                      verify_web_readonly_routes, verify_web_task_binding)


CHECKSUM = re.compile(r'[a-f0-9]{64}\Z')
RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
FINGERPRINT_VERSION = 'remote-route-readback-v1'
IDENTITY_KEYS = {'url', 'title_sha256', 'heading_sha256'}
LINK_KEYS = {'planned_link_indices', 'unregistered_link_count', 'links_truncated'}
LINK_SAMPLE_VERSION = 'remote-route-link-sample-v1'


def complete_link_sample_sha256(observation: dict) -> str | None:
    if (not isinstance(observation, dict) or observation.get('links_truncated') is not False
            or not isinstance(observation.get('planned_link_indices'), list)
            or type(observation.get('unregistered_link_count')) is not int):
        return None
    return digest({'version': LINK_SAMPLE_VERSION,
                   'planned_link_indices': observation['planned_link_indices'],
                   'unregistered_link_count': observation['unregistered_link_count']})


def _decode(value: str, expected: set[str]) -> dict:
    if not isinstance(value, str):
        raise ValueError('remote_route_evidence_missing_json')
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or set(decoded) != expected:
        raise ValueError('remote_route_evidence_shape_changed')
    return decoded


def _route_observation(value: str, route_count: int) -> dict:
    if not isinstance(value, str):
        raise ValueError('remote_route_evidence_missing_json')
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or set(decoded) not in (IDENTITY_KEYS, IDENTITY_KEYS | LINK_KEYS):
        raise ValueError('remote_route_evidence_shape_changed')
    if LINK_KEYS <= set(decoded):
        indices = decoded['planned_link_indices']
        count = decoded['unregistered_link_count']
        if (not isinstance(indices, list) or len(indices) > route_count
                or any(type(index) is not int or index < 0 or index >= route_count
                       for index in indices)
                or indices != sorted(set(indices))
                or type(count) is not int or not 0 <= count <= 64
                or type(decoded['links_truncated']) is not bool):
            raise ValueError('remote_route_evidence_link_shape_changed')
    return decoded


def _review_remote_route_evidence(database: Path, run_id: str, *, profiles: Path,
                                  selected_profile_sha256: str, selected_plan_sha256: str,
                                  source) -> dict:
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not isinstance(selected_profile_sha256, str)
            or CHECKSUM.fullmatch(selected_profile_sha256) is None
            or not isinstance(selected_plan_sha256, str)
            or CHECKSUM.fullmatch(selected_plan_sha256) is None):
        raise ValueError('remote_route_evidence_invalid_selection')
    profile_store = WebApplicationProfiles(profiles)
    with source as (snapshot, identity):
        row = snapshot.execute('''SELECT binding.*,job.kind,job.status AS job_status,
                   job.runtime_id AS job_runtime_id,run.status AS run_status,
                   run.outcome,run.policy_version
            FROM desktop_remote_route_bindings AS binding
            JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
            JOIN runs AS run ON run.run_id=binding.run_id
            WHERE binding.run_id=?''', (run_id,)).fetchone()
        if (row is None or row['kind'] != 'browser_remote_routes'
                or row['job_status'] != 'succeeded' or row['run_status'] != 'succeeded'
                or row['outcome'] != 'passed'
                or row['policy_version'] != 'browser-remote-routes-policy-v1'
                or row['profile_sha256'] != selected_profile_sha256
                or row['plan_sha256'] != selected_plan_sha256
                or row['job_runtime_id'] != row['browser_runtime_id']):
            raise ValueError('remote_route_evidence_run_unverified')
        draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
        plan = WebReadOnlyRoutePlan.model_validate_json(row['plan_json'])
        if (canonical(draft.model_dump(mode='json')) != row['draft_json']
                or canonical(plan.model_dump(mode='json')) != row['plan_json']
                or draft.profile_sha256 != selected_profile_sha256
                or draft.binding_sha256 != row['binding_sha256']
                or draft.runtime_sha256 != row['runtime_sha256']
                or draft.runtime.runtime_id != row['browser_runtime_id']
                or plan.profile_sha256 != selected_profile_sha256
                or digest(plan.model_dump()) != selected_plan_sha256):
            raise ValueError('remote_route_evidence_binding_changed')
        verify_web_task_binding(profile_store, draft)
        verify_web_readonly_routes(profile_store, draft.task, plan)
        route_actions = snapshot.execute('''SELECT * FROM actions WHERE run_id=?
            AND tool='browser.remote.route' ORDER BY created_at,action_id''', (run_id,)).fetchall()
        observe_actions = snapshot.execute('''SELECT * FROM actions WHERE run_id=?
            AND tool='browser.remote.observe' ORDER BY created_at,action_id''', (run_id,)).fetchall()
        approvals = snapshot.execute('''SELECT * FROM desktop_approvals WHERE job_id=?''',
                                     (row['job_id'],)).fetchall()
        verifications = snapshot.execute('''SELECT * FROM verifications WHERE run_id=?''',
                                         (run_id,)).fetchall()
        if (len(route_actions) != len(plan.routes) or len(observe_actions) != len(plan.routes)
                or len(approvals) != len(plan.routes)
                or len(verifications) != len(plan.routes)
                or snapshot.execute('SELECT count(*) FROM actions WHERE run_id=?',
                                    (run_id,)).fetchone()[0] != 2 * len(plan.routes)):
            raise ValueError('remote_route_evidence_count_changed')
        by_index = {}
        for action in route_actions:
            arguments = _decode(action['arguments_json'],
                                {'profile_sha256', 'binding_sha256', 'plan_sha256',
                                 'route_index', 'url'})
            index = arguments['route_index']
            if (type(index) is not int or index < 0 or index >= len(plan.routes)
                    or index in by_index or action['status'] != 'ok'
                    or action['actual_option'] != 'open_entry'
                    or arguments != {'profile_sha256': selected_profile_sha256,
                                     'binding_sha256': row['binding_sha256'],
                                     'plan_sha256': selected_plan_sha256,
                                     'route_index': index, 'url': plan.routes[index]}):
                raise ValueError('remote_route_evidence_action_changed')
            by_index[index] = action
        route_reports = []
        for index, url in enumerate(plan.routes):
            action = by_index[index]
            decision = snapshot.execute('''SELECT * FROM decisions WHERE decision_id=?
                AND run_id=? AND step_id=?''',
                (action['decision_id'], run_id, action['step_id'])).fetchone()
            if (decision is None or decision['selected_option'] != 'open_entry'
                    or decision['policy_result'] != 'allow'):
                raise ValueError('remote_route_evidence_decision_changed')
            matching_approvals = []
            for approval_row in approvals:
                approval = Approval.model_validate_json(approval_row['envelope_json'])
                if approval.action.action_id == action['action_id']:
                    matching_approvals.append((approval_row, approval))
            if len(matching_approvals) != 1:
                raise ValueError('remote_route_evidence_approval_missing')
            approval_row, approval = matching_approvals[0]
            if (approval_row['status'] != 'consumed'
                    or approval.job_id != row['job_id']
                    or approval.action.run_id != run_id
                    or approval.action.step_id != action['step_id']
                    or approval.action.runtime_id != row['browser_runtime_id']
                    or approval.action.tool != 'browser.remote.route'
                    or approval.action.arguments != json.loads(action['arguments_json'])
                    or approval.action_sha256 != approval_row['action_sha256']
                    or digest(approval.action.model_dump(mode='json')) != approval_row['action_sha256']):
                raise ValueError('remote_route_evidence_approval_changed')
            intervention = snapshot.execute('''SELECT actor,kind,payload_json FROM human_interventions
                WHERE run_id=? AND step_id=? AND kind='approve' ''',
                (run_id, action['step_id'])).fetchall()
            if not any(entry['actor'] == 'local_authenticated_user'
                       and json.loads(entry['payload_json']) == {
                           'approval_id': approval.approval_id,
                           'action_sha256': approval.action_sha256}
                       for entry in intervention):
                raise ValueError('remote_route_evidence_human_approval_missing')
            opened = _route_observation(action['result_json'], len(plan.routes))
            read_actions = [candidate for candidate in observe_actions
                            if candidate['decision_id'] == action['decision_id']
                            and candidate['step_id'] == action['step_id']
                            and json.loads(candidate['arguments_json']) == {'route_index': index}]
            if len(read_actions) != 1 or read_actions[0]['status'] != 'ok':
                raise ValueError('remote_route_evidence_readback_missing')
            readback = _route_observation(read_actions[0]['result_json'], len(plan.routes))
            matches = [candidate for candidate in verifications
                       if candidate['action_id'] == action['action_id']
                       and candidate['step_id'] == action['step_id']]
            if len(matches) != 1:
                raise ValueError('remote_route_evidence_verification_missing')
            verification = matches[0]
            expected = _decode(verification['expected_json'],
                               {'url', 'title_sha256', 'heading_sha256', 'response_sha256'})
            actual = _decode(verification['actual_json'],
                             {'url', 'title_sha256', 'heading_sha256', 'response_sha256'})
            evidence_refs = json.loads(verification['evidence_refs_json'])
            if (not isinstance(evidence_refs, list) or len(evidence_refs) != 1
                    or not isinstance(evidence_refs[0], str)):
                raise ValueError('remote_route_evidence_reference_changed')
            if (verification['result'] != 'passed'
                    or verification['method'] != 'independent_remote_route_readback'
                    or verification['verifier'] != 'aos-remote-routes-v1'
                    or expected != actual
                    or expected['url'] != url
                    or any(opened[key] != readback[key] or opened[key] != expected[key]
                           for key in IDENTITY_KEYS)
                    or not isinstance(expected['response_sha256'], str)
                    or CHECKSUM.fullmatch(expected['response_sha256']) is None):
                raise ValueError('remote_route_evidence_verification_changed')
            observation = snapshot.execute('''SELECT * FROM observations WHERE observation_id=?
                AND run_id=? AND step_id=?''',
                (evidence_refs[0], run_id, action['step_id'])).fetchone()
            if (observation is None or observation['action_id'] != read_actions[0]['action_id']
                    or observation['kind'] != 'browser.remote_route'
                    or json.loads(observation['payload_json']) != readback):
                raise ValueError('remote_route_evidence_observation_changed')
            if any(not isinstance(opened[key], str) or CHECKSUM.fullmatch(opened[key]) is None
                   for key in ('title_sha256', 'heading_sha256')) or opened['url'] != url:
                raise ValueError('remote_route_evidence_fingerprint_changed')
            url_sha256 = digest({'url': url})
            route_report = {'route_index': index, 'url_sha256': url_sha256,
                                  'title_sha256': opened['title_sha256'],
                                  'heading_sha256': opened['heading_sha256'],
                                  'page_fingerprint_sha256': digest({
                                      'version': FINGERPRINT_VERSION,
                                      'url_sha256': url_sha256,
                                      'title_sha256': opened['title_sha256'],
                                      'heading_sha256': opened['heading_sha256']}),
                                  'verification_ref': digest({'verification_id': verification['verification_id']})}
            if LINK_KEYS <= set(opened) and LINK_KEYS <= set(readback) and all(
                    opened[key] == readback[key] for key in LINK_KEYS):
                route_report.update({key: opened[key] for key in LINK_KEYS})
                route_report['link_inventory_readback_matched'] = True
            route_reports.append(route_report)
        report = {'schema_version': '1.0', 'mode': 'read_only_remote_route_evidence',
                  'status': 'historical_verified_metadata',
                  'fingerprint_version': FINGERPRINT_VERSION,
                  'snapshot_sha256': identity['sha256'],
                  'run_ref': digest({'run_id': run_id}),
                  'profile_sha256': selected_profile_sha256,
                  'plan_sha256': selected_plan_sha256,
                  'route_count': len(route_reports), 'routes': route_reports,
                  'profile_bound': True, 'readback_verified': True,
                  'origin_verified': False, 'account_verified': False,
                  'site_outcome_verified': False,
                  'reviewed': False, 'task_retrieval_authorized': False,
                  'execution_authorized': False, 'collection_authorized': False,
                  'training_ready': False}
        validator('remote_route_evidence').validate(report)
        return report


def review_remote_route_evidence(database: Path, run_id: str, *, profiles: Path,
                                 selected_profile_sha256: str, selected_plan_sha256: str) -> dict:
    return _review_remote_route_evidence(
        database, run_id, profiles=profiles,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256,
        source=audit_snapshot(database))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Review completed read-only HTTPS route metadata')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = review_remote_route_evidence(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote route evidence unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

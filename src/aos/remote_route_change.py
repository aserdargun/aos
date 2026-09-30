"""Read-only change preview for two completed, profile-bound HTTPS route runs."""

import argparse
from contextlib import nullcontext
from pathlib import Path
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .remote_route_evidence import (FINGERPRINT_VERSION, RUN_ID,
                                    _review_remote_route_evidence,
                                    complete_link_sample_sha256)


def _compare_remote_route_change_snapshot(database: Path, snapshot: sqlite3.Connection,
                                          identity: dict, before_run_id: str, after_run_id: str, *,
                                          profiles: Path, selected_profile_sha256: str,
                                          selected_plan_sha256: str) -> dict:
    if (not isinstance(before_run_id, str) or RUN_ID.fullmatch(before_run_id) is None
            or not isinstance(after_run_id, str) or RUN_ID.fullmatch(after_run_id) is None
            or before_run_id == after_run_id):
        raise ValueError('remote_route_change_invalid_runs')
    before = _review_remote_route_evidence(
        database, before_run_id, profiles=profiles,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256,
        source=nullcontext((snapshot, identity)))
    after = _review_remote_route_evidence(
        database, after_run_id, profiles=profiles,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256,
        source=nullcontext((snapshot, identity)))
    if (before['snapshot_sha256'] != after['snapshot_sha256']
            or before['fingerprint_version'] != FINGERPRINT_VERSION
            or after['fingerprint_version'] != FINGERPRINT_VERSION
            or before['run_ref'] != digest({'run_id': before_run_id})
            or after['run_ref'] != digest({'run_id': after_run_id})
            or before['profile_sha256'] != after['profile_sha256']
            or before['plan_sha256'] != after['plan_sha256']
            or before['route_count'] != after['route_count']
            or any(left['route_index'] != right['route_index']
                   or left['url_sha256'] != right['url_sha256']
                   for left, right in zip(before['routes'], after['routes']))):
        raise ValueError('remote_route_change_incomparable_sources')
    if identity['sha256'] != before['snapshot_sha256']:
        raise ValueError('remote_route_change_snapshot_changed')
    rows = snapshot.execute('''SELECT run_id,started_at FROM runs WHERE run_id IN (?,?)''',
                            (before_run_id, after_run_id)).fetchall()
    started = {row['run_id']: row['started_at'] for row in rows}
    if (len(started) != 2 or not isinstance(started[before_run_id], str)
            or not isinstance(started[after_run_id], str)
            or started[before_run_id] >= started[after_run_id]):
        raise ValueError('remote_route_change_order_invalid')
    routes = []
    for left, right in zip(before['routes'], after['routes']):
        route = {'route_index': left['route_index'],
                 'url_sha256': left['url_sha256'],
                 'before_fingerprint_sha256': left['page_fingerprint_sha256'],
                 'after_fingerprint_sha256': right['page_fingerprint_sha256'],
                 'changed': left['page_fingerprint_sha256'] != right['page_fingerprint_sha256']}
        if (left.get('link_inventory_readback_matched') is True
                and right.get('link_inventory_readback_matched') is True
                and left['links_truncated'] is False and right['links_truncated'] is False):
            route['sampled_link_inventory_changed'] = (
                left['planned_link_indices'] != right['planned_link_indices']
                or left['unregistered_link_count'] != right['unregistered_link_count'])
            if not route['sampled_link_inventory_changed']:
                route['link_sample_sha256'] = complete_link_sample_sha256(left)
                route['planned_link_indices'] = left['planned_link_indices']
        routes.append(route)
    report = {'schema_version': '1.0', 'mode': 'read_only_remote_route_change',
              'status': ('changed_profile_bound' if any(route['changed'] for route in routes)
                         else 'unchanged_profile_bound'),
              'fingerprint_version': FINGERPRINT_VERSION,
              'snapshot_sha256': before['snapshot_sha256'],
              'profile_sha256': before['profile_sha256'],
              'plan_sha256': before['plan_sha256'],
              'before_run_ref': before['run_ref'],
              'after_run_ref': after['run_ref'],
              'route_count': len(routes), 'routes': routes,
              'profile_bound': True, 'readback_verified': True,
              'origin_verified': False, 'account_verified': False,
              'site_outcome_verified': False, 'reviewed': False,
              'task_retrieval_authorized': False, 'execution_authorized': False,
              'collection_authorized': False, 'training_ready': False}
    validator('remote_route_change').validate(report)
    return report


def compare_remote_route_change(database: Path, before_run_id: str, after_run_id: str, *,
                                profiles: Path, selected_profile_sha256: str,
                                selected_plan_sha256: str) -> dict:
    if (not isinstance(before_run_id, str) or RUN_ID.fullmatch(before_run_id) is None
            or not isinstance(after_run_id, str) or RUN_ID.fullmatch(after_run_id) is None
            or before_run_id == after_run_id):
        raise ValueError('remote_route_change_invalid_runs')
    with audit_snapshot(database) as (snapshot, identity):
        return _compare_remote_route_change_snapshot(
            database, snapshot, identity, before_run_id, after_run_id,
            profiles=profiles, selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Compare two completed HTTPS route metadata runs',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = compare_remote_route_change(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote route change unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

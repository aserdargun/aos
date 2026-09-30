"""Read-only observed navigation graph from two audited HTTPS route runs."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical
from .dataset import validator
from .dataset_audit import audit_snapshot
from .remote_route_change import _compare_remote_route_change_snapshot


def preview_remote_navigation_graph(database: Path, before_run_id: str, after_run_id: str, *,
                                    profiles: Path, selected_profile_sha256: str,
                                    selected_plan_sha256: str) -> dict:
    with audit_snapshot(database) as (snapshot, identity):
        change = _compare_remote_route_change_snapshot(
            database, snapshot, identity, before_run_id, after_run_id,
            profiles=profiles, selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256)
    stable = {route['route_index']: route for route in change['routes'] if not route['changed']}
    pages = []
    edges = []
    incomplete = []
    for route_index, route in stable.items():
        page = {'route_index': route_index, 'url_sha256': route['url_sha256'],
                'page_fingerprint_sha256': route['after_fingerprint_sha256']}
        if 'link_sample_sha256' in route:
            page['link_sample_sha256'] = route['link_sample_sha256']
            for target_index in route['planned_link_indices']:
                if target_index in stable:
                    edges.append({'source_route_index': route_index,
                                  'target_route_index': target_index})
        else:
            incomplete.append(route_index)
        pages.append(page)
    report = {'schema_version': '1.0', 'mode': 'read_only_observed_navigation_graph',
              'status': 'candidate_observed_route_graph',
              'fingerprint_version': change['fingerprint_version'],
              'snapshot_sha256': change['snapshot_sha256'],
              'profile_sha256': change['profile_sha256'],
              'plan_sha256': change['plan_sha256'],
              'before_run_ref': change['before_run_ref'],
              'after_run_ref': change['after_run_ref'],
              'route_count': change['route_count'],
              'stable_pages': pages, 'observed_edges': edges,
              'changed_route_indices': [route['route_index'] for route in change['routes']
                                        if route['changed']],
              'incomplete_link_sample_indices': incomplete,
              'profile_bound': True, 'readback_verified': True,
              'semantic_page_keys_verified': False,
              'origin_verified': False, 'account_verified': False,
              'site_outcome_verified': False, 'reviewed': False,
              'task_retrieval_authorized': False, 'execution_authorized': False,
              'collection_authorized': False, 'training_ready': False}
    validator('remote_navigation_graph').validate(report)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Preview an audited observed HTTPS route graph',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = preview_remote_navigation_graph(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote navigation graph unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

"""Read-only match of stable HTTPS route evidence to an exact manual page draft."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .remote_route_change import _compare_remote_route_change_snapshot
from .site_knowledge import SiteKnowledgeStore
from .web_application import WebApplicationProfiles


def preview_remote_route_knowledge(database: Path, before_run_id: str, after_run_id: str, *,
                                   profiles: Path, store: Path, knowledge_sha256: str,
                                   selected_profile_sha256: str,
                                   selected_plan_sha256: str, route_index: int) -> dict:
    if type(route_index) is not int or not 0 <= route_index <= 7:
        raise ValueError('remote_route_knowledge_invalid_index')
    with audit_snapshot(database) as (snapshot, identity):
        change = _compare_remote_route_change_snapshot(
            database, snapshot, identity, before_run_id, after_run_id,
            profiles=profiles, selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256)
        if (route_index >= change['route_count']
                or change['routes'][route_index]['changed']):
            raise ValueError('remote_route_knowledge_unstable_source')
    profile_store = WebApplicationProfiles(profiles)
    profile = profile_store.get(selected_profile_sha256)
    for report in profile_store.list():
        if report['profile_sha256'] != selected_profile_sha256:
            successor = profile_store.get(report['profile_sha256'])
            if successor.previous_sha256 == selected_profile_sha256:
                raise ValueError('remote_route_knowledge_profile_superseded')
    page_store = SiteKnowledgeStore(store, profile_store)
    page = page_store.get(knowledge_sha256)
    route = change['routes'][route_index]
    if (page.profile_sha256 != selected_profile_sha256
            or page.application_key != profile.application_key
            or page.tenant_key != profile.tenant_key
            or page.account_role != profile.account_role
            or page.origin not in profile.allowed_origins
            or '{' in page.route_template
            or digest({'url': page.origin + page.route_template}) != route['url_sha256']
            or page.page_fingerprint_sha256 != route['after_fingerprint_sha256']):
        raise ValueError('remote_route_knowledge_draft_not_bound')
    inspection = page_store.inspect(
        knowledge_sha256, selected_profile_sha256=selected_profile_sha256,
        current_fingerprint_sha256=route['after_fingerprint_sha256'])
    if inspection['status'] != 'draft_match':
        raise ValueError('remote_route_knowledge_draft_stale')
    outgoing_route_indices = []
    outgoing_knowledge_sha256 = []
    if page.outgoing_page_keys:
        if route.get('link_sample_sha256') is None:
            raise ValueError('remote_route_knowledge_outgoing_links_unbound')
        reports = page_store.list(profile_sha256=selected_profile_sha256)
        for outgoing_key in page.outgoing_page_keys:
            matches = []
            for report in reports:
                if report['page_key'] != outgoing_key:
                    continue
                target = page_store.get(report['knowledge_sha256'])
                if (target.application_key != page.application_key
                        or target.tenant_key != page.tenant_key
                        or target.account_role != page.account_role
                        or target.origin != page.origin
                        or '{' in target.route_template):
                    continue
                target_url_sha256 = digest({'url': target.origin + target.route_template})
                for target_route in change['routes']:
                    if (target_route['url_sha256'] == target_url_sha256
                            and target_route['route_index'] in route['planned_link_indices']
                            and target_route['after_fingerprint_sha256'] == target.page_fingerprint_sha256
                            and page_store.inspect(
                                report['knowledge_sha256'],
                                selected_profile_sha256=selected_profile_sha256,
                                current_fingerprint_sha256=target_route['after_fingerprint_sha256'])['status']
                            == 'draft_match'):
                        matches.append((target_route['route_index'], report['knowledge_sha256']))
            if len(matches) != 1:
                raise ValueError('remote_route_knowledge_outgoing_key_unbound')
            outgoing_route_indices.append(matches[0][0])
            outgoing_knowledge_sha256.append(matches[0][1])
        if len(set(outgoing_route_indices)) != len(outgoing_route_indices):
            raise ValueError('remote_route_knowledge_outgoing_key_ambiguous')
    report = {'schema_version': '1.0', 'mode': 'read_only_remote_route_knowledge',
              'status': 'candidate_remote_profile_bound',
              'knowledge_sha256': knowledge_sha256,
              'draft_revision': page.revision, 'page_key': page.page_key,
              'route_index': route_index,
              'page_fingerprint_sha256': route['after_fingerprint_sha256'],
              'profile_sha256': selected_profile_sha256,
              'plan_sha256': selected_plan_sha256,
              'snapshot_sha256': change['snapshot_sha256'],
              'before_run_ref': change['before_run_ref'],
              'after_run_ref': change['after_run_ref'],
              'profile_bound': True, 'route_readback_bound': True,
              'origin_verified': False, 'account_verified': False,
              'site_outcome_verified': False, 'reviewed': False,
              'task_retrieval_authorized': False, 'execution_authorized': False,
              'collection_authorized': False, 'training_ready': False}
    if page.outgoing_page_keys:
        report['link_sample_sha256'] = route['link_sample_sha256']
        report['outgoing_route_indices'] = outgoing_route_indices
        report['outgoing_knowledge_sha256'] = outgoing_knowledge_sha256
    validator('remote_route_knowledge').validate(report)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Preview an exact manual page draft against remote route evidence',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--knowledge-sha256', required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--route-index', type=int, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = preview_remote_route_knowledge(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            profiles=arguments.profiles, store=arguments.store,
            knowledge_sha256=arguments.knowledge_sha256,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256,
            route_index=arguments.route_index)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote route knowledge unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

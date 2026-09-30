"""Read-only match of stable JSON-bundle evidence to one manual page draft."""

import argparse
from contextlib import nullcontext
from pathlib import Path
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .remote_readonly_data_change import compare_remote_readonly_data_change
from .site_knowledge import SiteKnowledgeStore
from .web_application import WebApplicationProfiles
from .web_application_binding import WebTaskAdmissionDraft
from .web_readonly_data import WebReadOnlyDataBundlePlan


def preview_remote_readonly_data_knowledge(
        database: Path, before_run_id: str, after_run_id: str, *, profiles: Path,
        store: Path, knowledge_sha256: str, selected_profile_sha256: str,
        selected_plan_sha256: str, _source=None) -> dict:
    with (audit_snapshot(database) if _source is None else _source) as (snapshot, identity):
        change = compare_remote_readonly_data_change(
            database, before_run_id, after_run_id, profiles=profiles,
            selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256,
            _source=nullcontext((snapshot, identity)))
        if (change['snapshot_sha256'] != identity['sha256']
                or change['status'] != 'unchanged_profile_bound'
                or change['before_fingerprint_sha256']
                != change['after_fingerprint_sha256']):
            raise ValueError('remote_readonly_data_knowledge_unstable_source')
        row = snapshot.execute('''SELECT draft_json,plan_json
            FROM desktop_remote_static_asset_bindings WHERE run_id=?''',
            (after_run_id,)).fetchone()
        if row is None:
            raise ValueError('remote_readonly_data_knowledge_binding_missing')
        draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
        plan = WebReadOnlyDataBundlePlan.model_validate_json(row['plan_json'])
        if (canonical(draft.model_dump(mode='json')) != row['draft_json']
                or canonical(plan.model_dump(mode='json')) != row['plan_json']
                or draft.profile_sha256 != selected_profile_sha256
                or plan.profile_sha256 != selected_profile_sha256
                or digest(plan.model_dump()) != selected_plan_sha256):
            raise ValueError('remote_readonly_data_knowledge_binding_changed')
    profile_store = WebApplicationProfiles(profiles)
    profile = profile_store.get(selected_profile_sha256)
    for report in profile_store.list():
        if report['profile_sha256'] != selected_profile_sha256:
            successor = profile_store.get(report['profile_sha256'])
            if successor.previous_sha256 == selected_profile_sha256:
                raise ValueError('remote_readonly_data_knowledge_profile_superseded')
    page_store = SiteKnowledgeStore(store, profile_store)
    page = page_store.get(knowledge_sha256)
    if (page.profile_sha256 != selected_profile_sha256
            or page.application_key != profile.application_key
            or page.tenant_key != profile.tenant_key
            or page.account_role != profile.account_role
            or page.origin not in profile.allowed_origins
            or '{' in page.route_template
            or page.origin + page.route_template != draft.task.entry_url
            or page.outgoing_page_keys
            or page.page_fingerprint_sha256 != change['after_fingerprint_sha256']
            or page_store.inspect(
                knowledge_sha256, selected_profile_sha256=selected_profile_sha256,
                current_fingerprint_sha256=change['after_fingerprint_sha256'])['status']
            != 'draft_match'):
        raise ValueError('remote_readonly_data_knowledge_draft_not_bound')
    result = {'schema_version': '1.0', 'mode': 'read_only_remote_json_bundle_knowledge',
              'status': 'candidate_remote_profile_bound',
              'knowledge_sha256': knowledge_sha256,
              'draft_revision': page.revision, 'page_key': page.page_key,
              'page_fingerprint_sha256': change['after_fingerprint_sha256'],
              'profile_sha256': selected_profile_sha256,
              'plan_sha256': selected_plan_sha256,
              'snapshot_sha256': change['snapshot_sha256'],
              'before_run_ref': change['before_run_ref'],
              'after_run_ref': change['after_run_ref'],
              'asset_count': change['asset_count'], 'data_count': change['data_count'],
              'profile_bound': True, 'transport_readback_bound': True,
              'origin_verified': False, 'account_verified': False,
              'site_outcome_verified': False, 'reviewed': False,
              'task_retrieval_authorized': False, 'execution_authorized': False,
              'collection_authorized': False, 'training_ready': False}
    validator('remote_readonly_data_knowledge').validate(result)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description='Preview a manual page draft against stable JSON-bundle evidence',
        allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--knowledge-sha256', required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        result = preview_remote_readonly_data_knowledge(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            profiles=arguments.profiles, store=arguments.store,
            knowledge_sha256=arguments.knowledge_sha256,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'JSON bundle knowledge unavailable: missing, changed or unsafe source.\n')
    print(canonical(result))


if __name__ == '__main__':
    main()

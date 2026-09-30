"""Read-only transport drift preview for two approved JSON-bundle runs."""

import argparse
from contextlib import nullcontext
from pathlib import Path
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .remote_static_learning_source import CHECKSUM, RUN_ID, _inspect_bundle_learning_source


FINGERPRINT_VERSION = 'remote-json-bundle-transport-v1'


def compare_remote_readonly_data_change(database: Path, before_run_id: str,
                                        after_run_id: str, *, profiles: Path,
                                        selected_profile_sha256: str,
                                        selected_plan_sha256: str, _source=None) -> dict:
    if (not isinstance(before_run_id, str) or RUN_ID.fullmatch(before_run_id) is None
            or not isinstance(after_run_id, str) or RUN_ID.fullmatch(after_run_id) is None
            or before_run_id == after_run_id
            or not isinstance(selected_profile_sha256, str)
            or CHECKSUM.fullmatch(selected_profile_sha256) is None
            or not isinstance(selected_plan_sha256, str)
            or CHECKSUM.fullmatch(selected_plan_sha256) is None):
        raise ValueError('remote_readonly_data_change_invalid_selection')
    with (audit_snapshot(database) if _source is None else _source) as (snapshot, identity):
        selections = {'profiles': profiles,
                      'selected_profile_sha256': selected_profile_sha256,
                      'selected_plan_sha256': selected_plan_sha256,
                      'data_bundle': True,
                      'include_verified_responses': True}
        before, before_transport = _inspect_bundle_learning_source(
            database, before_run_id, source=nullcontext((snapshot, identity)), **selections)
        after, after_transport = _inspect_bundle_learning_source(
            database, after_run_id, source=nullcontext((snapshot, identity)), **selections)
        if (before['snapshot_sha256'] != identity['sha256']
                or after['snapshot_sha256'] != identity['sha256']
                or before['profile_sha256'] != after['profile_sha256']
                or before['plan_sha256'] != after['plan_sha256']
                or before['asset_count'] != after['asset_count']
                or before['data_count'] != after['data_count']):
            raise ValueError('remote_readonly_data_change_incomparable_sources')
        rows = snapshot.execute('SELECT run_id,started_at FROM runs WHERE run_id IN (?,?)',
                                (before_run_id, after_run_id)).fetchall()
        started = {row['run_id']: row['started_at'] for row in rows}
        if (len(started) != 2 or not isinstance(started[before_run_id], str)
                or not isinstance(started[after_run_id], str)
                or started[before_run_id] >= started[after_run_id]):
            raise ValueError('remote_readonly_data_change_order_invalid')
        previous = before_transport['responses']
        current = after_transport['responses']
        asset_changed_indices = [index for index, (left, right) in enumerate(zip(
            previous['asset_response_sha256'], current['asset_response_sha256'])) if left != right]
        data_changed_indices = [index for index, (left, right) in enumerate(zip(
            previous['data_response_sha256'], current['data_response_sha256'])) if left != right]
        entry_response_changed = (previous['entry_response_sha256']
                                  != current['entry_response_sha256'])
        page_identity_changed = any(before_transport[key] != after_transport[key]
                                    for key in ('title_sha256', 'heading_sha256'))
        changed = (entry_response_changed or page_identity_changed
                   or bool(asset_changed_indices) or bool(data_changed_indices))
        report = {'schema_version': '1.0', 'mode': 'read_only_remote_json_bundle_change',
                  'status': 'changed_profile_bound' if changed else 'unchanged_profile_bound',
                  'fingerprint_version': FINGERPRINT_VERSION,
                  'snapshot_sha256': identity['sha256'],
                  'profile_sha256': selected_profile_sha256,
                  'plan_sha256': selected_plan_sha256,
                  'before_run_ref': before['run_ref'],
                  'after_run_ref': after['run_ref'],
                  'asset_count': before['asset_count'], 'data_count': before['data_count'],
                  'before_fingerprint_sha256': digest({
                      'version': FINGERPRINT_VERSION, **before_transport}),
                  'after_fingerprint_sha256': digest({
                      'version': FINGERPRINT_VERSION, **after_transport}),
                  'entry_response_changed': entry_response_changed,
                  'page_identity_changed': page_identity_changed,
                  'asset_changed_indices': asset_changed_indices,
                  'data_changed_indices': data_changed_indices,
                  'profile_bound': True, 'transport_readback_verified': True,
                  'site_outcome_verified': False, 'account_verified': False,
                  'rights_reviewed': False, 'reviewed': False,
                  'task_retrieval_authorized': False,
                  'execution_authorized': False, 'collection_authorized': False,
                  'training_ready': False}
        validator('remote_readonly_data_change').validate(report)
        return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Compare two approved JSON-bundle transport runs',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = compare_remote_readonly_data_change(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'JSON bundle change unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

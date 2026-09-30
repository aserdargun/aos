"""Read-only local-fixture page candidate with persisted profile-to-run provenance."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import REPO_ROOT, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .local_navigation_admission import SyntheticNavigationPin, check_synthetic_navigation_profile
from .site_knowledge import SiteKnowledgeStore
from .site_page_retrieval import preview_site_page_retrieval
from .web_application import WebApplicationProfiles


def preview_local_bound_site_page_retrieval(database: Path, before_run_id: str, after_run_id: str, *,
                                            before_verification_id: str, after_verification_id: str,
                                            profiles: Path, store: Path, knowledge_sha256: str,
                                            selected_profile_sha256: str) -> dict:
    unbound = preview_site_page_retrieval(
        database, before_run_id, after_run_id,
        before_verification_id=before_verification_id,
        after_verification_id=after_verification_id,
        profiles=profiles, store=store, knowledge_sha256=knowledge_sha256,
        selected_profile_sha256=selected_profile_sha256)
    profile_store = WebApplicationProfiles(profiles)
    page = SiteKnowledgeStore(store, profile_store).get(knowledge_sha256)
    if (page.page_key != unbound['page_key'] or page.route_template != '/' + page.page_key
            or page.origin != profile_store.get(selected_profile_sha256).allowed_origins[0]):
        raise ValueError('local_page_route_not_bound')
    pin_hashes = []
    with audit_snapshot(database) as (snapshot, identity):
        if identity['sha256'] != unbound['snapshot_sha256']:
            raise ValueError('local_page_snapshot_changed')
        for run_id in (before_run_id, after_run_id):
            row = snapshot.execute('''SELECT binding.*, job.kind,job.status AS job_status,
                       job.runtime_id AS job_runtime_id,run.status AS run_status,
                       run.outcome,run.policy_version
                FROM desktop_web_profile_bindings AS binding
                JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
                JOIN runs AS run ON run.run_id=binding.run_id
                WHERE binding.run_id=?''', (run_id,)).fetchone()
            if (row is None or row['profile_sha256'] != selected_profile_sha256
                    or row['task_key'] != 'synthetic-local-navigation'
                    or row['kind'] != 'browser_local_navigation'
                    or row['job_status'] != 'succeeded' or row['run_status'] != 'succeeded'
                    or row['outcome'] != 'passed'
                    or row['policy_version'] != 'browser-local-navigation-policy-v1'
                    or row['job_runtime_id'] != row['browser_runtime_id']):
                raise ValueError('local_page_run_binding_missing_or_changed')
            pin = SyntheticNavigationPin.model_validate_json(row['pin_json'])
            pin_record = pin.model_dump(mode='json')
            if (canonical(pin_record) != row['pin_json']
                    or digest(pin_record) != row['pin_sha256']
                    or pin.profile_sha256 != selected_profile_sha256):
                raise ValueError('local_page_pin_binding_changed')
            check_synthetic_navigation_profile(profile_store, pin)
            pin_hashes.append(row['pin_sha256'])
    report = {**unbound, 'mode': 'read_only_local_profile_page_retrieval',
              'status': 'candidate_local_profile_bound',
              'before_pin_sha256': pin_hashes[0], 'after_pin_sha256': pin_hashes[1],
              'profile_bound': True}
    validator('site_page_retrieval_bound').validate(report)
    return report


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, message):
        raise SystemExit('Local page retrieval unavailable: invalid arguments.')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Preview a local-fixture profile-bound page candidate')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--before-verification-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--after-verification-id', required=True)
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--knowledge-sha256', required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = preview_local_bound_site_page_retrieval(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            before_verification_id=arguments.before_verification_id,
            after_verification_id=arguments.after_verification_id,
            profiles=arguments.profiles, store=arguments.store,
            knowledge_sha256=arguments.knowledge_sha256,
            selected_profile_sha256=arguments.selected_profile_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Local page retrieval unavailable: missing, stale or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

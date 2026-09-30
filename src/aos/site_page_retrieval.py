"""Read-only, unbound retrieval candidate from an exact synthetic page draft and two runs."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import REPO_ROOT, canonical
from .dataset import validator
from .site_knowledge import SiteKnowledgeStore
from .site_page_change import compare_site_page_change
from .site_page_comparison import compare_site_page_draft
from .web_application import WebApplicationProfiles


def preview_site_page_retrieval(database: Path, before_run_id: str, after_run_id: str, *,
                                before_verification_id: str, after_verification_id: str,
                                profiles: Path, store: Path, knowledge_sha256: str,
                                selected_profile_sha256: str) -> dict:
    change = compare_site_page_change(
        database, before_run_id, after_run_id,
        before_verification_id=before_verification_id,
        after_verification_id=after_verification_id)
    comparison = compare_site_page_draft(
        database, after_run_id, profiles=profiles, store=store,
        knowledge_sha256=knowledge_sha256,
        selected_profile_sha256=selected_profile_sha256,
        verification_id=after_verification_id)
    if (change['status'] != 'unchanged_unbound'
            or comparison['status'] != 'hash_equal_unbound'
            or change['page_key'] != comparison['draft_page_key']
            or change['page_key'] != comparison['evidence_page_key']
            or change['after']['snapshot_sha256'] != comparison['snapshot_sha256']
            or change['after']['run_ref'] != comparison['run_ref']
            or change['after']['page_fingerprint_sha256'] != comparison['evidence_fingerprint_sha256']
            or change['fingerprint_version'] != comparison['evidence_fingerprint_version']):
        raise ValueError('site_page_retrieval_source_mismatch')
    inspection = SiteKnowledgeStore(store, WebApplicationProfiles(profiles)).inspect(
        knowledge_sha256, selected_profile_sha256=selected_profile_sha256,
        current_fingerprint_sha256=comparison['evidence_fingerprint_sha256'])
    if (inspection['status'] != 'draft_match'
            or inspection['page_key'] != comparison['draft_page_key']
            or inspection['revision'] != comparison['draft_revision']):
        raise ValueError('site_page_retrieval_draft_stale')
    report = {
        'schema_version': '1.0', 'mode': 'read_only_synthetic_page_retrieval',
        'status': 'candidate_unbound', 'knowledge_sha256': knowledge_sha256,
        'selected_profile_sha256': selected_profile_sha256,
        'draft_revision': comparison['draft_revision'], 'page_key': change['page_key'],
        'fingerprint_version': change['fingerprint_version'],
        'page_fingerprint_sha256': change['after']['page_fingerprint_sha256'],
        'snapshot_sha256': change['after']['snapshot_sha256'],
        'before_run_ref': change['before']['run_ref'],
        'after_run_ref': change['after']['run_ref'],
        'profile_bound': False, 'origin_verified': False, 'route_verified': False,
        'reviewed': False, 'execution_authorized': False,
        'collection_authorized': False, 'training_ready': False,
    }
    validator('site_page_retrieval').validate(report)
    return report


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, message):
        raise SystemExit('Site page retrieval unavailable: invalid arguments.')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Preview an unbound synthetic page retrieval candidate')
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
        report = preview_site_page_retrieval(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            before_verification_id=arguments.before_verification_id,
            after_verification_id=arguments.after_verification_id,
            profiles=arguments.profiles, store=arguments.store,
            knowledge_sha256=arguments.knowledge_sha256,
            selected_profile_sha256=arguments.selected_profile_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Site page retrieval unavailable: missing, stale or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

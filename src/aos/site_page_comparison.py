"""Read-only comparison of one immutable draft with verified synthetic page evidence."""

import argparse
from pathlib import Path
import re
import sqlite3

from .contracts import REPO_ROOT, canonical
from .dataset import validator
from .site_knowledge import SiteKnowledgeStore
from .site_page_evidence import review_site_page_evidence
from .web_application import WebApplicationProfiles


SHA256 = re.compile(r'[a-f0-9]{64}\Z')
VERIFICATION_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def compare_site_page_draft(database: Path, run_id: str, *, profiles: Path, store: Path,
                            knowledge_sha256: str, selected_profile_sha256: str,
                            verification_id: str) -> dict:
    if (not isinstance(knowledge_sha256, str) or SHA256.fullmatch(knowledge_sha256) is None
            or not isinstance(selected_profile_sha256, str)
            or SHA256.fullmatch(selected_profile_sha256) is None
            or not isinstance(verification_id, str) or VERIFICATION_ID.fullmatch(verification_id) is None):
        raise ValueError('invalid_site_page_comparison_selection')
    page = SiteKnowledgeStore(store, WebApplicationProfiles(profiles)).get(knowledge_sha256)
    if page.profile_sha256 != selected_profile_sha256:
        raise ValueError('site_page_profile_selection_mismatch')
    evidence = review_site_page_evidence(database, run_id)
    selected = [item for item in evidence['pages'] if item['source']['verification_id'] == verification_id]
    if len(selected) != 1:
        raise ValueError('site_page_evidence_selection_not_unique')
    observed = selected[0]
    if page.page_key != observed['page_key']:
        status = 'page_key_mismatch_unbound'
    elif page.page_fingerprint_sha256 == observed['page_fingerprint_sha256']:
        status = 'hash_equal_unbound'
    else:
        status = 'hash_different_unbound'
    report = {
        'schema_version': '1.0',
        'status': status,
        'knowledge_sha256': knowledge_sha256,
        'selected_profile_sha256': selected_profile_sha256,
        'draft_page_key': page.page_key,
        'draft_revision': page.revision,
        'evidence_page_key': observed['page_key'],
        'evidence_fingerprint_version': observed['fingerprint_version'],
        'evidence_fingerprint_sha256': observed['page_fingerprint_sha256'],
        'evidence_source': observed['source'],
        'run_ref': evidence['run_ref'],
        'snapshot_sha256': evidence['snapshot_sha256'],
        'profile_bound': False,
        'origin_verified': False,
        'route_verified': False,
        'fingerprint_semantics_verified': False,
        'reviewed': False,
        'execution_authorized': False,
        'collection_authorized': False,
        'training_ready': False,
    }
    validator('site_page_comparison').validate(report)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Compare a selected draft to synthetic local-page evidence')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--knowledge-sha256', required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--verification-id', required=True)
    arguments, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.exit(2, 'Site page comparison unavailable: invalid arguments.\n')
    try:
        report = compare_site_page_draft(
            arguments.database, arguments.run_id, profiles=arguments.profiles, store=arguments.store,
            knowledge_sha256=arguments.knowledge_sha256,
            selected_profile_sha256=arguments.selected_profile_sha256,
            verification_id=arguments.verification_id,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Site page comparison unavailable: missing, invalid or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

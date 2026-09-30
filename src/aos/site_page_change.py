"""Read-only comparison of two explicitly selected synthetic page observations."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .learning_events import RUN_ID
from .site_page_comparison import VERIFICATION_ID
from .site_page_evidence import FINGERPRINT_VERSION, review_site_page_evidence


def _selected_page(report: dict, run_id: str, verification_id: str) -> dict:
    if (report.get('mode') != 'read_only_synthetic_page_evidence'
            or report.get('run_ref') != digest({'run_id': run_id})
            or report.get('profile_bound') is not False
            or report.get('execution_authorized') is not False
            or report.get('collection_authorized') is not False
            or report.get('training_ready') is not False):
        raise ValueError('invalid_page_evidence_report')
    pages = report.get('pages')
    if (not isinstance(pages, list) or not 1 <= len(pages) <= 2
            or report.get('page_count') != len(pages)
            or any(not validator('site_page_evidence').is_valid(page) for page in pages)):
        raise ValueError('invalid_page_evidence_pages')
    if (len({page['page_key'] for page in pages}) != len(pages)
            or len({page['source']['verification_id'] for page in pages}) != len(pages)):
        raise ValueError('ambiguous_page_evidence')
    selected = [page for page in pages if page['source']['verification_id'] == verification_id]
    if len(selected) != 1:
        raise ValueError('page_source_not_unique')
    return selected[0]


def compare_site_page_change(database: Path, before_run_id: str, after_run_id: str, *,
                             before_verification_id: str, after_verification_id: str) -> dict:
    if (not isinstance(before_run_id, str) or RUN_ID.fullmatch(before_run_id) is None
            or not isinstance(after_run_id, str) or RUN_ID.fullmatch(after_run_id) is None
            or before_run_id == after_run_id
            or not isinstance(before_verification_id, str)
            or VERIFICATION_ID.fullmatch(before_verification_id) is None
            or not isinstance(after_verification_id, str)
            or VERIFICATION_ID.fullmatch(after_verification_id) is None):
        raise ValueError('invalid_page_change_selection')
    before_report = review_site_page_evidence(database, before_run_id)
    after_report = review_site_page_evidence(database, after_run_id)
    before_page = _selected_page(before_report, before_run_id, before_verification_id)
    after_page = _selected_page(after_report, after_run_id, after_verification_id)
    if (before_report['snapshot_sha256'] != after_report['snapshot_sha256']
            or before_page['page_key'] != after_page['page_key']
            or before_page['fingerprint_version'] != after_page['fingerprint_version']
            or before_page['fingerprint_version'] != FINGERPRINT_VERSION):
        raise ValueError('incomparable_page_sources')
    report = {
        'schema_version': '1.0', 'mode': 'read_only_synthetic_page_change',
        'status': ('unchanged_unbound' if before_page['page_fingerprint_sha256']
                   == after_page['page_fingerprint_sha256'] else 'changed_unbound'),
        'page_key': before_page['page_key'], 'fingerprint_version': FINGERPRINT_VERSION,
        'before': {'run_ref': before_report['run_ref'],
                   'snapshot_sha256': before_report['snapshot_sha256'],
                   'page_fingerprint_sha256': before_page['page_fingerprint_sha256']},
        'after': {'run_ref': after_report['run_ref'],
                  'snapshot_sha256': after_report['snapshot_sha256'],
                  'page_fingerprint_sha256': after_page['page_fingerprint_sha256']},
        'profile_bound': False, 'reviewed': False, 'execution_authorized': False,
        'collection_authorized': False, 'training_ready': False,
    }
    if not validator('site_page_change').is_valid(report):
        raise ValueError('invalid_page_change_report')
    return report


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, message):
        raise SystemExit('Site page change unavailable: invalid arguments.')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Compare two explicit audited synthetic page sources')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--before-verification-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--after-verification-id', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = compare_site_page_change(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            before_verification_id=arguments.before_verification_id,
            after_verification_id=arguments.after_verification_id)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Site page change unavailable: missing, invalid or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

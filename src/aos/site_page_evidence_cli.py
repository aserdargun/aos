"""Exact-selection CLI for existing synthetic local-page evidence and comparison."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical
from .site_page_comparison import VERIFICATION_ID, compare_site_page_draft
from .site_page_evidence import review_site_page_evidence


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, message):
        raise SystemExit('Site page review unavailable: invalid arguments.')


def select_site_page_evidence(database: Path, run_id: str, verification_id: str) -> dict:
    if not isinstance(verification_id, str) or VERIFICATION_ID.fullmatch(verification_id) is None:
        raise ValueError('invalid_site_page_verification_selection')
    report = review_site_page_evidence(database, run_id)
    selected = [page for page in report['pages']
                if page['source']['verification_id'] == verification_id]
    if len(selected) != 1:
        raise ValueError('site_page_evidence_selection_not_unique')
    return {**report, 'page_count': 1, 'pages': selected}


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Read-only synthetic local-page review')
    commands = parser.add_subparsers(dest='command', required=True,
                                     parser_class=PrivateArgumentParser)
    inspect = commands.add_parser('inspect')
    compare = commands.add_parser('compare')
    for command in (inspect, compare):
        command.add_argument('--database', type=Path, required=True)
        command.add_argument('--run-id', required=True)
        command.add_argument('--verification-id', required=True)
    compare.add_argument('--profiles', type=Path, required=True)
    compare.add_argument('--store', type=Path, required=True)
    compare.add_argument('--knowledge-sha256', required=True)
    compare.add_argument('--selected-profile-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == 'inspect':
            report = select_site_page_evidence(
                arguments.database, arguments.run_id, arguments.verification_id)
        else:
            report = compare_site_page_draft(
                arguments.database, arguments.run_id,
                profiles=arguments.profiles, store=arguments.store,
                knowledge_sha256=arguments.knowledge_sha256,
                selected_profile_sha256=arguments.selected_profile_sha256,
                verification_id=arguments.verification_id)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Site page review unavailable: missing, invalid or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

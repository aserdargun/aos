"""Exact-review CLI for private audited manual bootstrap skill candidates."""

import argparse
from pathlib import Path
import re
import sqlite3
import sys

from .contracts import canonical


def _sha256(value):
    if re.fullmatch('[a-f0-9]{64}', value) is None:
        raise argparse.ArgumentTypeError('expected lowercase SHA-256')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    candidate_commands = ('preview', 'publish', 'read')
    review_commands = ('review-preview', 'review-accept', 'review-read', 'revoke-preview', 'revoke',
                       'recovery-preview', 'recover')
    for command in candidate_commands + review_commands:
        subparser = commands.add_parser(command, allow_abbrev=False)
        subparser.add_argument('--project-directory', required=True, type=Path)
        subparser.add_argument('--project-manifest-sha256', required=True, type=_sha256)
        subparser.add_argument('--database', required=True, type=Path)
        subparser.add_argument('--bootstrap-journal-directory', required=True, type=Path)
        subparser.add_argument('--candidate-directory', required=True, type=Path)
        if command in review_commands:
            subparser.add_argument('--review-directory', required=True, type=Path)
        if command in ('read', 'review-preview', 'review-accept'):
            subparser.add_argument('--candidate-sha256', required=True, type=_sha256)
        elif command in ('review-read', 'revoke-preview', 'revoke'):
            subparser.add_argument('--review-sha256', required=True, type=_sha256)
        elif command in ('recovery-preview', 'recover'):
            subparser.add_argument('--record-sha256', required=True, type=_sha256)
        else:
            subparser.add_argument('--intent-sha256', required=True, type=_sha256)
        if command == 'publish':
            subparser.add_argument('--confirm-candidate-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True,
                                   choices=['PUBLISH_MANUAL_CANDIDATE'])
        elif command == 'review-accept':
            subparser.add_argument('--confirm-review-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True,
                                   choices=['ACCEPT_MANUAL_REVIEW'])
        elif command == 'revoke':
            subparser.add_argument('--confirm-revocation-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True,
                                   choices=['REVOKE_MANUAL_REVIEW'])
        elif command == 'recover':
            subparser.add_argument('--confirm-recovery-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True,
                                   choices=['RESTORE_ANCHORED_REVIEW_RECORD'])
    arguments = parser.parse_args(argv)
    try:
        from .owned_parameter_skill_candidate import (
            OwnedParameterSkillCandidateSession, candidate_summary,
        )

        session = OwnedParameterSkillCandidateSession(
            arguments.project_directory, arguments.project_manifest_sha256,
            arguments.database, arguments.bootstrap_journal_directory,
            arguments.candidate_directory)
        if arguments.command == 'preview':
            candidate, checksum = session.preview(arguments.intent_sha256)
        elif arguments.command == 'publish':
            candidate, checksum = session.publish(
                arguments.intent_sha256, arguments.confirm_candidate_sha256,
                arguments.human_confirmation)
        elif arguments.command == 'read':
            candidate, checksum = session.read(arguments.candidate_sha256)
        if arguments.command in candidate_commands:
            report = candidate_summary(candidate, checksum)
        else:
            from .owned_parameter_skill_review import (
                OwnedParameterSkillReviewSession, review_summary,
            )

            reviews = OwnedParameterSkillReviewSession(session, arguments.review_directory)
            if arguments.command == 'review-preview':
                review, checksum = reviews.preview(arguments.candidate_sha256)
            elif arguments.command == 'review-accept':
                review, checksum = reviews.accept(
                    arguments.candidate_sha256, arguments.confirm_review_sha256,
                    arguments.human_confirmation)
            elif arguments.command == 'review-read':
                review, checksum = reviews.read(arguments.review_sha256)
            elif arguments.command == 'revoke-preview':
                review, checksum = reviews.revocation_preview(arguments.review_sha256)
            elif arguments.command == 'revoke':
                review, checksum = reviews.revoke(
                    arguments.review_sha256, arguments.confirm_revocation_sha256,
                    arguments.human_confirmation)
            elif arguments.command == 'recovery-preview':
                review, checksum = reviews.recovery_preview(arguments.record_sha256)
            else:
                review, checksum = reviews.recover(
                    arguments.record_sha256, arguments.confirm_recovery_sha256,
                    arguments.human_confirmation)
            report = review_summary(review, checksum)
        report['command'] = arguments.command
        print(canonical(report))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as error:
        report = {'status': 'rejected', 'error': 'owned_parameter_skill_command_rejected'}
        if (isinstance(error, ValueError)
                and str(error) == 'owned_parameter_skill_review_history_migration_required'):
            report['error'] = 'owned_parameter_skill_review_history_migration_required'
            report['migration_required'] = True
        print(canonical(report), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

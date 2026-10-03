"""Exact manual skill release, selection, and anchored missing-record restoration."""

import argparse
from pathlib import Path
import sqlite3
import sys

from .contracts import canonical
from .owned_parameter_skill_cli import _sha256


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    release_commands = ('preview', 'release')
    selection_commands = ('select-preview', 'select', 'rollback-preview', 'rollback')
    recovery_commands = ('recovery-preview', 'recover')
    for command in release_commands + ('read', 'catalog') + selection_commands + recovery_commands:
        subparser = commands.add_parser(command, allow_abbrev=False)
        subparser.add_argument('--project-directory', required=True, type=Path)
        subparser.add_argument('--project-manifest-sha256', required=True, type=_sha256)
        subparser.add_argument('--database', required=True, type=Path)
        subparser.add_argument('--bootstrap-journal-directory', required=True, type=Path)
        subparser.add_argument('--candidate-directory', required=True, type=Path)
        subparser.add_argument('--review-directory', required=True, type=Path)
        subparser.add_argument('--release-directory', required=True, type=Path)
        if command in release_commands:
            subparser.add_argument('--review-sha256', required=True, type=_sha256)
            subparser.add_argument('--expected-parent-release-sha256', type=_sha256)
        elif command in recovery_commands:
            subparser.add_argument('--record-sha256', required=True, type=_sha256)
        elif command != 'catalog':
            subparser.add_argument('--release-sha256', required=True, type=_sha256)
        if command in selection_commands:
            subparser.add_argument('--expected-selection-sha256', type=_sha256,
                                   required=command.startswith('rollback'))
        if command == 'release':
            subparser.add_argument('--confirm-release-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True,
                                   choices=['RELEASE_MANUAL_SKILL'])
        elif command in ('select', 'rollback'):
            subparser.add_argument('--confirm-selection-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True,
                                   choices=['SELECT_MANUAL_SKILL' if command == 'select'
                                            else 'ROLLBACK_MANUAL_SKILL'])
        elif command == 'recover':
            subparser.add_argument('--confirm-recovery-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True,
                                   choices=['RESTORE_ANCHORED_RELEASE_RECORD'])
    arguments = parser.parse_args(argv)
    try:
        from .owned_parameter_skill_candidate import OwnedParameterSkillCandidateSession
        from .owned_parameter_skill_release import (
            OwnedParameterSkillReleaseSession, release_summary,
        )
        from .owned_parameter_skill_review import OwnedParameterSkillReviewSession

        candidates = OwnedParameterSkillCandidateSession(
            arguments.project_directory, arguments.project_manifest_sha256,
            arguments.database, arguments.bootstrap_journal_directory,
            arguments.candidate_directory)
        reviews = OwnedParameterSkillReviewSession(candidates, arguments.review_directory)
        session = OwnedParameterSkillReleaseSession(reviews, arguments.release_directory)
        if arguments.command == 'catalog':
            report = session.inventory()
        else:
            if arguments.command == 'preview':
                record, checksum = session.preview(
                    arguments.review_sha256, arguments.expected_parent_release_sha256)
            elif arguments.command == 'release':
                record, checksum = session.release(
                    arguments.review_sha256, arguments.expected_parent_release_sha256,
                    arguments.confirm_release_sha256, arguments.human_confirmation)
            elif arguments.command == 'read':
                record, checksum = session.read(arguments.release_sha256)
            elif arguments.command == 'recovery-preview':
                record, checksum = session.recovery_preview(arguments.record_sha256)
            elif arguments.command == 'recover':
                record, checksum = session.recover(
                    arguments.record_sha256, arguments.confirm_recovery_sha256,
                    arguments.human_confirmation)
            else:
                operation = 'rollback' if arguments.command.startswith('rollback') else 'select'
                if arguments.command.endswith('-preview'):
                    record, checksum = session.selection_preview(
                        arguments.release_sha256, arguments.expected_selection_sha256, operation)
                else:
                    record, checksum = session.select(
                        arguments.release_sha256, arguments.expected_selection_sha256, operation,
                        arguments.confirm_selection_sha256, arguments.human_confirmation)
            report = release_summary(record, checksum)
        report['command'] = arguments.command
        print(canonical(report))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as error:
        report = {'status': 'rejected', 'error': 'owned_parameter_skill_release_command_rejected'}
        migration_errors = (
            'owned_parameter_skill_release_history_migration_required',
            'owned_parameter_skill_review_history_migration_required',
        )
        if isinstance(error, ValueError) and str(error) in migration_errors:
            report['error'] = str(error)
            report['migration_required'] = True
        print(canonical(report), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

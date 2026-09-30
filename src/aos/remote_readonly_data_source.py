"""Read-only provenance audit for one approved JSON bundle run."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical
from .remote_static_learning_source import inspect_remote_readonly_data_source


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Inspect one approved JSON-bundle learning source',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    arguments = parser.parse_args(argv)
    try:
        report = inspect_remote_readonly_data_source(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'JSON bundle learning source unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

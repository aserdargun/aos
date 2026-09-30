"""Explicit, run-bound JSON-bundle model metadata polling."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical
from .remote_static_learning_stream import (poll_remote_readonly_data_stream,
                                            watch_remote_readonly_data_stream)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Explicit bounded JSON-bundle metadata polling',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--consents', type=Path, required=True)
    parser.add_argument('--consent-sha256', required=True)
    parser.add_argument('--outbox-dir', type=Path, required=True)
    parser.add_argument('--watch-seconds', type=float, default=0)
    parser.add_argument('--interval-seconds', type=float, default=1)
    arguments = parser.parse_args(argv)
    try:
        report = watch_remote_readonly_data_stream(
            arguments.database, profiles=arguments.profiles,
            consents=arguments.consents, consent_sha256=arguments.consent_sha256,
            outbox_dir=arguments.outbox_dir, watch_seconds=arguments.watch_seconds,
            interval_seconds=arguments.interval_seconds)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'JSON bundle learning stream unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

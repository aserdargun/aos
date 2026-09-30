"""Explicit read-only metadata review for one private trajectory run."""

import argparse
from pathlib import Path
import sqlite3

from .contracts import canonical
from .learning_events import review_learning_events


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Review content-free learning evidence for one existing run")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    arguments, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.exit(2, "Learning event review unavailable: invalid arguments.\n")
    try:
        report = review_learning_events(arguments.database, arguments.run_id)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, "Learning event review unavailable: missing, invalid or unsafe source.\n")
    print(canonical(report))


if __name__ == "__main__":
    main()

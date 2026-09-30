"""Bounded host-side retention sweep for private remote metadata outboxes."""

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Callable

from jsonschema import ValidationError

from .contracts import canonical
from .dataset import validator
from .remote_learning_consent import RemoteLearningConsents
from .remote_learning_lifecycle import purge_revoked_remote_learning
from .lifecycle import private_directory


MANAGED_SESSION = re.compile(r'app-[a-f0-9]{32}\Z')
MAX_MANAGED_SESSIONS = 1000
RETENTION_INTERVAL_SECONDS = 3600
RETENTION_STALE_AFTER_SECONDS = 2 * RETENTION_INTERVAL_SECONDS


class RetentionTelemetry:
    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self.state = 'pending' if enabled else 'disabled'
        self.last_attempt_at = None
        self.session_count = None
        self.purged_count = None
        self.failed_session_count = 0
        self.failed_consent_count = 0
        self._last_attempt_time = None
        self._last_attempt_monotonic = None

    def record(self, report: dict | None, failure: Exception | None) -> None:
        if not self.enabled:
            raise ValueError('retention_telemetry_not_enabled')
        if failure is None and report is not None:
            try:
                validator('remote_learning_managed_retention_report').validate(report)
            except ValidationError as error:
                failure = error
        self._last_attempt_time = datetime.now(timezone.utc)
        self._last_attempt_monotonic = time.monotonic()
        self.last_attempt_at = self._last_attempt_time.isoformat()
        if failure is not None or report is None:
            self.state = 'unavailable'
            self.session_count = None
            self.purged_count = None
            self.failed_session_count = 0
            self.failed_consent_count = 0
            return
        self.session_count = report['session_count']
        self.purged_count = report['purged_count']
        self.failed_session_count = len(report['failed_sessions'])
        self.failed_consent_count = len(report['failed_consent_sha256'])
        self.state = ('incomplete' if self.failed_session_count or self.failed_consent_count
                      else 'ok')

    def snapshot(self) -> dict:
        if (self.state in {'ok', 'incomplete'} and self._last_attempt_time is not None
                and self._last_attempt_monotonic is not None
                and (time.monotonic() - self._last_attempt_monotonic >= RETENTION_STALE_AFTER_SECONDS
                     or (datetime.now(timezone.utc) - self._last_attempt_time).total_seconds()
                     >= RETENTION_STALE_AFTER_SECONDS)):
            self.state = 'stale'
        report = {'schema_version': '1.0', 'mode': 'managed_remote_metadata_retention_status',
                  'enabled': self.enabled, 'state': self.state,
                  'last_attempt_at': self.last_attempt_at,
                  'session_count': None if self.state in {'stale', 'unavailable'} else self.session_count,
                  'purged_count': None if self.state in {'stale', 'unavailable'} else self.purged_count,
                  'failed_session_count': None if self.state in {'stale', 'unavailable'} else self.failed_session_count,
                  'failed_consent_count': None if self.state in {'stale', 'unavailable'} else self.failed_consent_count,
                  'metadata_only': True, 'training_ready': False}
        validator('remote_learning_retention_status').validate(report)
        return report


def sweep_remote_learning_retention(*, consents: Path, outbox_dir: Path,
                                    now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    store = RemoteLearningConsents(consents)
    candidates = store.retention_candidates(now=current)
    failures = []
    purged_count = 0
    outbox_absent_count = 0
    for checksum, due in candidates:
        try:
            if due:
                store.expire_for_retention(checksum, now=current)
            purged, present = purge_revoked_remote_learning(
                consents=consents, consent_sha256=checksum, outbox_dir=outbox_dir)
            purged_count += int(purged)
            outbox_absent_count += int(not present)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            failures.append(checksum)
    report = {'schema_version': '1.0', 'mode': 'remote_metadata_retention_sweep',
              'candidate_count': len(candidates),
              'retention_due_count': sum(due for _checksum, due in candidates),
              'purged_count': purged_count, 'outbox_absent_count': outbox_absent_count,
              'failed_consent_sha256': failures,
              'collection_authorized': False, 'training_ready': False}
    validator('remote_learning_retention_report').validate(report)
    return report


def sweep_managed_remote_learning_retention(*, consents: Path, sessions_root: Path,
                                            now: datetime | None = None) -> dict:
    try:
        directory = private_directory(sessions_root)
    except FileNotFoundError:
        return _managed_report(0, 0, [], [])
    try:
        names = sorted(name for name in os.listdir(directory) if MANAGED_SESSION.fullmatch(name))
        if len(names) > MAX_MANAGED_SESSIONS:
            raise ValueError('remote_learning_managed_retention_session_limit')
    finally:
        os.close(directory)
    failed_sessions = []
    failed_consents = set()
    purged_count = 0
    for name in names:
        session = sessions_root / name
        try:
            owned = private_directory(session)
            os.close(owned)
            report = sweep_remote_learning_retention(
                consents=consents, outbox_dir=session / 'remote-learning-outbox', now=now)
            purged_count += report['purged_count']
            failed_consents.update(report['failed_consent_sha256'])
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            failed_sessions.append(name)
    return _managed_report(len(names), purged_count, failed_sessions,
                           sorted(failed_consents))


def _managed_report(session_count: int, purged_count: int,
                    failed_sessions: list[str], failed_consents: list[str]) -> dict:
    report = {'schema_version': '1.0', 'mode': 'managed_remote_metadata_retention_sweep',
              'session_count': session_count, 'purged_count': purged_count,
              'failed_sessions': failed_sessions,
              'failed_consent_sha256': failed_consents,
              'collection_authorized': False, 'training_ready': False}
    validator('remote_learning_managed_retention_report').validate(report)
    return report


async def run_managed_retention_loop(*, consents: Path, sessions_root: Path,
                                     interval_seconds: float = RETENTION_INTERVAL_SECONDS,
                                     on_result: Callable[[dict | None, Exception | None], None] | None = None) -> None:
    if interval_seconds <= 0:
        raise ValueError('remote_learning_retention_interval_invalid')
    while True:
        try:
            report = await asyncio.to_thread(
                sweep_managed_remote_learning_retention,
                consents=consents, sessions_root=sessions_root)
        except Exception as failure:
            if on_result is not None:
                try:
                    on_result(None, failure)
                except Exception:
                    print('Remote learning retention status callback failed.', file=sys.stderr, flush=True)
        else:
            if on_result is not None:
                try:
                    on_result(report, None)
                except Exception:
                    print('Remote learning retention status callback failed.', file=sys.stderr, flush=True)
        await asyncio.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Enforce expired private remote metadata retention')
    parser.add_argument('--consents', type=Path, required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--outbox-dir', type=Path)
    scope.add_argument('--sessions-root', type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.sessions_root is not None:
            if not arguments.sessions_root.exists():
                raise ValueError('managed_retention_root_missing')
            report = sweep_managed_remote_learning_retention(
                consents=arguments.consents, sessions_root=arguments.sessions_root)
        else:
            report = sweep_remote_learning_retention(
                consents=arguments.consents, outbox_dir=arguments.outbox_dir)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        parser.exit(1, 'Remote learning retention sweep failed; inspect private stores.\n')
    print(canonical(report))
    if report['failed_consent_sha256'] or report.get('failed_sessions'):
        parser.exit(1, 'Remote learning retention sweep incomplete; inspect exact failed stores.\n')


if __name__ == '__main__':
    main()

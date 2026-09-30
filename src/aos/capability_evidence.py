import json
import math
import os
import re
import stat
from datetime import datetime, timezone

from .contracts import REPO_ROOT


CAPABILITY_CASES = ('contracts', 'ui', 'transport', 'real_tasks', 'real_mcp',
                    'real_takeover', 'real_reuse', 'real_learning')
COUNT_KEYS = ('passed', 'skipped', 'failed', 'error', 'expected_failure',
              'unexpected_success', 'unknown')
MAX_DIRECTORIES = 256
MAX_ENTRIES = 8192
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _row(case, status='not_run', started_at=None, counts=None, seconds=None):
    return {'case': case, 'status': status, 'started_at': started_at,
            'counts': counts, 'seconds': seconds}


def _private(metadata, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (kind(metadata.st_mode) and metadata.st_uid == os.getuid()
            and metadata.st_mode & 0o077 == 0
            and (directory or metadata.st_nlink == 1))


def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
            metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _integer(value):
    return type(value) is int and 0 <= value <= 1_000_000


def _names(value):
    return (isinstance(value, list) and len(value) <= len(CAPABILITY_CASES)
            and all(isinstance(item, str) and item in CAPABILITY_CASES for item in value)
            and len(set(value)) == len(value))


def _project(report):
    if (not isinstance(report, dict) or report.get('real_site_acceptance') is not False
            or report.get('approval_driver') != 'test harness'
            or not isinstance(report.get('started_at'), str)):
        raise ValueError('report envelope')
    stamp = datetime.fromisoformat(report['started_at'])
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError('report timestamp')
    timestamp = stamp.timestamp()
    started_at = stamp.astimezone(timezone.utc).isoformat()
    entries = report.get('cases')
    if not isinstance(entries, list) or len(entries) > len(CAPABILITY_CASES):
        raise ValueError('report cases')
    rows = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('case entry')
        case = entry.get('case')
        if not isinstance(case, str) or case not in CAPABILITY_CASES or case in rows:
            raise ValueError('case name')
        status = entry.get('status')
        if status == 'infrastructure_error':
            rows[case] = _row(case, status, started_at)
            continue
        counts = entry.get('counts')
        seconds = entry.get('seconds')
        if (not isinstance(counts, dict) or set(counts) != set(COUNT_KEYS)
                or not all(_integer(value) for value in counts.values())
                or type(seconds) not in (float, int) or not math.isfinite(seconds)
                or not 0 <= seconds <= 604800
                or type(entry.get('exit_code')) is not int
                or not all(_integer(entry.get(key, 0)) for key in
                           ('runner_errors', 'runner_failures', 'runner_skips', 'tests_run'))):
            raise ValueError('case metrics')
        failures = sum(counts[key] for key in ('failed', 'error', 'unexpected_success', 'unknown'))
        runner_failed = entry.get('runner_errors', 0) or entry.get('runner_failures', 0)
        expected = ('failed' if failures or runner_failed else
                    'not_verified' if not counts['passed'] else
                    'partial' if counts['skipped'] or counts['expected_failure']
                    or entry.get('runner_skips', 0) else 'passed')
        if status != expected or (status == 'passed' and entry['exit_code'] != 0):
            raise ValueError('case outcome')
        rows[case] = _row(case, status, started_at, dict(counts), float(seconds))
    requested = report.get('requested_cases')
    unrun = report.get('unrun_cases')
    if requested is not None or unrun is not None:
        if (not _names(requested) or not _names(unrun)
                or set(rows) & set(unrun) or set(rows) | set(unrun) != set(requested)):
            raise ValueError('requested cases')
        rows.update({case: _row(case, started_at=started_at) for case in unrun})
    if not rows:
        raise ValueError('empty report')
    return timestamp, rows


def _read(directory_fd, budget):
    descriptor = os.open('report.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not _private(before) or not 0 < before.st_size <= min(MAX_REPORT_BYTES, budget):
            raise ValueError('private report')
        chunks = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b''.join(chunks)
        if (len(content) != before.st_size or _identity(before) != _identity(os.fstat(descriptor))
                or _identity(before) != _identity(os.stat('report.json', dir_fd=directory_fd,
                                                        follow_symlinks=False))):
            raise ValueError('report changed')
        report = json.loads(content, object_pairs_hook=_object)
        timestamp, rows = _project(report)
        return timestamp, rows, len(content)
    finally:
        os.close(descriptor)


def read_capability_evidence():
    result = {'schema_version': '1.0', 'historical_only': True,
              'real_site_acceptance': False, 'approval_driver': 'test harness',
              'available': True, 'cases': [_row(case) for case in CAPABILITY_CASES]}
    chosen = {}
    barrier = float('-inf')
    base_fd = None
    try:
        base_fd = os.open(REPO_ROOT / 'data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        base_stat = os.fstat(base_fd)
        if base_stat.st_uid != os.getuid() or base_stat.st_mode & 0o022:
            raise ValueError('data owner')
        names = []
        with os.scandir(base_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES:
                    raise ValueError('scan limit')
                if entry.name.startswith('capability-check-'):
                    names.append(entry.name)
                    if len(names) > MAX_DIRECTORIES:
                        raise ValueError('directory limit')
        budget = MAX_TOTAL_BYTES
        for name in sorted(names):
            descriptor = None
            fallback = float('inf')
            try:
                before = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
                fallback = before.st_mtime
                if (re.fullmatch(r'capability-check-[a-z0-9_]{8}', name) is None
                        or not _private(before, directory=True)):
                    raise ValueError('private directory')
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=base_fd)
                if _identity(before) != _identity(os.fstat(descriptor)):
                    raise ValueError('directory replaced')
                report_stat = os.stat('report.json', dir_fd=descriptor, follow_symlinks=False)
                fallback = max(fallback, report_stat.st_mtime)
                allowance = budget
                budget -= max(0, report_stat.st_size)
                if budget < 0:
                    barrier = float('inf')
                    raise ValueError('total read limit')
                timestamp, rows, _length = _read(descriptor, allowance)
                after = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
                if (not _private(after, directory=True)
                        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
                    raise ValueError('directory unlinked')
                for case, row in rows.items():
                    if case not in chosen or timestamp > chosen[case][0]:
                        chosen[case] = (timestamp, row)
                    elif timestamp == chosen[case][0] and row != chosen[case][1]:
                        chosen[case] = (timestamp, _row(case, 'unavailable'))
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                barrier = max(barrier, fallback)
                result['available'] = False
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        linked = os.stat(REPO_ROOT / 'data', follow_symlinks=False)
        if (not stat.S_ISDIR(linked.st_mode) or linked.st_uid != os.getuid()
                or linked.st_mode & 0o022
                or (base_stat.st_dev, base_stat.st_ino) != (linked.st_dev, linked.st_ino)):
            raise ValueError('data replaced')
        result['cases'] = [chosen[case][1] if case in chosen and chosen[case][0] > barrier
                           else _row(case, 'unavailable') if barrier != float('-inf')
                           else _row(case) for case in CAPABILITY_CASES]
    except FileNotFoundError:
        result['available'] = False
        result['cases'] = [_row(case, 'unavailable') for case in CAPABILITY_CASES]
    except (OSError, ValueError):
        result['available'] = False
        result['cases'] = [_row(case, 'unavailable') for case in CAPABILITY_CASES]
    finally:
        if base_fd is not None:
            os.close(base_fd)
    return result

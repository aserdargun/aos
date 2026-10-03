"""Publish only sanitized usage aggregates through a dedicated bare Git repository."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from scripts.record_usage import iso, timestamp, vector
from scripts.summarize_usage import render


REPORT = 'docs/usage_latest.json'
PRICES = 'docs/usage_prices_20261003.json'
FILES = {'README.md', REPORT, 'MANIFEST.sha256'}
START = '<!-- aos-usage:start -->\n'
END = '<!-- aos-usage:end -->'


def public_snapshot(snapshot, prices):
    if snapshot.get('scope') != 'available_AOS_Codex_session_usage_records':
        raise ValueError('Only explicitly AOS-scoped snapshots may be published')
    render(snapshot, prices)
    cutoff = timestamp(snapshot['cutoff'])
    first = timestamp(snapshot['coverage']['first_usage_event'])
    last = timestamp(snapshot['coverage']['last_usage_event'])
    if not first <= last <= cutoff:
        raise ValueError('Usage observation ordering is invalid')
    return {
        'schema_version': '1', 'cutoff': iso(cutoff),
        'scope': 'available_AOS_Codex_session_usage_records', 'status': snapshot['status'],
        'billing_verified': False, 'whole_project_total_verified': False,
        'invoice_cost': None, 'subscription': None, 'goal_counters_included': False,
        'runtime': {'availability': 'not_requested', 'historical_runtime_total': None,
                    'provider_billing_tokens': False},
        'counts': vector(snapshot['counts']),
        'coverage': {'sessions': snapshot['coverage']['sessions'],
                     'first_usage_event': iso(first), 'last_usage_event': iso(last)},
        'models': [{**{key: row[key] for key in ('provider', 'model', 'effort')},
                    'counts': vector(row['counts'])} for row in snapshot['models']],
        'days': [{'day': row['day'], 'counts': vector(row['counts'])} for row in snapshot['days']],
    }


def report_updates(readme, manifest, previous, snapshot, prices):
    report = public_snapshot(snapshot, prices)
    old = public_snapshot(previous, prices)
    if timestamp(report['cutoff']) < timestamp(old['cutoff']):
        raise ValueError('Refusing an older snapshot')
    if any(report['counts'][field] < old['counts'][field] for field in report['counts']):
        raise ValueError('Counter decrease requires manual review')
    if all(report[key] == old[key] for key in ('counts', 'models', 'days', 'coverage', 'status')):
        return {}
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError('README accounting markers are ambiguous')
    prefix, current = readme.split(START)
    _, suffix = current.split(END)
    updates = {'README.md': (prefix + START + render(report, prices) + END + suffix).encode(),
               REPORT: (json.dumps(report, indent=2, ensure_ascii=False) + '\n').encode()}
    lines, seen = [], set()
    for line in manifest.splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_./-]+)', line)
        if match is None or match[2] in seen:
            raise ValueError('Manifest is malformed')
        checksum, name = match.groups()
        seen.add(name)
        if name in updates:
            checksum = hashlib.sha256(updates[name]).hexdigest()
        lines.append(checksum + '  ' + name)
    if not set(updates) <= seen:
        raise ValueError('Publish bootstrap files before enabling hourly updates')
    updates['MANIFEST.sha256'] = ('\n'.join(lines) + '\n').encode()
    return updates


def git(repository, *arguments, data=None, index=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1')
    if index is not None:
        environment['GIT_INDEX_FILE'] = str(index)
    result = subprocess.run(['git', '--git-dir=' + str(repository), '-c', 'core.hooksPath=/dev/null',
                             '-c', 'commit.gpgsign=false', *arguments], input=data,
                            capture_output=True, env=environment, timeout=120)
    if result.returncode:
        raise ValueError('Git operation failed: ' + arguments[0])
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError('Git output exceeds publication bound')
    return result.stdout


def publish(repository, snapshot, remote, branch, price_sha256, *, perform=False):
    repository = Path(repository).absolute()
    if repository.is_symlink() or repository.resolve() != repository:
        raise ValueError('Dedicated Git path must not contain symlinks')
    metadata = repository.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError('Dedicated bare repository must be private and current-user-owned')
    if git(repository, 'rev-parse', '--is-bare-repository').strip() != b'true':
        raise ValueError('A dedicated bare repository is required; worktrees are never modified')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_/-]*', branch):
        raise ValueError('Unsupported target branch')
    git(repository, 'check-ref-format', 'refs/heads/' + branch)
    for arguments in [('remote', 'get-url', '--all', 'origin'),
                      ('remote', 'get-url', '--push', '--all', 'origin')]:
        if git(repository, *arguments).decode().splitlines() != [remote]:
            raise ValueError('Remote differs from explicitly pinned target')
    lock = os.open(repository / 'aos-usage.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        git(repository, 'fetch', '--no-tags', 'origin', 'refs/heads/' + branch)
        parent = git(repository, 'rev-parse', 'FETCH_HEAD').decode().strip()
        sources = {}
        for name in sorted(FILES | {PRICES}):
            entry = git(repository, 'ls-tree', parent, '--', name).decode()
            if not entry.startswith('100644 blob ') or not entry.endswith('\t' + name + '\n'):
                raise ValueError('Expected regular publication file is absent')
            sources[name] = git(repository, 'show', parent + ':' + name)
        if hashlib.sha256(sources[PRICES]).hexdigest() != price_sha256:
            raise ValueError('Price reference changed; explicit review is required')
        manifest = sources['MANIFEST.sha256'].decode()
        for name in ('README.md', REPORT, PRICES):
            line = hashlib.sha256(sources[name]).hexdigest() + '  ' + name
            if manifest.splitlines().count(line) != 1:
                raise ValueError('Existing report or price differs from source manifest')
        updates = report_updates(sources['README.md'].decode(), manifest,
                                 json.loads(sources[REPORT]), snapshot, json.loads(sources[PRICES]))
        if not updates:
            return {'status': 'unchanged', 'parent': parent, 'pushed': False}
        if not perform:
            return {'status': 'ready', 'parent': parent, 'files': sorted(updates), 'pushed': False}
        with tempfile.TemporaryDirectory(prefix='usage-index-', dir=repository) as temporary:
            index = Path(temporary) / 'index'
            git(repository, 'read-tree', parent, index=index)
            for name, content in sorted(updates.items()):
                blob = git(repository, 'hash-object', '-w', '--stdin', data=content).decode().strip()
                git(repository, 'update-index', '--add', '--cacheinfo', '100644,' + blob + ',' + name, index=index)
            tree = git(repository, 'write-tree', index=index).decode().strip()
        message = 'docs: refresh hourly development usage ' + iso(timestamp(snapshot['cutoff'])) + '\n'
        commit = git(repository, 'commit-tree', tree, '-p', parent, data=message.encode()).decode().strip()
        changed = set(git(repository, 'diff-tree', '--no-commit-id', '--name-only', '-r', parent, commit).decode().splitlines())
        if changed != FILES:
            raise ValueError('Publication change set differs from the three-file allowlist')
        git(repository, 'push', 'origin', commit + ':refs/heads/' + branch)
        head = git(repository, 'ls-remote', 'origin', 'refs/heads/' + branch).decode().split()[0]
        if head != commit:
            raise ValueError('Remote moved after push; readback requires inspection')
        return {'status': 'published', 'parent': parent, 'commit': commit, 'pushed': True,
                'files': sorted(changed), 'cutoff': iso(timestamp(snapshot['cutoff']))}
    finally:
        os.close(lock)


def hourly_snapshot(directory, now):
    path = Path(directory) / now.strftime('%Y-%m-%dT%H0000Z.json')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_size > 4 * 1024 * 1024):
            raise ValueError('Bounded current-user snapshot required')
        snapshot = json.loads(stream.read(4 * 1024 * 1024 + 1))
    cutoff = timestamp(snapshot['cutoff'])
    if cutoff > now or cutoff.replace(minute=0, second=0, microsecond=0) != now.replace(minute=0, second=0, microsecond=0):
        raise ValueError('Current UTC hour snapshot required; stale snapshots are not published')
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git-dir', type=Path, required=True)
    parser.add_argument('--snapshots', type=Path, required=True)
    parser.add_argument('--remote', required=True)
    parser.add_argument('--branch', default='main')
    parser.add_argument('--price-sha256', required=True)
    parser.add_argument('--publish', action='store_true')
    arguments = parser.parse_args()
    try:
        snapshot = hourly_snapshot(arguments.snapshots, datetime.now(timezone.utc))
        result = publish(arguments.git_dir, snapshot, arguments.remote, arguments.branch,
                         arguments.price_sha256, perform=arguments.publish)
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, subprocess.TimeoutExpired):
        parser.exit(1, 'Usage publication failed: inspect snapshot freshness, pinned remote/branch/prices and Git access. No force push or worktree modification was attempted.\n')


if __name__ == '__main__':
    main()

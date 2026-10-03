import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


REPO_ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
          'output_tokens', 'reasoning_output_tokens', 'total_tokens')
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
MODELS = {'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-6.1-sol', 'gpt-5.3-codex-spark'}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}
LINE_LIMIT = 16 * 1024 * 1024


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone required')
    return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def in_workspace(value, workspace):
    return isinstance(value, str) and (value == str(workspace) or value.startswith(str(workspace) + '/'))


def safe_label(value, anomalies, allowed):
    if isinstance(value, str) and value in allowed and LABEL.fullmatch(value):
        return value
    anomalies['unknown_or_unsafe_label'] += 1
    return 'unknown'


def vector(value):
    if not isinstance(value, dict) or any(type(value.get(field)) is not int or value[field] < 0 for field in FIELDS):
        raise ValueError('Complete nonnegative usage vector required')
    if (value['total_tokens'] != value['input_tokens'] + value['output_tokens']
            or value['cached_input_tokens'] > value['input_tokens']
            or value['reasoning_output_tokens'] > value['output_tokens']):
        raise ValueError('Usage subsets differ')
    return {field: value[field] for field in FIELDS}


def readonly(database):
    connection = sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    return connection


def goal_counters(database, root_ids, cutoff, anomalies):
    if database is None:
        return {'availability': 'not_requested', 'additive_with_sessions': False, 'observations': []}
    observations = []
    try:
        with closing(readonly(database)) as connection:
            for root_id in sorted(root_ids):
                for row in connection.execute('SELECT tokens_used,time_used_seconds,created_at_ms,updated_at_ms,status '
                                              'FROM thread_goals WHERE thread_id=?', (root_id,)):
                    tokens, seconds, created, updated, status_value = row
                    if (any(type(value) is not int or value < 0 for value in (tokens, seconds, created, updated))
                            or updated < created or datetime.fromtimestamp(updated / 1000, timezone.utc) > cutoff):
                        anomalies['goal_counter_outside_cutoff_or_invalid'] += 1
                        continue
                    observations.append({'tokens_used': tokens, 'time_used_seconds': seconds,
                        'time_used_hours': seconds / 3600, 'scope': 'separate_AOS_root_goal_cumulative_counter',
                        'created_at_ms': created, 'updated_at_ms': updated,
                        'status': safe_label(status_value, anomalies, {'active', 'complete', 'blocked', 'paused'})})
        return {'availability': 'observed' if observations else 'unavailable_at_cutoff',
                'additive_with_sessions': False, 'observations': observations}
    except (OSError, sqlite3.Error):
        anomalies['goal_database_unavailable'] += 1
        return {'availability': 'unavailable', 'additive_with_sessions': False, 'observations': []}


def runtime_observation(database):
    if database is None:
        return {'availability': 'not_requested', 'scope': 'current_default_runtime_only',
                'provider_billing_tokens': False, 'historical_runtime_total': None}
    try:
        with closing(readonly(database)) as connection:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(model_calls)')}
            required = {'input_tokens', 'output_tokens', 'created_at', 'role'}
            if not required <= columns:
                raise ValueError('Unsupported runtime usage schema')
            rows = connection.execute('SELECT role,count(*),sum(input_tokens),sum(output_tokens),'
                                      'count(input_tokens),count(output_tokens) FROM model_calls GROUP BY role').fetchall()
            if any(row[0] not in {'system1', 'system2'} for row in rows):
                raise ValueError('Unsupported runtime role')
        return {'availability': 'observed', 'scope': 'current_default_runtime_only',
            'provider_billing_tokens': False, 'historical_runtime_total': None,
            'calls': sum(row[1] for row in rows),
            'roles': [dict(zip(('role', 'calls', 'input_tokens', 'output_tokens',
                               'input_observed_calls', 'output_observed_calls'), row)) for row in rows]}
    except (OSError, sqlite3.Error, ValueError):
        return {'availability': 'unavailable', 'scope': 'current_default_runtime_only',
                'provider_billing_tokens': False, 'historical_runtime_total': None}


def collect_usage(session_root, workspace, cutoff, *, goal_db=None, goal_thread_id=None, runtime_db=None):
    cutoff = timestamp(cutoff) if isinstance(cutoff, str) else timestamp(iso(cutoff))
    workspace = Path(workspace).absolute()
    totals = {field: 0 for field in FIELDS}
    groups = defaultdict(lambda: {field: 0 for field in FIELDS})
    anomalies, audit = Counter(), Counter()
    session_ids, root_ids, seen = set(), set(), set()
    starts, event_times = [], []
    try:
        files = sorted(Path(session_root).rglob('*.jsonl'))
        if not Path(session_root).is_dir():
            raise OSError('Session directory unavailable')
    except OSError:
        files = []
        anomalies['session_directory_unavailable'] += 1
    for path in files:
        owner = None
        model, effort, context_cwd = 'unknown', 'unknown', None
        previous = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError('Not a regular session file')
                remaining = metadata.st_size
                while remaining:
                    line = stream.readline(min(remaining, LINE_LIMIT + 1))
                    remaining -= len(line)
                    if not line:
                        anomalies['source_truncated_during_read'] += 1
                        break
                    if len(line) > LINE_LIMIT or not line.endswith(b'\n'):
                        anomalies['oversized_or_partial_record'] += 1
                        break
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError('Record must be an object')
                        kind, payload = record.get('type'), record.get('payload')
                        if kind not in {'session_meta', 'turn_context', 'event_msg'}:
                            continue
                        if not isinstance(payload, dict):
                            raise ValueError('Payload must be an object')
                        if kind == 'event_msg' and payload.get('type') != 'token_count':
                            continue
                        if owner is None:
                            if kind != 'session_meta':
                                continue
                            if not in_workspace(payload.get('cwd'), workspace):
                                break
                            start = timestamp(payload.get('timestamp'))
                            if start > cutoff:
                                break
                            if not isinstance(payload.get('id'), str) or not payload['id']:
                                raise ValueError('Session identity required internally')
                            owner = payload
                            session_ids.add(owner['id'])
                            starts.append(iso(start))
                            audit['scoped_files'] += 1
                            source = payload.get('source')
                            parent = payload.get('parent_thread_id') or payload.get('forked_from_id')
                            if not parent and not isinstance(source, dict):
                                root_ids.add(owner['id'])
                            provider = safe_label(owner.get('model_provider'), anomalies, {'openai'})
                            continue
                        if kind == 'session_meta':
                            audit['inherited_metadata_ignored'] += 1
                            continue
                        instant = timestamp(record.get('timestamp'))
                        if instant < start:
                            audit['inherited_records_excluded'] += 1
                            continue
                        if instant > cutoff:
                            audit['after_cutoff_records_excluded'] += 1
                            continue
                        if kind == 'turn_context':
                            model = safe_label(payload.get('model'), anomalies, MODELS)
                            effort = safe_label(payload.get('effort'), anomalies, EFFORTS)
                            context_cwd = payload.get('cwd')
                            continue
                        info = payload.get('info')
                        if info is None:
                            audit['usage_notification_without_info'] += 1
                            continue
                        if not isinstance(info, dict):
                            raise ValueError('Usage info invalid')
                        cumulative = vector(info.get('total_token_usage'))
                        if previous == cumulative:
                            audit['unchanged_notifications_excluded'] += 1
                            continue
                        if previous is None and cumulative['total_tokens'] == 0:
                            previous = cumulative
                            audit['zero_cumulative_notifications_excluded'] += 1
                            continue
                        latest = vector(info.get('last_token_usage'))
                        if any(cumulative[field] < latest[field] for field in FIELDS):
                            raise ValueError('Usage baseline invalid')
                        if context_cwd is not None and not in_workspace(context_cwd, workspace):
                            previous = cumulative
                            anomalies['changed_workspace_usage_omitted'] += 1
                            continue
                        event_key = (owner['id'], iso(instant), tuple(cumulative.values()), tuple(latest.values()))
                        if event_key in seen:
                            previous = cumulative
                            audit['duplicate_records_excluded'] += 1
                            continue
                        if previous is None:
                            if cumulative != latest:
                                audit['initial_nonzero_baselines_excluded'] += 1
                            increment = latest
                        elif any(cumulative[field] < previous[field] for field in FIELDS):
                            previous = cumulative
                            anomalies['counter_reset_or_regression_omitted'] += 1
                            continue
                        else:
                            increment = {field: cumulative[field] - previous[field] for field in FIELDS}
                            if increment != latest:
                                previous = cumulative
                                anomalies['unattributable_counter_gap_omitted'] += 1
                                continue
                        previous = cumulative
                        if model == 'unknown' or context_cwd is None:
                            anomalies['usage_without_model_context'] += 1
                        vector(increment)
                        seen.add(event_key)
                        for field in FIELDS:
                            totals[field] += increment[field]
                        day, hour = instant.strftime('%Y-%m-%d'), instant.strftime('%Y-%m-%dT%H:00:00Z')
                        for key in (('model', provider, model, effort), ('day', day), ('hour', hour),
                                    ('model_hour', provider, model, effort, hour)):
                            for field in FIELDS:
                                groups[key][field] += increment[field]
                        event_times.append(iso(instant))
                        audit['counted_usage_events'] += 1
                    except (ValueError, TypeError, KeyError, OverflowError):
                        anomalies['malformed_or_unsupported_record'] += 1
                after = os.fstat(stream.fileno())
                if (after.st_ino, after.st_dev) != (metadata.st_ino, metadata.st_dev) or after.st_size < metadata.st_size:
                    anomalies['source_changed_during_read'] += 1
        except OSError:
            anomalies['source_unreadable'] += 1
    if not event_times:
        anomalies['no_scoped_usage_observed'] += 1
    if goal_thread_id is not None:
        root_ids &= {goal_thread_id}
    goals = goal_counters(goal_db, root_ids, cutoff, anomalies)
    def rows(kind, labels):
        return [dict(zip(labels, key[1:]), counts=value)
                for key, value in sorted(groups.items()) if key[0] == kind]
    return {'schema_version': '1', 'observed_at': iso(datetime.now(timezone.utc)), 'cutoff': iso(cutoff),
        'scope': 'available_AOS_Codex_session_usage_records', 'status': 'partial' if anomalies else 'observed',
        'billing_verified': False, 'whole_project_total_verified': False, 'invoice_cost': None,
        'subscription': None, 'counts': totals,
        'coverage': {'sessions': len(session_ids), 'first_session_start': min(starts) if starts else None,
                     'first_usage_event': min(event_times) if event_times else None,
                     'last_usage_event': max(event_times) if event_times else None,
                     'utc_hour_buckets': sum(key[0] == 'hour' for key in groups)},
        'models': rows('model', ('provider', 'model', 'effort')), 'days': rows('day', ('day',)),
        'hours': rows('hour', ('hour',)), 'model_hours': rows('model_hour', ('provider', 'model', 'effort', 'hour')),
        'source_audit': {'source': 'allowlisted_local_session_metadata_and_usage_records',
                         'per_file_end_offset_captured': True, 'global_atomic_snapshot': False,
                         'audit': dict(audit), 'anomalies': dict(anomalies)},
        'goal_counters': goals, 'runtime': runtime_observation(runtime_db),
        'limits': ['Input includes cached input; output includes reasoning output. Do not add subsets.',
                   'Counters are locally recorded token units, not invoices or billed costs.',
                   'Goal counters are separate and must not be added to session counts.',
                   'Missing records and omitted reset/gap intervals have unknown usage.',
                   'Hourly bins use UTC usage-event timestamps, not active work hours.',
                   'Local GPU inference tokens are not provider billing tokens.']}


def public_report(snapshot):
    lines = ['# AOS development usage accounting', '',
        'Only sanitized aggregates are published. Private snapshots remain under ignored `data/accounting/`.', '',
        f"Observed: {snapshot['observed_at']} · fixed cutoff: {snapshot['cutoff']} · status: {snapshot['status']}", '',
        f"Scope: {snapshot['coverage']['sessions']} available AOS Codex sessions; recorded tokens: {snapshot['counts']['total_tokens']:,}.",
        'These are recorded token units, not billing. Whole-project totals, subscription and invoice cost are unavailable.', '',
        '| Provider | Model | Effort | Input | Cached input (subset) | Output | Reasoning (subset) | Total |',
        '|---|---|---|---:|---:|---:|---:|---:|']
    for row in snapshot['models']:
        counts = row['counts']
        lines.append('| ' + ' | '.join([row['provider'], row['model'], row['effort'],
            *[str(counts[field]) for field in ('input_tokens', 'cached_input_tokens', 'output_tokens',
                                             'reasoning_output_tokens', 'total_tokens')]]) + ' |')
    lines.extend(['', '| UTC day | Recorded tokens |', '|---|---:|'])
    for row in snapshot['days']:
        lines.append(f"| {row['day']} | {row['counts']['total_tokens']} |")
    lines.extend(['', 'UTC hourly and provider/model/hour vectors are retained in each private snapshot.', '',
        'Source audit: `' + json.dumps(snapshot['source_audit'], sort_keys=True) + '`', '',
        'Separate goal counters: `' + json.dumps(snapshot['goal_counters'], sort_keys=True) + '`', '',
        'Current default local runtime (never provider billing): `' + json.dumps(snapshot['runtime'], sort_keys=True) + '`', '',
        *['- ' + limit for limit in snapshot['limits']], '',
        'Run `python scripts/record_usage.py` once per hour. Repeated runs in the same UTC hour preserve the existing snapshot.',
        'Add `--goal-db PATH` or `--runtime-db PATH` only for the intended read-only stores.',
        'Public reporting requires explicit `--public-report docs/USAGE_ACCOUNTING.md`; no Git command, timer installation, model call or service mutation occurs.', ''])
    return '\n'.join(lines)


def write_private_snapshot(snapshot):
    directory = REPO_ROOT / 'data/accounting'
    directory.mkdir(mode=0o700, exist_ok=True)
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700 or directory.resolve() != directory):
        raise ValueError('Accounting requires the owned private directory')
    filename = timestamp(snapshot['cutoff']).strftime('%Y-%m-%dT%H0000Z.json')
    descriptor = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(snapshot, stream, ensure_ascii=True, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return filename


def main():
    parser = argparse.ArgumentParser(description='Record safe AOS usage aggregates; never bill, infer or commit')
    parser.add_argument('--session-root', type=Path, default=Path.home() / '.codex/sessions')
    parser.add_argument('--cutoff', default=iso(datetime.now(timezone.utc)))
    parser.add_argument('--goal-db', type=Path)
    parser.add_argument('--goal-thread-id')
    parser.add_argument('--runtime-db', type=Path)
    parser.add_argument('--public-report', type=Path)
    arguments = parser.parse_args()
    try:
        cutoff = timestamp(arguments.cutoff)
        if cutoff > datetime.now(timezone.utc):
            raise ValueError('Future cutoff denied')
        report_path = None
        if arguments.public_report is not None:
            report_path = arguments.public_report.absolute()
            if (report_path.parent != REPO_ROOT / 'docs' or report_path.suffix != '.md'
                    or report_path.is_symlink() or report_path.parent.resolve() != report_path.parent):
                raise ValueError('Public report must be an explicit repository docs Markdown file')
        snapshot = collect_usage(arguments.session_root, REPO_ROOT, cutoff, goal_db=arguments.goal_db,
            goal_thread_id=arguments.goal_thread_id, runtime_db=arguments.runtime_db)
        filename = write_private_snapshot(snapshot)
        if report_path is not None:
            report_path.write_text(public_report(snapshot))
        print(json.dumps({'status': snapshot['status'], 'snapshot': filename, 'cutoff': snapshot['cutoff'],
                          'recorded_total_tokens': snapshot['counts']['total_tokens'], 'billing_verified': False}))
    except FileExistsError:
        print(json.dumps({'status': 'already_recorded', 'billing_verified': False}))
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, 'Safe usage recording unavailable; no sensitive source content is reported.\n')


if __name__ == '__main__':
    main()

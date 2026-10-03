from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import record_usage


class RecordUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = self.root / 'workspace'
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.cutoff = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)

    def usage(self, total, latest=None, timestamp='2026-10-03T10:10:00Z'):
        def fields(amount):
            return dict(input_tokens=amount * 10, cached_input_tokens=amount * 8,
                        cache_write_input_tokens=0, output_tokens=amount * 2,
                        reasoning_output_tokens=amount, total_tokens=amount * 12)
        return {'type': 'event_msg', 'timestamp': timestamp,
                'payload': {'type': 'token_count', 'info': {
                    'total_token_usage': fields(total), 'last_token_usage': fields(total if latest is None else latest)}}}

    def metadata(self, session='synthetic-session', **extra):
        return {'type': 'session_meta', 'payload': dict(id=session, cwd=str(self.workspace),
            timestamp='2026-10-03T10:00:00Z', model_provider='openai', source='cli', **extra)}

    def context(self, model='gpt-6.1-sol', timestamp='2026-10-03T10:00:01Z', **extra):
        return {'type': 'turn_context', 'timestamp': timestamp,
                'payload': dict(model=model, effort='medium', cwd=str(self.workspace), **extra)}

    def write(self, records, name='session.jsonl'):
        path = self.sessions / name
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        return path

    def collect(self, **kwargs):
        return record_usage.collect_usage(self.sessions, self.workspace, self.cutoff, **kwargs)

    def test_monotonic_usage_model_change_and_fixed_utc_hour_cutoff(self):
        self.write([self.metadata(), self.context(), self.usage(1),
            self.context('gpt-6-astra', timestamp='2026-10-03T11:00:00Z'),
            self.usage(3, 2, '2026-10-03T11:10:00Z'), self.usage(4, 1, '2026-10-03T12:01:00Z')])
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 36)
        self.assertEqual([row['counts']['total_tokens'] for row in result['hours']], [12, 24])
        self.assertEqual({row['model']: row['counts']['total_tokens'] for row in result['models']},
                         {'gpt-6.1-sol': 12, 'gpt-6-astra': 24})
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['source_audit']['audit']['after_cutoff_records_excluded'], 1)

    def test_duplicate_copied_fork_events_and_initial_baseline_are_not_added(self):
        original = self.usage(1)
        self.write([self.metadata(), self.context(), original, original,
                    self.usage(1, 1, '2026-10-03T10:11:00Z')])
        child = self.metadata('synthetic-child', parent_thread_id='synthetic-session')
        child['payload']['timestamp'] = '2026-10-03T10:15:00Z'
        self.write([child,
                    self.context(), original, self.usage(11, 1, '2026-10-03T10:20:00Z')], 'fork.jsonl')
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 24)
        self.assertEqual(result['source_audit']['audit']['unchanged_notifications_excluded'], 2)
        self.assertEqual(result['source_audit']['audit']['initial_nonzero_baselines_excluded'], 1)

    def test_first_inherited_baseline_is_excluded_and_older_records_are_ignored(self):
        self.write([self.metadata(parent_thread_id='synthetic-parent'), self.context(),
            self.usage(100, 1, '2026-10-03T09:00:00Z'), self.usage(101, 1)])
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 12)
        self.assertEqual(result['source_audit']['audit']['inherited_records_excluded'], 1)
        self.assertEqual(result['source_audit']['audit']['initial_nonzero_baselines_excluded'], 1)

    def test_independent_equal_events_are_not_deduplicated(self):
        for session in ('synthetic-first', 'synthetic-second'):
            self.write([self.metadata(session), self.context(), self.usage(1)], session + '.jsonl')
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 24)
        self.assertEqual(result['status'], 'observed')

    def test_overlapping_same_owner_copy_advances_duplicate_baseline(self):
        self.write([self.metadata(), self.context(), self.usage(1),
                    self.usage(2, 1, '2026-10-03T10:20:00Z')], 'a.jsonl')
        self.write([self.metadata(), self.context(), self.usage(1),
                    self.usage(2, 1, '2026-10-03T10:20:00Z'),
                    self.usage(3, 1, '2026-10-03T10:30:00Z')], 'b.jsonl')
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 36)
        self.assertEqual(result['source_audit']['audit']['duplicate_records_excluded'], 2)
        self.assertEqual(result['status'], 'observed')

    def test_unauthorized_equal_event_does_not_suppress_authorized_copy(self):
        outside_context = self.context()
        outside_context['payload']['cwd'] = '/synthetic-outside'
        self.write([self.metadata(), outside_context, self.usage(1)], 'a.jsonl')
        self.write([self.metadata(), self.context(), self.usage(1)], 'b.jsonl')
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 12)
        self.assertEqual(result['source_audit']['anomalies']['changed_workspace_usage_omitted'], 1)

    def test_counter_reset_is_omitted_not_reinterpreted_as_new_usage(self):
        self.write([self.metadata(), self.context(), self.usage(5),
                    self.usage(1, 1, '2026-10-03T10:20:00Z'),
                    self.usage(2, 1, '2026-10-03T10:30:00Z')])
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 72)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['source_audit']['anomalies']['counter_reset_or_regression_omitted'], 1)

    def test_unattributable_gap_is_omitted(self):
        self.write([self.metadata(), self.context(), self.usage(1),
                    self.usage(10, 1, '2026-10-03T10:20:00Z')])
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 12)
        self.assertEqual(result['source_audit']['anomalies']['unattributable_counter_gap_omitted'], 1)

    def test_reset_to_zero_is_partial_even_when_new_counter_is_empty(self):
        self.write([self.metadata(), self.context(), self.usage(1),
                    self.usage(0, 0, '2026-10-03T10:20:00Z')])
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 12)
        self.assertEqual(result['source_audit']['anomalies']['counter_reset_or_regression_omitted'], 1)

    def test_unchanged_cumulative_context_notification_does_not_add_context_tokens(self):
        context_notice = self.usage(1, 1, '2026-10-03T10:20:00Z')
        context_notice['payload']['info']['last_token_usage'] = dict(input_tokens=0, cached_input_tokens=0,
            cache_write_input_tokens=0, output_tokens=0, reasoning_output_tokens=0, total_tokens=19132)
        self.write([self.metadata(), self.context(), self.usage(1), context_notice])
        result = self.collect()
        self.assertEqual(result['counts']['total_tokens'], 12)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['source_audit']['audit']['unchanged_notifications_excluded'], 1)

    def test_invalid_subsets_missing_fields_and_partial_log_are_unknown(self):
        for changed in ({'cached_input_tokens': 99}, {'reasoning_output_tokens': 99},
                        {'input_tokens': True}, {'total_tokens': 999}):
            usage = self.usage(1)
            usage['payload']['info']['total_token_usage'].update(changed)
            self.write([self.metadata(), self.context(), usage])
            result = self.collect()
            self.assertEqual(result['counts']['total_tokens'], 0)
            self.assertEqual(result['status'], 'partial')
        usage = self.usage(1)
        del usage['payload']['info']['last_token_usage']['cached_input_tokens']
        path = self.write([self.metadata(), self.context(), usage])
        with path.open('a') as stream:
            stream.write('{"SENSITIVE_SECRET":')
        result = self.collect()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['source_audit']['anomalies']['oversized_or_partial_record'], 1)

    def test_unknown_labels_and_raw_metadata_messages_paths_never_leak(self):
        secret = 'SENSITIVE_SECRET'
        self.write([self.metadata(base_instructions=secret, creator_account_id=secret),
            self.context(model=secret, summary=secret),
            {'type': 'response_item', 'payload': {'message': secret}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': secret}}, self.usage(1)])
        result = self.collect()
        encoded = json.dumps(result) + record_usage.public_report(result)
        self.assertNotIn(secret, encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn('synthetic-session', encoded)
        self.assertEqual(result['models'][0]['model'], 'unknown')
        self.assertEqual(result['status'], 'partial')

    def test_unreadable_source_and_missing_directory_mark_partial(self):
        self.write([self.metadata(), self.context(), self.usage(1)])
        with patch.object(record_usage.os, 'open', side_effect=PermissionError):
            result = self.collect()
        self.assertEqual(result['source_audit']['anomalies']['source_unreadable'], 1)
        result = record_usage.collect_usage(self.root / 'missing', self.workspace, self.cutoff)
        self.assertEqual(result['status'], 'partial')

    def test_goal_counter_is_separate_and_contains_no_ids_or_objective(self):
        self.write([self.metadata(), self.context(), self.usage(1)])
        database = self.root / 'goals.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE thread_goals(thread_id,tokens_used,time_used_seconds,created_at_ms,updated_at_ms,status,objective)')
            connection.execute('INSERT INTO thread_goals VALUES(?,?,?,?,?,?,?)',
                ('synthetic-session', 999, 3661, 1, 2, 'active', 'SENSITIVE_SECRET'))
            connection.commit()
        result = self.collect(goal_db=database)
        self.assertEqual(result['counts']['total_tokens'], 12)
        self.assertEqual(result['goal_counters']['observations'][0]['tokens_used'], 999)
        self.assertEqual(result['goal_counters']['observations'][0]['time_used_hours'], 3661 / 3600)
        self.assertFalse(result['goal_counters']['additive_with_sessions'])
        self.assertNotIn('SENSITIVE_SECRET', json.dumps(result))

    def test_private_hour_snapshot_permissions_and_no_overwrite(self):
        self.write([self.metadata(), self.context(), self.usage(1)])
        (self.root / 'data').mkdir()
        with patch.object(record_usage, 'REPO_ROOT', self.root):
            filename = record_usage.write_private_snapshot(self.collect())
            with self.assertRaises(FileExistsError):
                record_usage.write_private_snapshot(self.collect())
        directory = self.root / 'data/accounting'
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / filename).stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()

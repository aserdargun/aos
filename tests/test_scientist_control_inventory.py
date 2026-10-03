from contextlib import closing
import sqlite3
from types import SimpleNamespace
import unittest

from aos.scientist_inventory import scientist_control_inventory


class ScientistControlInventoryTests(unittest.TestCase):
    def test_counts_are_readonly_redacted_and_do_not_resolve_or_grant_authority(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.executescript('''
                CREATE TABLE scientist_evidence_controls(control_id TEXT,session_id TEXT,request_json TEXT);
                CREATE TABLE scientist_evidence_responses(control_id TEXT,response_json TEXT);
                INSERT INTO scientist_evidence_controls VALUES('first','current','synthetic-private-request');
                INSERT INTO scientist_evidence_controls VALUES('second','other','synthetic-private-request');
                INSERT INTO scientist_evidence_controls VALUES('third','current','synthetic-private-request');
                INSERT INTO scientist_evidence_responses VALUES('third','synthetic-private-response');
            ''')
            before = connection.total_changes
            connection.execute('PRAGMA query_only=ON')
            value = scientist_control_inventory(SimpleNamespace(connection=connection), 'current')
            self.assertEqual(value, {'available': True, 'supported': True, 'pending_count': 2,
                'current_session_pending_count': 1, 'other_session_pending_count': 1, 'metadata_only': True})
            self.assertNotIn('synthetic-private', str(value))
            self.assertNotIn('first', str(value))
            self.assertEqual(connection.total_changes, before)

    def test_legacy_absence_is_not_a_zero_count_or_automatic_upgrade(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.execute('PRAGMA query_only=ON')
            before = connection.total_changes
            value = scientist_control_inventory(SimpleNamespace(connection=connection), 'current')
            self.assertTrue(value['available'])
            self.assertFalse(value['supported'])
            self.assertIsNone(value['pending_count'])
            self.assertEqual(connection.total_changes, before)
            self.assertEqual(connection.execute('SELECT count(*) FROM sqlite_master').fetchone()[0], 0)

    def test_incomplete_or_malformed_tables_report_unavailable_not_safe_empty(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.execute('CREATE TABLE scientist_evidence_controls(malformed TEXT)')
            store = SimpleNamespace(connection=connection)
            self.assertFalse(scientist_control_inventory(store, 'current')['available'])
            connection.execute('CREATE TABLE scientist_evidence_responses(malformed TEXT)')
            before = connection.total_changes
            value = scientist_control_inventory(store, 'current')
            self.assertFalse(value['available'])
            self.assertFalse(value['supported'])
            self.assertIsNone(value['pending_count'])
            self.assertEqual(connection.total_changes, before)

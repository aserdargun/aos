"""Explicit report retention using owned synthetic CPU HTTP and SQLite fixtures."""

import asyncio
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import jsonschema

from aos.scientist_lab import ScientistLabClient
from aos.scientist_lab_readbacks import ScientistLabReadback
from aos.scientist_lab_service import ScientistLabService
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore

import test_scientist_lab_start_readback as start_fixtures


class ScientistLabReadbackTests(unittest.TestCase):
    def setUp(self):
        start_fixtures.ScientistLabStartServiceReadbackTests.setUp(self)
        self.service.execute(self.action_id)
        self.run_id = self.store.connection.execute('SELECT run_id FROM scientist_lab_jobs').fetchone()[0]
        self.fixture.server.state = 'completed'
        self.fixture.server.requests.clear()
        self.original = self.original_rows()

    def original_rows(self):
        return [[tuple(row) for row in self.store.connection.execute('SELECT * FROM ' + table)]
                for table in ('scientist_lab_jobs', 'scientist_lab_actions')]

    def count(self):
        return self.store.connection.execute('SELECT count(*) FROM scientist_lab_readbacks').fetchone()[0]

    def save(self, expected=None):
        return asyncio.run(self.service.save_report_async(self.run_id,
            expected_report_sha256=expected or self.fixture.server.report()['report_sha256']))

    def history(self):
        return self.service.inventory()['jobs'][0]['readbacks']

    def test_schema_and_explicit_http_save_have_exact_record_and_metadata_only_inventory(self):
        result = asyncio.run(self.service.read_async(self.run_id, 'lab.report'))
        self.assertEqual(self.count(), 0)
        self.fixture.server.requests.clear()
        saved = self.save(result['report_sha256'])
        self.assertEqual([entry[1] for entry in self.fixture.server.requests],
                         ['/v1/runs/' + self.fixture.server.run_id,
                          '/v1/runs/' + self.fixture.server.run_id + '/report',
                          '/v1/runs/' + self.fixture.server.run_id])
        loaded = self.service.read_saved_report(self.run_id, saved['readback_id'])
        self.assertEqual(loaded['record']['report'], result)
        self.assertEqual(loaded['record']['retention_source'], 'explicit_console_request')
        self.assertEqual(digest(loaded['record']), saved['record_sha256'])
        self.assertEqual(digest(result), saved['result_sha256'])
        self.assertFalse(saved['gpu_release_verified'])
        self.assertTrue(saved['historical'])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(self.history(), {'supported': True, 'available': True, 'items': [saved], 'truncated': False})
        self.assertNotIn('report', saved)
        schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/scientist_lab_readback.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(loaded['record'], schema)
        schema.pop('$schema')
        self.assertEqual(schema, ScientistLabReadback.model_json_schema())
        self.assertEqual(self.original_rows(), self.original)

    def test_duplicate_requires_fresh_readback_returns_first_immutable_record(self):
        first = self.save()
        self.fixture.server.requests.clear()
        self.assertEqual(self.save(), first)
        self.assertEqual(len(self.fixture.server.requests), 3)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.original_rows(), self.original)

    def test_reopen_same_session_reads_offline_only_with_fresh_permission(self):
        saved = self.save()
        self.store.close()
        self.store.lock = None
        self.store = TrajectoryStore(self.path)
        self.addCleanup(self.store.close)
        self.controller.store = self.store
        client = ScientistLabClient(self.fixture.server.url, self.fixture.token, principal_id='synthetic-aos',
                                    allowed_suites=self.fixture.client.allowed_suites)
        self.service = ScientistLabService(self.controller, client, authorization_context_sha256='a' * 64,
                                           program_version='director.v1', verify_capability=self.authority)
        self.fixture.server.requests.clear()
        with self.assertRaises((ScientistAdmissionError, ValueError)):
            self.service.read_saved_report(self.run_id, saved['readback_id'])
        self.controller.control('take-control')
        self.assertEqual(self.service.read_saved_report(self.run_id, saved['readback_id'])['record_sha256'], saved['record_sha256'])
        self.assertEqual(self.fixture.server.requests, [])
        self.authority.side_effect = ScientistAdmissionError('Synthetic source rights revoked')
        with self.assertRaises(ScientistAdmissionError):
            self.service.read_saved_report(self.run_id, saved['readback_id'])
        self.assertEqual(self.fixture.server.requests, [])
        self.assertEqual(self.count(), 1)

    def test_wrong_expected_hash_and_malformed_hash_never_retain(self):
        with self.assertRaises(ScientistAdmissionError): self.save('f' * 64)
        self.assertEqual(len(self.fixture.server.requests), 3)
        self.fixture.server.requests.clear()
        for value in ('', True, 'bad', 'A' * 64):
            with self.subTest(value=value), self.assertRaises(ScientistAdmissionError):
                asyncio.run(self.service.save_report_async(self.run_id, expected_report_sha256=value))
        self.assertEqual(self.fixture.server.requests, [])
        self.assertEqual(self.count(), 0)

    def test_foreign_remote_and_corrupt_report_hash_cannot_be_saved(self):
        for mode in ('foreign_run', 'bad_report_hash', 'status_drift'):
            self.fixture.server.state = 'completed'
            self.fixture.server.mode = mode
            with self.subTest(mode=mode), self.assertRaises((ScientistAdmissionError, ValueError)): self.save()
            self.assertEqual(self.count(), 0)
        self.assertEqual(self.original_rows(), self.original)

    def test_source_revocation_after_http_before_insert_rolls_back(self):
        def revoke_in_transaction(*_arguments):
            if self.store.connection.in_transaction:
                raise ScientistAdmissionError('Synthetic retention rights revoked')
        self.authority.side_effect = revoke_in_transaction
        with self.assertRaises(ScientistAdmissionError): self.save()
        self.assertEqual(len(self.fixture.server.requests), 3)
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.original_rows(), self.original)

    def test_post_insert_revocation_rolls_back_new_record(self):
        def revoke_after_insert(*_arguments):
            if self.count():
                raise ScientistAdmissionError('Synthetic post-insert revocation')
        self.authority.side_effect = revoke_after_insert
        with self.assertRaises(ScientistAdmissionError): self.save()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.original_rows(), self.original)

    def test_frozen_pre_await_binding_cannot_adopt_new_controller_fence(self):
        original_execute = self.service._execute_async
        async def drift(task, action):
            result = await original_execute(task, action)
            with self.store.connection:
                self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1 WHERE session_id=?',
                                              (self.controller.session_id,))
            return result
        with patch.object(self.service, '_execute_async', side_effect=drift):
            with self.assertRaises(ScientistAdmissionError): self.save()
        self.assertEqual(len(self.fixture.server.requests), 3)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.original_rows(), self.original)

    def test_foreign_session_and_failed_cleanup_are_not_history_authority_or_recovery(self):
        saved = self.save()
        original_session = self.controller.session_id
        self.controller.session_id = 'foreign-synthetic-session'
        with self.assertRaises(ScientistAdmissionError):
            self.service.read_saved_report(self.run_id, saved['readback_id'])
        self.controller.session_id = original_session
        self.service._closing = True
        before = list(self.fixture.server.requests)
        with self.assertRaises(ScientistAdmissionError): self.save()
        with self.assertRaises(ScientistAdmissionError):
            self.service.read_saved_report(self.run_id, saved['readback_id'])
        self.assertEqual(self.fixture.server.requests, before)
        self.assertEqual(self.original_rows(), self.original)

    def test_append_only_guards_and_corrupt_record_checksum_fail_closed(self):
        saved = self.save()
        for sql in ('UPDATE scientist_lab_readbacks SET created_at=created_at', 'DELETE FROM scientist_lab_readbacks'):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.connection: self.store.connection.execute(sql)
        trigger = self.store.connection.execute("SELECT sql FROM sqlite_master WHERE name='scientist_lab_readback_no_update'").fetchone()[0]
        with self.store.connection:
            self.store.connection.execute('DROP TRIGGER scientist_lab_readback_no_update')
            self.store.connection.execute("UPDATE scientist_lab_readbacks SET record_sha256=?", ('f' * 64,))
            self.store.connection.execute(trigger)
        with self.assertRaises(ScientistAdmissionError):
            self.service.read_saved_report(self.run_id, saved['readback_id'])
        self.assertEqual(self.history(), {'supported': True, 'available': False, 'items': [], 'truncated': False})

    def test_replace_cannot_overwrite_by_identifier_or_unique_report_body(self):
        self.save()
        original = dict(self.store.connection.execute('SELECT * FROM scientist_lab_readbacks').fetchone())
        for replace_id in (False, True):
            values = dict(original)
            if replace_id:
                values['readback_id'] = 'lab-readback-' + 'e' * 32
                body = json.loads(values['record_json'])
                body['readback_id'] = values['readback_id']
                values['record_json'] = canonical(body)
                values['record_sha256'] = digest(body)
            with self.subTest(replace_id=replace_id), self.assertRaises(sqlite3.IntegrityError):
                with self.store.connection:
                    self.store.connection.execute('INSERT OR REPLACE INTO scientist_lab_readbacks ('
                        + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', list(values.values()))
            self.assertEqual(dict(self.store.connection.execute('SELECT * FROM scientist_lab_readbacks').fetchone()), original)

    def test_missing_schema_and_missing_guard_have_distinct_unavailable_inventory(self):
        with self.store.connection:
            self.store.connection.execute('DELETE FROM schema_migrations WHERE version=27')
        self.assertEqual(self.history(), {'supported': False, 'available': False, 'items': [], 'truncated': False})
        with self.assertRaises(ScientistAdmissionError): self.save()
        self.assertEqual(self.fixture.server.requests, [])
        with self.store.connection:
            self.store.connection.execute("INSERT INTO schema_migrations VALUES(27,'scientist_lab_readbacks','synthetic')")
            self.store.connection.execute('DROP TRIGGER scientist_lab_readback_no_delete')
        self.assertEqual(self.history(), {'supported': True, 'available': False, 'items': [], 'truncated': False})
        with self.assertRaises(ScientistAdmissionError): self.save()
        self.assertEqual(self.fixture.server.requests, [])

    def test_bounded_history_keeps_twenty_metadata_rows_and_rejects_thirty_third(self):
        task = self.service._load_task(self.run_id)
        action = self.service._action(task, 'lab.report')
        for ordinal in range(32):
            result = self.fixture.server.report()
            result['report']['synthetic_ordinal'] = ordinal
            result['report_sha256'] = digest(result['report'])
            self.service.readbacks.save(task, action, result, expected_report_sha256=result['report_sha256'])
        history = self.history()
        self.assertTrue(history['truncated'])
        self.assertEqual(len(history['items']), 20)
        self.assertTrue(all('report' not in entry for entry in history['items']))
        with self.assertRaises(sqlite3.IntegrityError): self.save()
        self.assertEqual(self.count(), 32)
        self.assertEqual(len(self.fixture.server.requests), 3)
        self.assertEqual(self.original_rows(), self.original)

    def test_oversized_typed_report_and_failed_insert_do_not_persist(self):
        task = self.service._load_task(self.run_id)
        result = self.fixture.server.report()
        result['report']['synthetic_padding'] = 'x' * 262144
        result['report_sha256'] = digest(result['report'])
        with self.assertRaises(ScientistAdmissionError):
            self.service.readbacks.save(task, self.service._action(task, 'lab.report'), result,
                                       expected_report_sha256=result['report_sha256'])
        with patch.object(self.store, 'insert', side_effect=sqlite3.OperationalError('Synthetic write failure')):
            with self.assertRaises(sqlite3.OperationalError): self.save()
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.original_rows(), self.original)


if __name__ == '__main__':
    unittest.main()

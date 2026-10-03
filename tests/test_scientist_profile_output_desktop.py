import asyncio
from copy import deepcopy
import json
import os
import sqlite3
import unittest
from unittest.mock import Mock

from aos.contracts import HELLO_CONTENT, digest
from aos.scientist_admission_history import ScientistAdmissionHistory, ScientistProfilePinV2
from aos.scientist_desktop import create_scientist_desktop_scheduler
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError

import test_scientist_admission_history as history_fixture
import test_scientist_decider_receipt as receipt_fixture
import test_scientist_desktop as desktop_fixture
import test_scientist_profile_output as output_fixture


class ScientistProfileOutputDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await desktop_fixture.ScientistDesktopTests.asyncSetUp(self)
        self.output_contract = deepcopy(output_fixture.OUTPUT_PIN)

    async def asyncTearDown(self):
        if self.scheduler is not None:
            await self.scheduler.close()
        if self.broker is not None:
            self.broker.close()
        self.runtime.stop()
        self.store.close()
        self.temporary.cleanup()

    def build(self, *, wrong_bundle=False, legacy=False, invalid_metrics=False):
        def capture(request, _binding, peer):
            value = history_fixture.admission_capture_fixture(request, peer)
            if not legacy:
                value['admission_binding']['profile_pin']['output_contract'] = deepcopy(self.output_contract)
            return value

        self.capture = Mock(side_effect=capture)
        self.current = Mock(return_value=None)
        self.history = ScientistAdmissionHistory(self.store, capture=self.capture,
            verify_current=self.current, clock=lambda: 100, **({} if legacy else {'record_version': '2.0'}))
        authenticator = Mock()
        authenticator.authenticate.return_value = BrokerPeer(os.getpid(), os.getuid(), 1,
            history_fixture.BOOT_ID, 'd' * 32, '/synthetic-cpu-only')
        authenticator.still_current.return_value = True

        def transform(receipt):
            if invalid_metrics:
                receipt['response']['metrics']['input_tokens'] = True
                receipt['usage']['input_tokens'] = True

        self.broker = receipt_fixture.ReceiptBroker(self.root, transform=transform)
        expected = deepcopy(self.output_contract)
        if wrong_bundle:
            expected['bundle_sha256'] = 'f' * 64
        self.scheduler = create_scientist_desktop_scheduler(self.controller, self.settings,
            {'synthetic_cpu_fixture': True, 'model_files': {'model.safetensors': 'a' * 64},
             'checkpoint_revision': 'synthetic-cpu-fixture', 'tokenizer_revision': 'synthetic-cpu-fixture'},
            self.broker.path, confirm_runtime=lambda _profiles: None, authenticator=authenticator,
            timeout_seconds=2, admission_history=self.history, output_contract=expected)
        return self.scheduler

    def start(self):
        desktop = self.controller.state()
        return self.scheduler.start(desktop['lease_id'], desktop['generation'])

    async def test_v2_original_pin_socket_guard_approval_and_independent_hello_readback(self):
        scheduler = self.build()
        self.start()
        approval = await desktop_fixture.ScientistDesktopTests.approval(self)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        intent = self.store.connection.execute('SELECT * FROM scientist_turn_intents').fetchone()
        self.assertEqual(intent['state'], 'receipt_recorded')
        record, checksum = self.history.read(intent['request_id'])
        self.assertEqual(record.schema_version, '2.0')
        self.assertIs(type(record.admission_binding.profile_pin), ScientistProfilePinV2)
        self.assertEqual(record.admission_binding.profile_pin.output_contract.model_dump(), self.output_contract)
        self.assertEqual(checksum, digest(record.model_dump(mode='json')))
        self.assertEqual(record.intent_binding_sha256, digest(json.loads(intent['binding_json'])))
        original = self.store.connection.execute('SELECT record_json,record_sha256 FROM scientist_admission_history').fetchone()
        scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await asyncio.gather(scheduler.task, return_exceptions=True)
        self.assertEqual(scheduler.status()['jobs'][0]['status'], 'succeeded')
        with (self.runtime.root / 'hello.txt').open('rb') as independent_reader:
            self.assertEqual(independent_reader.read(), HELLO_CONTENT.encode())
        self.assertEqual(self.store.connection.execute('SELECT result FROM verifications').fetchone()[0], 'passed')
        self.assertEqual(tuple(self.store.connection.execute(
            'SELECT record_json,record_sha256 FROM scientist_admission_history').fetchone()), tuple(original))
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection:
            self.store.connection.execute('UPDATE scientist_admission_history SET record_sha256=?', ('f' * 64,))
        self.assertEqual(len(self.broker.requests), 1)
        self.capture.assert_called_once()
        with self.assertRaises(ScientistAdmissionError):
            self.start()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)

    async def assert_denied_before_receipt(self, **options):
        scheduler = self.build(**options)
        self.start()
        await asyncio.wait_for(asyncio.gather(scheduler.task, return_exceptions=True), timeout=3)
        self.assertEqual(scheduler.status()['jobs'][0]['status'], 'failed')
        self.assertIsNone(scheduler.status()['approval'])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        intent = self.store.connection.execute('SELECT request_id,state,receipt_json FROM scientist_turn_intents').fetchone()
        self.assertEqual((intent['state'], intent['receipt_json']), ('pending', None))
        dispatched = bool(options.get('invalid_metrics'))
        self.assertEqual(scheduler.engine.client.uncertain_request_id, intent['request_id'] if dispatched else None)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM decisions').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
        record, checksum = self.history.read(intent['request_id'])
        self.assertEqual(checksum, digest(record.model_dump(mode='json')))
        self.assertEqual(len(self.broker.requests), int(dispatched))
        self.capture.assert_called_once()
        with self.assertRaises(ScientistAdmissionError):
            self.start()
        self.assertEqual(len(self.broker.requests), int(dispatched))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)
        return record

    async def test_wrong_expected_bundle_keeps_original_pending_without_dispatch_or_replay(self):
        record = await self.assert_denied_before_receipt(wrong_bundle=True)
        self.assertEqual(record.admission_binding.profile_pin.output_contract.model_dump(), self.output_contract)

    async def test_legacy_history_configuration_cannot_adopt_v2_or_create_task_or_intent(self):
        with self.assertRaises(ScientistAdmissionError):
            self.build(legacy=True)
        self.assertEqual(self.history.record_version, '1.0')
        self.assertIsNone(self.scheduler)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.broker.requests, [])
        self.capture.assert_not_called()
        self.current.assert_not_called()
        for table in ('desktop_tasks', 'scientist_turn_intents', 'scientist_admission_history', 'decisions', 'verifications'):
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    async def test_matching_v2_pin_still_requires_strict_decider_output_before_receipt(self):
        await self.assert_denied_before_receipt(invalid_metrics=True)

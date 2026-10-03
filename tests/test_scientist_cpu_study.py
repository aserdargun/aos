"""Synthetic descriptor responses; no Scientist runtime, experiment or GPU is used."""

import asyncio
from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT
from aos.scientist_cpu_capability import ScientistCpuCapability, cpu_capability_sha256
from aos.scientist_cpu_study import CPU_STUDY_SCHEMA_SHA256, STUDY_BOUND, read_cpu_study_async, validate_cpu_study
from aos.scientist_transport import ScientistAdmissionError
import test_scientist_cpu_capability as fixtures
import test_scientist_cpu_session as sessions


def study_wire(capability):
    value = json.loads((REPO_ROOT / 'examples/scientist_cpu_study.json').read_text())
    value['capability'] = deepcopy(capability)
    value['snapshot']['snapshot_sha256'] = capability['snapshot_sha256']
    return value


class ScientistCpuStudyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ScientistCpuCapabilityTests()
        self.fixture.setUp()
        self.client = self.fixture.client
        self.grant = self.fixture.grant
        self.wire = study_wire(self.fixture.wire)
        self.client._request = Mock(return_value=json.dumps(self.wire).encode())

    async def asyncTearDown(self):
        await self.client.close_async(timeout_seconds=1)
        self.fixture.doCleanups()

    async def test_reviewed_peer_schema_and_synthetic_example_are_compatible(self):
        self.assertEqual(hashlib.sha256((REPO_ROOT / 'schemas/scientist_cpu_study_wire.schema.json').read_bytes()).hexdigest(),
                         CPU_STUDY_SCHEMA_SHA256)
        example = (REPO_ROOT / 'examples/scientist_cpu_study.json').read_bytes()
        capability = ScientistCpuCapability.model_validate(json.loads(example)['capability'])
        grant = self.grant.model_copy(update={'capability': capability,
            'capability_sha256': cpu_capability_sha256(capability.model_dump(mode='json', by_alias=True))})
        self.assertEqual(validate_cpu_study(example, grant), json.loads(example))

    async def test_one_get_exact_scope_no_effect_or_intent_or_admission(self):
        authority, effect = self.client.verify_authority, self.client.authorize_and_persist
        self.assertEqual(await read_cpu_study_async(self.client, self.grant), self.wire)
        arguments = self.client._request.call_args
        self.assertEqual(arguments.args[:3], ('GET', '/v1/aos-cpu-study/synthetic.cpu.v1', b''))
        self.assertEqual(arguments.kwargs, {'bound': STUDY_BOUND})
        self.assertIs(self.client.verify_authority, authority)
        self.assertIs(self.client.authorize_and_persist, effect)
        self.assertIsNone(self.client.uncertain_action_id)
        self.assertFalse(self.client.local_cleanup_pending)

    async def test_bad_schema_identity_snapshot_grid_and_configuration_fail_closed(self):
        changes = [
            ('schema', 'unknown'), ('capability.owner_id', 'foreign'), ('capability.max_experiments', 35),
            ('capability.model_tokens', False), ('capability.allocation_authority', 0),
            ('snapshot.entity_authority', 0), ('snapshot.snapshot_sha256', '0' * 64),
            ('snapshot.rows', 385), ('snapshot.train_rows', 192.0), ('snapshot.sensors', ['duplicate', 'duplicate']),
            ('snapshot.first_utc', '2025-01-01T00:00:00Z'), ('snapshot.first_utc', '2024-01-01T00:00:00'),
            ('grid.available_prefix_count', 2), ('grid.registered_configuration_count', 2),
            ('grid.configurations.0.position', 2), ('grid.configurations.0.method', 'optics'),
            ('grid.configurations.0.configuration_sha256', 'f' * 64),
            ('grid.configurations.0.configuration.seed', False),
            ('grid.configurations.0.configuration.lsh_merge_tables', 5),
            ('grid.configurations.0.configuration.private_path', '/synthetic'),
        ]
        for path, value in changes:
            with self.subTest(path=path):
                wire = deepcopy(self.wire)
                target = wire
                parts = path.split('.')
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                target[parts[-1]] = value
                self.client._request.return_value = json.dumps(wire).encode()
                with self.assertRaises(ScientistAdmissionError):
                    await read_cpu_study_async(self.client, self.grant)

    async def test_duplicate_json_oversize_missing_normalized_field_and_transport_fail_closed(self):
        incomplete = deepcopy(self.wire)
        del incomplete['grid']['configurations'][0]['configuration']['seed']
        for raw in (b'{"schema":1,"schema":2}', b' ' * (STUDY_BOUND + 1), b'{"value":NaN}',
                    json.dumps(incomplete).encode()):
            self.client._request.return_value = raw
            with self.assertRaises(ScientistAdmissionError):
                await read_cpu_study_async(self.client, self.grant)
        for error in (TimeoutError(), OSError(), ValueError('synthetic secret must not escape')):
            self.client._request.side_effect = error
            with self.assertRaisesRegex(ScientistAdmissionError, '^CPU study readback failed closed$'):
                await read_cpu_study_async(self.client, self.grant)

    async def test_closed_uncertain_or_changed_client_refuses_before_get(self):
        for field, value in (('_closed', True), ('_uncertain_action_id', 'synthetic-action'),
                             ('principal_id', 'foreign'), ('port', 19998)):
            with self.subTest(field=field), patch.object(self.client, field, value):
                with self.assertRaises(ScientistAdmissionError):
                    await read_cpu_study_async(self.client, self.grant)
        self.client._request.assert_not_called()

    async def test_deadline_and_binding_drift_after_get_are_rejected(self):
        def read(*arguments, **options):
            self.client.principal_id = 'changed'
            return json.dumps(self.wire).encode()
        self.client._request.side_effect = read
        with self.assertRaises(ScientistAdmissionError):
            await read_cpu_study_async(self.client, self.grant)
        self.client.principal_id = self.grant.principal_id
        self.client._request.side_effect = lambda *arguments, **options: json.dumps(self.wire).encode()
        with patch('aos.scientist_cpu_study.time', SimpleNamespace(monotonic=Mock(side_effect=[10, 99]))):
            with self.assertRaises(ScientistAdmissionError):
                await read_cpu_study_async(self.client, self.grant)

    async def test_cancelled_reader_retains_cleanup_handle_and_blocks_overlap(self):
        started, release = threading.Event(), threading.Event()
        def read(*arguments, **options):
            started.set()
            if not release.wait(5):
                raise TimeoutError()
            return json.dumps(self.wire).encode()
        self.client._request.side_effect = read
        task = asyncio.create_task(read_cpu_study_async(self.client, self.grant))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            with self.assertRaises(ScientistAdmissionError):
                await read_cpu_study_async(self.client, self.grant)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.client.local_cleanup_pending)
            with self.assertRaises(ScientistAdmissionError):
                await self.client.close_async(timeout_seconds=1)
            self.assertTrue(self.client.local_cleanup_pending)
            self.assertIsNone(self.client.uncertain_action_id)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await self.client.close_async(timeout_seconds=1)
        self.assertFalse(self.client.local_cleanup_pending)


class ScientistCpuStudyConsoleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = sessions.ScientistCpuSessionTests.asyncSetUp
    asyncTearDown = sessions.ScientistCpuSessionTests.asyncTearDown
    login = sessions.ScientistCpuSessionTests.login

    def transport(self, method, path, body, deadline, *, bound):
        if path.startswith('/v1/aos-cpu-study/'):
            self.requests.append((method, path))
            self.assertEqual((method, body, bound), ('GET', b'', STUDY_BOUND))
            return json.dumps(study_wire(self.fixture.wire)).encode()
        return sessions.ScientistCpuSessionTests.transport(self, method, path, body, deadline, bound=bound)

    async def test_authenticated_explicit_read_has_no_job_approval_or_intent(self):
        route = '/api/scientist/cpu-study'
        self.assertEqual((await self.client.get(route)).status_code, 401)
        self.assertEqual(self.requests, [])
        await self.login()
        self.assertTrue((await self.client.get('/api/scientist/jobs')).json()['cpu_study_supported'])
        self.assertEqual(self.requests, [])
        self.assertEqual((await self.client.get(route + '?suite=other')).status_code, 400)
        result = await self.client.get(route)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['metadata_only'])
        self.assertFalse(result.json()['execution_authorized'])
        self.assertFalse(result.json()['snapshot_content_verified'])
        self.assertFalse(result.json()['candidate_code_verified'])
        self.assertEqual(self.requests, [('GET', '/v1/aos-cpu-study/synthetic.cpu.v1')])
        for table in ('scientist_lab_actions', 'scientist_lab_jobs'):
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    async def test_drain_and_control_change_refuse_descriptor_without_effects(self):
        await self.login()
        self.service.latch_shared_drain()
        self.assertEqual((await self.client.get('/api/scientist/cpu-study')).status_code, 409)
        self.assertEqual(self.requests, [])

    async def test_network_wait_does_not_block_inventory_and_takeover_discards_readback(self):
        await self.login()
        started, release = threading.Event(), threading.Event()
        original = self.fixture.client._request
        def read(*arguments, **options):
            started.set()
            if not release.wait(5):
                raise TimeoutError()
            return original(*arguments, **options)
        self.fixture.client._request = read
        pending = asyncio.create_task(self.client.get('/api/scientist/cpu-study'))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            inventory = await asyncio.wait_for(self.client.get('/api/scientist/jobs'), timeout=0.5)
            self.assertEqual(inventory.status_code, 200)
            proposal = await asyncio.wait_for(self.client.post('/api/scientist/propose',
                headers=self.headers, json=self.proposal), timeout=0.5)
            self.assertEqual(proposal.status_code, 409)
            self.assertEqual(self.requests, [])
            original_state = self.controller.state()
            with patch.object(self.controller, 'state', return_value=original_state | {'generation': original_state['generation'] + 1}):
                release.set()
                self.assertEqual((await pending).status_code, 409)
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()

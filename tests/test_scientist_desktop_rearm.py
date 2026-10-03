from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.scientist_async import ScientistAsyncTurnClient
from aos.scientist_desktop import ScientistDesktopBinding
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_retained_host_integration as retained_cases


class ScientistDesktopRearmTests(unittest.TestCase):
    def setUp(self):
        self.integration = retained_cases.ScientistRetainedHostIntegrationTests()
        self.integration.setUp()
        self.addCleanup(self.integration.doCleanups)
        self.fixture = self.integration.fixture
        self.integration.exchange_pair()
        self.host = self.integration.host
        self.controller = SimpleNamespace(store=self.fixture.store,
            state=lambda: self.fixture.binding.model_dump())
        self.binding = ScientistDesktopBinding(self.controller,
            admission_history=self.fixture.history)
        self.client = ScientistAsyncTurnClient(self.integration.socket.server.path)
        self.binding.engine = SimpleNamespace(client=self.client)
        self.binding.scheduler = SimpleNamespace(closed=False, _scientist_engines=Mock())
        self.inspected = self.host.inspect(self.integration.reconcile_id,
            capability_control_id=self.integration.capability_id)

    def rearm(self, **changes):
        options = dict(reconcile_control_id=self.integration.reconcile_id,
            capability_control_id=self.integration.capability_id,
            response_sha256=self.inspected['response_sha256'])
        self.binding.rearm_retained_client(self.host, self.fixture.request.request_id,
                                           **(options | changes))

    def test_hook_runs_fresh_proof_without_new_socket_dispatch(self):
        before = len(self.integration.socket.server.requests)
        with patch.object(self.client, 'rearm',
                side_effect=lambda request_id, verify_resolution: verify_resolution(request_id)) as rearm:
            self.rearm()
        rearm.assert_called_once()
        self.assertGreater(self.integration.socket.physical.call_count, 0)
        self.assertEqual(len(self.integration.socket.server.requests), before)
        self.assertEqual(self.host.inspect_resolution(self.fixture.request.request_id,
            self.integration.capability_id)['request_id'], self.fixture.request.request_id)

    def test_hook_denies_changed_controller_before_client(self):
        self.controller.state = lambda: self.fixture.binding.model_copy(
            update={'generation': self.fixture.binding.generation + 1}).model_dump()
        with patch.object(self.client, 'rearm') as rearm:
            with self.assertRaises(ScientistAdmissionError):
                self.rearm()
        rearm.assert_not_called()

    def test_hook_denies_wrong_ack_and_revoked_resolution(self):
        with patch.object(self.client, 'rearm',
                side_effect=lambda request_id, verify_resolution: verify_resolution(request_id)):
            with self.assertRaises(ScientistAdmissionError):
                self.rearm(response_sha256='0' * 64)
            self.integration.resolution_authority.side_effect = ScientistAdmissionError('Revoked')
            with self.assertRaises(ScientistAdmissionError):
                self.rearm()
        self.assertEqual(self.fixture.store.connection.execute(
            'SELECT COUNT(*) FROM scientist_turn_resolutions').fetchone()[0], 0)

    def test_hook_denies_closed_or_replaced_client(self):
        self.binding.scheduler.closed = True
        with self.assertRaises(ScientistAdmissionError):
            self.rearm()
        self.binding.scheduler.closed = False
        self.binding.engine.client = Mock()
        with self.assertRaises(ScientistAdmissionError):
            self.rearm()


if __name__ == '__main__':
    unittest.main()

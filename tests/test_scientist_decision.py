import asyncio
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock

from aos.contracts import AOSFault, Option, State, digest
from aos.scientist_async import ScientistAsyncTurnClient
from aos.scientist_decision import ScientistDecisionEngine
from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnReceipt, ScientistTurnRequest
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError, ScientistUncertainTurn
from aos.storage import TrajectoryStore
import test_scientist_transport as socket_fixtures


class ScientistDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.store = TrajectoryStore(self.root / 'synthetic.sqlite3')
        with self.store.connection:
            self.store.connection.execute("INSERT INTO desktop_sessions VALUES('session','runtime','synthetic','AGENT','lease',0,'running','synthetic','synthetic')")
        self.binding = ScientistIntentBinding(session_id='session', runtime_id='runtime', owner='AGENT',
            lease_id='lease', generation=0, authorization_context_sha256='a' * 64)
        self.journal = ScientistIntentJournal(self.store, self.binding)
        self.request = ScientistTurnRequest(request_id='a' * 32, profile_id='aos.decider.turn.v1',
                                           deployment_digest='b' * 64, payload={'synthetic_cpu_fixture': True})
        self.authenticator = Mock()
        self.authenticator.authenticate.return_value = BrokerPeer(os.getpid(), os.getuid(), 1,
                                               'synthetic', 'c' * 32, '/synthetic')
        self.authenticator.still_current.return_value = True
        self.brokers = []
        self.pins = {'synthetic_cpu_fixture': True}
        self.state = State(task_id='task', run_id='run', step_id='step', runtime_id='runtime',
                           deployment_id='decider-' + digest(self.pins), owner_lease_id='lease')
        self.options = [Option(id='write', label='Write'), Option(id='ask', label='Ask')]

    async def asyncTearDown(self):
        for broker in self.brokers:
            broker.release.set()
            broker.close()
        self.store.close()
        self.temporary.cleanup()

    def client(self, mode='success', **changes):
        broker = socket_fixtures.SyntheticBroker(self.root, mode)
        self.brokers.append(broker)
        host_thread = threading.get_ident()

        def host(callback):
            def invoke(*arguments):
                self.assertEqual(threading.get_ident(), host_thread)
                return callback(*arguments)
            return invoke

        return ScientistAsyncTurnClient(broker.path, timeout_seconds=2, authenticator=self.authenticator,
            verify_admission=host(self.journal.verify_admission), persist_intent=host(self.journal.persist_intent),
            record_receipt=host(self.journal.record_receipt), **changes), broker

    async def test_actual_sqlite_intent_and_receipt_callbacks_stay_on_host_thread(self):
        client, broker = self.client()
        receipt = await client.infer(self.request)
        self.assertEqual(receipt.response, {'synthetic_cpu_fixture': True})
        row = self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()
        self.assertEqual(row[0], 'receipt_recorded')
        self.assertFalse(client.cleanup_pending)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.request.model_copy(update={'request_id': 'd' * 32}))

    async def test_cancellation_is_responsive_leaves_durable_unknown_and_blocks_new_turn(self):
        loop_errors = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda current, context: loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)
        client, broker = self.client('hold')
        operation = asyncio.create_task(client.infer(self.request))
        self.assertTrue(await asyncio.to_thread(broker.entered.wait, 1))
        started = time.monotonic()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertLess(time.monotonic() - started, 0.3)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.request.model_copy(update={'request_id': 'd' * 32}))
        deadline = time.monotonic() + 1
        while client.cleanup_pending and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertFalse(client.cleanup_pending)
        self.assertEqual(client.uncertain_request_id, self.request.request_id)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')
        self.assertFalse(broker.release.is_set())
        await asyncio.sleep(0)
        self.assertEqual(loop_errors, [])

    async def test_active_turn_does_not_block_event_loop_and_parallel_turn_is_denied(self):
        client, broker = self.client('hold')
        operation = asyncio.create_task(client.infer(self.request))
        self.assertTrue(await asyncio.to_thread(broker.entered.wait, 1))
        await asyncio.wait_for(asyncio.sleep(.01), .2)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.request.model_copy(update={'request_id': 'd' * 32}))
        broker.release.set()
        await operation

    async def test_lost_ack_blocks_future_turn_without_native_fallback(self):
        client, broker = self.client('disconnect')
        with self.assertRaises(ScientistUncertainTurn):
            await client.infer(self.request)
        with self.assertRaises(ScientistAdmissionError):
            await client.infer(self.request.model_copy(update={'request_id': 'd' * 32}))
        self.assertEqual(len(broker.requests), 1)

    def engine(self, mutate=None, verifier=None):
        async def infer(request):
            response = {'deployment_digest': request.deployment_digest,
                        'prediction': {'selected_option': 'write', 'probabilities': {'write': 1.0, 'ask': 0.0}},
                        'metrics': {'synthetic_cpu_fixture': True}}
            if mutate is not None:
                mutate(response)
            unit = 'swapp-aos-gpu-turn-' + 'c' * 32 + '.service'
            return ScientistTurnReceipt(version=1, request_id=request.request_id,
                profile_id=request.profile_id, deployment_digest=request.deployment_digest,
                generation={'unit': unit, 'invocation_id': 'd' * 32, 'main_pid': 1234,
                            'control_group': '/synthetic/' + unit}, response=response, usage={})

        client = Mock(infer=AsyncMock(side_effect=infer))
        engine = ScientistDecisionEngine(self.pins, client, **({} if verifier is None else {'verify_state': verifier}))
        return engine, client

    async def test_engine_default_authority_human_owner_and_wrong_deployment_denied(self):
        engine, client = self.engine()
        with self.assertRaises(ScientistAdmissionError):
            await engine.decide(self.state, self.options)
        client.infer.assert_not_called()
        engine.verify_state = lambda *arguments: None
        for update in ({'owner': 'HUMAN'}, {'deployment_id': 'wrong'}):
            with self.assertRaises(ScientistAdmissionError):
                await engine.decide(self.state.model_copy(update=update), self.options)
        client.infer.assert_not_called()

    async def test_decision_failure_diagnostic_preserves_exception_and_redacts_content(self):
        failure = ScientistAdmissionError('synthetic-private-error /private/synthetic/token')

        def reject(*arguments):
            raise failure

        engine, client = self.engine(verifier=reject)
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(ScientistAdmissionError) as raised:
            await engine.decide(self.state, self.options)
        self.assertIs(raised.exception, failure)
        diagnostic = json.loads(output.getvalue())
        self.assertEqual(diagnostic['boundary'], 'scientist_decision')
        self.assertEqual(diagnostic['locations'][0]['type'], 'ScientistAdmissionError')
        self.assertNotIn('synthetic-private-error', output.getvalue())
        self.assertNotIn('/private/synthetic/token', output.getvalue())
        self.assertLess(len(output.getvalue()), 10000)
        client.infer.assert_not_called()

    async def test_decision_client_failure_is_reported_without_retry(self):
        failure = RuntimeError('synthetic-private-response')
        engine, client = self.engine(verifier=lambda *arguments: None)
        client.infer.side_effect = failure
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(RuntimeError) as raised:
            await engine.decide(self.state, self.options)
        self.assertIs(raised.exception, failure)
        self.assertEqual(json.loads(output.getvalue())['locations'][0]['type'], 'RuntimeError')
        self.assertNotIn('synthetic-private-response', output.getvalue())
        client.infer.assert_awaited_once()

    async def test_engine_uses_native_finite_options_and_verified_metrics(self):
        engine, client = self.engine(verifier=lambda *arguments: None)
        result = await engine.decide(self.state, self.options)
        self.assertEqual(result.selected_option, 'write')
        request = client.infer.call_args.args[0]
        self.assertEqual(request.profile_id, 'aos.decider.turn.v1')
        self.assertEqual(request.payload['request']['options'], [option.model_dump() for option in self.options])
        self.assertIn('Observation:', request.payload['request']['state'])
        self.assertEqual(engine.last_metrics, {'synthetic_cpu_fixture': True})

    async def test_invalid_prediction_and_stale_post_readback_never_return_action(self):
        engine, client = self.engine(lambda response: response['prediction'].update(selected_option='unknown'),
                                     verifier=lambda *arguments: None)
        with self.assertRaises(AOSFault):
            await engine.decide(self.state, self.options)
        self.assertIsNone(engine.last_response)
        calls = 0

        def authority(state, options):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ScientistAdmissionError('Synthetic controller takeover')

        engine, client = self.engine(verifier=authority)
        with self.assertRaises(ScientistAdmissionError):
            await engine.decide(self.state, self.options)
        self.assertIsNone(engine.last_response)
        self.assertEqual(engine.last_metrics, {})

    async def test_dispatch_hook_cannot_change_the_pinned_model_identity(self):
        engine, client = self.engine(verifier=lambda *arguments: None)
        engine.before_decision_dispatch = lambda payload: engine.pins.update(unapproved='synthetic-drift')
        with self.assertRaises(ScientistAdmissionError):
            await engine.decide(self.state, self.options)
        client.infer.assert_not_called()

    async def test_boolean_host_authority_is_not_a_proof(self):
        engine, client = self.engine(verifier=lambda *arguments: True)
        with self.assertRaises(ScientistAdmissionError):
            await engine.decide(self.state, self.options)
        client.infer.assert_not_called()

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT, digest
from aos.lifecycle import ProcessIdentity
from aos.scientist_admission_history import ScientistServerGeneration
from aos.scientist_shared_launch import (
    TRANSPORT_SCHEMA_SHA256, TRANSPORT_V2_SCHEMA_SHA256, ScientistSharedLaunchAdapter,
    ScientistSharedLaunchReview, ScientistSharedLaunchReviewV2,
)
from aos.scientist_transport import BROKER_UNIT
from test_shared_desktop_host import SyntheticSharedHostFixture


class ScientistSharedLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-launch-adapter-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = ProcessIdentity(boot_id='00000000-0000-0000-0000-000000000001',
            pid=os.getpid(), start_ticks=42, pid_namespace=17, uid=os.getuid())
        self.broker = ScientistServerGeneration(unit=BROKER_UNIT, uid=os.getuid(), pid=os.getpid(),
            start_ticks=43, boot_id=self.manager.boot_id, invocation_id='4' * 32,
            control_group='/synthetic/' + BROKER_UNIT)
        self.fixture = SyntheticSharedHostFixture(self.root,
            broker_identity_sha256=digest(self.broker.model_dump(mode='json')))
        replacement = patch('aos.shared_desktop_host.REPO_ROOT', self.root)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.review = ScientistSharedLaunchReview(request_id='1' * 32, binding_sha256='2' * 64,
            transport_schema_sha256=TRANSPORT_SCHEMA_SHA256,
            plan_path=str(self.fixture.plan_path), plan_sha256=self.fixture.plan.plan_sha256(),
            activation_path=str(self.fixture.activation_path), activation_sha256=self.fixture.activation_sha,
            provision_sha256=self.fixture.provision_sha, manager=self.manager, broker=self.broker,
            issued_boottime=100.0, expires_boottime=200.0)
        self.consumed = False
        self.client = SimpleNamespace(expected_broker=self.broker,
            broker_hash=digest(self.broker.model_dump(mode='json')), request=Mock(side_effect=self.respond))
        self.adapter = self.make_adapter()
        self.fixture.host.activation_verifier = self.adapter.verify
        self.fixture.host.activation_claimer = self.adapter.claim

    def make_adapter(self, **updates):
        arguments = {'transport_schema_sha256': TRANSPORT_SCHEMA_SHA256,
                     'clock': lambda: self.fixture.clock, 'identity_reader': lambda pid: self.manager,
                     **updates}
        return ScientistSharedLaunchAdapter(self.client, self.review, **arguments)

    def respond(self, operation, request_id, binding_sha256, intent_sha256=None, *, deadline):
        self.assertGreater(deadline, self.fixture.clock)
        fresh = operation == 'claim' and not self.consumed
        if operation == 'claim':
            state = self.fixture.states[-1]
            self.assertEqual(state.phase, 'starting')
            self.assertEqual(intent_sha256, state.launch_intent_sha256)
            self.fixture.claim_authority(self.fixture.plan, self.fixture.activation, state)
            self.consumed = True
        return SimpleNamespace(consumed_now=fresh, status=SimpleNamespace(
            request_id=request_id, binding_sha256=binding_sha256,
            state='consumed' if self.consumed else 'reviewed', consumed=self.consumed,
            cleanup_required=self.consumed, expired=False))

    def test_full_host_flow_verifies_repeatedly_and_claims_only_once(self):
        state = self.fixture.start()
        self.assertEqual(state.phase, 'running', state.last_error)
        self.assertTrue(self.fixture.host.status(state)['task_admission_enabled'])
        operations = [call.args[0] for call in self.client.request.call_args_list]
        self.assertEqual(operations.count('claim'), 1)
        self.assertGreater(operations.count('verify'), 1)
        self.fixture.transport.start.assert_called_once()

    def test_explicit_v2_review_preserves_manager_verify_and_single_claim_flow(self):
        self.review = ScientistSharedLaunchReviewV2.model_validate(self.review.model_dump() | {
            'schema_version': '2.0', 'transport_schema_sha256': TRANSPORT_V2_SCHEMA_SHA256})
        self.adapter = self.make_adapter(transport_schema_sha256=TRANSPORT_V2_SCHEMA_SHA256)
        self.fixture.host.activation_verifier = self.adapter.verify
        self.fixture.host.activation_claimer = self.adapter.claim
        state = self.fixture.start()
        self.assertEqual(state.phase, 'running', state.last_error)
        operations = [call.args[0] for call in self.client.request.call_args_list]
        self.assertEqual(operations.count('claim'), 1)
        self.assertTrue(set(operations) <= {'verify', 'claim'})
        self.fixture.transport.start.assert_called_once()

    def test_review_versions_never_silently_upgrade_downgrade_or_accept_unknown_wire(self):
        with self.assertRaisesRegex(ValueError, 'transport differs'):
            self.make_adapter(transport_schema_sha256=TRANSPORT_V2_SCHEMA_SHA256)
        self.review = ScientistSharedLaunchReviewV2.model_validate(self.review.model_dump() | {
            'schema_version': '2.0', 'transport_schema_sha256': TRANSPORT_V2_SCHEMA_SHA256})
        with self.assertRaisesRegex(ValueError, 'transport differs'):
            self.make_adapter(transport_schema_sha256=TRANSPORT_SCHEMA_SHA256)
        with self.assertRaisesRegex(ValueError, 'transport differs'):
            self.make_adapter(transport_schema_sha256='f' * 64)
        self.review = self.review.model_copy(update={'schema_version': '3.0'})
        with self.assertRaisesRegex(ValueError, 'Unsupported explicitly reviewed'):
            self.make_adapter(transport_schema_sha256=TRANSPORT_V2_SCHEMA_SHA256)
        self.client.request.assert_not_called()

    def test_v2_review_cannot_smuggle_v1_wire_or_be_parsed_as_v1(self):
        with self.assertRaises(ValueError):
            ScientistSharedLaunchReviewV2.model_validate(self.review.model_dump())
        value = self.review.model_dump() | {'transport_schema_sha256': TRANSPORT_V2_SCHEMA_SHA256}
        with self.assertRaises(ValueError):
            ScientistSharedLaunchReview.model_validate(value)

    def test_v2_review_schema_and_synthetic_example_are_separate_from_v1(self):
        schema = json.loads((REPO_ROOT / 'schemas/scientist_shared_launch_review_v2.schema.json').read_text())
        self.assertEqual(schema, ScientistSharedLaunchReviewV2.model_json_schema())
        value = json.loads((REPO_ROOT / 'examples/scientist_shared_launch_review_v2.json').read_text())
        review = ScientistSharedLaunchReviewV2.model_validate(value)
        self.assertEqual(review.transport_schema_sha256, TRANSPORT_V2_SCHEMA_SHA256)
        with self.assertRaises(ValueError):
            ScientistSharedLaunchReview.model_validate(value)

    def test_lost_ack_consumes_once_and_never_retries(self):
        def lost(*args, **kwargs):
            result = self.respond(*args, **kwargs)
            if args[0] == 'claim':
                raise TimeoutError('Synthetic consumed request, lost acknowledgement')
            return result

        self.client.request.side_effect = lost
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertTrue(self.consumed)
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.adapter.claim(self.fixture.plan, self.fixture.activation, self.fixture.states[0])
        self.assertEqual(sum(call.args[0] == 'claim' for call in self.client.request.call_args_list), 1)
        self.fixture.transport.start.assert_not_called()

    def test_duplicate_claim_response_cannot_authorize_spawn(self):
        self.consumed = True
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertIn('not a fresh claim', state.last_error)
        self.fixture.transport.start.assert_not_called()

    def test_late_claim_reply_cannot_authorize_spawn(self):
        def delayed(*args, **kwargs):
            result = self.respond(*args, **kwargs)
            if args[0] == 'claim':
                self.fixture.clock += 4
            return result

        self.client.request.side_effect = delayed
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertIn('original call deadline', state.last_error)
        self.fixture.transport.start.assert_not_called()

    def test_post_claim_reviewed_state_is_not_a_valid_readback(self):
        def lost_consumption(*args, **kwargs):
            result = self.respond(*args, **kwargs)
            if args[0] == 'verify' and self.consumed:
                result.status.state = 'reviewed'
                result.status.consumed = False
                result.status.cleanup_required = False
            return result

        self.client.request.side_effect = lost_consumption
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.fixture.transport.start.assert_not_called()

    def test_invalid_status_and_foreign_reply_fail_closed(self):
        for changes in ({'request_id': '9' * 32}, {'binding_sha256': '9' * 64},
                        {'state': 'revoked'}, {'expired': True}, {'consumed': 0},
                        {'cleanup_required': 0}, {'expired': 0}, {'cleanup_required': True},
                        {'state': 'consumed'}, {'state': 'unknown'}):
            with self.subTest(changes=changes):
                reply = self.respond('verify', self.review.request_id, self.review.binding_sha256, deadline=153)
                for name, value in changes.items():
                    setattr(reply.status, name, value)
                self.client.request.side_effect = None
                self.client.request.return_value = reply
                with self.assertRaises(ValueError):
                    self.adapter.verify(self.fixture.plan, self.fixture.activation)

    def test_verify_cannot_return_fresh_consumption_or_integer_flag(self):
        for flag in (True, 0, 1, None):
            reply = self.respond('verify', self.review.request_id, self.review.binding_sha256, deadline=153)
            reply.consumed_now = flag
            self.client.request.side_effect = None
            self.client.request.return_value = reply
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                self.adapter.verify(self.fixture.plan, self.fixture.activation)

    def test_stale_manager_and_expired_window_make_no_request(self):
        self.adapter.identity_reader = lambda pid: self.manager.model_copy(update={'start_ticks': 99})
        with self.assertRaises(ValueError):
            self.adapter.verify(self.fixture.plan, self.fixture.activation)
        self.adapter.identity_reader = lambda pid: self.manager
        for clock in (99.0, 200.0, float('nan'), True):
            self.fixture.clock = clock
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                self.adapter.verify(self.fixture.plan, self.fixture.activation)
        self.client.request.assert_not_called()

    def test_client_generation_and_source_drift_make_no_request(self):
        self.client.broker_hash = 'f' * 64
        with self.assertRaises(ValueError):
            self.adapter.verify(self.fixture.plan, self.fixture.activation)
        self.client.broker_hash = digest(self.broker.model_dump(mode='json'))
        self.fixture.activation_path.write_text('{}')
        with self.assertRaises(ValueError):
            self.adapter.verify(self.fixture.plan, self.fixture.activation)
        self.client.request.assert_not_called()

    def test_unknown_transport_and_concurrent_calls_fail_closed(self):
        with self.assertRaises(ValueError):
            self.make_adapter(transport_schema_sha256='f' * 64)
        with self.adapter._lock:
            with self.assertRaises(ValueError):
                self.adapter.verify(self.fixture.plan, self.fixture.activation)
        self.client.request.assert_not_called()

    def test_wrong_durable_state_prevents_claim_request(self):
        def changed_state(plan, activation, state):
            self.adapter.claim(plan, activation, state.model_copy(update={'provision_sha256': 'f' * 64}))

        self.fixture.host.activation_claimer = changed_state
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertFalse(any(call.args[0] == 'claim' for call in self.client.request.call_args_list))
        self.fixture.transport.start.assert_not_called()

    def test_review_schema_and_synthetic_example(self):
        schema = json.loads((REPO_ROOT / 'schemas/scientist_shared_launch_review.schema.json').read_text())
        self.assertEqual(schema, ScientistSharedLaunchReview.model_json_schema())
        value = json.loads((REPO_ROOT / 'examples/scientist_shared_launch_review.json').read_text())
        ScientistSharedLaunchReview.model_validate(value, strict=True)

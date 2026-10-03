"""CPU preparation with synthetic Docker/process identity; no launch authority."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos import shared_cleanup_environment as environment
from aos.contracts import REPO_ROOT, canonical, digest
from aos.scientist_admission_history import ScientistServerGeneration
from aos.scientist_shared_launch import (
    ScientistSharedLaunchAdapter, ScientistSharedLaunchReview, ScientistSharedLaunchReviewV2,
    TRANSPORT_SCHEMA_SHA256, TRANSPORT_V2_SCHEMA_SHA256,
)
from aos.scientist_transport import BROKER_UNIT
from aos.shared_desktop_provision import LAUNCH_INTENT_NAME
from test_shared_desktop_host import SyntheticSharedHostFixture


class SharedCleanupEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.broker = ScientistServerGeneration(unit=BROKER_UNIT, uid=os.getuid(), pid=5678,
            start_ticks=100, boot_id='00000000-0000-0000-0000-000000000001', invocation_id='a' * 32,
            control_group='/synthetic/' + BROKER_UNIT)
        self.fixture = SyntheticSharedHostFixture(self.root,
            broker_identity_sha256=digest(self.broker.model_dump(mode='json')))
        self.clock = 80.0
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, b'SYNTHETIC-DAEMON\n', b''))
        self.identity = self.enterContext(patch.object(environment, 'process_identity', return_value=self.fixture.process))
        self.socket = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_dev=1, st_ino=2)
        actual_stat = os.stat

        def inspect(path, *arguments, **options):
            if str(path) == '/var/run/docker.sock':
                return self.socket
            return actual_stat(path, *arguments, **options)

        self.enterContext(patch.object(environment.os, 'stat', side_effect=inspect))
        self.enterContext(patch('aos.shared_desktop_host.REPO_ROOT', self.root))
        self.path = self.root / 'cleanup-environment.json'

    def capture(self):
        return environment.capture_cleanup_environment(self.fixture.plan, self.fixture.provision_sha,
            deadline=89.0, runner=self.runner, clock=lambda: self.clock)

    def prepare(self):
        candidate = self.capture()
        environment.write_cleanup_environment(self.path, candidate)
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.fixture.set_launch_input(json.loads(self.fixture.launch_input.read_text()),
                                      extra_config_files={str(self.path): self.sha})
        return candidate

    def load(self):
        return environment.load_cleanup_environment(self.path, self.sha, plan=self.fixture.plan,
            activation=self.fixture.activation, provision_sha256=self.fixture.provision_sha)

    def verify(self):
        return environment.verify_cleanup_launch_environment(self.path, self.sha, plan=self.fixture.plan,
            activation=self.fixture.activation, provision_sha256=self.fixture.provision_sha,
            deadline=159.0, runner=self.runner, clock=lambda: self.clock)

    def test_capture_is_non_authoritative_canonical_private_and_durable(self):
        candidate = self.prepare()
        self.assertEqual(candidate, self.load())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.read_bytes(), canonical(candidate.model_dump(mode='json')).encode())
        self.assertFalse(candidate.execution_authorized)
        self.assertFalse(candidate.cleanup_authorized)
        self.assertFalse((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).exists())
        self.assertEqual(list(Path(self.fixture.plan.workspace).iterdir()), [])
        with self.assertRaises(FileExistsError):
            environment.write_cleanup_environment(self.path, candidate)

    def test_capture_after_intent_or_workspace_effect_denies(self):
        workspace = Path(self.fixture.plan.workspace)
        (workspace / 'synthetic-effect').write_text('SYNTHETIC')
        with self.assertRaises(ValueError):
            self.capture()
        (workspace / 'synthetic-effect').unlink()
        (workspace.parent / LAUNCH_INTENT_NAME).write_text('SYNTHETIC')
        with self.assertRaises(ValueError):
            self.capture()

    def test_capture_cannot_write_inside_pristine_scope(self):
        candidate = self.capture()
        with self.assertRaisesRegex(ValueError, 'outside the pristine'):
            environment.write_cleanup_environment(Path(self.fixture.plan.session_directory) / 'environment.json', candidate)

    def test_capture_rejects_socket_replacement_owner_type_and_daemon_drift(self):
        for update in ({'st_uid': os.getuid()}, {'st_mode': stat.S_IFREG | 0o600}):
            original = dict(self.socket.__dict__)
            self.socket.__dict__.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.capture()
            self.socket.__dict__.update(original)
        self.runner.side_effect = [subprocess.CompletedProcess([], 0, name, b'')
                                   for name in (b'ORIGINAL\n', b'CHANGED\n')]
        with self.assertRaisesRegex(ValueError, 'changed during preparation'):
            self.capture()

    def test_capture_timeout_late_reply_and_nonzero_command_deny(self):
        self.runner.side_effect = subprocess.TimeoutExpired('synthetic', 2)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.capture()
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess([], 1, b'', b'failed')
        with self.assertRaisesRegex(ValueError, 'observation failed'):
            self.capture()
        self.runner.return_value = subprocess.CompletedProcess([], 0, b'SYNTHETIC\n', b'')
        self.runner.side_effect = lambda *arguments, **options: (setattr(self, 'clock', 90.0) or self.runner.return_value)
        with self.assertRaisesRegex(ValueError, 'finite BOOTTIME'):
            self.capture()

    def test_unpinned_changed_or_post_activation_document_denies(self):
        candidate = self.prepare()
        original_activation = self.fixture.activation
        self.fixture.activation = original_activation.model_copy(update={'config_files': {}})
        with self.assertRaisesRegex(ValueError, 'pinned in the activation'):
            self.load()
        self.fixture.activation = original_activation
        self.path.write_bytes(b'{}')
        with self.assertRaises(ValueError):
            self.load()
        self.path.write_bytes(canonical(candidate.model_copy(update={'captured_boottime': 101.0}).model_dump(mode='json')).encode())
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.fixture.activation = original_activation.model_copy(update={'config_files': {str(self.path): self.sha}})
        with self.assertRaisesRegex(ValueError, 'pre-activation scope'):
            self.load()

    def test_noncanonical_duplicate_keys_and_private_permissions_deny(self):
        self.prepare()
        original = self.path.read_bytes()
        for content in (original + b'\n', b'{"schema_version":"1.0",' + original[1:]):
            self.path.write_bytes(content)
            self.sha = hashlib.sha256(content).hexdigest()
            self.fixture.activation = self.fixture.activation.model_copy(update={'config_files': {str(self.path): self.sha}})
            with self.subTest(content=content[:20]), self.assertRaisesRegex(ValueError, 'canonical bytes'):
                self.load()
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.load()

    def test_launch_readback_preserves_original_manager_environment_and_window(self):
        candidate = self.prepare()
        self.clock = 150.0
        self.assertEqual(self.verify(), candidate)
        self.identity.return_value = self.fixture.process.model_copy(update={'start_ticks': 99})
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            self.verify()
        self.identity.return_value = self.fixture.process
        self.clock = 201.0
        with self.assertRaises(ValueError):
            self.verify()

    def test_pinned_cleanup_uses_original_file_even_after_launch_expiry(self):
        candidate = self.prepare()
        state = self.fixture.start().model_copy(update={'phase': 'uncertain', 'task_admission_enabled': False})
        self.clock = 250.0
        gate = Mock(return_value=None)
        bound = environment.PinnedSharedDesktopCleanupObserver(self.path, self.sha,
            verify_scope=gate, runner=self.runner, clock=lambda: self.clock)
        with patch.object(environment, 'SharedDesktopCleanupObserver') as observer:
            def observe(selected, *, deadline):
                observer.call_args.kwargs['verify_scope'](selected, deadline)
                return 'synthetic-physical-observation'
            observer.return_value.observe.side_effect = observe
            self.assertEqual(bound.observe(state, deadline=259.0), 'synthetic-physical-observation')
            self.assertEqual(observer.call_args.args[0], candidate.daemon())
            self.assertEqual(gate.call_args.args[1], candidate)
            self.path.write_bytes(b'{}')
            with self.assertRaises(ValueError):
                bound.observe(state, deadline=259.0)

    def test_pinned_cleanup_default_authority_and_forged_intent_deny(self):
        self.prepare()
        state = self.fixture.start().model_copy(update={'phase': 'uncertain', 'task_admission_enabled': False})
        birth = state.runtime_binding.birth.model_copy(update={
            'container_name': 'aos-desktop-' + hashlib.sha256(state.workspace.encode()).hexdigest()[:20]})
        state = state.model_copy(update={'runtime_binding': state.runtime_binding.model_copy(update={'birth': birth})})
        bound = environment.PinnedSharedDesktopCleanupObserver(self.path, self.sha, clock=lambda: 150.0)
        with self.assertRaisesRegex(ValueError, 'cleanup authority are unavailable'):
            bound.observe(state, deadline=159.0)
        wrong = state.model_copy(update={'launch_intent_sha256': 'f' * 64})
        with self.assertRaisesRegex(ValueError, 'durable original launch intent'):
            bound.observe(wrong, deadline=159.0)

    def test_schema_and_explicitly_synthetic_example(self):
        schema = json.loads((REPO_ROOT / 'schemas/shared_cleanup_environment.schema.json').read_text())
        self.assertEqual(schema, environment.SharedCleanupEnvironment.model_json_schema())
        candidate = environment.SharedCleanupEnvironment.model_validate_json(
            (REPO_ROOT / 'examples/shared_cleanup_environment.json').read_bytes())
        self.assertFalse(candidate.execution_authorized)
        for field in ('execution_authorized', 'cleanup_authorized'):
            with self.assertRaises(ValueError):
                environment.SharedCleanupEnvironment.model_validate(candidate.model_dump() | {field: True})

    def bound_adapter(self, *, change_after_claim=False, version_two=False):
        self.prepare()
        self.clock = 150.0
        review_type = ScientistSharedLaunchReviewV2 if version_two else ScientistSharedLaunchReview
        wire_sha = TRANSPORT_V2_SCHEMA_SHA256 if version_two else TRANSPORT_SCHEMA_SHA256
        review = review_type(request_id='1' * 32, binding_sha256='2' * 64,
            transport_schema_sha256=wire_sha,
            plan_path=str(self.fixture.plan_path), plan_sha256=self.fixture.plan.plan_sha256(),
            activation_path=str(self.fixture.activation_path), activation_sha256=self.fixture.activation_sha,
            provision_sha256=self.fixture.provision_sha, manager=self.fixture.process, broker=self.broker,
            issued_boottime=100.0, expires_boottime=200.0)

        def respond(operation, request_id, binding_sha256, intent_sha256=None, *, deadline):
            fresh = operation == 'claim' and not self.fixture.authority_claims
            if operation == 'claim':
                self.fixture.claim_authority(self.fixture.plan, self.fixture.activation, self.fixture.states[-1])
                if change_after_claim:
                    self.socket.st_ino += 1
            consumed = bool(self.fixture.authority_claims)
            return SimpleNamespace(consumed_now=fresh, status=SimpleNamespace(
                request_id=request_id, binding_sha256=binding_sha256, state='consumed' if consumed else 'reviewed',
                consumed=consumed, cleanup_required=consumed, expired=False))

        client = SimpleNamespace(expected_broker=self.broker,
            broker_hash=digest(self.broker.model_dump(mode='json')), request=Mock(side_effect=respond))
        adapter = ScientistSharedLaunchAdapter(client, review, transport_schema_sha256=wire_sha,
            clock=lambda: self.clock, identity_reader=lambda pid: self.fixture.process)
        bound = environment.EnvironmentBoundSharedLaunchAdapter(adapter, self.path, self.sha, runner=self.runner)
        self.fixture.host.activation_verifier = bound.verify
        self.fixture.host.activation_claimer = bound.claim
        return bound, client

    def test_environment_bound_host_claims_once_and_uses_same_review_after_launch(self):
        bound, client = self.bound_adapter()
        state = self.fixture.start()
        self.assertEqual(state.phase, 'running', state.last_error)
        self.assertEqual(sum(call.args[0] == 'claim' for call in client.request.call_args_list), 1)
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            bound.claim(self.fixture.plan, self.fixture.activation, self.fixture.states[0])
        self.fixture.transport.start.assert_called_once()

    def test_environment_drift_after_consumption_preserves_uncertain_intent_and_never_spawns(self):
        bound, client = self.bound_adapter(change_after_claim=True)
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertIn('no longer matches', state.last_error)
        self.assertEqual(sum(call.args[0] == 'claim' for call in client.request.call_args_list), 1)
        self.assertTrue((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).is_file())
        self.fixture.transport.start.assert_not_called()

    def test_v2_manager_composition_still_enforces_original_environment_and_single_claim(self):
        bound, client = self.bound_adapter(version_two=True)
        state = self.fixture.start()
        self.assertEqual(state.phase, 'running', state.last_error)
        self.assertEqual(bound.adapter.review.schema_version, '2.0')
        self.assertEqual(sum(call.args[0] == 'claim' for call in client.request.call_args_list), 1)
        self.socket.st_ino += 1
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            bound.verify(self.fixture.plan, self.fixture.activation)
        self.fixture.transport.start.assert_called_once()

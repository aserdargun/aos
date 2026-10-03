import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from aos.contracts import canonical, digest
from aos.lifecycle import LifecycleBirth, LifecycleEvent, ProcessIdentity
from aos.recovery_lifecycle import LifecycleInspection
from aos.recovery_session import SessionInspection
from aos.session_binding import SessionRuntimeBinding
from aos.shared_desktop_host import (
    SharedCleanupProof, SharedDesktopHost, SharedDesktopState, SharedServiceBinding,
    SystemdSharedDesktopTransport,
)
from aos.shared_desktop_plan import (
    SHARED_DESKTOP_UNIT, SharedDesktopActivation, SharedDesktopLimits, SharedDesktopTemplate, prepare_plan,
)
from aos.shared_desktop_provision import LAUNCH_INTENT_NAME, PROVISION_NAME, provision_scope


class SyntheticSharedHostFixture:
    def __init__(self, root):
        self.root = root
        for path in (root / 'data', root / 'data/local-app-v1', root / 'runs', root / 'scripts'):
            path.mkdir(mode=0o700)
        self.boot = '00000000-0000-0000-0000-000000000001'
        self.process = ProcessIdentity(boot_id=self.boot, pid=12345, start_ticks=12,
                                       pid_namespace=34, uid=os.getuid())
        self.service = SharedServiceBinding(invocation_id='3' * 32, process=self.process,
                                            control_group='/synthetic/' + SHARED_DESKTOP_UNIT)
        self.python = self.file('synthetic-python', b'SYNTHETIC never executed')
        self.launcher = self.file('scripts/aos_native_launch.py', b'SYNTHETIC never imported')
        self.policy = self.file('policy.json', canonical({'enabled': True, 'caller_unit': SHARED_DESKTOP_UNIT}).encode())
        self.profile = self.file('profiles.json', b'{"synthetic":true}')
        configs = {str(path): self.sha(path.read_bytes()) for path in (self.policy, self.profile)}
        sources = {str(path): self.sha(path.read_bytes()) for path in (self.python, self.launcher)}
        self.template = SharedDesktopTemplate(origin='http://127.0.0.1:8765',
            manager_base=str(root / 'data/local-app-v1'), session_root=str(root / 'data/local-app-v1'),
            python_path=str(self.python), python_sha256=sources[str(self.python)],
            launcher_path=str(self.launcher), launcher_sha256=sources[str(self.launcher)],
            source_files=sources, source_sha256=digest(sources), config_files=configs, config_sha256=digest(configs),
            broker_socket=str(root / 'synthetic-broker.sock'), broker_identity_sha256='b' * 64,
            limits=SharedDesktopLimits(cpu_quota_percent=200, memory_max_bytes=1073741824,
                                       tasks_max=128, stop_timeout_seconds=2))
        self.plan = prepare_plan(self.template, predecessor=None, new_session='app-' + '2' * 32)
        self.provision = provision_scope(self.plan, current_boot_id=self.boot)
        self.provision_path = Path(self.plan.session_directory) / PROVISION_NAME
        self.provision_sha = self.sha(self.provision_path.read_bytes())
        configs = {**configs, str(self.provision_path): self.provision_sha}
        self.launch_input = self.file('launch.json', canonical({'schema': 'scientist.native-launch-review.v1',
            'caller_unit': SHARED_DESKTOP_UNIT, 'factory_arguments': {
                'config_files': configs,
                'policy_path': str(self.policy), 'policy_file_sha256': configs[str(self.policy)],
                'profile_config_path': str(self.profile), 'profile_file_sha256': configs[str(self.profile)]}}).encode())
        configs = {**configs, str(self.launch_input): self.sha(self.launch_input.read_bytes())}
        self.activation = SharedDesktopActivation(plan_sha256=self.plan.plan_sha256(),
            reviewed_launch_input_path=str(self.launch_input), reviewed_launch_input_sha256=configs[str(self.launch_input)],
            source_files=sources, source_sha256=digest(sources), config_files=configs, config_sha256=digest(configs),
            boot_id=self.boot, issued_monotonic=100.0, expires_monotonic=200.0,
            capability_proof_sha256='c' * 64, source_proof_sha256='d' * 64)
        self.plan_path = self.file('plan.json', canonical(self.plan.model_dump(mode='json')).encode())
        self.activation_path = self.file('activation.json', canonical(self.activation.model_dump(mode='json')).encode())
        self.activation_sha = self.sha(self.activation_path.read_bytes())
        self.token_path = root / 'runs/desktop-console-5555555555555555.token'
        self.live = False
        self.states = []
        self.transport = Mock(spec=SystemdSharedDesktopTransport)
        self.transport.read.side_effect = lambda: self.service if self.live else None
        self.transport.start.side_effect = self.launch
        self.transport.token_path.return_value = self.token_path
        self.transport.stop.side_effect = self.terminate
        self.transport.stopped.side_effect = lambda binding: not self.live and binding == self.service
        self.clock = 150.0
        self.readback_edit = lambda actual: actual
        self.authority_claims = []
        self.host = SharedDesktopHost(transport=self.transport, activation_verifier=lambda plan, activation: None,
            activation_claimer=self.claim_authority,
            predecessor_verifier=lambda plan, predecessor: None, cleanup_prover=self.cleanup,
            readback=self.readback, lifecycle_reader=lambda path: (self.events, '6' * 64),
            process_observer=lambda process: 'same_process' if self.live else 'not_observed',
            clock=lambda: self.clock, boot_reader=lambda: self.boot)

    @staticmethod
    def sha(content):
        return hashlib.sha256(content).hexdigest()

    def file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        path.chmod(0o600)
        return path

    def claim_authority(self, plan, activation, state):
        if self.authority_claims or not self.states or self.states[-1] != state:
            raise ValueError('Synthetic claim requires original durable state and no prior consumption')
        marker = Path(plan.session_directory) / LAUNCH_INTENT_NAME
        if not marker.is_file() or self.live:
            raise AssertionError('Authority claim must follow durable intent and precede spawn')
        self.authority_claims.append(state.launch_intent_sha256)

    def launch(self, plan, activation):
        if self.authority_claims != [self.states[-1].launch_intent_sha256]:
            raise AssertionError('Launch preceded the distinct single-use authority claim')
        if not self.states or self.states[-1].phase != 'starting':
            raise AssertionError('Launch preceded durable intent')
        if not (Path(plan.session_directory) / LAUNCH_INTENT_NAME).exists():
            raise AssertionError('Launch preceded exclusive durable provision claim')
        self.live = True
        self.file('runs/' + self.token_path.name, b'SYNTHETIC_TOKEN_NOT_REAL_12345678901234567890')
        return self.service

    def terminate(self, binding, timeout):
        if binding != self.service:
            raise AssertionError('Wrong generation signalled')
        self.live = False
        self.token_path.unlink()

    def cleanup(self, state, stage):
        return SharedCleanupProof(state_sha256=digest(state.model_dump(mode='json')), service_binding=self.service,
            native_gpu_excluded=True, admission_closed=True, owned_runtime_removed=not self.live,
            tokens_removed=not self.token_path.exists(), evidence_sha256='7' * 64)

    def readback(self, state, plan):
        birth = LifecycleBirth(runtime_id='desktop-' + '8' * 32, container_name='aos-desktop-' + '9' * 20,
            image_id='sha256:' + 'a' * 64, source_sha256='b' * 64, workspace=state.workspace_identity,
            process=self.process)
        self.events = [LifecycleEvent(birth=birth, sequence=0, previous_sha256=None, stage='intent',
            container_id=None, recorded_at='2026-10-03T00:00:00Z'),
            LifecycleEvent(birth=birth, sequence=2, previous_sha256='c' * 64, stage='started',
                           container_id='d' * 64, recorded_at='2026-10-03T00:00:01Z')]
        binding = SessionRuntimeBinding(session_id='desktop-session-' + 'e' * 32, generation=1,
                                        birth=birth, container_id='d' * 64)
        lifecycle = LifecycleInspection(journal_sha256='6' * 64, birth_ref=digest(birth.model_dump()),
            runtime_ref=digest({'runtime_id': birth.runtime_id}), workspace_sha256=digest(birth.workspace.model_dump()),
            image_id=birth.image_id, source_sha256=birth.source_sha256, recorded_stage='started',
            owner_observation='same_process', workspace_busy=True, container_observation='running',
            observation_sha256='f' * 64, inspected_at='2026-10-03T00:00:02Z')
        inspection = SessionInspection(snapshot_sha256='a' * 64,
            session_ref=digest({'session_id': binding.session_id}), binding_sha256=digest(binding.model_dump()),
            session_status='running', session_owner='AGENT', generation=1, job_count=0, unresolved_inputs=0,
            lifecycle=lifecycle, inspected_at='2026-10-03T00:00:02Z')
        return self.readback_edit({'session': {'authenticated': True}, 'ui_status': 200,
            'state': {'control': {'session_id': binding.session_id, 'generation': 1, 'owner': 'AGENT',
                'status': 'running', 'runtime_id': birth.runtime_id},
                'runtime': {'runtime_id': birth.runtime_id, 'container_id': binding.container_id,
                            'running': True, 'workspace_identity': birth.workspace.model_dump(mode='json')}},
            'binding': inspection.model_dump(mode='json'),
            'tasks': {'busy': False, 'reserved': False, 'jobs': [], 'approval': None, 'auto_approval': None},
            'scientist': {'jobs': [], 'inference': {'configured': True, 'model_binding_valid': True,
                'wire_version': 1, 'admission_blocked': False, 'local_cleanup_pending': False,
                'unresolved_count': 0, 'unresolved_lab_effect_count': 0,
                'evidence_controls': {'available': True, 'supported': True, 'pending_count': 0}}}})

    def start(self):
        return self.host.start(str(self.plan_path), self.plan.plan_sha256(), str(self.activation_path),
                               self.activation_sha, provision_sha256=self.provision_sha,
                               predecessor=None, persist_state=self.states.append)

    def set_launch_input(self, config, *, extra_config_files=None):
        self.launch_input.write_text(canonical(config))
        configs = {**self.activation.config_files, **(extra_config_files or {}),
                   str(self.launch_input): self.sha(self.launch_input.read_bytes())}
        self.activation = self.activation.model_copy(update={'config_files': configs,
            'config_sha256': digest(configs), 'reviewed_launch_input_sha256': configs[str(self.launch_input)]})
        self.activation_path.write_text(canonical(self.activation.model_dump(mode='json')))
        self.activation_sha = self.sha(self.activation_path.read_bytes())


class SharedDesktopHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.fixture = SyntheticSharedHostFixture(self.root)
        self.patches = [patch('aos.shared_desktop_host.REPO_ROOT', self.root),
                        patch('aos.shared_desktop_host.FIXED_LAUNCHER', self.fixture.launcher),
                        patch('aos.shared_desktop_host.SCIENTIST_ROOT', self.root / 'synthetic-peer')]
        for replacement in self.patches:
            replacement.start()

    def tearDown(self):
        for replacement in reversed(self.patches):
            replacement.stop()
        self.temporary.cleanup()

    def test_real_host_composition_durable_start_authenticated_status_and_exact_stop(self):
        state = self.fixture.start()
        self.assertEqual(state.phase, 'running', state.last_error)
        self.assertEqual(state.service_binding.process, self.fixture.process)
        self.assertEqual(state.runtime_binding.birth.process, self.fixture.process)
        self.assertTrue(self.fixture.host.status(state)['task_admission_enabled'])
        self.assertTrue(self.fixture.host.token_value(state).startswith('SYNTHETIC_TOKEN'))
        stopped = self.fixture.host.stop(state, persist_state=self.fixture.states.append)
        self.assertEqual(stopped.phase, 'stopped', stopped.last_error)
        self.assertTrue(self.fixture.host.clean_shutdown(stopped))
        self.fixture.transport.start.assert_called_once()
        self.fixture.transport.stop.assert_called_once()

    def test_default_trusted_composition_and_changed_pins_deny_before_effect(self):
        self.fixture.host.activation_verifier = None
        with self.assertRaises(ValueError):
            self.fixture.start()
        self.assertEqual(self.fixture.states, [])
        self.fixture.transport.start.assert_not_called()
        self.assertTrue(Path(self.fixture.plan.workspace).exists())
        self.assertFalse((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).exists())
        self.fixture.host.activation_verifier = lambda plan, activation: None
        self.fixture.python.write_bytes(b'changed selected bytes')
        with self.assertRaises(ValueError):
            self.fixture.start()
        self.fixture.transport.start.assert_not_called()

    def test_missing_authority_claimer_denies_before_local_intent(self):
        self.fixture.host.activation_claimer = None
        with self.assertRaisesRegex(ValueError, 'claimer is unavailable'):
            self.fixture.start()
        self.assertEqual(self.fixture.states, [])
        self.assertFalse((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).exists())
        self.fixture.transport.start.assert_not_called()

    def test_authority_claim_lost_reply_retains_intent_without_spawn_or_retry(self):
        def lost_reply(plan, activation, state):
            self.fixture.claim_authority(plan, activation, state)
            raise TimeoutError('Synthetic consumed claim with lost reply')

        self.fixture.host.activation_claimer = lost_reply
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertEqual(self.fixture.authority_claims, [state.launch_intent_sha256])
        with self.assertRaises(ValueError):
            self.fixture.start()
        self.assertEqual(len(self.fixture.authority_claims), 1)
        self.fixture.transport.start.assert_not_called()

    def test_authority_claim_expiry_before_spawn_retains_uncertain_state(self):
        def expired_claim(plan, activation, state):
            self.fixture.claim_authority(plan, activation, state)
            self.fixture.clock = activation.expires_monotonic

        self.fixture.host.activation_claimer = expired_claim
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.fixture.transport.start.assert_not_called()

    def test_claim_status_only_is_not_permission_and_status_never_claims(self):
        self.fixture.host.activation_claimer = Mock(return_value={'status': 'already_consumed'})
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.fixture.host.status(state)
        self.fixture.host.activation_claimer.assert_called_once()
        self.fixture.transport.start.assert_not_called()

    def test_source_drift_after_claim_denies_before_spawn(self):
        def drift_after_claim(plan, activation, state):
            self.fixture.claim_authority(plan, activation, state)
            self.fixture.python.write_bytes(b'SYNTHETIC changed source during claim')

        self.fixture.host.activation_claimer = drift_after_claim
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.fixture.transport.start.assert_not_called()

    def test_revoked_authority_at_claim_retains_local_intent(self):
        self.fixture.host.activation_claimer = Mock(side_effect=ValueError('Synthetic revoked authority'))
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertTrue((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).is_file())
        self.fixture.transport.start.assert_not_called()

    def test_expired_authority_before_start_denies_and_after_start_preserves_ui_only(self):
        self.fixture.clock = 201.0
        with self.assertRaises(ValueError):
            self.fixture.start()
        self.fixture.clock = 150.0
        state = self.fixture.start()
        self.fixture.clock = 201.0
        status = self.fixture.host.status(state)
        self.assertTrue(status['ui_online'])
        self.assertFalse(status['task_admission_enabled'])
        self.assertIn('admission_blocker', status)
        stopped = self.fixture.host.stop(state, persist_state=self.fixture.states.append)
        self.assertEqual(stopped.phase, 'stopped')

    def test_lost_start_ack_remains_uncertain_without_retry_or_cleanup(self):
        self.fixture.transport.start.side_effect = TimeoutError('Synthetic lost ACK')
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertFalse(state.cleanup_verified)
        with self.assertRaises(ValueError):
            self.fixture.start()
        self.fixture.transport.start.assert_called_once()
        self.fixture.transport.stop.assert_not_called()

    def test_persist_failure_leaves_exclusive_marker_and_direct_host_replay_denied(self):
        def failed_persist(state):
            raise OSError('Synthetic durable state write failure')
        with self.assertRaises(OSError):
            self.fixture.host.start(str(self.fixture.plan_path), self.fixture.plan.plan_sha256(),
                str(self.fixture.activation_path), self.fixture.activation_sha,
                provision_sha256=self.fixture.provision_sha, predecessor=None, persist_state=failed_persist)
        marker = Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME
        self.assertTrue(marker.is_file())
        original = marker.read_bytes()
        with self.assertRaises(ValueError):
            self.fixture.start()
        self.assertEqual(marker.read_bytes(), original)
        self.fixture.transport.start.assert_not_called()

    def test_receipt_requires_both_effective_configuration_pins(self):
        original = self.fixture.activation
        configs = dict(original.config_files)
        del configs[str(self.fixture.provision_path)]
        activation = original.model_copy(update={'config_files': configs, 'config_sha256': digest(configs)})
        self.fixture.activation_path.write_text(canonical(activation.model_dump(mode='json')))
        self.fixture.activation_sha = self.fixture.sha(self.fixture.activation_path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'both activation'):
            self.fixture.start()
        launch = json.loads(self.fixture.launch_input.read_bytes())
        del launch['factory_arguments']['config_files'][str(self.fixture.provision_path)]
        self.fixture.launch_input.write_text(canonical(launch))
        configs = dict(original.config_files)
        configs[str(self.fixture.launch_input)] = self.fixture.sha(self.fixture.launch_input.read_bytes())
        activation = original.model_copy(update={'config_files': configs, 'config_sha256': digest(configs),
            'reviewed_launch_input_sha256': configs[str(self.fixture.launch_input)]})
        self.fixture.activation_path.write_text(canonical(activation.model_dump(mode='json')))
        self.fixture.activation_sha = self.fixture.sha(self.fixture.activation_path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'both activation'):
            self.fixture.start()
        self.fixture.transport.start.assert_not_called()
        self.assertFalse((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).exists())

    def test_optional_retained_review_complete_pin_is_accepted_without_minting_authority(self):
        retained = self.fixture.file('synthetic-retained.json', canonical({
            'schema': 'scientist.native-retained-factory-review.v1',
            'config_files': {str(self.fixture.provision_path): self.fixture.provision_sha}}).encode())
        fingerprint = self.fixture.sha(retained.read_bytes())
        launch = json.loads(self.fixture.launch_input.read_bytes())
        launch.update(retained_review_path=str(retained), retained_review_sha256=fingerprint)
        self.fixture.set_launch_input(launch, extra_config_files={str(retained): fingerprint})
        state = self.fixture.start()
        self.assertEqual(state.phase, 'running', state.last_error)
        self.assertEqual(state.provision_sha256, self.fixture.provision_sha)
        self.fixture.transport.start.assert_called_once()

    def test_retained_pair_activation_and_receipt_membership_fail_closed(self):
        original = json.loads(self.fixture.launch_input.read_bytes())
        retained = self.fixture.file('synthetic-retained.json', canonical({
            'schema': 'scientist.native-retained-factory-review.v1', 'config_files': {}}).encode())
        cases = [
            {'retained_review_path': str(retained)},
            {'retained_review_sha256': 'f' * 64},
            {'retained_review_path': str(retained), 'retained_review_sha256': 'f' * 64},
            {'retained_review_path': [], 'retained_review_sha256': 'f' * 64},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.fixture.set_launch_input({**original, **changes})
                with self.assertRaises(ValueError):
                    self.fixture.start()
        for review in (
            {'schema': 'scientist.native-retained-factory-review.v1', 'config_files': {}},
            {'schema': 'scientist.native-retained-factory-review.v1',
             'config_files': {str(self.fixture.provision_path): 'f' * 64}},
            {'schema': 'scientist.native-retained-factory-review.v1', 'config_files': []},
            {'schema': 'unsupported', 'config_files': {str(self.fixture.provision_path): self.fixture.provision_sha}},
        ):
            with self.subTest(review=review):
                retained.write_text(canonical(review))
                fingerprint = self.fixture.sha(retained.read_bytes())
                self.fixture.set_launch_input({**original, 'retained_review_path': str(retained),
                    'retained_review_sha256': fingerprint}, extra_config_files={str(retained): fingerprint})
                with self.assertRaises(ValueError):
                    self.fixture.start()
        self.fixture.transport.start.assert_not_called()
        self.assertFalse((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).exists())

    def test_malformed_factory_and_config_maps_raise_valueerror_before_launch(self):
        original = json.loads(self.fixture.launch_input.read_bytes())
        for factory in (None, [], 'invalid', {**original['factory_arguments'], 'config_files': []},
                        {**original['factory_arguments'], 'config_files': None},
                        {**original['factory_arguments'], 'policy_path': []}):
            with self.subTest(factory=factory):
                self.fixture.set_launch_input({**original, 'factory_arguments': factory})
                with self.assertRaises(ValueError):
                    self.fixture.start()
        self.fixture.transport.start.assert_not_called()
        self.assertEqual(self.fixture.states, [])

    def test_partial_scope_or_changed_provision_identity_never_adopted(self):
        database = Path(self.fixture.plan.database)
        database.write_bytes(b'SYNTHETIC partial artifact, not a database')
        with self.assertRaisesRegex(ValueError, 'Provision must contain only'):
            self.fixture.start()
        database.unlink()
        workspace = Path(self.fixture.plan.workspace)
        workspace.rename(workspace.with_name('old-workspace'))
        workspace.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'Provision directory'):
            self.fixture.start()
        self.fixture.transport.start.assert_not_called()

    def test_stale_boot_and_partial_marker_block_launch_without_authority_or_replay(self):
        self.fixture.host.boot_reader = lambda: '00000000-0000-0000-0000-000000000002'
        with self.assertRaises(ValueError):
            self.fixture.start()
        marker = Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME
        self.assertFalse(marker.exists())
        self.fixture.host.boot_reader = lambda: self.fixture.boot
        marker.write_bytes(b'{"synthetic_partial_intent":')
        marker.chmod(0o600)
        with self.assertRaises(ValueError):
            self.fixture.start()
        self.assertEqual(marker.read_bytes(), b'{"synthetic_partial_intent":')
        self.fixture.transport.start.assert_not_called()

    def test_post_claim_hook_artifact_insertion_stays_uncertain_without_transport(self):
        def persisted_with_unexpected_artifact(state):
            self.fixture.states.append(state)
            Path(self.fixture.plan.database).write_bytes(b'SYNTHETIC unauthorized pre-start artifact')
        result = self.fixture.host.start(str(self.fixture.plan_path), self.fixture.plan.plan_sha256(),
            str(self.fixture.activation_path), self.fixture.activation_sha,
            provision_sha256=self.fixture.provision_sha, predecessor=None,
            persist_state=persisted_with_unexpected_artifact)
        self.assertEqual(result.phase, 'uncertain')
        self.assertTrue((Path(self.fixture.plan.session_directory) / LAUNCH_INTENT_NAME).exists())
        self.fixture.transport.start.assert_not_called()

    def test_post_start_provision_replacement_blocks_status_token_and_stop(self):
        state = self.fixture.start()
        workspace = Path(self.fixture.plan.workspace)
        workspace.rename(workspace.with_name('old-workspace'))
        workspace.mkdir(mode=0o700)
        self.assertFalse(self.fixture.host.status(state)['ui_online'])
        with self.assertRaises(ValueError):
            self.fixture.host.token_value(state)
        with self.assertRaises(ValueError):
            self.fixture.host.stop(state, persist_state=self.fixture.states.append)
        self.fixture.transport.stop.assert_not_called()

    def test_http200_does_not_replace_current_controller_and_engine_proof(self):
        def invalid(actual):
            actual['scientist']['inference']['configured'] = False
            return actual
        self.fixture.readback_edit = invalid
        state = self.fixture.start()
        self.assertEqual(state.phase, 'uncertain')
        self.assertFalse(state.task_admission_enabled)
        self.fixture.transport.stop.assert_not_called()

    def test_reused_unit_generation_never_signalled_or_adopted(self):
        state = self.fixture.start()
        self.fixture.transport.read.side_effect = None
        self.fixture.transport.read.return_value = self.fixture.service.model_copy(update={'invocation_id': '4' * 32})
        with self.assertRaises(ValueError):
            self.fixture.host.stop(state, persist_state=self.fixture.states.append)
        self.fixture.transport.stop.assert_not_called()
        self.assertFalse(self.fixture.host.status(state)['ui_online'])

    def test_busy_and_unproven_gpu_or_closed_admission_refuse_stop(self):
        state = self.fixture.start()
        def busy(actual):
            actual['tasks']['busy'] = True
            return actual
        self.fixture.readback_edit = busy
        with self.assertRaises(ValueError):
            self.fixture.host.stop(state, persist_state=self.fixture.states.append)
        self.fixture.readback_edit = lambda actual: actual
        self.fixture.host.cleanup_prover = None
        for field in ('native_gpu_excluded', 'admission_closed'):
            self.fixture.host.cleanup_prover = lambda current, stage, field=field: self.fixture.cleanup(current, stage).model_copy(update={field: False})
            with self.assertRaises(ValueError):
                self.fixture.host.stop(state, persist_state=self.fixture.states.append)
        self.fixture.transport.stop.assert_not_called()

    def test_failed_physical_cleanup_never_claims_release_or_repeats_stop(self):
        state = self.fixture.start()
        self.fixture.host.cleanup_prover = lambda current, stage: self.fixture.cleanup(current, stage).model_copy(
            update={'owned_runtime_removed': False})
        result = self.fixture.host.stop(state, persist_state=self.fixture.states.append)
        self.assertEqual(result.phase, 'uncertain')
        self.assertFalse(result.cleanup_verified)
        with self.assertRaises(ValueError):
            self.fixture.host.stop(result, persist_state=self.fixture.states.append)
        self.fixture.transport.stop.assert_called_once()

    def test_completed_history_and_terminal_autoapproval_are_not_active_work(self):
        state = self.fixture.start()
        actual = self.fixture.readback(state, self.fixture.plan)
        actual['tasks'].update(jobs=[{'job_id': 'synthetic-job', 'status': 'succeeded'}],
                               auto_approval={'job_id': 'synthetic-job', 'kind': 'synthetic'})
        self.assertTrue(SharedDesktopHost._idle(actual))
        actual['tasks']['auto_approval']['job_id'] = 'other-job'
        self.assertFalse(SharedDesktopHost._idle(actual))
        actual['tasks']['auto_approval'] = None
        actual['scientist']['jobs'] = [{'actions': [{'state': 'intent'}]}]
        self.assertFalse(SharedDesktopHost._idle(actual))

    def test_fixed_launcher_arguments_have_no_native_fallback_or_acceptance_unit(self):
        command = SystemdSharedDesktopTransport.launch_command(self.fixture.plan, self.fixture.activation)
        self.assertEqual(command[0], '/usr/bin/systemd-run')
        self.assertIn('--collect', command)
        self.assertIn('--unit=' + SHARED_DESKTOP_UNIT, command)
        self.assertIn('--browser-tasks', command)
        self.assertEqual(command[command.index('--engine') + 1], 'scientist')
        self.assertNotIn('--prewarm-decider', command)
        self.assertNotIn('swapp-aos-gpu-joint-acceptance.service', ' '.join(command))

    def test_all_nineteen_paths_are_explicit_exact_session_children_for_default_and_named(self):
        named_base = self.root / 'data/local-app-project-synthetic'
        named_template = SharedDesktopTemplate.model_validate({**self.fixture.template.model_dump(),
            'project': 'synthetic', 'manager_base': str(named_base), 'session_root': str(named_base),
            'origin': 'http://127.0.0.1:8766'}, strict=True)
        named = prepare_plan(named_template, predecessor=None, new_session='app-' + '4' * 32)
        children = {
            '--workspace': 'workspace', '--database': 'trajectory.sqlite3',
            '--trajectory-database': 'trajectory.sqlite', '--web-profiles-root': 'web-applications',
            '--knowledge-root': 'knowledge', '--web-task-root': 'web-tasks',
            '--web-form-plan-root': 'form-plans', '--web-form-value-root': 'form-values',
            '--web-form-state-root': 'form-states', '--web-route-root': 'routes',
            '--web-static-root': 'static', '--web-readonly-data-root': 'readonly-data',
            '--console-assets-root': 'console-assets', '--site-knowledge-root': 'site-knowledge',
            '--site-skills-root': 'site-skills', '--page-seed-root': 'site-page-seeds',
            '--route-review-root': 'remote-route-reviews', '--json-review-root': 'remote-json-reviews',
            '--json-page-seed-root': 'json-page-seeds',
        }
        self.assertEqual(len(children), 19)
        for plan in (self.fixture.plan, named):
            with self.subTest(project=plan.template.project):
                command = SystemdSharedDesktopTransport.launch_command(plan, self.fixture.activation)
                for flag, name in children.items():
                    self.assertEqual(command.count(flag), 1)
                    selected = Path(command[command.index(flag) + 1])
                    self.assertEqual(selected, Path(plan.session_directory) / name)
                    self.assertEqual(selected.parent, Path(plan.session_directory))
                    self.assertNotEqual(selected, self.root / 'data' / name)
                self.assertNotEqual(command[command.index('--database') + 1],
                                    command[command.index('--trajectory-database') + 1])
        self.assertEqual(set(Path(self.fixture.plan.session_directory).iterdir()),
                         {self.fixture.provision_path, Path(self.fixture.plan.workspace)})
        self.assertFalse(Path(named.session_directory).exists())

    def test_schema_and_example_are_canonical_and_synthetic(self):
        repository = Path(__file__).resolve().parents[1]
        schema = json.loads((repository / 'schemas/shared_desktop_state_v2.schema.json').read_text())
        example = json.loads((repository / 'examples/shared_desktop_state_v2.json').read_text())
        self.assertEqual(schema, SharedDesktopState.model_json_schema())
        self.assertEqual(SharedDesktopState.model_validate(example, strict=True).phase, 'starting')
        self.assertIn('/synthetic/', example['workspace'])


class SharedDesktopTransportTests(unittest.TestCase):
    def setUp(self):
        self.process = ProcessIdentity(boot_id='00000000-0000-0000-0000-000000000001', pid=12345,
                                       start_ticks=12, pid_namespace=34, uid=os.getuid())
        self.binding = SharedServiceBinding(invocation_id='3' * 32, process=self.process,
                                            control_group='/synthetic/' + SHARED_DESKTOP_UNIT)

    def unit(self, *, phase='active', invocation=None):
        return ('Id=' + SHARED_DESKTOP_UNIT + '\nLoadState=loaded\nActiveState=' + phase
            + '\nSubState=' + ('running' if phase == 'active' else 'dead')
            + '\nMainPID=' + ('12345' if phase == 'active' else '0')
            + '\nInvocationID=' + (invocation or self.binding.invocation_id)
            + '\nControlGroup=' + self.binding.control_group + '\n').encode()

    def test_inactive_cleanup_requires_exact_original_invocation(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, self.unit(phase='inactive'), b''))
        transport = SystemdSharedDesktopTransport(runner=runner)
        self.assertTrue(transport.stopped(self.binding))
        runner.return_value.stdout = self.unit(phase='inactive', invocation='4' * 32)
        self.assertFalse(transport.stopped(self.binding))

    def test_pidfd_stop_never_systemctl_stops_unit_or_signals_reused_generation(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, self.unit(), b''))
        descriptor = os.open('/dev/null', os.O_RDONLY)
        observer = Mock(side_effect=['same_process', 'same_process', 'same_process', 'not_observed'])
        def signal_owned(*args):
            runner.return_value.stdout = self.unit(phase='inactive')
        signaller = Mock(side_effect=signal_owned)
        transport = SystemdSharedDesktopTransport(runner=runner, identity_reader=lambda pid: self.process,
            process_observer=observer, group_reader=lambda pid: self.binding.control_group,
            pidfd_open=lambda pid, flags: descriptor, pidfd_signal=signaller,
            waiter=lambda descriptors, write, error, timeout: (descriptors, [], []))
        transport.stop(self.binding, timeout=1)
        signaller.assert_called_once()
        self.assertTrue(all(call.args[0][2] == 'show' for call in runner.call_args_list))
        runner.return_value.stdout = self.unit(invocation='4' * 32)
        signaller.reset_mock()
        observer.side_effect = None
        observer.return_value = 'same_process'
        with self.assertRaises(ValueError):
            transport.stop(self.binding, timeout=1)
        signaller.assert_not_called()

    def test_token_receipt_polling_is_bounded_and_exact_mainpid_authenticated(self):
        elapsed = [0.0]
        rows = [b'', json.dumps({'_SYSTEMD_INVOCATION_ID': self.binding.invocation_id,
            '_SYSTEMD_USER_UNIT': SHARED_DESKTOP_UNIT, '_PID': str(self.process.pid), '_UID': str(os.getuid()),
            'MESSAGE': 'Local token file (0600): /synthetic/desktop-console-5555555555555555.token'}).encode()]
        def runner(arguments, **kwargs):
            content = self.unit() if arguments[0] == '/usr/bin/systemctl' else rows.pop(0)
            return subprocess.CompletedProcess(arguments, 0, content, b'')
        transport = SystemdSharedDesktopTransport(runner=runner, identity_reader=lambda pid: self.process,
            process_observer=lambda process: 'same_process', group_reader=lambda pid: self.binding.control_group,
            clock=lambda: elapsed[0], sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
        self.assertEqual(transport.token_path(self.binding).parent, Path('/synthetic'))
        self.assertGreater(elapsed[0], 0)
        rows.append(json.dumps({'_SYSTEMD_INVOCATION_ID': self.binding.invocation_id,
            '_SYSTEMD_USER_UNIT': SHARED_DESKTOP_UNIT, '_PID': '999', '_UID': str(os.getuid()),
            'MESSAGE': 'Local token file (0600): /synthetic/other.token'}).encode())
        with self.assertRaises(ValueError):
            transport.token_path(self.binding)


if __name__ == '__main__':
    unittest.main()

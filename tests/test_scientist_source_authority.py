"""Synthetic actual files and owned UDS; no Scientist imports, runtime or GPU."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from aos.desktop_control import DesktopController
from aos.scientist_admission_history import ScientistAdmissionBindingV2
from aos.scientist_bootstrap import CONTROL_DESCRIPTOR_SHA256
from aos.scientist_bootstrap_factory import ScientistBootstrapAdmissionFactory
from aos.scientist_source_authority import PROFILES, SOURCE_MINIMUM, ScientistConfiguredSourceVerifier
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError

from test_desktop_tasks import FixtureDesktop
from test_scientist_evidence_client import SyntheticEvidenceServer
import test_scientist_bootstrap as bootstrap_cases


class ScientistConfiguredSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = bootstrap_cases.ScientistBootstrapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.roots = {group: self.root / group for group in SOURCE_MINIMUM}
        self.sources = {}
        for group, minimum in SOURCE_MINIMUM.items():
            self.sources[group] = {}
            for relative in sorted(minimum):
                path = self.roots[group] / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                raw = ('Synthetic CPU source fixture: ' + group + '/' + relative + '\n').encode()
                path.write_bytes(raw)
                self.sources[group][relative] = hashlib.sha256(raw).hexdigest()
        self.config = self.root / 'synthetic-profile.json'
        self.config.write_text('{"synthetic":true}\n')
        self.configs = {self.config: hashlib.sha256(self.config.read_bytes()).hexdigest()}
        stable = deepcopy(self.fixture.stable)
        stable['source_fingerprints'] = {group: digest(entries) for group, entries in self.sources.items()}
        self.policy = {'schema': 'swapp-aos-control-policy.v1', 'enabled': True,
            'caller_unit': stable['caller_generation']['unit'], 'control_socket': str(self.root / 'evidence.sock'),
            'history_reconcile': False, 'source_files': deepcopy(self.sources),
            'profile_pins': {profile: deepcopy(stable['profile_pin']) for profile in PROFILES},
            'infer_schema_sha256': stable['infer_schema']['sha256'], 'control_schema_sha256': stable['control_schema']['sha256']}
        self.policy_path = self.root / 'synthetic-policy.json'
        self.policy_path.write_text(json.dumps(self.policy, indent=2))
        self.policy_path.chmod(0o600)
        stable['policy_sha256'] = digest(self.policy)
        self.fixture.stable = stable
        self.reviewed = {stable['profile_id']: stable}
        self.selected = {profile: ScientistAdmissionBindingV2.model_validate(value, strict=True)
                         for profile, value in self.reviewed.items()}
        self.runtime = Mock(return_value=None)
        self.verifier = self.make_verifier()

    def make_verifier(self, **changes):
        options = {'source_roots': self.roots, 'reviewed_bindings': self.reviewed, 'source_files': self.sources,
                   'config_files': self.configs, 'verify_runtime': self.runtime}
        return ScientistConfiguredSourceVerifier(self.policy_path, **(options | changes))

    def source_path(self):
        return self.roots['scientist'] / 'lab/llm/native_runtime.py'

    async def test_two_fresh_file_sweeps_and_current_runtime_bracketing(self):
        self.assertIsNone(self.verifier(self.selected))
        self.assertEqual(self.runtime.call_count, 3)
        self.assertEqual(self.runtime.call_args.args[0], self.selected)
        self.assertIsNot(self.runtime.call_args.args[0], self.selected)

    async def test_actual_factory_consumes_source_callback_and_prefetches_typed_capture(self):
        desktop = FixtureDesktop(self.root / 'workspace')
        desktop.start()
        self.addCleanup(desktop.stop)
        controller = DesktopController(self.fixture.store, desktop)
        state = controller.state()
        binding = self.fixture.binding.model_copy(update={key: state[key] for key in
            ('session_id', 'runtime_id', 'owner', 'lease_id', 'generation')})
        server = SyntheticEvidenceServer(self.root)
        self.addCleanup(server.close)
        factory = ScientistBootstrapAdmissionFactory(self.reviewed, server.path,
            control_descriptor_sha256=CONTROL_DESCRIPTOR_SHA256, verify_source=self.verifier,
            authenticator=self.fixture.authenticator, clock=lambda: self.fixture.now)
        factory.confirm_runtime({self.fixture.request.profile_id: self.fixture.request.deployment_digest})
        history = factory(controller)
        await history.capture.prepare_async(self.fixture.request, binding,
            factory.expected_peer(self.fixture.request, binding))
        self.assertEqual(len(server.requests), 1)
        self.assertIs(history.store, controller.store)
        self.assertEqual(history.record_version, '2.0')
        self.assertGreater(self.runtime.call_count, 3)

    async def test_outer_deadline_is_shared_by_every_source_read(self):
        from aos.scientist_source_authority import _read

        with patch('aos.scientist_source_authority.time.monotonic', return_value=10), \
                patch('aos.scientist_source_authority._read', wraps=_read) as reader:
            self.verifier(self.selected, deadline=10.25)
        self.assertGreater(reader.call_count, 2)
        self.assertTrue(all(call.args[2] == 10.25 for call in reader.call_args_list))

    async def test_long_outer_deadline_does_not_extend_local_limit(self):
        from aos.scientist_source_authority import _read

        with patch('aos.scientist_source_authority.time.monotonic', return_value=10), \
                patch('aos.scientist_source_authority._read', wraps=_read) as reader:
            self.verifier(self.selected, deadline=100)
        self.assertTrue(all(call.args[2] == 15 for call in reader.call_args_list))

    async def test_invalid_or_expired_outer_deadline_denies_before_runtime_and_reads(self):
        with patch('aos.scientist_source_authority.os.open') as opened:
            for deadline in [True, 'later', float('inf'), float('nan'), 10, 9]:
                with self.subTest(deadline=deadline), patch('aos.scientist_source_authority.time.monotonic', return_value=10):
                    with self.assertRaises(ScientistAdmissionError):
                        self.verifier(self.selected, deadline=deadline)
            opened.assert_not_called()
        self.runtime.assert_not_called()

    async def test_runtime_callback_cannot_renew_expired_outer_deadline(self):
        current = [10]
        self.runtime.side_effect = lambda selected: current.__setitem__(0, 10.5)
        with patch('aos.scientist_source_authority.time.monotonic', side_effect=lambda: current[0]), \
                patch('aos.scientist_source_authority.os.open') as opened:
            with self.assertRaises(ScientistAdmissionError):
                self.verifier(self.selected, deadline=10.25)
            opened.assert_not_called()
        self.runtime.assert_called_once()

    async def test_default_runtime_authority_denies_before_file_reads(self):
        from aos.scientist_source_authority import _deny_runtime
        verifier = self.make_verifier(verify_runtime=_deny_runtime)
        with patch('aos.scientist_source_authority.os.open') as opened:
            with self.assertRaises(ScientistAdmissionError): verifier(self.selected)
            opened.assert_not_called()

    async def test_boolean_or_revoked_runtime_gate_denies(self):
        self.runtime.return_value = True
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
        self.runtime.return_value = None
        self.runtime.side_effect = [None, None, ScientistAdmissionError('Synthetic runtime revoked')]
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)

    async def test_wrong_source_bytes_and_config_bytes_are_never_adopted(self):
        source = self.source_path()
        original = source.read_bytes()
        source.write_bytes(b'Synthetic changed source')
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
        source.write_bytes(original)
        self.config.write_text('{"synthetic":"changed"}')
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)

    async def test_source_and_config_drift_between_sweeps_deny(self):
        original = self.source_path().read_bytes()
        for path in (self.source_path(), self.config):
            count = 0
            def mutate(_selected):
                nonlocal count
                count += 1
                if count == 2:
                    path.write_bytes(b'Synthetic mid-verification drift')
            self.runtime.side_effect = mutate
            with self.subTest(path=path), self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
            self.source_path().write_bytes(original)

    async def test_policy_disabled_wrong_hash_source_map_or_profile_are_rejected(self):
        for change in ('enabled', 'source', 'profile', 'caller', 'hash'):
            policy = deepcopy(self.policy)
            if change == 'enabled': policy['enabled'] = False
            elif change == 'source': policy['source_files']['aos']['synthetic-extra.py'] = 'f' * 64
            elif change == 'profile': policy['profile_pins'][self.fixture.request.profile_id]['config_sha256'] = 'f' * 64
            elif change == 'caller': policy['caller_unit'] = 'swapp-aos-foreign.service'
            else: policy['control_socket'] += '.different'
            self.policy_path.write_text(canonical(policy))
            with self.subTest(change=change), self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)

    async def test_duplicate_nonfinite_and_nonobject_policy_json_denied(self):
        for raw in ('{"enabled":true,"enabled":false}', '{"value":NaN}', '{"value":1e999}', '[]'):
            self.policy_path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)

    async def test_foreign_selected_binding_denies_before_runtime_or_reads(self):
        selected = deepcopy(self.selected)
        profile = self.fixture.request.profile_id
        selected[profile] = selected[profile].model_copy(update={'policy_sha256': 'f' * 64})
        with patch('aos.scientist_source_authority.os.open') as opened:
            with self.assertRaises(ScientistAdmissionError): self.verifier(selected)
            opened.assert_not_called()
        self.runtime.assert_not_called()

    async def test_explicit_maps_are_copied_and_runtime_cannot_replace_reviewed_pins(self):
        self.sources['scientist']['lab/llm/native_runtime.py'] = 'f' * 64
        self.reviewed[self.fixture.request.profile_id]['policy_sha256'] = 'e' * 64
        def mutate(selected):
            selected.clear()
        self.runtime.side_effect = mutate
        self.assertIsNone(self.verifier(self.selected))

    async def test_symlink_leaf_parent_and_hardlink_are_denied(self):
        source = self.source_path()
        backup = source.with_suffix('.synthetic-backup')
        source.rename(backup)
        source.symlink_to(backup)
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
        source.unlink()
        os.link(backup, source)
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
        source.unlink()
        backup.rename(source)
        parent = source.parent
        moved = parent.with_name('synthetic-moved')
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)

    async def test_private_policy_regular_files_and_byte_deadline_bounds(self):
        self.policy_path.chmod(0o644)
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
        self.policy_path.chmod(0o600)
        with patch('aos.scientist_source_authority.TOTAL_LIMIT', 1):
            with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
        with patch('aos.scientist_source_authority.time.monotonic', side_effect=[0, 6]):
            with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)

    async def test_missing_minimum_traversal_config_and_shared_generation_fail_closed(self):
        for kind in ('minimum', 'traversal', 'config', 'fingerprint', 'generation'):
            sources, reviewed, configs = deepcopy(self.sources), deepcopy(self.reviewed), deepcopy(self.configs)
            if kind == 'minimum': del sources['scientist']['lab/llm/native_runtime.py']
            elif kind == 'traversal': sources['aos']['../escape.py'] = 'f' * 64
            elif kind == 'config': configs = {}
            elif kind == 'fingerprint': reviewed[self.fixture.request.profile_id]['source_fingerprints']['aos'] = 'f' * 64
            else:
                other = deepcopy(reviewed[self.fixture.request.profile_id])
                other['profile_id'] = 'aos.bonsai.vision.v1'
                other['server_generation']['start_ticks'] += 1
                reviewed[other['profile_id']] = other
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError):
                self.make_verifier(source_files=sources, reviewed_bindings=reviewed, config_files=configs)

    async def test_actual_policy_profile_set_and_evidence_pair_shape_are_required_even_with_reviewed_hash(self):
        for change in ('profiles', 'evidence', 'transport', 'retained', 'caller'):
            policy = deepcopy(self.policy)
            reviewed = deepcopy(self.reviewed)
            if change == 'profiles': del policy['profile_pins']['aos.bonsai.vision.v1']
            elif change == 'evidence': policy['evidence_schema_sha256'] = 'f' * 64
            elif change == 'transport': policy['evidence_transport_schema_sha256'] = 'f' * 64
            elif change == 'retained': policy['retained_evidence_transport_schema_sha256'] = 'f' * 64
            else:
                policy['caller_unit'] = 'synthetic-invalid.service'
                reviewed[self.fixture.request.profile_id]['caller_generation']['unit'] = policy['caller_unit']
            self.policy_path.write_text(canonical(policy))
            reviewed[self.fixture.request.profile_id]['policy_sha256'] = digest(policy)
            verifier = self.make_verifier(reviewed_bindings=reviewed)
            selected = {profile: ScientistAdmissionBindingV2.model_validate(value, strict=True) for profile, value in reviewed.items()}
            with self.subTest(change=change), self.assertRaises(ScientistAdmissionError): verifier(selected)

    async def test_policy_revocation_in_final_runtime_check_is_reread_before_success(self):
        count = 0
        def revoke(_selected):
            nonlocal count
            count += 1
            if count == 3:
                self.policy_path.write_text(canonical(self.policy | {'enabled': False}))
        self.runtime.side_effect = revoke
        with self.assertRaises(ScientistAdmissionError): self.verifier(self.selected)
        self.assertEqual(count, 3)


if __name__ == '__main__':
    unittest.main()

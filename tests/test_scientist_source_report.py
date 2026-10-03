import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import scientist_source_report as report


class ScientistSourceReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Synthetic',
                        '-c', 'user.email=synthetic@example.invalid', 'commit',
                        '--allow-empty', '-qm', 'synthetic'], check=True)
        for name in report.SOURCE_FILES:
            source = self.root / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('synthetic source\n')

    def test_untracked_source_is_identified_without_admission_or_private_status(self):
        (self.root / 'private-token.txt').write_text('synthetic secret')
        observation = report.source_report(self.root)
        self.assertTrue(observation['checkout_dirty'])
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertNotIn('private-token', str(observation))
        self.assertEqual(len(report.SOURCE_FILES), len(observation['source_sha256']))
        first = report.SOURCE_FILES[0]
        self.assertEqual(observation['source_sha256'][first],
                         hashlib.sha256(b'synthetic source\n').hexdigest())

    def test_selected_untracked_edit_changes_source_fingerprint_not_git_diff(self):
        before = report.source_report(self.root)
        (self.root / report.SOURCE_FILES[0]).write_text('changed synthetic source\n')
        after = report.source_report(self.root)
        self.assertEqual(before['tracked_diff_sha256'], after['tracked_diff_sha256'])
        self.assertNotEqual(before['selected_source_sha256'], after['selected_source_sha256'])

    def test_concurrent_edit_rejects_observation(self):
        original = report.read_source_member
        reads = 0

        def read(root, name):
            nonlocal reads
            reads += 1
            if reads == len(report.SOURCE_FILES) + 1:
                (root / name).write_text('concurrent synthetic edit\n')
            return original(root, name)

        with patch.object(report, 'read_source_member', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'changed'):
                report.source_report(self.root)

    def test_symlink_and_missing_source_are_rejected(self):
        source = self.root / report.SOURCE_FILES[0]
        source.unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root)
        source.symlink_to(self.root / report.SOURCE_FILES[1])
        with self.assertRaises(OSError):
            report.source_report(self.root)

    def test_nested_checkout_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exact'):
            report.source_report(self.root / 'scripts')

    def provision_v2(self):
        for name in report.SOURCE_PROFILES['admission_v2']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic version2 source\n')

    def test_v2_profile_covers_new_guards_schemas_and_unchanged_migrations(self):
        self.provision_v2()
        observation = report.source_report(self.root, source_profile='admission_v2')
        self.assertEqual(observation['source_profile'], 'admission_v2')
        self.assertEqual(set(observation['source_sha256']), set(report.SOURCE_PROFILES['admission_v2']))
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertIn('src/aos/scientist_profile_output.py', observation['source_sha256'])
        self.assertIn('schemas/scientist_admission_record_v2.schema.json', observation['source_sha256'])
        self.assertIn('database/migrations/0023_scientist_admission_history.sql', observation['source_sha256'])

    def test_expected_pin_rejects_guard_change_hidden_from_legacy_selected_profile(self):
        self.provision_v2()
        original = report.source_report(self.root, source_profile='admission_v2')
        pinned = report.source_report(self.root, source_profile='admission_v2',
                                      expected_selected_source_sha256=original['selected_source_sha256'])
        self.assertEqual(pinned, original)
        legacy = report.source_report(self.root)
        (self.root / 'src/aos/scientist_profile_output.py').write_text('changed synthetic output guard\n')
        self.assertEqual(report.source_report(self.root)['selected_source_sha256'], legacy['selected_source_sha256'])
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='admission_v2',
                                 expected_selected_source_sha256=original['selected_source_sha256'])

    def test_profiles_and_expected_pins_cannot_expand_source_selection(self):
        for profile in ('../private', '', None, True):
            with self.assertRaises(ValueError):
                report.source_report(self.root, source_profile=profile)
        for checksum in ('A' * 64, 'a' * 63, True, '../private'):
            with self.assertRaises(ValueError):
                report.source_report(self.root, expected_selected_source_sha256=checksum)

    def test_v2_missing_schema_does_not_fall_back_to_legacy_profile(self):
        self.provision_v2()
        (self.root / 'schemas/scientist_admission_record_v2.schema.json').unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='admission_v2')

    def test_terminal_candidate_requires_separate_module_and_full_schema(self):
        self.provision_v2()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='terminal_candidate_v1')
        for name in report.SOURCE_PROFILES['terminal_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic terminal candidate source\n')
        observation = report.source_report(self.root, source_profile='terminal_candidate_v1')
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertIn('src/aos/scientist_terminal.py', observation['source_sha256'])
        self.assertIn('schemas/scientist_terminal_evidence.schema.json', observation['source_sha256'])

    def test_evidence_transport_profile_requires_codec_and_request_schema_without_fallback(self):
        self.provision_v2()
        for name in report.SOURCE_PROFILES['terminal_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic terminal source\n')
        legacy = report.source_report(self.root, source_profile='terminal_candidate_v1')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='evidence_transport_candidate_v2')

        for name in report.SOURCE_PROFILES['evidence_transport_candidate_v2']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic evidence transport source\n')
        observation = report.source_report(self.root, source_profile='evidence_transport_candidate_v2')
        self.assertEqual(len(observation['source_sha256']), len(legacy['source_sha256']) + 3)
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertEqual(report.source_report(self.root, source_profile='terminal_candidate_v1'), legacy)
        codec = self.root / 'src/aos/scientist_evidence_transport.py'
        codec.write_text('changed synthetic codec\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='evidence_transport_candidate_v2',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='terminal_candidate_v1'), legacy)
        (self.root / 'schemas/scientist_evidence_transport_request.schema.json').unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='evidence_transport_candidate_v2')

    def test_evidence_client_profile_adds_authenticated_dispatch_source_explicitly(self):
        for name in report.SOURCE_PROFILES['evidence_transport_candidate_v2']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic evidence transport source\n')
        transport = report.source_report(self.root, source_profile='evidence_transport_candidate_v2')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='evidence_client_candidate_v2')
        client = self.root / 'src/aos/scientist_evidence_client.py'
        client.write_text('synthetic authenticated dispatch source\n')
        observation = report.source_report(self.root, source_profile='evidence_client_candidate_v2')
        self.assertEqual(len(observation['source_sha256']), len(transport['source_sha256']) + 1)
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        client.write_text('changed synthetic dispatch source\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='evidence_client_candidate_v2',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='evidence_transport_candidate_v2'), transport)

    def test_retained_host_profile_requires_explicit_trusted_host_composition(self):
        for name in report.SOURCE_PROFILES['resolution_candidate_v3']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic resolution source\n')
        legacy = report.source_report(self.root, source_profile='resolution_candidate_v3')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='retained_host_candidate_v3')
        host = self.root / 'src/aos/scientist_retained_host.py'
        host.write_text('synthetic retained host\n')
        observation = report.source_report(self.root, source_profile='retained_host_candidate_v3')
        self.assertEqual(len(observation['source_sha256']), 55)
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertEqual(report.source_report(self.root, source_profile='resolution_candidate_v3'), legacy)
        host.write_text('changed synthetic host\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='retained_host_candidate_v3',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        host.unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='retained_host_candidate_v3')

    def test_physical_observer_profile_pins_bounded_subprocess_without_rewriting_host_membership(self):
        for name in report.SOURCE_PROFILES['retained_host_candidate_v3']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic retained host source\n')
        legacy = report.source_report(self.root, source_profile='retained_host_candidate_v3')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='physical_observer_candidate_v1')
        helper = self.root / 'src/aos/bounded_process.py'
        helper.write_text('synthetic bounded observer dependency\n')
        observed = report.source_report(self.root, source_profile='physical_observer_candidate_v1')
        self.assertEqual(len(observed['source_sha256']), 56)
        self.assertEqual(set(observed['source_sha256']) - set(legacy['source_sha256']),
                         {'src/aos/bounded_process.py'})
        self.assertFalse(observed['admission_allowed'])
        self.assertFalse(observed['gpu_release_verified'])
        helper.write_text('changed synthetic observer dependency\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='physical_observer_candidate_v1',
                                 expected_selected_source_sha256=observed['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='retained_host_candidate_v3'), legacy)
        helper.unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='physical_observer_candidate_v1')

    def test_bootstrap_capture_profile_requires_explicit_source_without_changing_physical_profile(self):
        for name in report.SOURCE_PROFILES['physical_observer_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic physical source\n')
        legacy = report.source_report(self.root, source_profile='physical_observer_candidate_v1')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='bootstrap_capture_candidate_v1')
        capture = self.root / 'src/aos/scientist_bootstrap.py'
        capture.write_text('synthetic bootstrap capture source\n')
        observation = report.source_report(self.root, source_profile='bootstrap_capture_candidate_v1')
        self.assertEqual(len(observation['source_sha256']), 57)
        self.assertEqual(set(observation['source_sha256']) - set(legacy['source_sha256']),
                         {'src/aos/scientist_bootstrap.py'})
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        capture.write_text('changed synthetic bootstrap source\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='bootstrap_capture_candidate_v1',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='physical_observer_candidate_v1'), legacy)

    def test_retained_provider_profile_pins_client_without_promoting_bootstrap_profile(self):
        for name in report.SOURCE_PROFILES['bootstrap_capture_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic bootstrap source\n')
        legacy = report.source_report(self.root, source_profile='bootstrap_capture_candidate_v1')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='retained_provider_candidate_v1')
        provider = self.root / 'src/aos/scientist_retained_provider.py'
        provider.write_text('synthetic retained provider client\n')
        observation = report.source_report(self.root, source_profile='retained_provider_candidate_v1')
        self.assertEqual(len(observation['source_sha256']), 58)
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        provider.write_text('changed synthetic retained provider client\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='retained_provider_candidate_v1',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='bootstrap_capture_candidate_v1'), legacy)

    def test_bootstrap_factory_requires_reviewed_composition_source_without_changing_provider_membership(self):
        for name in report.SOURCE_PROFILES['retained_provider_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic retained provider source\n')
        legacy = report.source_report(self.root, source_profile='retained_provider_candidate_v1')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='bootstrap_factory_candidate_v1')
        factory = self.root / 'src/aos/scientist_bootstrap_factory.py'
        factory.write_text('synthetic reviewed bootstrap factory\n')
        observed = report.source_report(self.root, source_profile='bootstrap_factory_candidate_v1')
        self.assertEqual(len(observed['source_sha256']), 59)
        self.assertFalse(observed['admission_allowed'])
        self.assertFalse(observed['gpu_release_verified'])
        factory.write_text('changed synthetic bootstrap factory\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='bootstrap_factory_candidate_v1',
                                 expected_selected_source_sha256=observed['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='retained_provider_candidate_v1'), legacy)

    def test_configured_source_profile_requires_independent_verifier_without_changing_factory_membership(self):
        for name in report.SOURCE_PROFILES['bootstrap_factory_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic reviewed bootstrap source\n')
        legacy = report.source_report(self.root, source_profile='bootstrap_factory_candidate_v1')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='configured_source_candidate_v1')
        authority = self.root / 'src/aos/scientist_source_authority.py'
        authority.write_text('synthetic independently reviewed source authority\n')
        observed = report.source_report(self.root, source_profile='configured_source_candidate_v1')
        self.assertEqual(len(observed['source_sha256']), 60)
        self.assertFalse(observed['admission_allowed'])
        self.assertFalse(observed['gpu_release_verified'])
        authority.write_text('changed synthetic authority\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='configured_source_candidate_v1',
                                 expected_selected_source_sha256=observed['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='bootstrap_factory_candidate_v1'), legacy)

    def test_lab_history_profile_pins_append_only_record_schema_and_migration(self):
        for name in report.SOURCE_PROFILES['configured_source_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic reviewed configured source\n')
        legacy = report.source_report(self.root, source_profile='configured_source_candidate_v1')
        added = report.SOURCE_PROFILES['lab_readback_history_candidate_v1'][60:]
        for name in added:
            with self.assertRaises(OSError):
                report.source_report(self.root, source_profile='lab_readback_history_candidate_v1')
            source = self.root / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('synthetic immutable readback source\n')
        observed = report.source_report(self.root, source_profile='lab_readback_history_candidate_v1')
        self.assertEqual(len(observed['source_sha256']), 63)
        self.assertFalse(observed['admission_allowed'])
        self.assertFalse(observed['gpu_release_verified'])
        (self.root / added[2]).write_text('changed synthetic migration\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='lab_readback_history_candidate_v1',
                                 expected_selected_source_sha256=observed['selected_source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='configured_source_candidate_v1'), legacy)

    def test_resolution_profile_requires_explicit_original_journal_resolution_sources(self):
        for name in report.SOURCE_PROFILES['retained_evidence_candidate_v3']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic retained transport source\n')
        legacy = report.source_report(self.root, source_profile='retained_evidence_candidate_v3')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='resolution_candidate_v3')
        for name in report.SOURCE_PROFILES['resolution_candidate_v3']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic resolution source\n')
        observation = report.source_report(self.root, source_profile='resolution_candidate_v3')
        self.assertEqual(len(observation['source_sha256']), 54)
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertEqual(report.source_report(self.root, source_profile='retained_evidence_candidate_v3'), legacy)
        resolver = self.root / 'src/aos/scientist_resolution.py'
        resolver.write_text('changed synthetic resolution source\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='resolution_candidate_v3',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        resolver.unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='resolution_candidate_v3')

    def test_retained_v3_profile_requires_new_codec_schema_and_additive_migration(self):
        for name in report.SOURCE_PROFILES['release_proof_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic proof source\n')
        legacy = report.source_report(self.root, source_profile='release_proof_candidate_v1')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='retained_evidence_candidate_v3')
        for name in report.SOURCE_PROFILES['retained_evidence_candidate_v3']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic retained transport source\n')
        observation = report.source_report(self.root, source_profile='retained_evidence_candidate_v3')
        self.assertEqual(len(observation['source_sha256']), 52)
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertEqual(report.source_report(self.root, source_profile='release_proof_candidate_v1'), legacy)
        migration = self.root / 'database/migrations/0025_scientist_retained_evidence_controls.sql'
        migration.write_text('changed synthetic retained migration\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='retained_evidence_candidate_v3',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        migration.unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='retained_evidence_candidate_v3')

    def test_release_proof_profile_requires_budget_and_physical_verifier_sources(self):
        for name in report.SOURCE_PROFILES['evidence_journal_candidate_v2']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic journal source\n')
        legacy = report.source_report(self.root, source_profile='evidence_journal_candidate_v2')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='release_proof_candidate_v1')
        for name in report.SOURCE_PROFILES['release_proof_candidate_v1']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic proof source\n')
        observation = report.source_report(self.root, source_profile='release_proof_candidate_v1')
        self.assertEqual(len(observation['source_sha256']), 48)
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertEqual(report.source_report(self.root, source_profile='evidence_journal_candidate_v2'), legacy)
        (self.root / 'src/aos/scientist_budget_witness.py').write_text('changed synthetic budget\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='release_proof_candidate_v1',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        (self.root / 'schemas/scientist_release_proof.schema.json').unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='release_proof_candidate_v1')

    def test_evidence_journal_profile_requires_original_store_migration_and_audit_sources(self):
        for name in report.SOURCE_PROFILES['evidence_client_candidate_v2']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic evidence client source\n')
        client = report.source_report(self.root, source_profile='evidence_client_candidate_v2')
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='evidence_journal_candidate_v2')
        for name in report.SOURCE_PROFILES['evidence_journal_candidate_v2']:
            source = self.root / name
            if not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('synthetic original-store journal source\n')
        observation = report.source_report(self.root, source_profile='evidence_journal_candidate_v2')
        self.assertFalse(observation['admission_allowed'])
        self.assertFalse(observation['gpu_release_verified'])
        self.assertEqual(len(observation['source_sha256']), len(client['source_sha256']) + 5)
        self.assertIn('database/migrations/0024_scientist_evidence_controls.sql', observation['source_sha256'])
        self.assertEqual(report.source_report(self.root, source_profile='evidence_client_candidate_v2'), client)
        (self.root / 'src/aos/scientist_evidence_journal.py').write_text('changed synthetic journal\n')
        with self.assertRaisesRegex(ValueError, 'expected pin'):
            report.source_report(self.root, source_profile='evidence_journal_candidate_v2',
                                 expected_selected_source_sha256=observation['selected_source_sha256'])
        (self.root / 'database/migrations/0024_scientist_evidence_controls.sql').unlink()
        with self.assertRaises(OSError):
            report.source_report(self.root, source_profile='evidence_journal_candidate_v2')

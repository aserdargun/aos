import hashlib
import json
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from aos.contracts import digest
from aos.owned_form_candidate_execution import (
    load_candidate_execution_bundle, load_candidate_execution_replay_inventory,
    persist_candidate_execution_bundle, persist_candidate_execution_completion)
import test_owned_candidate_execution_persistence as persistence_fixture


class OwnedCandidateReplayInventoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = persistence_fixture.OwnedCandidateExecutionPersistenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def inventory(self, **changes):
        arguments = {
            'source_run_ref': self.fixture.source_run_ref,
            'source_invocation_sha256': '5' * 64,
            'source_manifest_sha256': '6' * 64,
        }
        arguments.update(changes)
        return load_candidate_execution_replay_inventory(self.fixture.root, **arguments)

    def version(self, version):
        fixture = self.fixture
        preview = {
            'schema_version': version, 'available': True, 'status': 'preview',
            'candidate_sha256': fixture.candidate_sha256,
            'source_run_ref': fixture.source_run_ref,
            'source_invocation_sha256': '5' * 64,
            'source_group_sha256': fixture.candidate['source_group_sha256'],
            'profile_sha256': fixture.candidate['profile_sha256'],
            'skill_sha256': fixture.invocation['skill_sha256'],
            'case_key': fixture.invocation['case_key'],
            'parameter_variant_sha256': fixture.parameter_variant_sha256,
            'invocation_sha256': fixture.invocation_sha256,
            'recipe_sha256': digest(fixture.recipe),
            'steps': fixture.invocation['steps'],
            'purpose': 'development_variation', 'independent_held_out': False,
            'form_plan_sha256': digest(fixture.form_plan),
            'state_plan_sha256': digest(fixture.state_plan), 'report': None,
            'review_sha256': 'a' * 64,
        }
        pins = {'review_sha256': 'a' * 64}
        if version == '1.2':
            pins.update({'release_sha256': 'b' * 64, 'selection_sha256': 'c' * 64})
            preview.update(pins)
        prepared = dict(fixture.prepared, preview_sha256=digest(preview))
        identity = {
            'preview_sha256': prepared['preview_sha256'],
            'admission_sha256': digest(fixture.admission),
            'source_run_ref': fixture.source_run_ref,
        }
        if version == '1.2':
            identity.update({key: pins[key] for key in ('release_sha256', 'selection_sha256')})
        execution_sha256 = digest(identity)
        directory, _checksum = persist_candidate_execution_bundle(
            fixture.candidate_directory, fixture.candidate_sha256, execution_sha256,
            prepared, persistence_fixture.DumpValue(fixture.admission), **pins)
        return directory, execution_sha256, prepared['preview_sha256']

    def test_all_versions_burn_previews_without_completion_or_network(self):
        reviewed = self.version('1.1')
        selected = self.version('1.2')
        before = {str(path.relative_to(self.fixture.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.fixture.root.rglob('*') if path.is_file()}
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
            inventory = self.inventory()
        self.assertEqual(inventory, {
            'direct': frozenset((self.fixture.prepared['preview_sha256'], reviewed[2])),
            'selected': frozenset((selected[2],)),
        })
        after = {str(path.relative_to(self.fixture.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.fixture.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(any(self.fixture.root.rglob('completion.json')))

    def test_completed_and_noncompleted_previews_are_both_consumed(self):
        directory, execution_sha256, preview_sha256 = self.version('1.2')
        persist_candidate_execution_completion(
            directory, execution_sha256, self.fixture.candidate_sha256,
            self.fixture.invocation_sha256, 'job-new', 'run-new')
        self.assertEqual(self.inventory()['selected'], frozenset((preview_sha256,)))
        self.assertEqual(self.inventory()['direct'], frozenset((self.fixture.prepared['preview_sha256'],)))

    def test_fresh_network_forbidden_process_recovers_persisted_previews(self):
        selected = self.version('1.2')
        process = subprocess.run([sys.executable, '-c', '''
import json, sys
from pathlib import Path
from unittest.mock import patch
from aos.owned_form_candidate_execution import load_candidate_execution_replay_inventory
with patch('socket.socket', side_effect=RuntimeError('network forbidden')):
    inventory = load_candidate_execution_replay_inventory(Path(sys.argv[1]),
        source_run_ref=sys.argv[2], source_invocation_sha256='5' * 64,
        source_manifest_sha256='6' * 64)
print(json.dumps({lane: sorted(previews) for lane, previews in inventory.items()}))
''', str(self.fixture.root), self.fixture.source_run_ref],
            capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {
            'direct': [self.fixture.prepared['preview_sha256']], 'selected': [selected[2]],
        })

    def test_source_pin_mismatches_reject_whole_inventory(self):
        for key in ('source_run_ref', 'source_invocation_sha256', 'source_manifest_sha256'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'replay_source_changed'):
                self.inventory(**{key: 'f' * 64})

    def test_invalid_arguments_are_rejected(self):
        for arguments in ({'source_run_ref': '../escape'}, {'source_manifest_sha256': None},
                          {'allow_missing': 1}):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, 'replay_source_invalid'):
                self.inventory(**arguments)

    def test_missing_root_requires_explicit_fresh_source(self):
        self.fixture.candidate_directory.rename(self.fixture.root / 'retained')
        with self.assertRaisesRegex(ValueError, 'replay_inventory_missing'):
            self.inventory()
        self.assertEqual(self.inventory(allow_missing=True),
                         {'direct': frozenset(), 'selected': frozenset()})
        self.assertFalse(self.fixture.candidate_directory.exists())

    def test_partial_bundle_cannot_be_ignored_even_for_fresh_source(self):
        partial = self.fixture.candidate_directory / ('f' * 64)
        partial.mkdir(mode=0o700)
        for allow_missing in (False, True):
            with self.subTest(allow_missing=allow_missing), self.assertRaises(FileNotFoundError):
                self.inventory(allow_missing=allow_missing)

    def test_changed_artifact_cannot_be_ignored(self):
        (self.fixture.bundle_directory / 'form-body.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'file_changed'):
            self.inventory()

    def test_unexpected_entry_is_rejected(self):
        (self.fixture.candidate_directory / 'unrecognized').mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'replay_inventory_invalid'):
            self.inventory()

    def test_symlink_entry_is_rejected(self):
        (self.fixture.candidate_directory / ('f' * 64)).symlink_to(self.fixture.bundle_directory)
        with self.assertRaisesRegex(ValueError, 'replay_directory_invalid'):
            self.inventory()

    def test_symlink_inventory_root_is_rejected(self):
        retained = self.fixture.root / 'retained'
        self.fixture.candidate_directory.rename(retained)
        self.fixture.candidate_directory.symlink_to(retained)
        with self.assertRaises(OSError):
            self.inventory(allow_missing=True)

    def test_public_directory_is_rejected(self):
        for directory in (self.fixture.root, self.fixture.candidate_directory,
                          self.fixture.bundle_directory):
            with self.subTest(directory=directory.name):
                directory.chmod(0o755)
                try:
                    with self.assertRaisesRegex(ValueError, 'replay_directory_invalid'):
                        self.inventory()
                finally:
                    directory.chmod(0o700)

    def test_concurrent_entry_added_during_scan_is_rejected(self):
        def racing_load(*arguments):
            loaded = load_candidate_execution_bundle(*arguments)
            (self.fixture.candidate_directory / ('f' * 64)).mkdir(mode=0o700)
            return loaded

        with patch('aos.owned_form_candidate_execution.load_candidate_execution_bundle',
                   side_effect=racing_load), self.assertRaisesRegex(ValueError, 'replay_inventory_changed'):
            self.inventory()

    def test_replaced_inventory_path_is_rejected(self):
        def racing_load(*arguments):
            loaded = load_candidate_execution_bundle(*arguments)
            self.fixture.candidate_directory.rename(self.fixture.root / 'retained')
            self.fixture.candidate_directory.mkdir(mode=0o700)
            return loaded

        with patch('aos.owned_form_candidate_execution.load_candidate_execution_bundle',
                   side_effect=racing_load), self.assertRaisesRegex(ValueError, 'replay_.*changed'):
            self.inventory()

    def test_inventory_size_is_bounded_before_loading_artifacts(self):
        for number in range(256):
            (self.fixture.candidate_directory / f'{number:064x}').mkdir(mode=0o700)
        with patch('aos.owned_form_candidate_execution.load_candidate_execution_bundle') as loader:
            with self.assertRaisesRegex(ValueError, 'replay_inventory_invalid'):
                self.inventory()
            loader.assert_not_called()

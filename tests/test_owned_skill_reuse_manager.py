"""Manager handoff tests with synthetic audit results, not runtime acceptance."""

import json
import os
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from aos import local_app
from aos.contracts import REPO_ROOT, canonical, digest
from aos.lifecycle import process_identity
from aos.owned_learning_workspace import OwnedLearningWorkspace


class ReservedSocket:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.address = None

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        return None

    def setsockopt(self, *_arguments):
        return None

    def bind(self, address):
        self.address = address

    def listen(self, _backlog):
        return None

    def fileno(self):
        return self.descriptor


class OwnedSkillReuseManagerTests(unittest.TestCase):
    def setUp(self):
        self.base = REPO_ROOT / 'data' / ('local-app-test-' + uuid4().hex)
        self.base.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.base)
        patcher = patch('aos.local_app.BASE', self.base)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.previous = local_app.LocalAppState(
            session='app-' + 'a' * 32, mode='real', phase='stopped',
            supervisor=process_identity(os.getpid()), started_at='synthetic',
            owned_synthetic_form_invocation=True,
            owned_form_manifest_sha256='a' * 64,
            owned_form_invocation_sha256='b' * 64)
        self.source = self.base / self.previous.session
        self.source.mkdir(mode=0o700)
        (self.source / 'owned-form').mkdir(mode=0o700)
        self.directory = self.base / ('app-' + 'c' * 32)
        self.preview = json.loads(
            (REPO_ROOT / 'examples/owned_skill_reuse_preview.json').read_text())
        self.material = {'preview': self.preview, 'preview_sha256': digest(self.preview),
                         'context': {}}
        self.pins = {'owned_skill_release_sha256': self.preview['release_sha256'],
                     'owned_skill_selection_sha256': self.preview['selection_sha256'],
                     'owned_skill_reuse_confirm_sha256': digest(self.preview)}

    def write_startup(self):
        self.directory.mkdir(mode=0o700)
        local_app.write_new_private_plan(
            self.directory / 'owned-skill-reuse-previous-state.json',
            canonical(self.previous.model_dump()).encode())
        local_app.write_new_private_plan(
            self.directory / 'owned-skill-reuse-preview.json', canonical(self.preview).encode())

    def test_state_requires_paired_pins_and_distinct_owned_source(self):
        valid = self.previous.model_dump() | {
            'session': self.directory.name, 'owned_skill_reuse_sha256': digest(self.preview),
            'owned_skill_source_session': self.previous.session}
        local_app.LocalAppState.model_validate(valid)
        for change in ({'owned_skill_source_session': None},
                       {'owned_skill_reuse_sha256': None},
                       {'owned_skill_source_session': self.directory.name},
                       {'owned_synthetic_form_invocation': False},
                       {'mode': 'fixture'}, {'owned_synthetic_form_recipe': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                local_app.LocalAppState.model_validate(valid | change)

    def test_prepare_derives_original_source_for_repeated_reuse(self):
        previous = self.previous.model_copy(update={
            'session': self.directory.name, 'owned_skill_reuse_sha256': 'd' * 64,
            'owned_skill_source_session': self.previous.session})
        with patch('aos.owned_skill_reuse.preview_owned_skill_reuse',
                   return_value=self.material) as preview:
            self.assertEqual(local_app.prepare_owned_skill_reuse(
                previous, 'e' * 64, 'f' * 64), self.material)
        self.assertEqual(preview.call_args.kwargs, {
            'previous_session_directory': self.directory, 'previous_state': previous,
            'owned_root': self.source / 'owned-form', 'database': self.source / 'store.sqlite',
            'manifest_sha256': 'a' * 64, 'release_sha256': 'e' * 64,
            'selection_sha256': 'f' * 64})

    def test_prepare_rejects_foreign_base_before_audit(self):
        with patch('aos.owned_skill_reuse.preview_owned_skill_reuse') as audit:
            for previous, base in ((None, self.base), (self.previous, '/tmp/local-app-v1'),
                                   (self.previous, self.base / 'nested')):
                with self.assertRaises(ValueError):
                    local_app.prepare_owned_skill_reuse(previous, 'a' * 64, 'b' * 64, base=base)
            audit.assert_not_called()

    def test_load_reaudits_canonical_files_and_preserves_source_identity(self):
        self.write_startup()
        with patch('aos.local_app.prepare_owned_skill_reuse', return_value=self.material) as audit:
            loaded, previous, source = local_app.load_owned_skill_reuse_startup(
                self.directory, digest(self.preview))
        self.assertEqual((loaded, previous, source), (self.material, self.previous, self.source))
        audit.assert_called_once_with(self.previous, self.preview['release_sha256'],
                                      self.preview['selection_sha256'], base=self.base)
        self.assertFalse((self.directory / 'store.sqlite').exists())
        self.assertFalse((self.directory / 'owned-form').exists())

    def test_load_rejects_changed_preview_before_reaudit(self):
        self.write_startup()
        with patch('aos.local_app.prepare_owned_skill_reuse') as audit:
            with self.assertRaisesRegex(ValueError, 'preview changed'):
                local_app.load_owned_skill_reuse_startup(self.directory, 'f' * 64)
            audit.assert_not_called()

    def test_load_rejects_noncanonical_previous_state(self):
        self.write_startup()
        path = self.directory / 'owned-skill-reuse-previous-state.json'
        with path.open('ab') as destination:
            destination.write(b'\n')
        with patch('aos.local_app.prepare_owned_skill_reuse') as audit:
            with self.assertRaisesRegex(ValueError, 'not canonical'):
                local_app.load_owned_skill_reuse_startup(self.directory, digest(self.preview))
            audit.assert_not_called()

    def test_load_rejects_changed_source_after_reaudit(self):
        self.write_startup()
        with patch('aos.local_app.prepare_owned_skill_reuse',
                   return_value=self.material | {'preview_sha256': 'f' * 64}):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                local_app.load_owned_skill_reuse_startup(self.directory, digest(self.preview))

    def test_backend_keeps_original_database_and_new_workspace(self):
        with patch('aos.local_app.load_owned_skill_reuse_startup',
                   return_value=(self.material, self.previous, self.source)):
            command = local_app.managed_backend_command(
                self.directory, 'real', 23, owned_form_listener_fd=24,
                owned_form_manifest_sha256='a' * 64, owned_learning_lock_fd=25,
                owned_skill_reuse_sha256=digest(self.preview))
        for option, expected in {
                '--database': self.source / 'store.sqlite',
                '--workspace': self.directory / 'workspace',
                '--owned-learning-lock-fd': 25,
                '--owned-skill-reuse-sha256': digest(self.preview)}.items():
            self.assertEqual(command[command.index(option) + 1], str(expected))
        self.assertNotIn('--managed-retention', command)

    def test_backend_rejects_missing_inherited_locks_before_source_read(self):
        with patch('aos.local_app.load_owned_skill_reuse_startup') as read:
            for options in ({}, {'owned_form_listener_fd': 24}, {'owned_learning_lock_fd': 25}):
                with self.assertRaisesRegex(ValueError, 'inherited source and listener locks'):
                    local_app.managed_backend_command(
                        self.directory, 'real', 23, owned_skill_reuse_sha256='a' * 64, **options)
            read.assert_not_called()

    def test_start_rejects_partial_or_conflicting_pins_before_sockets(self):
        with patch('aos.local_app.socket.socket') as sockets, \
                patch('aos.local_app.subprocess.Popen') as launch:
            for change in ({'owned_skill_selection_sha256': None},
                           {'owned_skill_reuse_confirm_sha256': 'INVALID'},
                           {'owned_synthetic_form_invocation': True},
                           {'mode': 'fixture'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    local_app.start(check=False, **(self.pins | change))
            sockets.assert_not_called()
            launch.assert_not_called()

    def test_start_rejects_running_source_without_restart(self):
        local_app.write_state(self.previous.model_copy(update={'phase': 'running'}))
        with patch('aos.local_app.socket.socket') as sockets, \
                patch('aos.local_app.subprocess.Popen') as launch, \
                patch('aos.local_app.prepare_owned_skill_reuse') as prepare:
            with self.assertRaisesRegex(ValueError, 'Previous session requires inspection'):
                local_app.start(check=False, **self.pins)
            sockets.assert_not_called()
            launch.assert_not_called()
            prepare.assert_not_called()

    def test_wrong_confirmation_rejects_under_lock_before_sockets(self):
        local_app.write_state(self.previous)
        with patch('aos.local_app.observe_process', return_value='not_observed'), \
                patch('aos.local_app.prepare_owned_skill_reuse', return_value=self.material) as audit, \
                patch('aos.local_app.socket.socket') as sockets, \
                patch('aos.local_app.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'confirmation changed'):
                local_app.start(check=False, **(self.pins | {
                    'owned_skill_reuse_confirm_sha256': 'f' * 64}))
            self.assertEqual(audit.call_count, 2)
            sockets.assert_not_called()
            launch.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_competing_source_owner_blocks_before_bind_or_spawn(self):
        local_app.write_state(self.previous)
        with OwnedLearningWorkspace.acquire(self.source / 'owned-form', create=True), \
                patch('aos.local_app.observe_process', return_value='not_observed'), \
                patch('aos.local_app.prepare_owned_skill_reuse', return_value=self.material), \
                patch('aos.local_app.socket.socket') as sockets, \
                patch('aos.local_app.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):
                local_app.start(check=False, **self.pins)
            sockets.assert_not_called()
            launch.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_start_hands_off_exact_port_source_lock_and_immutable_preview(self):
        running = self.previous.model_copy(update={'session': self.directory.name, 'phase': 'running'})
        listeners = [ReservedSocket(23), ReservedSocket(24)]
        with patch('aos.local_app.observe_process', return_value='not_observed'), \
                patch('aos.local_app.prepare_owned_skill_reuse', return_value=self.material), \
                patch('aos.local_app.socket.socket', side_effect=listeners), \
                patch('aos.local_app.subprocess.Popen') as launch, \
                patch('aos.local_app.read_state', side_effect=[self.previous, running]), \
                patch('aos.local_app.current_status', return_value={'phase': 'running'}), \
                patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='c' * 32)), \
                patch('aos.owned_form_invocation_session.provision_owned_synthetic_form_invocation') as provision:
            launch.return_value.wait.return_value = 0
            self.assertEqual(local_app.start(check=False, **self.pins), {'phase': 'running'})
            provision.assert_not_called()
        self.assertEqual(listeners[0].address, ('127.0.0.1', 8765))
        self.assertEqual(listeners[1].address, ('127.0.0.1', 9443))
        command = launch.call_args.args[0]
        descriptor = int(command[command.index('--owned-learning-lock-fd') + 1])
        self.assertIn(descriptor, launch.call_args.kwargs['pass_fds'])
        self.assertEqual(command[command.index('--owned-skill-reuse-sha256') + 1], digest(self.preview))
        self.assertEqual((self.directory / 'owned-skill-reuse-preview.json').read_bytes(),
                         canonical(self.preview).encode())
        self.assertEqual((self.directory / 'owned-skill-reuse-previous-state.json').read_bytes(),
                         canonical(self.previous.model_dump()).encode())
        self.assertFalse((self.directory / 'owned-form').exists())
        self.assertFalse((self.directory / 'store.sqlite').exists())

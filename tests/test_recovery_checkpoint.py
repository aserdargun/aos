import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import HELLO_CONTENT, REPO_ROOT, Settings, canonical, digest
from aos.dataset import validator
from aos.decision import FixtureDecisionEngine
from aos.export import export_run
from aos.operator import Operator
from aos.recovery_checkpoint import CheckpointReport, inspect_checkpoint
from aos.storage import TrajectoryStore
from aos.workspace_identity import WorkspaceIdentity


class RecoveryCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'source.sqlite'
        self.workspace = self.root / 'workspace'
        self.deployment = digest(FixtureDecisionEngine.identity)
        settings = Settings(database=self.database, workspace=self.workspace)
        store = TrajectoryStore(self.database)
        runtime = WorkspaceRuntime(self.workspace)
        runtime.start()
        try:
            result = asyncio.run(Operator(settings, store, runtime, FixtureDecisionEngine()).hello())
            self.run_id = result['run_id']
        finally:
            runtime.stop()
            store.close()

    def inspect(self):
        return inspect_checkpoint(self.database, self.workspace, self.run_id, self.deployment)

    def update_environment(self, changes):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            environment = json.loads(connection.execute('SELECT environment_json FROM runs').fetchone()[0])
            connection.execute('UPDATE runs SET environment_json=?', (canonical({**environment, **changes}),))

    def test_environment_fixture_closes_connection_after_commit(self):
        connection = sqlite3.connect(self.database)
        with patch('test_recovery_checkpoint.sqlite3.connect', return_value=connection):
            self.update_environment({'workspace_identity': None})
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_environment_fixture_closes_connection_after_rollback(self):
        before = self.database.read_bytes()
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE runs SET status='paused'")
        with patch('test_recovery_checkpoint.sqlite3.connect', return_value=connection):
            with self.assertRaises(TypeError):
                self.update_environment({'workspace_identity': object()})
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
        self.assertEqual(self.database.read_bytes(), before)
        self.assertTrue(self.inspect().criterion_observed)

    def test_exact_effect_is_not_authority_causality_or_resolution(self):
        before = self.database.read_bytes()
        report = self.inspect()
        self.assertEqual(report.effect, 'exact_content')
        self.assertTrue(report.criterion_observed)
        self.assertTrue(report.workspace_binding_verified)
        self.assertEqual(report.unresolved_actions, 0)
        self.assertEqual(self.database.read_bytes(), before)
        for field in ('execution_authorized', 'resume_authorized', 'automatic_replay_allowed',
                      'action_causality_verified', 'uncertain_effect_resolved', 'live_deployment_verified'):
            self.assertFalse(getattr(report, field))
        for secret in (str(self.workspace), str(self.database), self.run_id, HELLO_CONTENT, 'owner_lease_id'):
            self.assertNotIn(secret, report.model_dump_json())

    def test_absent_and_different_bytes_are_not_repaired(self):
        target = self.workspace / 'hello.txt'
        target.unlink()
        self.assertEqual(self.inspect().effect, 'absent')
        self.assertFalse(target.exists())
        for content in (b'private bytes', HELLO_CONTENT.rstrip().encode(), b'\xff'):
            target.write_bytes(content)
            report = self.inspect()
            self.assertEqual(report.effect, 'different_content')
            self.assertFalse(report.criterion_observed)
            self.assertEqual(target.read_bytes(), content)

    def test_symlink_hardlink_fifo_directory_and_oversize_are_unreadable(self):
        target = self.workspace / 'hello.txt'
        target.unlink()
        outside = self.root / 'private.txt'
        outside.write_text(HELLO_CONTENT)
        target.symlink_to(outside)
        self.assertEqual(self.inspect().effect, 'unreadable')
        target.unlink()
        os.link(outside, target)
        self.assertEqual(self.inspect().effect, 'unreadable')
        target.unlink()
        os.mkfifo(target)
        self.assertEqual(self.inspect().effect, 'unreadable')
        target.unlink()
        target.mkdir()
        self.assertEqual(self.inspect().effect, 'unreadable')
        target.rmdir()
        target.write_bytes(b'x' * 4097)
        self.assertEqual(self.inspect().effect, 'unreadable')

    def test_changed_file_during_read_is_not_verified(self):
        original = os.read

        def changing_read(descriptor, size):
            content = original(descriptor, size)
            (self.workspace / 'hello.txt').write_text('changed')
            return content

        with patch('aos.recovery_checkpoint.os.read', changing_read):
            self.assertEqual(self.inspect().effect, 'unreadable')

    def test_active_workspace_lock_is_not_taken_over(self):
        runtime = WorkspaceRuntime(self.workspace)
        runtime.start()
        try:
            with self.assertRaises(BlockingIOError):
                self.inspect()
            self.assertTrue(runtime.status()['running'])
        finally:
            runtime.stop()
        self.assertTrue(self.inspect().criterion_observed)

    def test_replaced_directory_same_path_and_content_is_rejected(self):
        self.workspace.rename(self.root / 'original')
        self.workspace.mkdir()
        (self.workspace / 'hello.txt').write_text(HELLO_CONTENT)
        with self.assertRaisesRegex(ValueError, 'checkpoint_workspace_mismatch'):
            self.inspect()

    def test_renamed_directory_during_inspection_is_rejected(self):
        def replace_directory(descriptor):
            self.workspace.rename(self.root / 'original')
            self.workspace.mkdir()
            return 'exact_content'

        with patch('aos.recovery_checkpoint.inspect_effect', replace_directory):
            with self.assertRaisesRegex(ValueError, 'checkpoint_workspace_changed'):
                self.inspect()

    def test_missing_workspace_symlink_ancestor_and_traversal_never_create(self):
        missing = self.root / 'missing'
        with self.assertRaises(FileNotFoundError):
            inspect_checkpoint(self.database, missing, self.run_id, self.deployment)
        self.assertFalse(missing.exists())
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            inspect_checkpoint(self.database, link / 'workspace', self.run_id, self.deployment)
        with self.assertRaisesRegex(ValueError, 'workspace_parent_traversal'):
            inspect_checkpoint(self.database, self.workspace / '..' / 'workspace', self.run_id, self.deployment)

    def test_old_unbound_and_unsupported_runtime_are_rejected(self):
        self.update_environment({'workspace_identity': None})
        with self.assertRaises(ValueError):
            self.inspect()
        self.update_environment({'kind': 'docker_xfce'})
        with self.assertRaisesRegex(ValueError, 'checkpoint_runtime_unsupported'):
            self.inspect()

    def test_wrong_deployment_and_scope_state_tampering_rejected(self):
        with self.assertRaisesRegex(ValueError, 'checkpoint_deployment_mismatch'):
            inspect_checkpoint(self.database, self.workspace, self.run_id, 'a' * 64)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            state = json.loads(connection.execute('SELECT state_json FROM runtime_states').fetchone()[0])
            connection.execute('UPDATE runtime_states SET state_json=?', (canonical({**state, 'authorized_path': '/workspace/other'}),))
        with self.assertRaisesRegex(ValueError, 'checkpoint_scope_mismatch'):
            self.inspect()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('UPDATE runtime_states SET state_json=?', (canonical({**state, 'observation': 'modified'}),))
        with self.assertRaisesRegex(ValueError, 'checkpoint_state_mismatch'):
            self.inspect()

    def test_identity_includes_directory_not_just_path(self):
        runtime = WorkspaceRuntime(self.workspace)
        self.assertIsNone(runtime.status()['workspace_identity'])
        runtime.start()
        try:
            identity = WorkspaceIdentity.model_validate(runtime.status()['workspace_identity'])
            metadata = self.workspace.stat()
            self.assertEqual((identity.device, identity.inode, identity.owner_uid),
                             (metadata.st_dev, metadata.st_ino, metadata.st_uid))
        finally:
            runtime.stop()

    def test_workspace_identity_remains_local_not_in_trajectory_export(self):
        store = TrajectoryStore(self.database, readonly=True)
        try:
            self.assertNotIn('workspace_identity', export_run(store, self.run_id)['run']['environment'])
            environment = json.loads(store.connection.execute('SELECT environment_json FROM runs').fetchone()[0])
            self.assertIn('workspace_identity', environment)
        finally:
            store.close()

    def test_schema_fixture_flags_and_criterion_consistency(self):
        for name, model in (('recovery_checkpoint', CheckpointReport), ('workspace_identity', WorkspaceIdentity)):
            schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()}
            self.assertEqual(schema, json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text()))
        fixture = json.loads((REPO_ROOT / 'examples/recovery_checkpoint.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('workspace_identity').validate(fixture['workspace_identity'])
        validator('recovery_checkpoint').validate(fixture['report'])
        report = self.inspect().model_dump()
        validator('recovery_checkpoint').validate(report)
        for changes in ({'execution_authorized': True}, {'resume_authorized': True}, {'automatic_replay_allowed': True},
                        {'uncertain_effect_resolved': True}, {'live_deployment_verified': True}, {'raw_content': 'secret'}):
            self.assertFalse(validator('recovery_checkpoint').is_valid({**report, **changes}))
            with self.assertRaises(ValueError):
                CheckpointReport.model_validate({**report, **changes})
        with self.assertRaises(ValueError):
            CheckpointReport.model_validate({**report, 'effect': 'absent'})

    def test_cli_minimized_output_and_generic_failure(self):
        command = [sys.executable, '-m', 'aos.recovery_checkpoint', '--database', str(self.database),
                   '--workspace', str(self.workspace), '--run-id', self.run_id, '--deployment-sha256', self.deployment]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['effect'], 'exact_content')
        command[-1] = 'private-invalid-value'
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('private-invalid-value', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_actual_crash_before_and_after_effect_never_replays_or_settles_action(self):
        script = '''
import asyncio, os, sys
from pathlib import Path
from aos.computer import WorkspaceRuntime
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore
class CrashRuntime(WorkspaceRuntime):
    def write(self, path, content):
        if sys.argv[3] == 'after':
            super().write(path, content)
        os._exit(73)
settings = Settings(database=Path(sys.argv[1]), workspace=Path(sys.argv[2]))
store = TrajectoryStore(settings.database)
runtime = CrashRuntime(settings.workspace)
runtime.start()
asyncio.run(Operator(settings, store, runtime, FixtureDecisionEngine()).hello())
'''
        for stage in ('before', 'after'):
            with self.subTest(stage=stage):
                database, workspace = self.root / f'{stage}.sqlite', self.root / stage
                result = subprocess.run([sys.executable, '-c', script, str(database), str(workspace), stage],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 73, result.stderr)
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                    run_id = connection.execute('SELECT run_id FROM runs').fetchone()[0]
                    self.assertEqual(connection.execute('SELECT status FROM actions').fetchone()[0], 'running')
                sources = [path for path in (database, Path(str(database) + '-wal')) if path.exists()]
                hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
                report = inspect_checkpoint(database, workspace, run_id, self.deployment)
                self.assertEqual(report.effect, 'exact_content' if stage == 'after' else 'absent')
                self.assertEqual(report.unresolved_actions, 1)
                self.assertFalse(report.uncertain_effect_resolved)
                self.assertEqual(hashes, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources})
                store = TrajectoryStore(database)
                try:
                    state = store.state(run_id)
                    self.assertEqual(state.owner, 'PAUSED')
                    self.assertTrue(state.owner_lease_id.startswith('revoked-'))
                    repeated = inspect_checkpoint(database, workspace, run_id, self.deployment)
                    self.assertEqual(repeated.effect, report.effect)
                    self.assertEqual(repeated.unresolved_actions, 1)
                    self.assertEqual(store.connection.execute('SELECT status FROM actions').fetchone()[0], 'uncertain')
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 1)
                    self.assertEqual(store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)
                finally:
                    store.close()
                self.assertEqual((workspace / 'hello.txt').exists(), stage == 'after')


if __name__ == '__main__':
    unittest.main()

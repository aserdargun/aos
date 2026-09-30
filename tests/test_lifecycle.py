import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, canonical, digest, identifier
from aos.dataset import validator
from aos.desktop import DOCKER, DesktopRuntime
from aos.lifecycle import LifecycleEvent, LifecycleJournal, ProcessIdentity, observe_process, process_identity, read_journal
from aos.recovery_lifecycle import LifecycleInspection, inspect_lifecycle, observe_container


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'
        self.runtime = WorkspaceRuntime(self.workspace)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.journal = LifecycleJournal(self.workspace, self.runtime.descriptor, identifier('desktop'), 'sha256:' + 'a' * 64, 'b' * 64)
        self.addCleanup(self.journal.close)
        self.path = self.journal.path
        self.container_id = 'c' * 64

    def inspect(self):
        return inspect_lifecycle(self.path, self.workspace, self.journal.birth.image_id, self.journal.birth.source_sha256)

    def release(self):
        self.journal.close()
        self.runtime.stop()

    def test_private_intent_precedes_container_and_chain_is_append_only(self):
        self.assertFalse(self.path.is_relative_to(self.workspace))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(read_journal(self.path)[0][0].stage, 'intent')
        before = self.path.read_bytes()
        for stage in ('created', 'started', 'removed'):
            self.journal.record(stage, self.container_id)
        events, source_hash = read_journal(self.path)
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertEqual([event.stage for event in events], ['intent', 'created', 'started', 'removed'])
        self.assertEqual(source_hash, hashlib.sha256(self.path.read_bytes()).hexdigest())
        with self.assertRaises(ValueError):
            self.journal.record('started', self.container_id)

    def test_duplicate_runtime_file_is_never_overwritten(self):
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            LifecycleJournal(self.workspace, self.runtime.descriptor, self.journal.birth.runtime_id, self.journal.birth.image_id, self.journal.birth.source_sha256)
        self.assertEqual(self.path.read_bytes(), before)

    def test_short_write_is_completed_and_failed_fsync_disables_writer(self):
        original = os.write

        def short_write(descriptor, content):
            return original(descriptor, content[:31])

        with patch('aos.lifecycle.os.write', side_effect=short_write):
            self.journal.record('created', self.container_id)
        self.assertEqual(read_journal(self.path)[0][-1].stage, 'created')
        with patch('aos.lifecycle.os.fsync', side_effect=OSError('synthetic durability failure')):
            with self.assertRaises(OSError):
                self.journal.record('started', self.container_id)
        self.assertTrue(self.journal.failed)
        with self.assertRaises(ValueError):
            self.journal.record('removed', self.container_id)

    def test_existing_journal_directory_still_requires_parent_sync(self):
        before = list(self.path.parent.iterdir())
        with patch('aos.lifecycle.os.fsync', side_effect=OSError('synthetic retry parent failure')) as sync:
            with self.assertRaises(OSError):
                LifecycleJournal(self.workspace, self.runtime.descriptor, identifier('desktop'), self.journal.birth.image_id, self.journal.birth.source_sha256)
        sync.assert_called_once()
        self.assertEqual(list(self.path.parent.iterdir()), before)

    def test_partial_corrupt_duplicate_and_reordered_events_fail_closed(self):
        self.journal.record('created', self.container_id)
        self.journal.record('started', self.container_id)
        self.release()
        original = self.path.read_bytes()
        lines = original.splitlines(keepends=True)
        changed = json.loads(lines[1]); changed['previous_sha256'] = '0' * 64
        alternatives = [original[:-1], original + b'{', lines[0] + lines[0], lines[1] + lines[0],
                        lines[0] + (canonical(changed) + '\n').encode() + lines[2], original * 5]
        for content in alternatives:
            with self.subTest(content_size=len(content)):
                self.path.write_bytes(content)
                with self.assertRaises(ValueError):
                    read_journal(self.path)
        self.path.write_bytes(original)

    def test_symlink_hardlink_and_permissive_journal_are_rejected(self):
        self.release()
        alias = self.path.parent / 'alias'
        os.link(self.path, alias)
        with self.assertRaises(ValueError):
            read_journal(self.path)
        alias.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_journal(self.path)
        self.path.chmod(0o600)
        saved = self.path.with_suffix('.saved')
        self.path.rename(saved)
        self.path.symlink_to(saved)
        with self.assertRaises(OSError):
            read_journal(self.path)

    def test_live_owner_and_busy_workspace_never_query_docker(self):
        with patch('aos.recovery_lifecycle.observe_container') as probe:
            report = self.inspect()
        probe.assert_not_called()
        self.assertEqual(report.owner_observation, 'same_process')
        self.assertTrue(report.workspace_busy)
        self.assertEqual(report.container_observation, 'not_queried')
        self.assertFalse(report.orphan_confirmed)
        self.assertNotIn(self.journal.birth.process.boot_id, report.model_dump_json())
        self.assertNotIn(str(self.workspace), report.model_dump_json())
        self.release()
        with patch('aos.recovery_lifecycle.observe_container') as probe:
            self.assertEqual(self.inspect().owner_observation, 'same_process')
        probe.assert_not_called()

    def test_owner_absence_and_stable_missing_container_never_grant_cleanup(self):
        self.release()
        before = self.path.read_bytes()
        with patch('aos.recovery_lifecycle.observe_process', return_value='not_observed'), \
                patch('aos.recovery_lifecycle.observe_container', return_value=None):
            report = self.inspect()
        self.assertEqual(report.container_observation, 'missing')
        self.assertEqual(self.path.read_bytes(), before)
        for field in ('execution_authorized', 'cleanup_authorized', 'resume_authorized', 'automatic_replay_allowed'):
            self.assertFalse(getattr(report, field))

    def test_owner_unknown_does_not_mean_dead_or_query_docker(self):
        self.release()
        for result in ('incomparable', 'unavailable'):
            with patch('aos.recovery_lifecycle.observe_process', return_value=result), patch('aos.recovery_lifecycle.observe_container') as probe:
                self.assertEqual(self.inspect().container_observation, 'not_queried')
                probe.assert_not_called()

    def test_removed_record_with_present_container_is_not_silently_accepted(self):
        for stage in ('created', 'started', 'removed'):
            self.journal.record(stage, self.container_id)
        self.release()
        with patch('aos.recovery_lifecycle.observe_process', return_value='not_observed'), \
                patch('aos.recovery_lifecycle.observe_container', return_value={'status': 'running'}):
            with self.assertRaises(ValueError):
                self.inspect()

    def test_daemon_failure_is_not_absence_or_a_cleanup_grant(self):
        self.release()
        with patch('aos.recovery_lifecycle.observe_process', return_value='not_observed'), \
                patch('aos.recovery_lifecycle.observe_container', side_effect=ValueError('synthetic daemon failure')):
            with self.assertRaises(ValueError):
                self.inspect()

    def test_owner_container_and_journal_races_are_rejected(self):
        self.runtime.stop()
        with patch('aos.recovery_lifecycle.observe_process', side_effect=['not_observed', 'same_process']), \
                patch('aos.recovery_lifecycle.observe_container', return_value=None):
            with self.assertRaises(ValueError):
                self.inspect()
        with patch('aos.recovery_lifecycle.observe_process', return_value='not_observed'), \
                patch('aos.recovery_lifecycle.observe_container', side_effect=[None, {'status': 'running'}]):
            with self.assertRaises(ValueError):
                self.inspect()

        def changed(birth, container_id, workspace):
            if self.journal.last.stage == 'intent':
                self.journal.record('created', self.container_id)
            return None

        with patch('aos.recovery_lifecycle.observe_process', return_value='not_observed'), \
                patch('aos.recovery_lifecycle.observe_container', side_effect=changed):
            with self.assertRaises(ValueError):
                self.inspect()

    def test_wrong_workspace_and_pins_fail_before_docker(self):
        with self.assertRaises(ValueError):
            inspect_lifecycle(self.path, self.workspace, 'sha256:' + '0' * 64, self.journal.birth.source_sha256)
        self.release()
        self.workspace.rename(self.root / 'previous')
        self.workspace.mkdir()
        with patch('aos.recovery_lifecycle.observe_container') as probe:
            with self.assertRaises(ValueError):
                self.inspect()
        probe.assert_not_called()

    def test_pre_ack_lookup_uses_exact_name_and_birth_label(self):
        birth = self.journal.birth
        with patch('aos.recovery_lifecycle.docker_read', return_value=b'') as read:
            self.assertIsNone(observe_container(birth, None, self.workspace))
        self.assertEqual(read.call_args.args[0], ['container', 'ls', '--all', '--no-trunc', '--filter',
                                                'name=^/' + birth.container_name + '$', '--format', '{{.ID}}'])
        with patch('aos.recovery_lifecycle.docker_read', side_effect=[(self.container_id + '\n').encode(), b'{"birth_ref":"wrong"}']), \
                patch('aos.recovery_lifecycle.validate_container'):
            with self.assertRaises(ValueError):
                observe_container(birth, None, self.workspace)

    def test_pid_birth_boot_namespace_and_real_child_exit(self):
        identity = process_identity(os.getpid())
        self.assertEqual(observe_process(identity), 'same_process')
        self.assertEqual(observe_process(identity.model_copy(update={'start_ticks': identity.start_ticks + 1})), 'different_process')
        self.assertEqual(observe_process(identity.model_copy(update={'boot_id': '00000000-0000-0000-0000-000000000000'})), 'different_boot')
        self.assertEqual(observe_process(identity.model_copy(update={'pid_namespace': identity.pid_namespace + 1})), 'incomparable')
        process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            child = process_identity(process.pid)
            self.assertEqual(observe_process(child), 'same_process')
            process.terminate()
            process.wait(timeout=10)
            self.assertEqual(observe_process(child), 'not_observed')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        with patch('aos.lifecycle.process_identity', side_effect=PermissionError):
            self.assertEqual(observe_process(identity), 'unavailable')

    def test_cli_missing_journal_creates_nothing(self):
        missing = self.root / 'missing' / 'record.jsonl'
        result = subprocess.run([sys.executable, '-m', 'aos.recovery_lifecycle', '--journal', str(missing),
                                 '--workspace', str(self.workspace), '--image-id', self.journal.birth.image_id,
                                 '--source-sha256', self.journal.birth.source_sha256], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(missing.parent.exists())
        self.assertNotIn('Traceback', result.stderr)

    def test_canonical_contracts_and_synthetic_fixture(self):
        fixture = json.loads((REPO_ROOT / 'examples/lifecycle.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model, payload in (('lifecycle_event', LifecycleEvent, fixture['event']), ('lifecycle_inspection', LifecycleInspection, fixture['report'])):
            model.model_validate(payload)
            validator(name).validate(payload)
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()})
        for field in ('cleanup_authorized', 'execution_authorized', 'resume_authorized', 'automatic_replay_allowed', 'orphan_confirmed', 'model_deployment_verified'):
            with self.assertRaises(ValueError):
                LifecycleInspection.model_validate({**fixture['report'], field: True})


CRASH_SCRIPT = '''
import json, os, sys
from pathlib import Path
from aos.desktop import DesktopRuntime
from aos.lifecycle import LifecycleJournal
root, stage = Path(sys.argv[1]), sys.argv[2]
runtime = DesktopRuntime(root/'workspace', Path(sys.argv[3]))
print(json.dumps({'runtime_id': runtime.runtime_id}), flush=True)
original_docker, original_record = runtime.docker, LifecycleJournal.record
def crash_docker(arguments, payload=None, timeout=30):
    if arguments[0] == 'create' and stage == 'before_create':
        os._exit(79)
    result = original_docker(arguments, payload, timeout)
    if arguments[0] == 'create' and stage == 'after_create' or arguments[0] == 'start' and stage == 'after_start':
        os._exit(79)
    return result
def crash_record(self, event, container_id):
    original_record(self, event, container_id)
    if (event, stage) in {('created','after_created'), ('started','after_started'), ('removed','after_removed')}:
        os._exit(79)
runtime.docker, LifecycleJournal.record = crash_docker, crash_record
runtime.start()
runtime.stop()
'''


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Real Docker requires AOS_DESKTOP_TESTS=1')
class LifecycleDockerTests(unittest.TestCase):
    def test_real_create_ack_start_and_remove_crash_boundaries(self):
        manifest = REPO_ROOT / 'models/desktop-manifest.json'
        pins = json.loads(manifest.read_text())
        expected = {'before_create': ('intent', 'missing'), 'after_create': ('intent', 'created'),
                    'after_created': ('created', 'created'), 'after_start': ('created', 'running'),
                    'after_started': ('started', 'running'), 'after_removed': ('removed', 'missing')}
        for stage, (recorded, observed) in expected.items():
            with self.subTest(stage=stage):
                root = Path(tempfile.mkdtemp(prefix='lifecycle-crash-', dir=REPO_ROOT / 'data'))
                process = subprocess.Popen([sys.executable, '-c', CRASH_SCRIPT, str(root), stage, str(manifest)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                owner = None
                name = 'aos-desktop-' + hashlib.sha256(str(root / 'workspace').encode()).hexdigest()[:20]
                try:
                    output, error = process.communicate(timeout=90)
                    owner = json.loads(output.splitlines()[0])['runtime_id']
                    self.assertEqual(process.returncode, 79, error)
                    journal = root / '.aos-lifecycle' / (owner + '.jsonl')
                    before = journal.read_bytes()
                    report = inspect_lifecycle(journal, root / 'workspace', pins['image_id'], pins['source_sha256'])
                    self.assertEqual(report.recorded_stage, recorded)
                    self.assertEqual(report.container_observation, observed)
                    self.assertEqual(report.owner_observation, 'not_observed')
                    self.assertFalse(report.cleanup_authorized)
                    self.assertEqual(journal.read_bytes(), before)
                    self.assertFalse((root / 'store.sqlite').exists())
                    self.assertFalse((root / 'workspace/hello.txt').exists())
                    print(canonical({'evidence_directory': str(root), 'stage': stage, 'real_docker_lifecycle': report.model_dump(), 'model_called': False}))
                finally:
                    if process.poll() is None:
                        process.kill()
                        output, error = process.communicate()
                        if output:
                            owner = json.loads(output.splitlines()[0])['runtime_id']
                    process.stdout.close()
                    process.stderr.close()
                    inspected = subprocess.run([*DOCKER, 'container', 'inspect', name], capture_output=True, timeout=10)
                    if inspected.returncode == 0:
                        container = json.loads(inspected.stdout)[0]
                        if owner is not None and container['Config']['Labels'].get('com.aos.runtime') == owner:
                            subprocess.run([*DOCKER, 'rm', '-f', container['Id']], capture_output=True, check=True, timeout=20)

    def test_intent_durability_failure_never_creates_container(self):
        with tempfile.TemporaryDirectory(prefix='lifecycle-failure-', dir=REPO_ROOT / 'data') as temporary:
            runtime = DesktopRuntime(Path(temporary) / 'workspace', REPO_ROOT / 'models/desktop-manifest.json')
            original = runtime.docker
            calls = []

            def observed(arguments, payload=None, timeout=30):
                calls.append(arguments[0])
                return original(arguments, payload, timeout)

            original_fsync = os.fsync

            def fail_file_sync(descriptor):
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError('synthetic intent fsync failure')
                return original_fsync(descriptor)

            try:
                with patch.object(runtime, 'docker', side_effect=observed), patch('aos.lifecycle.os.fsync', side_effect=fail_file_sync):
                    with self.assertRaises(OSError):
                        runtime.start()
                self.assertEqual(calls, ['image'])
                self.assertIsNone(runtime.container_id)
                self.assertIsNone(runtime.descriptor)
            finally:
                runtime.stop()

    def test_failed_parent_sync_retry_cannot_skip_directory_durability(self):
        with tempfile.TemporaryDirectory(prefix='lifecycle-parent-failure-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            runtime = DesktopRuntime(root / 'workspace', REPO_ROOT / 'models/desktop-manifest.json')
            original_docker = runtime.docker
            original_fsync = os.fsync
            parent_inode = root.stat().st_ino
            calls = []
            synced = []

            def observed(arguments, payload=None, timeout=30):
                calls.append(arguments[0])
                if arguments[0] != 'image':
                    raise AssertionError('Container operation before parent durability')
                return original_docker(arguments, payload, timeout)

            def fail_parent_sync(descriptor):
                metadata = os.fstat(descriptor)
                synced.append(metadata.st_ino)
                if metadata.st_ino == parent_inode:
                    raise OSError('synthetic parent fsync failure')
                return original_fsync(descriptor)

            try:
                with patch.object(runtime, 'docker', side_effect=observed), patch('aos.lifecycle.os.fsync', side_effect=fail_parent_sync):
                    for attempt in range(2):
                        with self.subTest(attempt=attempt), self.assertRaises(OSError):
                            runtime.start()
                        (root / '.aos-lifecycle').mkdir(mode=0o700, exist_ok=True)
                        self.assertEqual(list((root / '.aos-lifecycle').iterdir()), [])
                        self.assertIsNone(runtime.container_id)
                        self.assertIsNone(runtime.descriptor)
                self.assertEqual(calls, ['image', 'image'])
                self.assertEqual(synced.count(parent_inode), 2)
            finally:
                runtime.stop()

    def test_nested_workspace_ancestor_sync_failure_prevents_container_create(self):
        with tempfile.TemporaryDirectory(prefix='lifecycle-nested-failure-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            runtime = DesktopRuntime(root / 'new-parent' / 'nested' / 'workspace', REPO_ROOT / 'models/desktop-manifest.json')
            original_docker = runtime.docker
            original_fsync = os.fsync
            synced = []
            calls = []

            def observed(arguments, payload=None, timeout=30):
                calls.append(arguments[0])
                if arguments[0] != 'image':
                    raise AssertionError('Container operation before ancestor durability')
                return original_docker(arguments, payload, timeout)

            def fail_ancestor_sync(descriptor):
                inode = os.fstat(descriptor).st_ino
                synced.append(inode)
                if inode == root.stat().st_ino:
                    raise OSError('synthetic ancestor fsync failure')
                return original_fsync(descriptor)

            try:
                with patch.object(runtime, 'docker', side_effect=observed), patch('aos.lifecycle.os.fsync', side_effect=fail_ancestor_sync):
                    with self.assertRaises(OSError):
                        runtime.start()
                self.assertEqual(synced, [directory.stat().st_ino for directory in
                                         (runtime.root, runtime.root.parent, root / 'new-parent', root)])
                self.assertEqual(calls, ['image'])
                self.assertFalse((runtime.root.parent / '.aos-lifecycle').exists())
                self.assertIsNone(runtime.descriptor)
                self.assertIsNone(runtime.container_id)
            finally:
                runtime.stop()


if __name__ == '__main__':
    unittest.main()

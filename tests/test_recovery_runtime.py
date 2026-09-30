from contextlib import closing
import copy
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
from aos.contracts import REPO_ROOT, State, canonical, digest, identifier, now
from aos.dataset import validator
from aos.decision import FixtureDecisionEngine
from aos.desktop import DesktopRuntime, DOCKER
from aos.recovery_runtime import INSPECT_FORMAT, RuntimeInspection, container_observation, docker_read, inspect_runtime
from aos.storage import TrajectoryStore
from aos.workspace_identity import workspace_identity


class RuntimeInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database, self.workspace = self.root / 'store.sqlite', self.root / 'workspace'
        self.runtime = WorkspaceRuntime(self.workspace)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.binding = workspace_identity(self.workspace, self.runtime.descriptor).model_dump()
        self.image_id = 'sha256:' + 'a' * 64
        self.container_id = 'b' * 64
        self.runtime_id = identifier('desktop')
        self.state = State(task_id=identifier('task'), run_id=identifier('run'), step_id=identifier('step'),
                           runtime_id=self.runtime_id, deployment_id=FixtureDecisionEngine.identity['deployment_id'], owner_lease_id=identifier('lease'))
        self.store = TrajectoryStore(self.database)
        self.addCleanup(self.store.close)
        self.store.connection.execute('PRAGMA journal_mode=WAL')
        self.store.create_run(self.state, FixtureDecisionEngine.identity,
                              {'kind': 'docker_xfce', 'runtime_id': self.runtime_id, 'container_id': self.container_id,
                               'image_id': self.image_id, 'workspace_identity': self.binding, 'running': True,
                               'desktop': True, 'network': False, 'real_execution': True, 'recovery_probe': False, 'mount': '/workspace'})
        self.runtime.stop()
        self.observation = {'id': self.container_id, 'image': self.image_id, 'runtime': self.runtime_id,
                            'status': 'running', 'running': True, 'started_at': now(), 'finished_at': '', 'restart_count': 0,
                            'user': f'{os.getuid()}:{os.getgid()}', 'network': 'none', 'readonly': True, 'privileged': False,
                            'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges'], 'ports': {},
                            'mounts': [{'Type': 'bind', 'Source': str(self.workspace), 'Destination': '/workspace', 'RW': True, 'Propagation': 'rprivate'}]}

    def inspect(self):
        return inspect_runtime(self.database, self.workspace, self.state.run_id, digest(FixtureDecisionEngine.identity), self.image_id)

    def mutate(self, query, parameters=()):
        with self.store.connection:
            self.store.connection.execute(query, parameters)

    def test_readonly_running_candidate_preserves_wal_and_grants_no_authority(self):
        paths = [self.database, Path(str(self.database) + '-wal')]
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        with patch('aos.recovery_runtime.container_observation', return_value=self.observation) as probe:
            report = self.inspect()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(report.disposition, 'running_candidate')
        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
        self.assertEqual(self.store.state(self.state.run_id), self.state)
        self.assertEqual(self.store.connection.execute('SELECT status FROM runs').fetchone()[0], 'running')
        for private in (str(self.workspace), self.container_id, self.runtime_id, self.state.owner_lease_id):
            self.assertNotIn(private, report.model_dump_json())
        validator('recovery_runtime').validate(report.model_dump())

    def test_busy_workspace_does_not_query_docker_or_claim_orphan(self):
        self.runtime.start_existing()
        with patch('aos.recovery_runtime.container_observation') as probe:
            report = self.inspect()
        probe.assert_not_called()
        self.assertEqual(report.disposition, 'workspace_busy')
        self.assertIsNone(report.observation_sha256)
        self.assertFalse(report.orphan_confirmed)

    def test_unresolved_action_is_counted_without_recording_a_result(self):
        with self.store.connection:
            self.store.insert('actions', action_id=identifier('action'), run_id=self.state.run_id, step_id=self.state.step_id,
                              idempotency_key=identifier('intent'), tool='filesystem.write', arguments_json=canonical({'path': '/workspace/hello.txt'}),
                              status='uncertain', error_code='RUNTIME_CRASH', created_at=now())
        with patch('aos.recovery_runtime.container_observation', return_value=self.observation):
            report = self.inspect()
        self.assertEqual(report.unresolved_actions, 1)
        self.assertEqual(tuple(self.store.connection.execute('SELECT status,result_json FROM actions').fetchone()), ('uncertain', None))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)

    def test_forged_latest_state_hash_is_rejected_before_docker(self):
        self.mutate('UPDATE state_snapshots SET content_sha256=?', ('0' * 64,))
        with patch('aos.recovery_runtime.container_observation') as probe:
            with self.assertRaises(ValueError):
                self.inspect()
        probe.assert_not_called()

    def test_missing_and_stopped_are_observations_not_cleanup_permissions(self):
        stopped = {**self.observation, 'running': False, 'status': 'exited'}
        for observation, expected in ((None, 'container_missing'), (stopped, 'stopped_container')):
            with self.subTest(expected=expected), patch('aos.recovery_runtime.container_observation', return_value=observation):
                report = self.inspect()
                self.assertEqual(report.disposition, expected)
                self.assertFalse(report.cleanup_authorized)

    def test_daemon_failure_is_not_container_missing(self):
        with patch('aos.recovery_runtime.container_observation', side_effect=ValueError('daemon unavailable')):
            with self.assertRaises(ValueError):
                self.inspect()

    def test_mismatched_live_identity_or_isolation_is_rejected(self):
        changes = [{'id': 'c' * 64}, {'runtime': identifier('desktop')}, {'image': 'sha256:' + 'c' * 64},
                   {'user': '0:0'}, {'network': 'host'}, {'readonly': False}, {'privileged': True},
                   {'cap_drop': []}, {'security_opt': []}, {'ports': {'6080/tcp': []}},
                   {'status': 'paused'}, {'running': 1}, {'mounts': []}, {'restart_count': True}]
        foreign_mount = copy.deepcopy(self.observation['mounts'])
        foreign_mount[0]['Source'] = '/private'
        changes.append({'mounts': foreign_mount})
        changes.append({'mounts': [*self.observation['mounts'], {'Type': 'volume', 'Destination': '/private'}]})
        for change in changes:
            with self.subTest(change=change), patch('aos.recovery_runtime.container_observation', return_value={**self.observation, **change}):
                with self.assertRaises(ValueError):
                    self.inspect()

    def test_observation_race_or_database_change_fails_closed(self):
        for changed in (None, {**self.observation, 'restart_count': 1}):
            with patch('aos.recovery_runtime.container_observation', side_effect=[self.observation, changed]):
                with self.assertRaises(ValueError):
                    self.inspect()

        def change_database(container_id):
            self.mutate("UPDATE tasks SET original_goal='synthetic changed source'")
            return self.observation

        with patch('aos.recovery_runtime.container_observation', side_effect=change_database):
            with self.assertRaises(ValueError):
                self.inspect()

    def test_legacy_missing_workspace_identity_never_queries_docker(self):
        self.mutate("UPDATE runs SET environment_json=json_remove(environment_json,'$.workspace_identity')")
        with patch('aos.recovery_runtime.container_observation') as probe:
            with self.assertRaises(ValueError):
                self.inspect()
        probe.assert_not_called()

    def test_replaced_workspace_or_symlink_never_queries_docker(self):
        self.workspace.rename(self.root / 'original')
        self.workspace.mkdir()
        with patch('aos.recovery_runtime.container_observation') as probe:
            with self.assertRaises(ValueError):
                self.inspect()
        probe.assert_not_called()
        self.workspace.rmdir()
        self.workspace.symlink_to(self.root / 'original', target_is_directory=True)
        with self.assertRaises(OSError):
            self.inspect()

    def test_wrong_pin_state_or_injected_container_id_is_rejected(self):
        with self.assertRaises(ValueError):
            inspect_runtime(self.database, self.workspace, self.state.run_id, '0' * 64, self.image_id)
        with self.assertRaises(ValueError):
            inspect_runtime(self.database, self.workspace, self.state.run_id, digest(FixtureDecisionEngine.identity), 'latest')
        self.mutate("UPDATE runs SET environment_json=json_set(environment_json,'$.container_id','--help')")
        with patch('aos.recovery_runtime.container_observation') as probe:
            with self.assertRaises(ValueError):
                self.inspect()
        probe.assert_not_called()

    def test_exact_id_scoped_docker_read_commands_only(self):
        with patch('aos.recovery_runtime.docker_read', side_effect=[(self.container_id + '\n').encode(), canonical(self.observation).encode()]) as read:
            self.assertEqual(container_observation(self.container_id), self.observation)
        commands = [call.args[0] for call in read.call_args_list]
        self.assertEqual(commands[0], ['container', 'ls', '--all', '--no-trunc', '--filter', 'id=' + self.container_id, '--format', '{{.ID}}'])
        self.assertEqual(commands[1], ['container', 'inspect', '--format', INSPECT_FORMAT, self.container_id])
        self.assertNotIn('.Config.Env', INSPECT_FORMAT)
        for output in (b'foreign\n', (self.container_id + '\n' + self.container_id + '\n').encode()):
            with patch('aos.recovery_runtime.docker_read', return_value=output):
                with self.assertRaises(ValueError):
                    container_observation(self.container_id)

    def test_bounded_reader_error_output_and_timeout(self):
        for script in ("import sys;sys.exit(1)", "print('x'*70000)"):
            with patch('aos.recovery_runtime.DOCKER', [sys.executable, '-c', script]):
                with self.assertRaises(ValueError):
                    docker_read([])
        with patch('aos.recovery_runtime.DOCKER', [sys.executable, '-c', 'import time;time.sleep(60)']), \
                patch('aos.recovery_runtime.time.monotonic', side_effect=[0, 11]):
            with self.assertRaises(ValueError):
                docker_read([])

    def test_cli_invalid_source_never_creates_database(self):
        missing = self.root / 'missing.sqlite'
        result = subprocess.run([sys.executable, '-m', 'aos.recovery_runtime', '--database', str(missing),
                                 '--workspace', str(self.workspace), '--run-id', self.state.run_id,
                                 '--deployment-sha256', digest(FixtureDecisionEngine.identity), '--image-id', self.image_id],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(missing.exists())
        self.assertNotIn('Traceback', result.stderr)

    def test_contract_fixture_and_canonical_schema(self):
        fixture = json.loads((REPO_ROOT / 'examples/recovery_runtime.json').read_text())
        self.assertTrue(fixture['synthetic'])
        report = RuntimeInspection.model_validate(fixture['report'])
        validator('recovery_runtime').validate(report.model_dump())
        schema = json.loads((REPO_ROOT / 'schemas/recovery_runtime.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **RuntimeInspection.model_json_schema()})
        for field in ('execution_authorized', 'resume_authorized', 'cleanup_authorized', 'automatic_replay_allowed',
                      'orphan_confirmed', 'process_liveness_verified'):
            with self.assertRaises(ValueError):
                RuntimeInspection.model_validate({**report.model_dump(), field: True})
        with self.assertRaises(ValueError):
            RuntimeInspection.model_validate({**report.model_dump(), 'observation_sha256': None})


CRASH_SCRIPT = '''
import asyncio, os, sys
from pathlib import Path
from aos.contracts import Settings, canonical
from aos.decision import FixtureDecisionEngine
from aos.desktop import DesktopRuntime
from aos.operator import Operator
from aos.storage import TrajectoryStore
root = Path(sys.argv[1])
runtime = DesktopRuntime(root/'workspace', Path(sys.argv[2]))
print(canonical({'runtime_id':runtime.runtime_id}), flush=True)
store = TrajectoryStore(root/'store.sqlite')
runtime.start()
def crash(state):
    print(canonical({'run_id':state.run_id, 'container_id':runtime.container_id}), flush=True)
    if sys.argv[3] == 'after_created':
        os._exit(78)
original_write = runtime.write
def crash_write(path, content):
    original_write(path, content)
    os._exit(78)
runtime.write = crash_write
asyncio.run(Operator(Settings(database=root/'store.sqlite',workspace=root/'workspace'), store, runtime, FixtureDecisionEngine()).hello(on_created=crash))
'''


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Real Docker requires AOS_DESKTOP_TESTS=1')
class RealRuntimeInspectionTests(unittest.TestCase):
    def test_real_process_crash_live_candidate_stop_and_missing_without_replay(self):
        for stage in ('after_created', 'after_write'):
            with self.subTest(stage=stage):
                self.exercise(stage)

    def exercise(self, stage):
        root = Path(tempfile.mkdtemp(prefix='runtime-crash-', dir=REPO_ROOT / 'data'))
        manifest = REPO_ROOT / 'models/desktop-manifest.json'
        image_id = json.loads(manifest.read_text())['image_id']
        name = 'aos-desktop-' + hashlib.sha256(str(root / 'workspace').encode()).hexdigest()[:20]
        owner = None
        process = subprocess.Popen([sys.executable, '-c', CRASH_SCRIPT, str(root), str(manifest), stage], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, errors = process.communicate(timeout=90)
            rows = [json.loads(line) for line in output.splitlines()]
            owner = rows[0]['runtime_id']
            self.assertEqual(process.returncode, 78, errors)
            run_id, container_id = rows[1]['run_id'], rows[1]['container_id']
            database = root / 'store.sqlite'
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            def inspect():
                return inspect_runtime(database, root / 'workspace', run_id, digest(FixtureDecisionEngine.identity), image_id)
            report = inspect()
            self.assertEqual(report.disposition, 'running_candidate')
            self.assertEqual(report.unresolved_actions, 1 if stage == 'after_write' else 0)
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())
            with closing(sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True)) as connection:
                self.assertEqual(connection.execute('SELECT status FROM runs').fetchone()[0], 'running')
                self.assertEqual(connection.execute('SELECT count(*) FROM actions').fetchone()[0], 1 if stage == 'after_write' else 0)
                if stage == 'after_write':
                    self.assertEqual(tuple(connection.execute('SELECT status,result_json FROM actions').fetchone()), ('running', None))
                    self.assertEqual((root / 'workspace/hello.txt').read_text(), 'Hello from the local agent.\n')
                self.assertEqual(connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
            subprocess.run([*DOCKER, 'stop', '--time', '10', container_id], check=True, capture_output=True, timeout=20)
            self.assertEqual(inspect().disposition, 'stopped_container')
            subprocess.run([*DOCKER, 'rm', container_id], check=True, capture_output=True, timeout=10)
            self.assertEqual(inspect().disposition, 'container_missing')
            print(canonical({'evidence_directory': str(root), 'stage': stage, 'real_docker_fixture_crash': report.model_dump(), 'model_called': False}))
        finally:
            if process.poll() is None:
                process.kill()
                output, errors = process.communicate()
                if owner is None and output:
                    owner = json.loads(output.splitlines()[0])['runtime_id']
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            inspected = subprocess.run([*DOCKER, 'container', 'inspect', name], capture_output=True, timeout=10)
            if inspected.returncode == 0:
                value = json.loads(inspected.stdout)[0]
                if owner is not None and value['Config']['Labels'].get('com.aos.runtime') == owner:
                    subprocess.run([*DOCKER, 'rm', '-f', value['Id']], check=True, capture_output=True, timeout=20)


if __name__ == '__main__':
    unittest.main()

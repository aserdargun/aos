import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, State, canonical, digest, identifier, now
from aos.dataset import validator
from aos.decision import FixtureDecisionEngine
from aos.desktop import DOCKER, DesktopRuntime
from aos.desktop_control import DesktopController
from aos.lifecycle import LifecycleJournal
from aos.recovery_session import SessionInspection, inspect_session
from aos.session_binding import SessionRuntimeBinding
from aos.storage import TrajectoryStore


class SessionInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'store.sqlite'
        self.runtime = DesktopRuntime(self.root / 'workspace', self.root / 'unused.json')
        WorkspaceRuntime.start(self.runtime)
        self.addCleanup(lambda: WorkspaceRuntime.stop(self.runtime))
        self.runtime.pins = {'image_id': 'sha256:' + 'a' * 64, 'source_sha256': 'b' * 64}
        self.runtime.container_id = 'c' * 64
        self.runtime.lifecycle = LifecycleJournal(self.runtime.root, self.runtime.descriptor, self.runtime.runtime_id,
                                                   self.runtime.pins['image_id'], self.runtime.pins['source_sha256'])
        self.addCleanup(self.runtime.lifecycle.close)
        for stage in ('created', 'started'):
            self.runtime.lifecycle.record(stage, self.runtime.container_id)
        self.path = self.runtime.lifecycle.path
        self.store = TrajectoryStore(self.database)
        self.addCleanup(self.store.close)
        self.store.connection.execute('PRAGMA journal_mode=WAL')
        self.controller = DesktopController(self.store, self.runtime)

    def inspect(self, **overrides):
        arguments = {'database': self.database, 'journal': self.path, 'workspace': self.runtime.root,
                     'session_id': self.controller.session_id, **self.runtime.pins}
        arguments.update(overrides)
        return inspect_session(**arguments)

    def mutate(self, query, values=()):
        with self.store.connection:
            self.store.connection.execute(query, values)

    def add_job(self):
        session = self.controller.state()
        state = State(task_id=identifier('task'), run_id=identifier('run'), step_id=identifier('step'),
                      runtime_id=self.runtime.runtime_id, owner_lease_id=session['lease_id'],
                      deployment_id=FixtureDecisionEngine.identity['deployment_id'])
        environment = {'kind': 'docker_xfce', 'runtime_id': self.runtime.runtime_id,
                       'container_id': self.runtime.container_id, **self.runtime.pins,
                       'workspace_identity': self.runtime.lifecycle.birth.workspace.model_dump(),
                       'lifecycle_ref': digest(self.runtime.lifecycle.birth.model_dump()),
                       'running': True, 'desktop': True, 'real_execution': True, 'network': False,
                       'mount': '/workspace', 'recovery_probe': False}
        self.store.create_run(state, FixtureDecisionEngine.identity, environment)
        job_id = identifier('job')
        with self.store.connection:
            self.store.insert('desktop_tasks', job_id=job_id, session_id=session['session_id'], run_id=state.run_id,
                              runtime_id=state.runtime_id, kind='hello', lease_id=session['lease_id'], generation=0,
                              status='running', real_model=0, created_at=now(), updated_at=now())
        return job_id, state

    def test_session_binding_is_atomic_and_minimized_inspection_preserves_wal(self):
        sources = [self.database, Path(str(self.database) + '-wal'), self.path]
        before = {str(path): path.read_bytes() for path in sources}
        with patch('aos.recovery_lifecycle.observe_container') as probe:
            report = self.inspect()
        probe.assert_not_called()
        self.assertEqual(report.lifecycle.owner_observation, 'same_process')
        self.assertTrue(report.lifecycle.workspace_busy)
        self.assertEqual(report.job_count, 0)
        self.assertIsNone(report.job)
        self.assertEqual(before, {str(path): path.read_bytes() for path in sources})
        for private in (self.controller.session_id, self.runtime.runtime_id, self.runtime.container_id,
                        str(self.runtime.root), self.controller.state()['lease_id'], '"pid":', '"boot_id":'):
            self.assertNotIn(private, report.model_dump_json())
        validator('recovery_session').validate(report.model_dump())

    def test_controller_refuses_missing_or_mismatched_birth_before_session_insert(self):
        for attribute, value in (('container_id', 'd' * 64), ('runtime_id', identifier('desktop')), ('lifecycle', None)):
            with self.subTest(attribute=attribute), patch.object(self.runtime, attribute, value):
                with self.assertRaises(ValueError):
                    DesktopController(self.store, self.runtime)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_sessions').fetchone()[0], 1)

    def test_event_write_failure_rolls_back_session_insert(self):
        with patch.object(DesktopController, 'record_binding', side_effect=ValueError('synthetic SQL failure')):
            with self.assertRaises(ValueError):
                DesktopController(self.store, self.runtime)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_sessions').fetchone()[0], 1)

    def test_missing_legacy_binding_fails_before_docker(self):
        self.mutate('DELETE FROM desktop_events')
        with patch('aos.recovery_session.inspect_lifecycle') as probe:
            with self.assertRaisesRegex(ValueError, 'binding_missing'):
                self.inspect()
        probe.assert_not_called()

    def test_duplicate_binding_and_session_payload_tampering_fail_closed(self):
        payload = json.loads(self.store.connection.execute('SELECT payload_json FROM desktop_events').fetchone()[0])
        for field, changed in (('session_id', identifier('desktop-session')), ('generation', 2), ('container_id', 'd' * 64)):
            self.mutate('UPDATE desktop_events SET payload_json=?', (canonical({**payload, field: changed}),))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.inspect()
        self.mutate('UPDATE desktop_events SET payload_json=?', (canonical(payload),))
        with self.store.connection:
            self.store.insert('desktop_events', event_id=identifier('event'), session_id=self.controller.session_id,
                              kind='runtime_binding', payload_json=canonical(payload), created_at=now())
        with self.assertRaises(ValueError):
            self.inspect()

    def test_wrong_pins_session_and_replaced_workspace_fail_closed(self):
        for override in ({'image_id': 'sha256:' + '0' * 64}, {'source_sha256': '0' * 64},
                         {'session_id': identifier('desktop-session')}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.inspect(**override)
        self.runtime.root.rename(self.root / 'old-workspace')
        self.runtime.root.mkdir()
        with self.assertRaises(ValueError):
            self.inspect()

    def test_exact_hello_job_run_state_environment_and_deployment_are_bound(self):
        job_id, state = self.add_job()
        report = self.inspect(job_id=job_id, deployment_sha256=digest(FixtureDecisionEngine.identity))
        self.assertEqual(report.job.run_ref, digest({'run_id': state.run_id}))
        self.assertEqual(report.job.status, 'running')
        self.assertEqual(report.job.run_status, 'running')
        self.assertEqual(report.job_count, 1)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.lifecycle.model_deployment_verified)

    def test_unsupported_or_missing_job_and_mismatched_deployment_are_rejected(self):
        job_id, state = self.add_job()
        for overrides in ({'job_id': job_id}, {'deployment_sha256': '0' * 64},
                          {'job_id': job_id, 'deployment_sha256': '0' * 64},
                          {'job_id': identifier('job'), 'deployment_sha256': digest(FixtureDecisionEngine.identity)}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.inspect(**overrides)
        self.mutate("UPDATE desktop_tasks SET kind='browser_form'")
        with self.assertRaisesRegex(ValueError, 'job_binding'):
            self.inspect(job_id=job_id, deployment_sha256=digest(FixtureDecisionEngine.identity))

    def test_run_environment_and_state_snapshot_tampering_are_rejected(self):
        job_id, state = self.add_job()
        original = self.store.connection.execute('SELECT environment_json FROM runs').fetchone()[0]
        environment = json.loads(original)
        for field, value in (('lifecycle_ref', '0' * 64), ('container_id', 'e' * 64), ('workspace_identity', None), ('network', True)):
            self.mutate('UPDATE runs SET environment_json=?', (canonical({**environment, field: value}),))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.inspect(job_id=job_id, deployment_sha256=digest(FixtureDecisionEngine.identity))
        self.mutate('UPDATE runs SET environment_json=?', (original,))
        self.mutate('UPDATE state_snapshots SET content_sha256=?', ('0' * 64,))
        with self.assertRaisesRegex(ValueError, 'state_binding'):
            self.inspect(job_id=job_id, deployment_sha256=digest(FixtureDecisionEngine.identity))

    def test_database_changes_during_lifecycle_inspection_are_rejected(self):
        from aos.recovery_lifecycle import inspect_lifecycle

        def changed(*arguments):
            report = inspect_lifecycle(*arguments)
            self.mutate("UPDATE desktop_sessions SET owner='PAUSED'")
            return report

        with patch('aos.recovery_session.inspect_lifecycle', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'database_changed'):
                self.inspect()

    def test_journal_changes_during_inspection_are_rejected(self):
        from aos.recovery_lifecycle import inspect_lifecycle

        def changed(*arguments):
            report = inspect_lifecycle(*arguments)
            self.runtime.lifecycle.record('removed', self.runtime.container_id)
            return report

        with patch('aos.recovery_session.inspect_lifecycle', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'journal_changed'):
                self.inspect()

    def restart_fixture(self):
        self.runtime.lifecycle.record('removed', self.runtime.container_id)
        self.runtime.lifecycle.close()
        self.runtime.runtime_id = identifier('desktop')
        self.runtime.container_id = 'd' * 64
        self.runtime.lifecycle = LifecycleJournal(self.runtime.root, self.runtime.descriptor, self.runtime.runtime_id,
                                                   self.runtime.pins['image_id'], self.runtime.pins['source_sha256'])
        self.addCleanup(self.runtime.lifecycle.close)
        for stage in ('created', 'started'):
            self.runtime.lifecycle.record(stage, self.runtime.container_id)
        self.path = self.runtime.lifecycle.path

    def test_restart_commits_new_identity_and_old_job_does_not_match_new_birth(self):
        job_id, state = self.add_job()
        original_path = self.path
        with patch.object(self.runtime, 'restart', side_effect=self.restart_fixture):
            self.controller.control('restart')
        self.assertEqual(self.inspect().generation, 1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM desktop_events WHERE kind='runtime_binding'").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            self.inspect(journal=original_path)
        with self.assertRaises(ValueError):
            self.inspect(job_id=job_id, deployment_sha256=digest(FixtureDecisionEngine.identity))

    def test_restart_sql_failure_does_not_publish_new_binding(self):
        with patch.object(self.runtime, 'restart', side_effect=self.restart_fixture), \
                patch.object(self.controller, 'record_binding', side_effect=ValueError('synthetic commit failure')):
            with self.assertRaises(ValueError):
                self.controller.control('restart')
        self.assertEqual(self.controller.state()['status'], 'paused')
        with self.assertRaisesRegex(ValueError, 'runtime_binding'):
            self.inspect()
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM desktop_events WHERE kind='runtime_binding'").fetchone()[0], 1)

    def test_restart_without_initial_binding_history_fails_closed(self):
        with patch.object(self.runtime, 'restart', side_effect=self.restart_fixture):
            self.controller.control('restart')
        self.mutate("DELETE FROM desktop_events WHERE kind='runtime_binding' AND json_extract(payload_json,'$.generation')=0")
        with self.assertRaisesRegex(ValueError, 'birth_binding'):
            self.inspect()

    def test_unresolved_inputs_remain_unresolved_without_restored_authority(self):
        state = self.controller.state()
        input_id = self.controller.enqueue(state['lease_id'], state['generation'])
        self.mutate("UPDATE desktop_inputs SET status='uncertain' WHERE input_id=?", (input_id,))
        report = self.inspect()
        self.assertEqual(report.unresolved_inputs, 1)
        self.assertFalse(report.lease_restored)
        self.assertFalse(report.approval_restored)
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_inputs').fetchone()[0], 'uncertain')

    def test_cli_success_is_read_only_and_hash_only(self):
        before = self.path.read_bytes()
        result = subprocess.run([sys.executable, '-m', 'aos.recovery_session', '--database', str(self.database),
                                 '--workspace', str(self.runtime.root), '--journal', str(self.path),
                                 '--session-id', self.controller.session_id, '--image-id', self.runtime.pins['image_id'],
                                 '--source-sha256', self.runtime.pins['source_sha256']], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = SessionInspection.model_validate_json(result.stdout)
        self.assertEqual(report.lifecycle.owner_observation, 'same_process')
        self.assertEqual(report.lifecycle.container_observation, 'not_queried')
        self.assertNotIn(self.controller.session_id.encode(), result.stdout)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_missing_inputs_never_creates_database_or_workspace(self):
        missing = self.root / 'missing'
        result = subprocess.run([sys.executable, '-m', 'aos.recovery_session', '--database', str(missing / 'db'),
                                 '--workspace', str(missing / 'workspace'), '--journal', str(missing / 'journal'),
                                 '--session-id', self.controller.session_id, '--image-id', self.runtime.pins['image_id'],
                                 '--source-sha256', self.runtime.pins['source_sha256']], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertFalse(missing.exists())

    def test_canonical_synthetic_contract_and_no_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/recovery_session.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, model, key in (('session_runtime_binding', SessionRuntimeBinding, 'binding'),
                                 ('recovery_session', SessionInspection, 'report')):
            model.model_validate(fixture[key])
            validator(name).validate(fixture[key])
            self.assertEqual(json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text()),
                             {'$schema': 'https://json-schema.org/draft/2020-12/schema', **model.model_json_schema()})
        for field in ('execution_authorized', 'resume_authorized', 'cleanup_authorized', 'automatic_replay_allowed',
                      'lease_restored', 'approval_restored'):
            with self.assertRaises(ValueError):
                SessionInspection.model_validate({**fixture['report'], field: True})
        payload = copy.deepcopy(fixture['report'])
        payload['lifecycle']['recorded_stage'] = 'intent'
        with self.assertRaises(ValueError):
            SessionInspection.model_validate(payload)


CRASH_SCRIPT = '''
import asyncio, json, os, sys
from pathlib import Path
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine
from aos.desktop import DesktopRuntime
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.storage import TrajectoryStore
root, stage = Path(sys.argv[1]), sys.argv[2]
runtime = DesktopRuntime(root/'workspace', Path(sys.argv[3]))
runtime.start()
store = TrajectoryStore(root/'store.sqlite')
store.connection.execute('PRAGMA journal_mode=WAL')
controller = DesktopController(store, runtime)
print(json.dumps({'session_id': controller.session_id, 'runtime_id': runtime.runtime_id}), flush=True)
if stage == 'session_commit':
    os._exit(81)
if stage == 'restart_gap':
    original = runtime.restart
    def restart():
        original()
        print(json.dumps({'new_runtime_id': runtime.runtime_id}), flush=True)
        os._exit(81)
    runtime.restart = restart
    controller.control('restart')
async def run():
    scheduler = DesktopScheduler(controller, Settings(workspace=root/'workspace', database=root/'store.sqlite'), FixtureDecisionEngine())
    state = controller.state()
    job = scheduler.start(state['lease_id'], state['generation'])
    print(json.dumps(job), flush=True)
    while scheduler.status()['approval'] is None:
        await asyncio.sleep(.001)
    os._exit(81)
asyncio.run(run())
'''


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Real Docker requires AOS_DESKTOP_TESTS=1')
class SessionDockerTests(unittest.TestCase):
    def test_real_session_job_and_restart_commit_crash_boundaries(self):
        manifest = REPO_ROOT / 'models/desktop-manifest.json'
        pins = json.loads(manifest.read_text())
        for stage in ('session_commit', 'job_approval', 'restart_gap'):
            with self.subTest(stage=stage):
                root = Path(tempfile.mkdtemp(prefix='session-crash-', dir=REPO_ROOT / 'data'))
                owners = set()
                name = 'aos-desktop-' + hashlib.sha256(str(root / 'workspace').encode()).hexdigest()[:20]
                with subprocess.Popen([sys.executable, '-c', CRASH_SCRIPT, str(root), stage, str(manifest)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    try:
                        output, error = process.communicate(timeout=90)
                        records = [json.loads(line) for line in output.splitlines()]
                        owners = {value for record in records for key, value in record.items() if 'runtime_id' in key}
                        self.assertEqual(process.returncode, 81, error)
                        identity = records[0]
                        runtime_id = records[-1]['new_runtime_id'] if stage == 'restart_gap' else identity['runtime_id']
                        journal = root / '.aos-lifecycle' / (runtime_id + '.jsonl')
                        arguments = {'database': root / 'store.sqlite', 'journal': journal, 'workspace': root / 'workspace',
                                     'session_id': identity['session_id'], 'image_id': pins['image_id'], 'source_sha256': pins['source_sha256']}
                        if stage == 'job_approval':
                            arguments.update(job_id=records[1]['job_id'], deployment_sha256=digest(FixtureDecisionEngine.identity))
                        paths = [root / 'store.sqlite', Path(str(root / 'store.sqlite') + '-wal'), journal]
                        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
                        if stage == 'restart_gap':
                            with self.assertRaisesRegex(ValueError, 'runtime_binding'):
                                inspect_session(**arguments)
                            report = None
                        else:
                            report = inspect_session(**arguments)
                            self.assertEqual(report.lifecycle.owner_observation, 'not_observed')
                            self.assertEqual(report.lifecycle.container_observation, 'running')
                            self.assertFalse(report.resume_authorized)
                            if stage == 'job_approval':
                                self.assertEqual(report.job.pending_approvals, 1)
                                self.assertEqual(report.job.unresolved_actions, 0)
                        self.assertEqual(before, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
                        self.assertFalse((root / 'workspace/hello.txt').exists())
                        print(canonical({'evidence_directory': str(root), 'stage': stage, 'source_hashes': before,
                                         'real_docker_session': report.model_dump() if report else None, 'model_called': False}))
                    finally:
                        if process.poll() is None:
                            process.kill()
                            output, error = process.communicate()
                            owners = {value for line in output.splitlines() for key, value in json.loads(line).items() if 'runtime_id' in key}
                        inspected = subprocess.run([*DOCKER, 'container', 'inspect', name], capture_output=True, timeout=10)
                        if inspected.returncode == 0:
                            container = json.loads(inspected.stdout)[0]
                            if container['Config']['Labels'].get('com.aos.runtime') in owners:
                                subprocess.run([*DOCKER, 'rm', '-f', container['Id']], capture_output=True, check=True, timeout=20)

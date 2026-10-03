import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
import jsonschema

from aos import native_maintenance as maintenance
from aos.contracts import canonical, digest
from aos.lifecycle import process_identity
from aos.native_handover import NativeHandoverPreview, ObservedNativeProcess, _procedure, _summarize
from aos.local_app import LocalAppState


ROOT = Path(__file__).resolve().parents[1]


class NativeMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.repository = self.root / 'repository'
        self.repository.mkdir(mode=0o700)
        self.base = self.repository / 'data/local-app-v1'
        self.base.mkdir(mode=0o700, parents=True)
        self.recipe = self.repository / 'scripts/shared-only-runtime-v1'
        self.recipe.mkdir(parents=True)
        self.manifest = json.loads((ROOT / 'scripts/shared-only-runtime-v1/manifest.json').read_text())
        for name in ('manifest.json', 'source.patch.txt'):
            shutil.copyfile(ROOT / 'scripts/shared-only-runtime-v1' / name, self.recipe / name)
        self.files = {}
        helpers = ['src/aos/native_maintenance.py', 'src/aos/native_exclusion.py', 'scripts/native_maintenance.py',
                   'schemas/native_maintenance_store.schema.json', 'schemas/native_maintenance_request.schema.json',
                   'schemas/native_maintenance_receipt.schema.json', 'schemas/native_exclusion_evidence.schema.json',
                   'src/aos/native_handover.py', 'src/aos/lifecycle.py', 'src/aos/workspace_identity.py',
                   'src/aos/shared_desktop_plan.py', 'src/aos/contracts.py']
        for name in [entry['path'] for entry in self.manifest['files']] + list(self.manifest['preserved_files']) + helpers:
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
            path.chmod(0o644)
            self.files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        for name in ('python', 'server'):
            path = self.repository / name
            path.write_text('Synthetic executable pin, never executed\n')
            self.files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.files.update({'/usr/bin/docker': 'a' * 64, '/usr/bin/nvidia-smi': 'b' * 64})
        self.files.update({str(self.recipe / name): hashlib.sha256((self.recipe / name).read_bytes()).hexdigest()
                           for name in ('manifest.json', 'source.patch.txt')})
        self.config = {}
        for name in ('decider', 'bonsai'):
            path = self.repository / 'models' / (name + '-manifest.json')
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"synthetic":true}\n')
            self.config[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.identity = process_identity(os.getpid())
        self.supervisor = self.identity.model_copy(update={'pid': 2147483000, 'start_ticks': 123})
        self.backend = self.supervisor.model_copy(update={'pid': 2147483001})
        self.state = LocalAppState(session='app-' + 'a' * 32, mode='real', phase='running',
            supervisor=self.supervisor, backend=self.backend,
            token_name='desktop-console-' + 'b' * 16 + '.token', started_at='2026-10-03T00:00:00Z')
        self.current = self.base / 'current.json'
        self.current.write_text(canonical(self.state.model_dump(mode='json')) + '\n')
        self.current.chmod(0o600)
        (self.base / 'manager.lock').touch(mode=0o600)
        self.control = {'session_id': 'desktop-session-' + 'c' * 32, 'runtime_id': 'synthetic-runtime',
                        'lease_id': 'synthetic-lease', 'generation': 1, 'owner': 'AGENT', 'status': 'running'}
        self.binding = {'snapshot_sha256': '1' * 64, 'session_ref': digest({'session_id': self.control['session_id']}),
            'binding_sha256': '2' * 64, 'session_status': 'running', 'session_owner': 'AGENT', 'generation': 1,
            'job_count': 0, 'unresolved_inputs': 0, 'inspected_at': '2026-10-03T00:00:00Z',
            'lifecycle': {'journal_sha256': '3' * 64, 'birth_ref': '4' * 64,
                'runtime_ref': digest({'runtime_id': 'synthetic-runtime'}), 'workspace_sha256': '5' * 64,
                'image_id': 'sha256:' + '6' * 64, 'source_sha256': '7' * 64, 'recorded_stage': 'started',
                'owner_observation': 'same_process', 'workspace_busy': True, 'container_observation': 'not_queried',
                'observation_sha256': None, 'inspected_at': '2026-10-03T00:00:00Z'}}
        self.tasks = {'available': True, 'busy': False, 'reserved': False, 'jobs': [], 'approval': None,
            'auto_approval': None, 'restart_quiesced': False, 'owned_skill_planning': {'available': False},
            'owned_web_goal_planning': {'available': False}}
        self.scientist = {'configured': False, 'jobs': [], 'inference': {'configured': False,
            'unresolved_count': 0, 'unresolved_lab_effect_count': 0, 'other_session_count': 0, 'truncated': False,
            'local_cleanup_pending': False, 'evidence_controls': {'available': True, 'supported': True, 'pending_count': 0}}}
        self.payloads = {'/api/state': {'control': self.control}, '/api/tasks': self.tasks,
                         '/api/scientist/jobs': self.scientist, '/api/session/binding': self.binding}
        control, tasks, scientist, binding, blockers = _summarize(self.payloads)
        self.assertEqual(blockers, [])
        preview = NativeHandoverPreview(recorded_at='2026-10-03T00:00:00Z', manager_base=str(self.base),
            expected_session=self.state.session, current_state_sha256=hashlib.sha256(self.current.read_bytes()).hexdigest(),
            supervisor=self.supervisor, backend=self.backend, observed_descendants=[],
            endpoint_observations=[{'path': path, 'available': True} for path in maintenance.ENDPOINTS],
            control=control, tasks=tasks, scientist=scientist, session_binding=binding,
            snapshot_continuity_verified=True, local_idle_observed=True, blockers=['consent_required'],
            procedure=_procedure(self.state.session), limitations=['Explicitly synthetic CPU fixture'])
        self.preview_path = self.root / 'preview.json'
        self.preview_path.write_text(canonical(preview.model_dump(mode='json')) + '\n')
        self.preview_path.chmod(0o600)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, attribute, value in [(maintenance, 'REPO_ROOT', self.repository), (maintenance, 'RECIPE', self.recipe),
                (maintenance, 'STORE_DIRECTORY', self.base / 'native-maintenance-v1'),
                (maintenance.local_app, 'BASE', self.base)]:
            self.stack.enter_context(patch.object(target, attribute, value))
        self.store = maintenance.provision_native_maintenance_store()
        issued = maintenance._clock()
        self.request = maintenance.NativeMaintenanceRequest(request_id='native-maintenance-' + 'd' * 32,
            store=self.store, principal='synthetic-reviewer', owner_uid=os.getuid(), expected_session=self.state.session,
            handover_path=str(self.preview_path), handover_sha256=hashlib.sha256(self.preview_path.read_bytes()).hexdigest(),
            original_state_sha256=preview.current_state_sha256, shared_plan_sha256='8' * 64,
            candidate_manifest_sha256=hashlib.sha256((self.recipe / 'manifest.json').read_bytes()).hexdigest(),
            candidate_patch_sha256=self.manifest['patch_sha256'], source_files=self.files, source_sha256=digest(self.files),
            config_files=self.config, config_sha256=digest(self.config), native_python_real_paths=[str(self.repository / 'python')],
            native_server_paths=[str(self.repository / 'server')], gpu_uuid='GPU-11111111-1111-1111-1111-111111111111',
            boot_id=self.identity.boot_id, issued_boottime=issued, expires_boottime=issued + 900)
        self.stopped = False
        self.http_requests = []
        self.client_class = httpx.Client

    def handler(self, request):
        self.http_requests.append((request.method, request.url.path))
        if request.url.path == '/api/login':
            return httpx.Response(200, json={'authenticated': True})
        if request.url.path == '/api/restart/quiesce':
            self.assertEqual(json.loads(request.content), {'session_id': self.control['session_id']})
            self.tasks['restart_quiesced'] = True
            return httpx.Response(200, json={'quiesced': True, 'session_id': self.control['session_id']})
        return httpx.Response(200, json=self.payloads[request.url.path])

    def client(self, **arguments):
        self.assertFalse(arguments['trust_env'])
        self.assertFalse(arguments['follow_redirects'])
        return self.client_class(**arguments, transport=httpx.MockTransport(self.handler))

    def stop(self, **arguments):
        self.assertTrue(self.tasks['restart_quiesced'])
        self.assertEqual(arguments, {'expected_session': self.state.session, 'expected_state': self.state})
        self.stopped = True
        self.current.write_text(canonical(self.state.model_copy(update={'phase': 'stopped', 'token_name': None}).model_dump(mode='json')))
        return {'phase': 'stopped'}

    def execution_boundaries(self, *, cleanup_error=None):
        stack = ExitStack()
        stack.enter_context(patch.object(maintenance.local_app, 'token_value', return_value='synthetic-token-never-log'))
        stack.enter_context(patch.object(maintenance.local_app, 'stop', side_effect=self.stop))
        stack.enter_context(patch.object(maintenance.httpx, 'Client', side_effect=self.client))
        stack.enter_context(patch.object(maintenance, 'observe_descendants', return_value=([
            ObservedNativeProcess(identity=self.supervisor, cgroups=['0::/shared-fixture']),
            ObservedNativeProcess(identity=self.backend, cgroups=['0::/shared-fixture'])], [])))
        stack.enter_context(patch.object(maintenance, 'observe_process', side_effect=lambda _identity: 'not_observed' if self.stopped else 'same_process'))
        stack.enter_context(patch.object(maintenance.os, 'pidfd_open', side_effect=lambda _pid: os.open('/dev/null', os.O_RDONLY)))
        stack.enter_context(patch.object(maintenance.select, 'select', side_effect=lambda inputs, *_args: (inputs if self.stopped else [], [], [])))
        stack.enter_context(patch.object(maintenance, '_native_inventory', return_value=([], [])))
        stack.enter_context(patch.object(maintenance, '_verify_inventory_selection'))
        original_verify = maintenance._verify_files
        stack.enter_context(patch.object(maintenance, '_verify_files', side_effect=lambda request, files:
            original_verify(request, {name: value for name, value in files.items() if not name.startswith('/usr/bin/')})))
        def cleanup(request, original, retired):
            if cleanup_error:
                raise cleanup_error
            return maintenance.observe_native_absence(request, retired, deadline=request.expires_boottime), {'synthetic-journal': '9' * 64}
        stack.enter_context(patch.object(maintenance, '_physical_cleanup', side_effect=cleanup))
        return stack

    def execute(self):
        return maintenance.execute_native_maintenance(self.request, confirm_request_sha256=digest(self.request.model_dump(mode='json')))

    def test_actual_41_source_promotion_and_durable_receipt_with_mock_physical_boundaries(self):
        with self.execution_boundaries():
            receipt = self.execute()
        for entry in self.manifest['files']:
            self.assertEqual(hashlib.sha256((self.repository / entry['path']).read_bytes()).hexdigest(), entry['after_sha256'])
            self.assertEqual(hashlib.sha256((ROOT / entry['path']).read_bytes()).hexdigest(), entry['before_sha256'])
        readback, checksum = maintenance.read_native_maintenance_receipt(self.store, receipt.request_sha256)
        self.assertEqual(readback, receipt)
        path = Path(self.store.record.directory) / 'receipt.json'
        self.assertEqual(checksum, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(receipt.gpu_release_verified)
        self.assertFalse(receipt.shared_launch_authorized)
        self.assertNotIn('synthetic-token-never-log', path.read_text())
        self.assertEqual([operation for operation in self.http_requests if operation[0] == 'POST'],
                         [('POST', '/api/login'), ('POST', '/api/restart/quiesce')])
        with self.execution_boundaries(), self.assertRaisesRegex(ValueError, 'replay'):
            self.execute()

    def test_busy_configured_inference_and_unknown_lab_never_stop(self):
        self.tasks['busy'] = True
        with self.execution_boundaries(), self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.stopped)
        self.assertEqual(self.current.read_text(), canonical(self.state.model_dump(mode='json')) + '\n')
        self.tasks['busy'] = False
        for alteration in ('configured', 'inference', 'unknown'):
            with self.subTest(alteration=alteration):
                self.scientist['configured'] = False
                self.scientist['inference']['configured'] = False
                self.scientist['inference']['evidence_controls']['available'] = True
                if alteration == 'configured':
                    self.scientist['configured'] = True
                elif alteration == 'inference':
                    self.scientist['inference']['configured'] = True
                else:
                    self.scientist['inference']['evidence_controls']['available'] = False
                with self.client(base_url='http://127.0.0.1:8765', trust_env=False, follow_redirects=False) as client:
                    with self.assertRaises(ValueError):
                        maintenance._idle(client)
                self.assertFalse(self.stopped)
                self.assertEqual(self.current.read_text(), canonical(self.state.model_dump(mode='json')) + '\n')

    def test_wrong_consent_and_stale_state_deny_before_effect(self):
        with self.assertRaisesRegex(ValueError, 'consent'):
            maintenance.execute_native_maintenance(self.request, confirm_request_sha256='0' * 64)
        self.current.write_text(canonical(self.state.model_copy(update={'phase': 'failed'}).model_dump(mode='json')))
        with self.execution_boundaries(), self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.http_requests)
        self.assertFalse(self.stopped)

    def test_failed_cleanup_keeps_stopped_source_unchanged_and_no_receipt(self):
        with self.execution_boundaries(cleanup_error=ValueError('Unknown original container')), self.assertRaises(ValueError):
            self.execute()
        self.assertTrue(self.stopped)
        self.assertFalse((Path(self.store.record.directory) / 'receipt.json').exists())
        self.assertTrue((Path(self.store.record.directory) / 'failure.json').exists())
        for entry in self.manifest['files']:
            self.assertEqual(hashlib.sha256((self.repository / entry['path']).read_bytes()).hexdigest(), entry['before_sha256'])

    def test_partial_source_write_never_reverses_or_restarts(self):
        original_promote = maintenance._promote_file
        def fail(request, directory, sequence, entry, content):
            if sequence == 11:
                raise OSError('Synthetic interrupted promotion')
            original_promote(request, directory, sequence, entry, content)
        with self.execution_boundaries(), patch.object(maintenance, '_promote_file', side_effect=fail), self.assertRaises(OSError):
            self.execute()
        self.assertTrue(self.stopped)
        self.assertEqual(hashlib.sha256((self.repository / self.manifest['files'][0]['path']).read_bytes()).hexdigest(),
                         self.manifest['files'][0]['after_sha256'])
        self.assertEqual(hashlib.sha256((self.repository / self.manifest['files'][1]['path']).read_bytes()).hexdigest(),
                         self.manifest['files'][1]['before_sha256'])
        with self.execution_boundaries(), self.assertRaisesRegex(ValueError, 'replay'):
            self.execute()

    def test_replaced_manager_lock_after_stop_denies_promotion(self):
        stop = self.stop
        def replacement(**arguments):
            result = stop(**arguments)
            (self.base / 'manager.lock').rename(self.base / 'retired-manager.lock')
            (self.base / 'manager.lock').touch(mode=0o600)
            return result
        with self.execution_boundaries(), patch.object(maintenance.local_app, 'stop', side_effect=replacement), self.assertRaisesRegex(ValueError, 'replaced'):
            self.execute()
        self.assertFalse((Path(self.store.record.directory) / 'receipt.json').exists())

    def test_store_replacement_symlink_and_partial_record_fail_closed(self):
        path = Path(self.store.record.directory)
        with self.assertRaises(FileExistsError):
            maintenance.provision_native_maintenance_store()
        (path / 'store.json').rename(path / 'old-store.json')
        (path / 'store.json').symlink_to(path / 'old-store.json')
        with self.assertRaises((OSError, ValueError)):
            with maintenance._maintenance_lock(self.store):
                self.fail('Symlink store admitted')

    def test_controller_change_and_cancellation_leave_quiesced_without_restart(self):
        original_handler = self.handler
        def changed(request):
            response = original_handler(request)
            if request.url.path == '/api/restart/quiesce':
                self.control['generation'] += 1
            return response
        with self.execution_boundaries(), patch.object(self, 'handler', side_effect=changed), self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.stopped)
        self.assertTrue(self.tasks['restart_quiesced'])
        self.assertTrue((Path(self.store.record.directory) / 'failure.json').exists())

    def test_interrupt_during_stop_persists_failure_and_never_releases_quiesce(self):
        with self.execution_boundaries(), patch.object(maintenance.local_app, 'stop', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertTrue(self.tasks['restart_quiesced'])
        self.assertFalse(self.stopped)
        failure = json.loads((Path(self.store.record.directory) / 'failure.json').read_text())
        self.assertEqual(failure['exception_type'], 'KeyboardInterrupt')
        self.assertFalse(failure['automatic_restart'])

    def test_quiet_gpu_observer_rejects_active_process_or_wrong_device(self):
        quiet = subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'')
        identity = subprocess.CompletedProcess([], 0, stdout=(self.request.gpu_uuid + '\n').encode(), stderr=b'')
        with patch.object(maintenance, '_command', side_effect=[quiet, identity]):
            maintenance._gpu_quiet(self.request)
        active = subprocess.CompletedProcess([], 0, stdout=b'123, synthetic-worker, 1\n', stderr=b'')
        with patch.object(maintenance, '_command', return_value=active), self.assertRaises(ValueError):
            maintenance._gpu_quiet(self.request)
        wrong = subprocess.CompletedProcess([], 0, stdout=b'GPU-wrong\n', stderr=b'')
        with patch.object(maintenance, '_command', side_effect=[quiet, wrong]), self.assertRaises(ValueError):
            maintenance._gpu_quiet(self.request)

    def test_actual_asgi_quiesce_contract_and_scheduler_latch(self):
        import asyncio
        from types import SimpleNamespace
        from aos.contracts import Settings
        from aos.decision import FixtureDecisionEngine
        from aos.desktop_console import create_console
        from aos.desktop_control import DesktopController
        from aos.desktop_tasks import DesktopScheduler
        from aos.storage import TrajectoryStore
        database = self.root / 'synthetic-asgi.sqlite'
        store = TrajectoryStore(database)
        runtime = SimpleNamespace(runtime_id='synthetic-asgi-runtime', pins={'image_id': 'synthetic-image'})
        controller = DesktopController(store, runtime)
        scheduler = DesktopScheduler(controller, Settings(workspace=self.root / 'asgi-workspace', database=database), FixtureDecisionEngine())
        app = create_console(controller, 'synthetic-token', 'http://testserver', self.root,
                             scheduler=scheduler, web_profiles_root=self.root / 'asgi-profiles')
        async def forward(request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                         headers={'Origin': 'http://testserver'}) as client:
                self.assertEqual((await client.post('/api/login', json={'token': 'synthetic-token'})).status_code, 200)
                response = await client.post(request.url.path, content=request.content,
                                             headers={'Content-Type': 'application/json'})
                return httpx.Response(response.status_code, content=response.content)
        try:
            with httpx.Client(transport=httpx.MockTransport(lambda request: asyncio.run(forward(request))),
                              base_url='http://testserver') as client:
                control = maintenance.NativeControlSnapshot.model_validate({name: controller.state()[name]
                    for name in maintenance.NativeControlSnapshot.model_fields})
                self.assertEqual(client.post('/api/restart/quiesce', json={'expected_session_id': control.session_id}).status_code, 400)
                maintenance._quiesce(client, control)
                self.assertTrue(scheduler.restart_quiesced)
        finally:
            asyncio.run(scheduler.close())
            store.close()

    def test_physical_cleanup_inspects_original_fixed_docker_daemon(self):
        from types import SimpleNamespace
        directory = self.base / self.state.session
        workspace = directory / 'workspace'
        workspace.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
        journals = directory / '.aos-lifecycle'
        journals.mkdir(mode=0o700)
        name = 'desktop-' + 'e' * 32 + '.jsonl'
        (journals / name).touch(mode=0o600)
        descriptor = maintenance.local_app.private_directory(workspace)
        try:
            identity = maintenance.workspace_identity(workspace, descriptor)
        finally:
            os.close(descriptor)
        container = 'f' * 64
        events = [SimpleNamespace(birth=SimpleNamespace(process=self.backend, workspace=identity,
                    runtime_id=name[:-6])), SimpleNamespace(stage='removed', container_id=container)]
        observation = maintenance.NativeAbsenceObservation(recorded_at='2026-10-03T00:00:00Z',
            boot_id=self.identity.boot_id, retired_processes=[self.supervisor, self.backend], scan_sha256=['1' * 64] * 2)
        absent = subprocess.CompletedProcess([], 1, stdout=b'',
            stderr=('Error response from daemon: No such container: ' + container).encode())
        with patch.object(maintenance, 'observe_native_absence', return_value=observation), \
                patch.object(maintenance, '_gpu_quiet'), patch.object(maintenance, 'read_journal', return_value=(events, '2' * 64)), \
                patch.object(maintenance, '_command', return_value=absent) as command:
            maintenance._physical_cleanup(self.request, self.state, [self.supervisor, self.backend])
        self.assertEqual(command.call_args.args[1], ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock',
            'inspect', '--type', 'container', '--format', '{{.Id}}', container])

    def test_concrete_readonly_absence_checks_repeated_scan_and_foreign_generation(self):
        with patch.object(maintenance, 'observe_process', return_value='not_observed'), \
                patch.object(maintenance, '_native_inventory', return_value=([], [])) as inventory:
            observation = maintenance.observe_native_absence(self.request, [self.supervisor, self.backend], deadline=self.request.expires_boottime)
        self.assertEqual(inventory.call_count, 2)
        self.assertFalse(observation.gpu_release_verified)
        with patch.object(maintenance, 'observe_process', return_value='different_process'), self.assertRaises(ValueError):
            maintenance.observe_native_absence(self.request, [self.supervisor, self.backend], deadline=self.request.expires_boottime)
        with patch.object(maintenance, 'observe_process', return_value='not_observed'), \
                patch.object(maintenance, '_native_inventory', return_value=([], [self.backend])), self.assertRaises(ValueError):
            maintenance.observe_native_absence(self.request, [self.supervisor, self.backend], deadline=self.request.expires_boottime)

    def test_real_cpu_process_absence_never_signals_shared_group(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.read()'], stdin=subprocess.PIPE)
        try:
            identity = process_identity(child.pid)
            child.communicate(timeout=5)
            retired = [identity, self.supervisor]
            with patch.object(maintenance, '_native_inventory', return_value=([], [])):
                observation = maintenance.observe_native_absence(self.request, retired, deadline=self.request.expires_boottime)
            self.assertTrue(observation.native_processes_absent)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait()

    def test_actual_relative_script_and_module_process_inventory(self):
        worker = self.repository / 'services/decider/worker.py'
        worker.write_text('import sys\nsys.stdout.write("ready\\n");sys.stdout.flush();sys.stdin.read()\n')
        module = self.repository / 'src/aos/cli.py'
        module.write_text('import sys\nsys.stdout.write("ready\\n");sys.stdout.flush();sys.stdin.read()\n')
        (module.parent / '__init__.py').touch()
        for arguments in ([sys.executable, 'services/decider/worker.py'], [sys.executable, '-m', 'aos.cli'],
                          [sys.executable, '-maos.cli']):
            with self.subTest(arguments=arguments):
                child = subprocess.Popen(arguments, cwd=self.repository, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(self.repository / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
                try:
                    self.assertEqual(child.stdout.readline(), b'ready\n')
                    identity = process_identity(child.pid)
                    synthetic_proc = self.root / ('proc-' + str(identity.pid))
                    synthetic_proc.mkdir()
                    (synthetic_proc / str(identity.pid)).symlink_to(Path('/proc') / str(identity.pid), target_is_directory=True)
                    original_path = Path
                    with patch.object(maintenance, 'Path', side_effect=lambda value: synthetic_proc if value == '/proc' else original_path(value)):
                        _inventory, matches = maintenance._native_inventory(self.request, self.request.expires_boottime)
                    self.assertEqual(matches, [identity])
                finally:
                    child.communicate(timeout=5)
        entries = maintenance._reviewed_entry_paths(self.request)
        with self.assertRaises(ValueError):
            maintenance._entry_matches(['python', '-m', 'aos.cli', '-m', 'aos.cli'], self.repository, entries)

    def test_floating_point_deadline_exact_bound_does_not_renew_authority(self):
        issued = 32080.3521725
        expires = issued + 900
        self.assertGreater(expires - issued, 900)
        payload = self.request.model_dump(mode='json') | {'issued_boottime': issued, 'expires_boottime': expires}
        request = maintenance.NativeMaintenanceRequest.model_validate(payload)
        self.assertEqual(request.expires_boottime, expires)
        for invalid in (math.nextafter(expires, math.inf), issued, issued - 1, math.inf, math.nan):
            with self.subTest(expires=invalid), self.assertRaises(ValueError):
                maintenance.NativeMaintenanceRequest.model_validate(payload | {'expires_boottime': invalid})

    def test_canonical_schema_equality_and_request_validation(self):
        for model, name in [(maintenance.NativeMaintenanceStore, 'native_maintenance_store'),
                            (maintenance.NativeMaintenanceRequest, 'native_maintenance_request'),
                            (maintenance.NativeMaintenanceReceipt, 'native_maintenance_receipt')]:
            schema = json.loads((ROOT / 'schemas' / (name + '.schema.json')).read_text())
            self.assertEqual(schema, model.model_json_schema())
        jsonschema.validate(self.request.model_dump(mode='json'), maintenance.NativeMaintenanceRequest.model_json_schema())
        with self.assertRaises(ValueError):
            maintenance.NativeMaintenanceRequest.model_validate(self.request.model_dump(mode='json') | {'expires_boottime': self.request.issued_boottime + 901})
        with self.assertRaises(ValueError):
            maintenance.NativeMaintenanceRequest.model_validate(self.request.model_dump(mode='json') | {'source_sha256': '0' * 64})


if __name__ == '__main__':
    unittest.main()

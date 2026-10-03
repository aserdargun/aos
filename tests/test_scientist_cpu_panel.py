"""Synthetic local launcher checks; no Docker, Scientist API or model execution."""

import asyncio
import copy
import fcntl
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from aos.contracts import REPO_ROOT
from aos.scientist_cpu_capability import CPU_SCHEMA_SHA256, cpu_capability_sha256
from aos.scientist_transport import ScientistAdmissionError
from scripts import scientist_cpu_panel as panel
from scripts import serve_desktop
from test_desktop_tasks import FixtureDesktop


class ScientistCpuPanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-cpu-panel-')
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.root = self.repo / 'data/local-app-project-scientist-synthetic'
        self.root.mkdir(parents=True, mode=0o700)
        (self.root / 'review').mkdir(mode=0o700)
        (self.root / 'ui').mkdir(mode=0o700)
        (self.root / 'ui/index.html').write_text('<title>Synthetic UI only</title>')
        (self.repo / 'models').mkdir()
        (self.repo / 'models/desktop-manifest.json').write_text('{}')
        (self.root / 'review/scientist.token').write_text('synthetic-private-token\n')
        (self.root / 'review/scientist.token').chmod(0o600)
        capability = json.loads((REPO_ROOT / 'examples/scientist_cpu_capability.json').read_text())
        capability.update(max_experiments=1, max_wall_seconds=600)
        self.startup = {'authority_url': 'http://127.0.0.1:19999',
            'token_file': str(self.root / 'review/scientist.token'),
            'principal_id': capability['owner_id'], 'allowed_suites': [capability['suite_id']],
            'program_version': capability['program_version'], 'authorization_context_sha256': 'a' * 64}
        self.grant = {'profile': 'scientist-cpu-mode-grid.v1',
            'authority_url': self.startup['authority_url'], 'principal_id': capability['owner_id'],
            'authorization_context_sha256': 'a' * 64, 'scientist_source_manifest_sha256': 'b' * 64,
            'aos_source_manifest_sha256': 'c' * 64, 'capability_schema_sha256': CPU_SCHEMA_SHA256,
            'capability': capability, 'capability_sha256': cpu_capability_sha256(capability)}
        self.startup_sha = self.write_review('startup', self.startup)
        self.grant_sha = self.write_review('grant', self.grant)
        for name in ('REPO_ROOT', 'SOURCE_ROOT'):
            patcher = patch.object(panel, name, self.repo)
            patcher.start()
            self.addCleanup(patcher.stop)
        snapshot = patch.object(panel, 'source_snapshot', return_value=('c' * 64, []))
        self.snapshot = snapshot.start()
        self.addCleanup(snapshot.stop)

    def write_review(self, name, value):
        payload = json.dumps(value).encode()
        path = self.root / 'review' / (name + '.json')
        path.write_bytes(payload)
        path.chmod(0o600)
        return hashlib.sha256(payload).hexdigest()

    def arguments(self, *extra):
        return ['--project', 'scientist-synthetic', '--session', 'app-' + 'd' * 32,
            '--port', '19998', '--startup-sha256', self.startup_sha,
            '--grant-sha256', self.grant_sha, '--source-manifest-sha256', 'c' * 64,
            '--desktop-manifest-sha256', hashlib.sha256(b'{}').hexdigest(),
            '--ui-artifacts-sha256', panel.ui_artifacts_sha256(self.root / 'ui'), *extra]

    def denied(self, arguments=None):
        with patch('sys.stderr', StringIO()) as stderr, patch.object(panel, 'start') as start:
            with self.assertRaises(SystemExit) as raised:
                panel.main(self.arguments() if arguments is None else arguments)
            self.assertEqual(raised.exception.code, 2)
            start.assert_not_called()
            self.assertNotIn('synthetic-private-token', stderr.getvalue())
            self.assertFalse(any(path.name.startswith('app-') for path in self.root.iterdir()))

    def test_default_check_is_local_read_only_and_does_not_reserve_port(self):
        before = sorted(path.relative_to(self.root) for path in self.root.rglob('*'))
        with patch('sys.stdout', StringIO()) as stdout, patch.object(panel.socket, 'socket') as sockets, \
                patch('aos.scientist_lab.ScientistLabClient._request') as remote, \
                patch.object(panel, 'start') as start:
            panel.main(self.arguments())
            report = json.loads(stdout.getvalue())
            self.assertTrue(report['configuration_verified'])
            self.assertFalse(report['runtime_started'])
            self.assertFalse(report['remote_requested'])
            self.assertFalse(report['gpu_authorized'])
            self.assertNotIn('synthetic-private-token', stdout.getvalue())
            sockets.assert_not_called()
            remote.assert_not_called()
            start.assert_not_called()
        self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob('*')))

    def test_review_hash_mismatch_duplicates_permissions_and_symlinks_are_denied(self):
        path = self.root / 'review/startup.json'
        original = path.read_bytes()
        path.write_bytes(original + b' ')
        self.denied()
        path.write_bytes(original)
        path.chmod(0o644)
        self.denied()
        path.chmod(0o600)
        saved = path.with_suffix('.saved')
        path.rename(saved)
        path.symlink_to(saved)
        self.denied()
        path.unlink()
        saved.rename(path)
        payload = original[:-1] + b',"principal_id":"synthetic-duplicate"}'
        path.write_bytes(payload)
        self.startup_sha = hashlib.sha256(payload).hexdigest()
        self.denied()

    def test_unreviewed_scope_owner_source_and_budgets_fail_before_runtime(self):
        for change in ({'principal_id': 'synthetic-wrong-owner'},
                       {'aos_source_manifest_sha256': '0' * 64}):
            with self.subTest(change=change):
                self.grant_sha = self.write_review('grant', self.grant | change)
                self.denied()
        for change in ({'max_experiments': 2}, {'max_wall_seconds': 601}, {'model_tokens': 1}):
            with self.subTest(change=change):
                grant = copy.deepcopy(self.grant)
                grant['capability'].update(change)
                grant['capability_sha256'] = cpu_capability_sha256(grant['capability'])
                self.grant_sha = self.write_review('grant', grant)
                self.denied()
        self.grant_sha = self.write_review('grant', self.grant)
        self.startup_sha = self.write_review('startup', self.startup | {'allowed_suites': ['synthetic.other']})
        self.denied()
        self.startup_sha = self.write_review('startup', self.startup)
        self.snapshot.return_value = ('e' * 64, [])
        self.denied()

    def test_artifact_changes_and_unsafe_identity_are_denied(self):
        arguments = self.arguments()
        (self.root / 'ui/index.html').write_text('changed after review')
        self.denied(arguments)
        arguments = self.arguments()
        (self.repo / 'models/desktop-manifest.json').write_text('changed after review')
        self.denied(arguments)
        for flag, value in (('--port', '8765'), ('--project', 'pilot'), ('--session', '../outside')):
            with self.subTest(flag=flag):
                arguments = self.arguments()
                arguments[arguments.index(flag) + 1] = value
                self.denied(arguments)

    def test_start_binds_first_scopes_stores_disables_gpu_and_retains_failed_session(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        arguments = self.arguments('--start')
        arguments[arguments.index('--port') + 1] = str(port)
        session = self.root / ('app-' + 'd' * 32)
        original_arguments = sys.argv
        original_cuda = os.environ.get('CUDA_VISIBLE_DEVICES')

        def failed_serve(**configuration):
            self.assertEqual(configuration['scientist_lab_config'].principal_id, self.startup['principal_id'])
            self.assertEqual(configuration['scientist_cpu_grant'].capability.model_tokens, 0)
            self.assertEqual(os.environ['CUDA_VISIBLE_DEVICES'], '-1')
            self.assertNotIn('--local-ui-auto-login', sys.argv)
            for option in (*panel.STORE_OPTIONS, 'workspace', 'database', 'trajectory-database'):
                self.assertTrue(Path(sys.argv[sys.argv.index('--' + option) + 1]).is_relative_to(session))
            with socket.socket(fileno=os.dup(int(sys.argv[sys.argv.index('--listen-fd') + 1]))) as listener:
                self.assertEqual(listener.getsockname(), ('127.0.0.1', port))
                self.assertEqual(listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN), 1)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            raise RuntimeError('synthetic startup failure')

        with patch('scripts.serve_desktop.main', side_effect=failed_serve) as serve:
            with self.assertRaisesRegex(RuntimeError, 'synthetic startup failure'):
                panel.main(arguments)
            serve.assert_called_once()
        self.assertIs(sys.argv, original_arguments)
        self.assertEqual(os.environ.get('CUDA_VISIBLE_DEVICES'), original_cuda)
        self.assertTrue(session.is_dir())
        self.assertEqual(session.stat().st_mode & 0o777, 0o700)
        with patch('sys.stderr', StringIO()), patch.object(panel, 'start') as start:
            with self.assertRaises(SystemExit):
                panel.main(arguments)
            start.assert_not_called()
        with socket.socket() as released:
            released.bind(('127.0.0.1', port))

    def test_busy_port_does_not_create_session_or_call_backend(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            reservation.listen()
            arguments = self.arguments('--start')
            arguments[arguments.index('--port') + 1] = str(reservation.getsockname()[1])
            with patch('scripts.serve_desktop.main') as serve, patch('sys.stderr', StringIO()):
                with self.assertRaises(SystemExit):
                    panel.main(arguments)
                serve.assert_not_called()
        self.assertFalse(any(path.name.startswith('app-') for path in self.root.iterdir()))

    def test_public_command_composes_real_cpu_console_with_private_ui_and_cookie(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        arguments = self.arguments('--start', '--local-ui-auto-login')
        arguments[arguments.index('--port') + 1] = str(port)
        runtime = FixtureDesktop(self.root / ('app-' + 'd' * 32) / 'workspace')
        runtime.pins = {'image_id': 'sha256:' + 'a' * 64}
        runtime.docker = Mock()
        runtime.container_id = 'synthetic-container'
        services = []

        def run(app, **options):
            async def lifecycle():
                async with app.router.lifespan_context(app):
                    services.append(console.call_args.kwargs['scientist_lab'])
                    self.assertEqual(console.call_args.kwargs['ui_root'], self.root / 'ui')
                    origin = f'http://127.0.0.1:{port}'
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                        self.assertEqual((await client.get('/api/scientist/jobs')).status_code, 401)
                        response = await client.post('/api/login/local', headers={'Origin': origin}, json={})
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(f'aos_project_scientist-synthetic_{port}', client.cookies)
                        self.assertNotIn('aos_session', client.cookies)
                        inventory = await client.get('/api/scientist/jobs')
                        self.assertEqual(inventory.status_code, 200)
                        self.assertTrue(inventory.json()['configured'])
                        self.assertFalse(inventory.json()['joint_runtime_admitted'])
                        self.assertEqual(inventory.json()['request_limits']['model_tokens'], 0)
                        self.assertEqual((await client.get('/ui/')).text, '<title>Synthetic UI only</title>')
            asyncio.run(lifecycle())

        with patch('sys.stdout', StringIO()), patch.object(serve_desktop, 'REPO_ROOT', self.repo), \
                patch('aos.desktop_console.REPO_ROOT', self.repo), \
                patch.object(serve_desktop, 'DesktopRuntime', return_value=runtime), \
                patch.object(serve_desktop, 'create_console', wraps=serve_desktop.create_console) as console, \
                patch.object(serve_desktop.uvicorn, 'run', side_effect=run), \
                patch('aos.scientist_lab.ScientistLabClient._request') as remote, \
                patch.object(serve_desktop.DeciderEngine, '__init__', side_effect=AssertionError('Native forbidden')) as native, \
                patch.object(serve_desktop.BonsaiVisionSupervisor, '__init__', side_effect=AssertionError('Vision forbidden')) as vision:
            panel.main(arguments)
        remote.assert_not_called()
        native.assert_not_called()
        vision.assert_not_called()
        self.assertTrue(services[0].client._closed)
        self.assertIsNone(runtime.descriptor)
        self.assertEqual(list((self.repo / 'runs').glob('*.token')), [])

    def test_transport_configuration_failure_does_not_disclose_private_exception(self):
        with patch.object(panel, 'prepare_scientist_cpu_startup',
                          side_effect=ScientistAdmissionError('synthetic-private-token')):
            self.denied()


if __name__ == '__main__':
    unittest.main()

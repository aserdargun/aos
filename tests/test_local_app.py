import json
import hashlib
from io import StringIO
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from aos import local_app
from aos.contracts import REPO_ROOT
from aos.lifecycle import LifecycleBirth, LifecycleEvent, process_identity
from aos.workspace_identity import open_existing_workspace, workspace_identity


class LocalAppTests(unittest.TestCase):
    def test_help_exposes_learning_entry_and_explicit_reuse_without_starting(self):
        output = StringIO()
        with patch('sys.argv', ['aos-v1', '--help']), patch('sys.stdout', output), \
                patch('aos.local_app.start') as start, patch('aos.local_app.read_state') as read_state:
            with self.assertRaises(SystemExit) as stopped:
                local_app.main()
        self.assertEqual(stopped.exception.code, 0)
        help_text = ' '.join(output.getvalue().split())
        self.assertIn('--owned-synthetic-form-invocation', help_text)
        self.assertIn('synthetic learning pilot', help_text)
        self.assertIn('Exact stopped-source preview SHA-256', help_text)
        self.assertNotIn('--owned-learning-lock-fd', help_text)
        start.assert_not_called()
        read_state.assert_not_called()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / 'local-app-v1'
        self.patcher = patch('aos.local_app.BASE', self.base)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.state = local_app.LocalAppState(session='app-' + 'a' * 32, mode='fixture', phase='starting',
                                            supervisor=process_identity(os.getpid()), started_at='synthetic')

    def test_new_private_state_roundtrip_and_no_token_in_status(self):
        self.assertEqual(local_app.current_status()['phase'], 'not_started')
        local_app.prepare_base()
        self.assertEqual(self.base.stat().st_mode & 0o777, 0o700)
        local_app.write_state(self.state)
        self.assertEqual(local_app.read_state(), self.state)
        self.assertEqual((self.base / 'current.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('token_file', local_app.current_status())
        self.assertNotIn('token_value', local_app.current_status())

    def test_managed_route_review_pins_private_historical_snapshot(self):
        with tempfile.TemporaryDirectory(prefix='managed-review-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            database = root / 'source.sqlite'
            database.write_bytes(b'synthetic')
            database.chmod(0o600)
            site_store = root / 'site'
            review_store = root / 'reviews'
            profile_sha256 = 'a' * 64
            plan_sha256 = 'b' * 64
            review_sha256 = 'c' * 64
            with patch('aos.remote_route_knowledge_review.prepare_live_remote_route_knowledge',
                       return_value=SimpleNamespace(source_snapshot_sha256='d' * 64)) as prepare:
                prepared = local_app.prepare_managed_remote_route_review(
                    'real', profile_sha256, plan_sha256,
                    database, site_store, review_store, review_sha256)
            self.assertEqual(prepared[:5], (database, site_store, review_store,
                                            review_sha256, 'd' * 64))
            self.assertEqual(len(prepared[5]), 64)
            self.assertEqual(prepare.call_args.kwargs['selected_plan_sha256'], plan_sha256)
            self.assertEqual(prepare.call_args.kwargs['selected_profile_sha256'], profile_sha256)
            command = local_app.managed_backend_command(
                self.base / self.state.session, 'real', 23,
                remote_entry_profile_sha256=profile_sha256,
                remote_entry_task_sha256='e' * 64,
                remote_routes_plan_sha256=plan_sha256,
                remote_route_review_source_database=database,
                remote_route_review_site_store=site_store,
                remote_route_review_store=review_store,
                remote_route_review_sha256=review_sha256,
                remote_route_review_source_snapshot_sha256='d' * 64)
            self.assertEqual(command[command.index('--remote-route-review-source-snapshot-sha256') + 1],
                             'd' * 64)
            self.assertEqual(command[command.index('--remote-route-review-sha256') + 1],
                             review_sha256)
            with patch('aos.remote_route_knowledge_review.prepare_live_remote_route_knowledge',
                       return_value=SimpleNamespace(source_snapshot_sha256='f' * 64)):
                changed = local_app.prepare_managed_remote_route_review(
                    'real', profile_sha256, plan_sha256,
                    database, site_store, review_store, review_sha256)
            self.assertNotEqual(changed[5], prepared[5])
            with self.assertRaisesRegex(ValueError, 'four exact options'):
                local_app.prepare_managed_remote_route_review(
                    'real', profile_sha256, plan_sha256,
                    database, site_store, review_store, None)
            with self.assertRaisesRegex(ValueError, 'private data'):
                local_app.prepare_managed_remote_route_review(
                    'real', profile_sha256, plan_sha256,
                    Path('/tmp/source.sqlite'), site_store, review_store, review_sha256)
            database.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'unavailable or stale'):
                local_app.prepare_managed_remote_route_review(
                    'real', profile_sha256, plan_sha256,
                    database, site_store, review_store, review_sha256)
            with self.assertRaisesRegex(ValueError, 'Invalid managed backend mode'):
                local_app.managed_backend_command(
                    self.base / self.state.session, 'fixture', 23,
                    remote_route_review_source_database=database,
                    remote_route_review_site_store=site_store,
                    remote_route_review_store=review_store,
                    remote_route_review_sha256=review_sha256,
                    remote_route_review_source_snapshot_sha256='d' * 64)

    def test_managed_json_review_requires_private_v2_source_pin(self):
        with tempfile.TemporaryDirectory(prefix='managed-json-review-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            database = root / 'source.sqlite'
            database.write_bytes(b'synthetic')
            database.chmod(0o600)
            site_store = root / 'site'
            review_store = root / 'reviews'
            profile_sha256 = 'a' * 64
            plan_sha256 = 'b' * 64
            review_sha256 = 'c' * 64
            with patch('aos.remote_readonly_data_knowledge_review.prepare_live_remote_readonly_data_knowledge',
                       return_value=SimpleNamespace(source_snapshot_sha256='d' * 64)) as prepare:
                prepared = local_app.prepare_managed_remote_json_review(
                    'real', profile_sha256, plan_sha256,
                    database, site_store, review_store, review_sha256)
            self.assertEqual(prepared[:5], (database, site_store, review_store,
                                            review_sha256, 'd' * 64))
            self.assertEqual(prepare.call_args.kwargs['selected_plan_sha256'], plan_sha256)
            command = local_app.managed_backend_command(
                self.base / self.state.session, 'real', 23,
                remote_entry_profile_sha256=profile_sha256,
                remote_entry_task_sha256='e' * 64,
                remote_static_assets_plan_sha256=plan_sha256,
                remote_json_review_source_database=database,
                remote_json_review_site_store=site_store,
                remote_json_review_store=review_store,
                remote_json_review_sha256=review_sha256,
                remote_json_review_source_snapshot_sha256='d' * 64)
            self.assertEqual(command[command.index('--remote-json-review-source-snapshot-sha256') + 1],
                             'd' * 64)
            with self.assertRaisesRegex(ValueError, 'four exact options'):
                local_app.prepare_managed_remote_json_review(
                    'real', profile_sha256, plan_sha256,
                    database, site_store, review_store, None)
            with self.assertRaisesRegex(ValueError, 'private data'):
                local_app.prepare_managed_remote_json_review(
                    'real', profile_sha256, plan_sha256,
                    Path('/tmp/source.sqlite'), site_store, review_store, review_sha256)
            database.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'unavailable or stale'):
                local_app.prepare_managed_remote_json_review(
                    'real', profile_sha256, plan_sha256,
                    database, site_store, review_store, review_sha256)
            with self.assertRaisesRegex(ValueError, 'Invalid managed backend mode'):
                local_app.managed_backend_command(
                    self.base / self.state.session, 'fixture', 23,
                    remote_json_review_source_database=database,
                    remote_json_review_site_store=site_store,
                    remote_json_review_store=review_store,
                    remote_json_review_sha256=review_sha256,
                    remote_json_review_source_snapshot_sha256='d' * 64)

    def test_preview_remote_json_review_is_offline_and_does_not_start(self):
        profile_sha256 = 'a' * 64
        plan_sha256 = 'b' * 64
        review_sha256 = 'c' * 64
        database = REPO_ROOT / 'data' / 'synthetic-json-source.sqlite'
        site_store = REPO_ROOT / 'data' / 'synthetic-json-site'
        review_store = REPO_ROOT / 'data' / 'synthetic-json-review'
        command = ['aos-v1', 'preview-remote-json-review',
                   '--remote-entry-profile-sha256', profile_sha256,
                   '--remote-entry-task-file', str(REPO_ROOT / 'data' / 'task.json'),
                   '--remote-readonly-data-plan-file', str(REPO_ROOT / 'data' / 'plan.json'),
                   '--remote-json-review-source-database', str(database),
                   '--remote-json-review-site-store', str(site_store),
                   '--remote-json-review-store', str(review_store),
                   '--remote-json-review-sha256', review_sha256]
        review = (database, site_store, review_store, review_sha256,
                  'd' * 64, 'e' * 64)
        with (patch('sys.argv', command),
              patch('sys.stdout', new_callable=StringIO) as output,
              patch('aos.local_app.prepare_remote_static_assets',
                    return_value=(b'{"schema_version":"2.0"}', plan_sha256)) as plan,
              patch('aos.local_app.prepare_managed_remote_json_review',
                    return_value=review) as prepare,
              patch('aos.local_app.prepare_base') as prepare_base,
              patch('aos.local_app.subprocess.Popen') as launch):
            local_app.main()
        self.assertEqual(json.loads(output.getvalue()), {
            'profile_sha256': profile_sha256, 'plan_sha256': plan_sha256,
            'review_sha256': review_sha256,
            'source_snapshot_sha256': 'd' * 64,
            'status': 'validated_historical_metadata',
            'execution_authorized': False, 'collection_authorized': False,
            'training_ready': False})
        plan.assert_called_once()
        prepare.assert_called_once_with('real', profile_sha256, plan_sha256,
                                        database, site_store, review_store, review_sha256)
        prepare_base.assert_not_called()
        launch.assert_not_called()

    def test_managed_json_review_start_forwards_exact_source_pin(self):
        source = REPO_ROOT / 'data' / 'synthetic-json-source.sqlite'
        site_store = REPO_ROOT / 'data' / 'synthetic-json-site'
        review_store = REPO_ROOT / 'data' / 'synthetic-json-review'
        review_sha256 = 'c' * 64
        source_sha256 = 'f' * 64
        prepared = (source, site_store, review_store, review_sha256,
                    'd' * 64, source_sha256)
        running = local_app.LocalAppState(
            session='app-' + 'c' * 32, mode='real', phase='running',
            supervisor=process_identity(os.getpid()), backend=process_identity(os.getpid()),
            started_at='synthetic', remote_entry_profile_sha256='a' * 64,
            remote_entry_task_sha256='e' * 64,
            remote_static_assets_plan_sha256='b' * 64,
            remote_json_review_sha256=review_sha256,
            remote_json_review_source_sha256=source_sha256)
        with (patch('aos.local_app.prepare_remote_entry', return_value=(b'{}', 'e' * 64)),
              patch('aos.local_app.prepare_remote_static_assets',
                    return_value=(b'{"schema_version":"2.0"}', 'b' * 64)),
              patch('aos.local_app.prepare_managed_remote_json_review',
                    return_value=prepared),
              patch('aos.local_app.socket.socket') as socket_factory,
              patch('aos.local_app.subprocess.Popen') as launch,
              patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='c' * 32)),
              patch('aos.local_app.read_state', side_effect=[None, running, running])):
            listener = socket_factory.return_value.__enter__.return_value
            listener.fileno.return_value = 23
            launch.return_value.wait.return_value = 0
            result = local_app.start(
                'real', check=False, remote_entry_profile_sha256='a' * 64,
                remote_entry_task_file=source,
                remote_readonly_data_plan_file=source,
                remote_json_review_source_database=source,
                remote_json_review_site_store=site_store,
                remote_json_review_store=review_store,
                remote_json_review_sha256=review_sha256)
        self.assertEqual(result['remote_json_review_sha256'], review_sha256)
        self.assertEqual(result['remote_json_review_source_sha256'], source_sha256)
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index('--remote-json-review-source-sha256') + 1],
                         source_sha256)
        self.assertEqual(command[command.index('--remote-json-review-source-database') + 1],
                         str(source))

    def test_preview_remote_route_review_audits_without_starting(self):
        profile_sha256 = 'a' * 64
        plan_sha256 = 'b' * 64
        review_sha256 = 'c' * 64
        source_snapshot_sha256 = 'd' * 64
        database = REPO_ROOT / 'data' / 'synthetic-source.sqlite'
        site_store = REPO_ROOT / 'data' / 'synthetic-site'
        review_store = REPO_ROOT / 'data' / 'synthetic-review'
        command = ['aos-v1', 'preview-remote-route-review',
                   '--remote-entry-profile-sha256', profile_sha256,
                   '--remote-entry-task-file', str(REPO_ROOT / 'data' / 'task.json'),
                   '--remote-routes-plan-file', str(REPO_ROOT / 'data' / 'plan.json'),
                   '--remote-route-review-source-database', str(database),
                   '--remote-route-review-site-store', str(site_store),
                   '--remote-route-review-store', str(review_store),
                   '--remote-route-review-sha256', review_sha256]
        review = (database, site_store, review_store, review_sha256,
                  source_snapshot_sha256, 'e' * 64)
        with (patch('sys.argv', command),
              patch('sys.stdout', new_callable=StringIO) as output,
              patch('aos.local_app.prepare_remote_routes', return_value=(b'{}', plan_sha256)) as routes,
              patch('aos.local_app.prepare_managed_remote_route_review', return_value=review) as prepare,
              patch('aos.local_app.prepare_base') as prepare_base,
              patch('aos.local_app.subprocess.Popen') as launch):
            local_app.main()
        self.assertEqual(json.loads(output.getvalue()), {
            'profile_sha256': profile_sha256, 'plan_sha256': plan_sha256,
            'review_sha256': review_sha256,
            'source_snapshot_sha256': source_snapshot_sha256,
            'status': 'validated_historical_metadata', 'execution_authorized': False,
            'collection_authorized': False, 'training_ready': False})
        self.assertEqual(routes.call_count, 1)
        prepare.assert_called_once_with('real', profile_sha256, plan_sha256,
                                        database, site_store, review_store, review_sha256)
        prepare_base.assert_not_called()
        launch.assert_not_called()
        for invalid_command, route_result, review_result in (
                (command + ['--fixture'], (b'{}', plan_sha256), review),
                (command[:-2], (b'{}', plan_sha256), None),
                (command, None, review)):
            with (patch('sys.argv', invalid_command),
                  patch('sys.stderr', new_callable=StringIO),
                  patch('aos.local_app.prepare_remote_routes', return_value=route_result),
                  patch('aos.local_app.prepare_managed_remote_route_review',
                        return_value=review_result),
                  patch('aos.local_app.prepare_base') as prepare_base,
                  patch('aos.local_app.subprocess.Popen') as launch,
                  self.assertRaises(SystemExit)):
                local_app.main()
            prepare_base.assert_not_called()
            launch.assert_not_called()

    def test_managed_route_review_start_forwards_only_exact_source_pin(self):
        source = REPO_ROOT / 'data' / 'synthetic-source.sqlite'
        site_store = REPO_ROOT / 'data' / 'synthetic-site'
        review_store = REPO_ROOT / 'data' / 'synthetic-review'
        review_sha256 = 'c' * 64
        source_sha256 = 'f' * 64
        prepared = (source, site_store, review_store, review_sha256,
                    'd' * 64, source_sha256)
        running = local_app.LocalAppState(
            session='app-' + 'c' * 32, mode='real', phase='running',
            supervisor=process_identity(os.getpid()), backend=process_identity(os.getpid()),
            started_at='synthetic', remote_entry_profile_sha256='a' * 64,
            remote_entry_task_sha256='e' * 64, remote_routes_plan_sha256='b' * 64,
            remote_route_review_sha256=review_sha256,
            remote_route_review_source_sha256=source_sha256)
        with (patch('aos.local_app.prepare_remote_entry', return_value=(b'{}', 'e' * 64)),
              patch('aos.local_app.prepare_remote_routes', return_value=(b'{}', 'b' * 64)),
              patch('aos.local_app.prepare_managed_remote_route_review', return_value=prepared),
              patch('aos.local_app.socket.socket') as socket_factory,
              patch('aos.local_app.subprocess.Popen') as launch,
              patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='c' * 32)),
              patch('aos.local_app.read_state', side_effect=[None, running, running])):
            listener = socket_factory.return_value.__enter__.return_value
            listener.fileno.return_value = 23
            launch.return_value.wait.return_value = 0
            result = local_app.start(
                'real', check=False, remote_entry_profile_sha256='a' * 64,
                remote_entry_task_file=source, remote_routes_plan_file=source,
                remote_route_review_source_database=source,
                remote_route_review_site_store=site_store,
                remote_route_review_store=review_store,
                remote_route_review_sha256=review_sha256)
        self.assertEqual(result['remote_route_review_sha256'], review_sha256)
        self.assertEqual(result['remote_route_review_source_sha256'], source_sha256)
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index('--remote-route-review-source-sha256') + 1],
                         source_sha256)
        self.assertEqual(command[command.index('--remote-route-review-source-database') + 1],
                         str(source))
        with (patch('sys.argv', ['aos-v1', 'status',
                                 '--remote-route-review-sha256', review_sha256]),
              patch('sys.stderr', new_callable=StringIO) as error,
              self.assertRaises(SystemExit)):
            local_app.main()
        self.assertIn('require start or preview', error.getvalue())

    def test_managed_backend_command_exposes_only_pinned_mcp_task(self):
        for mode in ('real', 'fixture'):
            with self.subTest(mode=mode):
                command = local_app.managed_backend_command(self.base / self.state.session, mode, 23)
                self.assertEqual(command.count('--desktop-navigation-mcp-manifest'), 1)
                self.assertNotIn('--desktop-mcp-manifest', command)
                self.assertEqual(command[command.index('--desktop-navigation-mcp-manifest') + 1],
                                 str(local_app.MCP_MANIFEST))
                self.assertIn('--desktop-browser', command)
                self.assertIn('--browser-tasks', command)
                self.assertIn('--local-ui-auto-login', command)
                self.assertNotIn('--desktop-staging-mcp-manifest', command)
                self.assertNotIn('--synthetic-learning-stream-dir', command)
                self.assertEqual(command[command.index('--engine') + 1],
                                 'decider' if mode == 'real' else 'fixture')
                if mode == 'real':
                    self.assertEqual(command[command.index('--prewarm-idle-seconds') + 1], '300')
                    self.assertEqual(command[command.index('--gpu-idle-seconds') + 1], '30')
                else:
                    self.assertNotIn('--prewarm-idle-seconds', command)
                    self.assertNotIn('--gpu-idle-seconds', command)
                opt_in = local_app.managed_backend_command(
                    self.base / self.state.session, mode, 23, synthetic_staging=True)
                self.assertEqual(opt_in.count('--desktop-staging-mcp-manifest'), 1)
                self.assertNotIn('--local-ui-auto-login', opt_in)
                self.assertEqual(opt_in[opt_in.index('--desktop-staging-mcp-manifest') + 1],
                                 str(local_app.MCP_MANIFEST))
                learning = local_app.managed_backend_command(
                    self.base / self.state.session, mode, 23, synthetic_learning=True)
                self.assertEqual(learning.count('--synthetic-learning-stream-dir'), 1)
                self.assertNotIn('--local-ui-auto-login', learning)
                self.assertEqual(learning[learning.index('--synthetic-learning-stream-dir') + 1],
                                 str(self.base / self.state.session / 'learning-stream'))
        with self.assertRaises(ValueError):
            local_app.managed_backend_command(self.base, 'disabled', 23)
        with self.assertRaises(ValueError):
            local_app.managed_backend_command(self.base, 'real', 23, synthetic_staging='true')
        with self.assertRaises(ValueError):
            local_app.managed_backend_command(self.base, 'real', 23, synthetic_learning='true')

    def test_owned_invocation_state_requires_exclusive_exact_pins(self):
        pins = {'owned_synthetic_form_invocation': True,
                'owned_form_manifest_sha256': 'a' * 64,
                'owned_form_invocation_sha256': 'b' * 64}
        state = local_app.LocalAppState(
            session='app-' + 'b' * 32, mode='real', phase='starting',
            supervisor=process_identity(os.getpid()), started_at='synthetic', **pins)
        self.assertTrue(state.owned_synthetic_form_invocation)
        for change in (
                {'mode': 'fixture'},
                {'owned_form_manifest_sha256': None},
                {'owned_form_invocation_sha256': None},
                {'synthetic_staging': True},
                {'synthetic_learning': True},
                {'remote_form_plan_sha256': 'c' * 64},
                {'remote_form_cookie_sha256': 'd' * 64},
                {'owned_synthetic_form_invocation': False},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                local_app.LocalAppState.model_validate({**state.model_dump(), **change})
        with self.assertRaises(ValueError):
            local_app.LocalAppState(
                session='app-' + 'c' * 32, mode='real', phase='starting',
                supervisor=process_identity(os.getpid()), started_at='synthetic',
                owned_synthetic_form_invocation=False,
                owned_form_invocation_sha256='b' * 64)

    def test_owned_invocation_backend_command_forwards_only_fd_and_manifest_pin(self):
        command = local_app.managed_backend_command(
            self.base / self.state.session, 'real', 23,
            owned_form_listener_fd=41, owned_form_manifest_sha256='a' * 64)
        self.assertEqual(command[command.index('--owned-form-listener-fd') + 1], '41')
        self.assertNotIn('--local-ui-auto-login', command)
        self.assertEqual(command[command.index('--owned-form-manifest-sha256') + 1], 'a' * 64)
        self.assertNotIn('--remote-form-public-plan-sha256', command)
        self.assertNotIn('--remote-form-public-state-plan-sha256', command)
        self.assertNotIn('--remote-form-cookie-file', command)
        for options in (
                {'owned_form_listener_fd': 41},
                {'owned_form_manifest_sha256': 'a' * 64},
                {'owned_form_listener_fd': 41, 'owned_form_manifest_sha256': 'bad'},
                {'owned_form_listener_fd': 41, 'owned_form_manifest_sha256': 'a' * 64,
                 'remote_form_plan_sha256': 'b' * 64},
                {'owned_form_listener_fd': 41, 'owned_form_manifest_sha256': 'a' * 64,
                 'synthetic_staging': True},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                local_app.managed_backend_command(
                    self.base / self.state.session, 'real', 23, **options)

    def test_public_start_rejects_owned_mode_before_socket_or_spawn(self):
        with (patch('aos.local_app.socket.socket') as make_socket,
              patch('aos.local_app.subprocess.Popen') as launch):
            with self.assertRaisesRegex(ValueError, 'exclusive opt-in mode'):
                local_app.start('fixture', check=False,
                                owned_synthetic_form_invocation=True)
        make_socket.assert_not_called()
        launch.assert_not_called()

    def test_owned_start_provisions_private_bundle_and_hands_listener_to_supervisor(self):
        class ReservedSocket:
            def __init__(self, descriptor, port):
                self.descriptor = descriptor
                self.port = port

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def setsockopt(self, *_args):
                return None

            def bind(self, _address):
                return None

            def listen(self, _backlog):
                return None

            def fileno(self):
                return self.descriptor

            def getsockname(self):
                return ('127.0.0.1', self.port)

        bundle = {'manifest_sha256': 'a' * 64, 'invocation_sha256': 'b' * 64}
        observed = {'calls': 0}

        def read_state():
            observed['calls'] += 1
            if observed['calls'] == 1:
                return None
            return local_app.LocalAppState(
                session='app-' + 'd' * 32, mode='real', phase='running',
                supervisor=process_identity(os.getpid()), backend=process_identity(os.getpid()),
                started_at='synthetic', owned_synthetic_form_invocation=True,
                owned_form_manifest_sha256=bundle['manifest_sha256'],
                owned_form_invocation_sha256=bundle['invocation_sha256'])

        listeners = [ReservedSocket(23, 8765), ReservedSocket(24, 41777)]
        def provision_bundle(directory, _port):
            directory.mkdir(mode=0o700)
            return bundle

        with (patch('aos.local_app.socket.socket', side_effect=listeners) as make_socket,
              patch('aos.local_app.subprocess.Popen') as launch,
              patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='d' * 32)),
              patch('aos.local_app.read_state', side_effect=read_state),
              patch('aos.owned_form_invocation_session.provision_owned_synthetic_form_invocation',
                    side_effect=provision_bundle) as provision):
            launch.return_value.wait.return_value = 0
            status = local_app.start('real', check=False,
                                     owned_synthetic_form_invocation=True)
        self.assertEqual(status['phase'], 'running')
        self.assertTrue(status['owned_synthetic_form_invocation'])
        self.assertEqual(status['owned_form_manifest_sha256'], bundle['manifest_sha256'])
        self.assertEqual(status['owned_form_invocation_sha256'], bundle['invocation_sha256'])
        provision.assert_called_once_with(self.base / ('app-' + 'd' * 32) / 'owned-form', 41777)
        self.assertEqual(make_socket.call_count, 2)
        manager_command = launch.call_args.args[0]
        self.assertIn('--owned-synthetic-form-invocation', manager_command)
        self.assertEqual(manager_command[manager_command.index('--owned-form-listener-fd') + 1],
                         '24')
        self.assertEqual(manager_command[manager_command.index('--owned-form-manifest-sha256') + 1],
                         bundle['manifest_sha256'])
        self.assertEqual(launch.call_args.kwargs['pass_fds'][1:3], (23, 24))
        self.assertEqual(str(launch.call_args.kwargs['pass_fds'][3]),
                         manager_command[manager_command.index('--owned-learning-lock-fd') + 1])

    def test_running_default_session_cannot_silently_enable_staging(self):
        local_app.prepare_base()
        running = self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor})
        local_app.write_state(running)
        with patch('aos.local_app.instance_lock', side_effect=BlockingIOError):
            self.assertFalse(local_app.start('fixture', check=False)['synthetic_staging'])
            with self.assertRaisesRegex(ValueError, 'Another local application manager'):
                local_app.start('fixture', check=False, synthetic_staging=True)
            with self.assertRaisesRegex(ValueError, 'Another local application manager'):
                local_app.start('fixture', check=False, synthetic_learning=True)
        self.assertEqual(local_app.read_state(), running)

    def test_managed_remote_entry_requires_private_profile_bound_task(self):
        from aos.contracts import canonical
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract

        with tempfile.TemporaryDirectory(prefix='managed-remote-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profile = WebApplicationProfile.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(root / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task = WebTaskContract.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_binding.json').read_text())['task'])
            task_file = root / 'task.json'
            task_file.write_text(json.dumps(task.model_dump(mode='json')))
            task_file.chmod(0o600)
            with patch('aos.local_app.WEB_PROFILES', profiles.root):
                content, task_sha256 = local_app.prepare_remote_entry('real', profile_sha256, task_file)
                with (patch('sys.argv', ['aos-v1', 'preview-remote-entry',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file)]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                self.assertEqual(json.loads(output.getvalue()), {
                    'profile_sha256': profile_sha256, 'task_sha256': task_sha256,
                    'status': 'validated_input', 'execution_authorized': False,
                    'collection_authorized': False})
                prepare.assert_not_called()
                launch.assert_not_called()
                for options in ([], ['--fixture', '--remote-entry-profile-sha256', profile_sha256,
                                     '--remote-entry-task-file', str(task_file)]):
                    with (self.subTest(options=options),
                          patch('sys.argv', ['aos-v1', 'preview-remote-entry', *options]),
                          patch('sys.stderr', new_callable=StringIO),
                          patch('aos.local_app.prepare_base') as prepare,
                          patch('aos.local_app.subprocess.Popen') as launch,
                          self.assertRaises(SystemExit) as rejected):
                        local_app.main()
                    self.assertEqual(rejected.exception.code, 1)
                    prepare.assert_not_called()
                    launch.assert_not_called()
                self.assertEqual(content, canonical(task.model_dump(mode='json')).encode())
                self.assertEqual(task_sha256, hashlib.sha256(content).hexdigest())
                snapshot = root / 'session'
                snapshot.mkdir(mode=0o700)
                local_app.write_remote_entry_task(snapshot, content)
                copied = snapshot / 'remote-entry-task.json'
                self.assertEqual(local_app.private_read(copied), content)
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    local_app.write_remote_entry_task(snapshot, content)
                rejected = subprocess.run([
                    local_app.sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                    '--port', '18765', '--engine', 'fixture', '--browser-tasks', '--desktop-browser',
                    '--remote-entry-mcp-manifest', str(local_app.MCP_MANIFEST),
                    '--remote-entry-profile-sha256', profile_sha256,
                    '--remote-entry-task-file', str(copied),
                    '--remote-entry-task-sha256', '0' * 64],
                    capture_output=True, timeout=15)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'Remote entry task file is unavailable or invalid', rejected.stderr)
                command = local_app.managed_backend_command(
                    self.base / self.state.session, 'real', 23,
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=task_sha256)
                self.assertEqual(command[command.index('--remote-entry-mcp-manifest') + 1],
                                 str(local_app.MCP_MANIFEST))
                self.assertEqual(command[command.index('--remote-entry-task-file') + 1],
                                 str(self.base / self.state.session / 'remote-entry-task.json'))
                self.assertEqual(command[command.index('--remote-entry-task-sha256') + 1], task_sha256)
                self.assertNotIn(str(task_file), command)
                with self.assertRaisesRegex(ValueError, 'together'):
                    local_app.prepare_remote_entry('real', profile_sha256, None)
                with self.assertRaisesRegex(ValueError, 'real mode'):
                    local_app.prepare_remote_entry('fixture', profile_sha256, task_file)
                with self.assertRaisesRegex(ValueError, 'Invalid managed backend mode'):
                    local_app.managed_backend_command(
                        self.base, 'fixture', 23,
                        remote_entry_profile_sha256=profile_sha256,
                        remote_entry_task_sha256=task_sha256)
                task_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'owner-only'):
                    local_app.prepare_remote_entry('real', profile_sha256, task_file)
                task_file.chmod(0o600)
                alias = root / 'alias.json'
                alias.symlink_to(task_file)
                with self.assertRaises(OSError):
                    local_app.prepare_remote_entry('real', profile_sha256, alias)
                task_file.write_text(json.dumps({**task.model_dump(mode='json'),
                                                 'profile_sha256': '0' * 64}))
                with self.assertRaisesRegex(ValueError, 'differs'):
                    local_app.prepare_remote_entry('real', profile_sha256, task_file)
                task_file.write_text(json.dumps(task.model_dump(mode='json')))
                local_app.prepare_base()
                running = local_app.LocalAppState.model_validate({**self.state.model_dump(),
                    'mode': 'real', 'phase': 'running', 'backend': self.state.supervisor.model_dump()})
                local_app.write_state(running)
                with patch('aos.local_app.instance_lock', side_effect=BlockingIOError):
                    with self.assertRaisesRegex(ValueError, 'Another local application manager'):
                        local_app.start('real', check=False,
                                        remote_entry_profile_sha256=profile_sha256,
                                        remote_entry_task_file=task_file)
                self.assertEqual(local_app.read_state(), running)

    def test_managed_remote_entry_start_passes_private_copy_to_supervisor(self):
        from aos.contracts import canonical
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract

        with tempfile.TemporaryDirectory(prefix='managed-remote-start-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profile = WebApplicationProfile.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(root / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task = WebTaskContract.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_binding.json').read_text())['task'])
            task_file = root / 'task.json'
            task_file.write_text(json.dumps(task.model_dump(mode='json')))
            task_file.chmod(0o600)
            content = canonical(task.model_dump(mode='json')).encode()
            task_sha256 = hashlib.sha256(content).hexdigest()
            running = local_app.LocalAppState(
                session='app-' + 'b' * 32, mode='real', phase='running',
                supervisor=process_identity(os.getpid()), backend=process_identity(os.getpid()),
                started_at='synthetic', remote_entry_profile_sha256=profile_sha256,
                remote_entry_task_sha256=task_sha256)
            with (patch('aos.local_app.WEB_PROFILES', profiles.root),
                  patch('aos.local_app.socket.socket') as socket_factory,
                  patch('aos.local_app.subprocess.Popen') as launch,
                  patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='b' * 32)),
                  patch('aos.local_app.read_state', side_effect=[None, running, running])):
                listener = socket_factory.return_value.__enter__.return_value
                listener.fileno.return_value = 23
                launch.return_value.wait.return_value = 0
                status = local_app.start('real', check=False,
                                         remote_entry_profile_sha256=profile_sha256,
                                         remote_entry_task_file=task_file)
            self.assertEqual(status['phase'], 'running')
            self.assertEqual(status['remote_entry_profile_sha256'], profile_sha256)
            self.assertEqual(status['remote_entry_task_sha256'], task_sha256)
            self.assertEqual(listener.bind.call_args.args[0], ('127.0.0.1', 8765))
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index('--remote-entry-profile-sha256') + 1], profile_sha256)
            self.assertEqual(command[command.index('--remote-entry-task-sha256') + 1], task_sha256)
            self.assertNotIn(str(task_file), command)
            copied = self.base / status['session'] / 'remote-entry-task.json'
            self.assertEqual(local_app.private_read(copied), content)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)

    def test_static_asset_plan_cli_and_private_managed_start(self):
        from aos.contracts import canonical, digest
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract
        from aos.web_static_assets import plan_web_static_assets

        with tempfile.TemporaryDirectory(prefix='managed-static-assets-',
                                          dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profile = WebApplicationProfile.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(root / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task = WebTaskContract.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_binding.json').read_text())['task'])
            assets = [
                {'url': 'https://crm.example.invalid/assets/app.js?v=1',
                 'content_type': 'application/javascript'},
                {'url': 'https://crm.example.invalid/assets/site.css',
                 'content_type': 'text/css'}]
            expected = plan_web_static_assets(profiles, task, assets)
            task_file = root / 'task.json'
            task_file.write_text(canonical(task.model_dump(mode='json')))
            task_file.chmod(0o600)
            source_file = root / 'assets.json'
            source_file.write_text(canonical(assets))
            source_file.chmod(0o600)
            plan_file = root / 'static-plan.json'
            args = ['--remote-entry-profile-sha256', profile_sha256,
                    '--remote-entry-task-file', str(task_file),
                    '--remote-static-assets-plan-file', str(plan_file)]
            with patch('aos.local_app.WEB_PROFILES', profiles.root):
                with (patch('sys.argv', ['aos-v1', 'plan-remote-static-assets', *args,
                                         '--remote-static-assets-source-file', str(source_file)]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare_base,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                self.assertEqual(json.loads(output.getvalue()), {
                    'profile_sha256': profile_sha256,
                    'plan_sha256': digest(expected.model_dump()), 'asset_count': 2,
                    'status': 'draft', 'execution_authorized': False,
                    'collection_authorized': False})
                self.assertEqual(plan_file.read_text(), canonical(expected.model_dump(mode='json')))
                self.assertEqual(plan_file.stat().st_mode & 0o777, 0o600)
                prepare_base.assert_not_called()
                launch.assert_not_called()
                with (patch('sys.argv', ['aos-v1', 'preview-remote-static-assets', *args]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare_base,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                self.assertEqual(json.loads(output.getvalue()), {
                    'profile_sha256': profile_sha256,
                    'plan_sha256': digest(expected.model_dump()),
                    'status': 'validated_input', 'execution_authorized': False,
                    'collection_authorized': False})
                prepare_base.assert_not_called()
                launch.assert_not_called()
                with self.assertRaises(FileExistsError):
                    local_app.plan_remote_static_assets(
                        profile_sha256, task_file, plan_file, source_file)
                running = local_app.LocalAppState(
                    session='app-' + 'd' * 32, mode='real', phase='running',
                    supervisor=process_identity(os.getpid()),
                    backend=process_identity(os.getpid()), started_at='synthetic',
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=hashlib.sha256(task_file.read_bytes()).hexdigest(),
                    remote_static_assets_plan_sha256=hashlib.sha256(plan_file.read_bytes()).hexdigest())
                with (patch('aos.local_app.socket.socket') as socket_factory,
                      patch('aos.local_app.subprocess.Popen') as launch,
                      patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='d' * 32)),
                      patch('aos.local_app.read_state', side_effect=[None, running, running])):
                    listener = socket_factory.return_value.__enter__.return_value
                    listener.fileno.return_value = 23
                    launch.return_value.wait.return_value = 0
                    status = local_app.start(
                        'real', check=False, remote_entry_profile_sha256=profile_sha256,
                        remote_entry_task_file=task_file,
                        remote_static_assets_plan_file=plan_file)
                self.assertEqual(status['remote_static_assets_plan_sha256'],
                                 running.remote_static_assets_plan_sha256)
                copied = self.base / status['session'] / 'remote-static-assets-plan.json'
                self.assertEqual(local_app.private_read(copied, 20000), plan_file.read_bytes())
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
                command = launch.call_args.args[0]
                self.assertEqual(command[command.index('--remote-static-assets-plan-sha256') + 1],
                                 running.remote_static_assets_plan_sha256)
                self.assertNotIn(str(plan_file), command)
                backend = local_app.managed_backend_command(
                    copied.parent, 'real', 23,
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=running.remote_entry_task_sha256,
                    remote_static_assets_plan_sha256=running.remote_static_assets_plan_sha256)
                self.assertEqual(backend[backend.index('--remote-static-assets-plan-file') + 1],
                                 str(copied))
                self.assertNotIn(str(plan_file), backend)
                with self.assertRaisesRegex(ValueError, 'Invalid managed backend mode'):
                    local_app.managed_backend_command(
                        copied.parent, 'fixture', 23,
                        remote_static_assets_plan_sha256=running.remote_static_assets_plan_sha256)
                rejected = subprocess.run([
                    local_app.sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                    '--port', '18765', '--engine', 'fixture', '--browser-tasks',
                    '--desktop-browser', '--remote-entry-mcp-manifest',
                    str(local_app.MCP_MANIFEST), '--remote-entry-profile-sha256',
                    profile_sha256, '--remote-entry-task-file',
                    str(copied.parent / 'remote-entry-task.json'),
                    '--remote-entry-task-sha256', running.remote_entry_task_sha256,
                    '--remote-static-assets-plan-file', str(copied),
                    '--remote-static-assets-plan-sha256', '0' * 64],
                    capture_output=True, timeout=15)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'Static asset plan file is unavailable or invalid',
                              rejected.stderr)
                source_file.write_text(json.dumps(assets, indent=2) + '\n')
                pretty_plan = root / 'pretty-plan.json'
                local_app.plan_remote_static_assets(
                    profile_sha256, task_file, pretty_plan, source_file)
                self.assertEqual(pretty_plan.read_text(), plan_file.read_text())
                source_file.write_text('[{"url":"https://crm.example.invalid/assets/app.js",'
                                       '"url":"https://crm.example.invalid/assets/other.js",'
                                       '"content_type":"application/javascript"}]')
                with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
                    local_app.plan_remote_static_assets(
                        profile_sha256, task_file, root / 'bad-plan.json', source_file)
                source_file.write_text(canonical(assets))
                source_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'owner-only'):
                    local_app.plan_remote_static_assets(
                        profile_sha256, task_file, root / 'bad-plan.json', source_file)
                source_file.chmod(0o600)
                plan_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'owner-only'):
                    local_app.prepare_remote_static_assets(
                        'real', profile_sha256, task_file, plan_file)
                plan_file.chmod(0o600)
                with self.assertRaisesRegex(ValueError, 'ignored data'):
                    local_app.plan_remote_static_assets(
                        profile_sha256, task_file, Path('/tmp/plan.json'), source_file)

    def test_readonly_json_plan_cli_and_private_managed_start(self):
        from aos.contracts import canonical, digest
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract
        from aos.web_readonly_data import plan_web_readonly_data_bundle

        with tempfile.TemporaryDirectory(prefix='managed-readonly-data-',
                                          dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profile = WebApplicationProfile.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(root / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task = WebTaskContract.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_binding.json').read_text())['task'])
            source = {'assets': [{'url': 'https://crm.example.invalid/assets/app.js?v=1',
                                  'content_type': 'application/javascript'}],
                      'data_resources': [{'url': 'https://crm.example.invalid/api/summary?view=compact&page=1'}]}
            expected = plan_web_readonly_data_bundle(
                profiles, task, source['assets'], source['data_resources'])
            task_file = root / 'task.json'
            task_file.write_text(canonical(task.model_dump(mode='json')))
            task_file.chmod(0o600)
            source_file = root / 'data-source.json'
            source_file.write_text(canonical(source))
            source_file.chmod(0o600)
            plan_file = root / 'data-plan.json'
            args = ['--remote-entry-profile-sha256', profile_sha256,
                    '--remote-entry-task-file', str(task_file),
                    '--remote-readonly-data-plan-file', str(plan_file)]
            with patch('aos.local_app.WEB_PROFILES', profiles.root):
                with (patch('sys.argv', ['aos-v1', 'plan-remote-readonly-data', *args,
                                         '--remote-readonly-data-source-file', str(source_file)]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare_base,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                self.assertEqual(json.loads(output.getvalue()), {
                    'profile_sha256': profile_sha256,
                    'plan_sha256': digest(expected.model_dump()),
                    'asset_count': 1, 'data_count': 1, 'status': 'draft',
                    'execution_authorized': False, 'collection_authorized': False})
                self.assertEqual(plan_file.read_text(), canonical(expected.model_dump(mode='json')))
                self.assertEqual(plan_file.stat().st_mode & 0o777, 0o600)
                prepare_base.assert_not_called()
                launch.assert_not_called()
                with (patch('sys.argv', ['aos-v1', 'preview-remote-readonly-data', *args]),
                      patch('sys.stdout', new_callable=StringIO) as output):
                    local_app.main()
                self.assertEqual(json.loads(output.getvalue()), {
                    'profile_sha256': profile_sha256,
                    'plan_sha256': digest(expected.model_dump()),
                    'status': 'validated_input', 'execution_authorized': False,
                    'collection_authorized': False})
                with self.assertRaises(FileExistsError):
                    local_app.plan_remote_readonly_data(
                        profile_sha256, task_file, plan_file, source_file)
                running = local_app.LocalAppState(
                    session='app-' + 'e' * 32, mode='real', phase='running',
                    supervisor=process_identity(os.getpid()),
                    backend=process_identity(os.getpid()), started_at='synthetic',
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=hashlib.sha256(task_file.read_bytes()).hexdigest(),
                    remote_static_assets_plan_sha256=hashlib.sha256(plan_file.read_bytes()).hexdigest())
                with (patch('aos.local_app.socket.socket') as socket_factory,
                      patch('aos.local_app.subprocess.Popen') as launch,
                      patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='e' * 32)),
                      patch('aos.local_app.read_state', side_effect=[None, running, running])):
                    listener = socket_factory.return_value.__enter__.return_value
                    listener.fileno.return_value = 23
                    launch.return_value.wait.return_value = 0
                    status = local_app.start(
                        'real', check=False, remote_entry_profile_sha256=profile_sha256,
                        remote_entry_task_file=task_file,
                        remote_readonly_data_plan_file=plan_file)
                copied = self.base / status['session'] / 'remote-static-assets-plan.json'
                self.assertEqual(local_app.private_read(copied, 32000), plan_file.read_bytes())
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
                self.assertEqual(launch.call_args.args[0][
                    launch.call_args.args[0].index('--remote-static-assets-plan-sha256') + 1],
                    running.remote_static_assets_plan_sha256)
                backend = local_app.managed_backend_command(
                    copied.parent, 'real', 23,
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=running.remote_entry_task_sha256,
                    remote_static_assets_plan_sha256=running.remote_static_assets_plan_sha256)
                self.assertEqual(backend[backend.index('--remote-static-assets-plan-file') + 1],
                                 str(copied))
                with (patch('sys.argv', ['aos-v1', 'preview-remote-static-assets',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-static-assets-plan-file', str(plan_file)]),
                      patch('sys.stdout', new_callable=StringIO),
                      patch('sys.stderr', new_callable=StringIO) as errors):
                    with self.assertRaises(SystemExit):
                        local_app.main()
                self.assertIn('Static asset preview requires a v1 plan', errors.getvalue())
                source_file.write_text('{"assets":[],"assets":[],"data_resources":[]}')
                with self.assertRaisesRegex(ValueError, 'duplicate JSON field'):
                    local_app.plan_remote_readonly_data(
                        profile_sha256, task_file, root / 'duplicate.json', source_file)
                source_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'owner-only'):
                    local_app.plan_remote_readonly_data(
                        profile_sha256, task_file, root / 'public.json', source_file)
                with self.assertRaisesRegex(ValueError, 'Only one web bundle'):
                    local_app.start('real', check=False,
                                    remote_entry_profile_sha256=profile_sha256,
                                    remote_entry_task_file=task_file,
                                    remote_static_assets_plan_file=plan_file,
                                    remote_readonly_data_plan_file=plan_file)

    def test_managed_readonly_routes_preview_and_private_start_copy(self):
        from aos.contracts import canonical, digest
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract, plan_web_readonly_routes

        with tempfile.TemporaryDirectory(prefix='managed-routes-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            profile = WebApplicationProfile.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(root / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task = WebTaskContract.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_binding.json').read_text())['task'])
            plan = plan_web_readonly_routes(profiles, task,
                                            [profile.entry_url,
                                             profile.entry_url.rstrip('/')
                                             + '/details?view=compact&page=1'])
            task_file = root / 'task.json'
            task_file.write_text(canonical(task.model_dump(mode='json')))
            task_file.chmod(0o600)
            plan_file = root / 'plan.json'
            plan_file.write_text(canonical(plan.model_dump(mode='json')))
            plan_file.chmod(0o600)
            routes_file = root / 'routes.txt'
            routes_file.write_text('\n'.join(plan.routes) + '\n')
            routes_file.chmod(0o600)
            with patch('aos.local_app.WEB_PROFILES', profiles.root):
                generated_file = root / 'generated-plan.json'
                with (patch('sys.argv', ['aos-v1', 'plan-remote-routes',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-routes-source-file', str(routes_file),
                                         '--remote-routes-plan-file', str(generated_file)]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                self.assertEqual(generated_file.read_bytes(), plan_file.read_bytes())
                self.assertEqual(generated_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(output.getvalue())['routes'], plan.routes)
                self.assertFalse(json.loads(output.getvalue())['execution_authorized'])
                prepare.assert_not_called()
                launch.assert_not_called()
                with self.assertRaises(FileExistsError):
                    local_app.plan_remote_routes(profile_sha256, task_file,
                                                 generated_file, routes_file)
                public_directory = root / 'public'
                public_directory.mkdir(mode=0o755)
                with self.assertRaisesRegex(ValueError, 'private_directory'):
                    local_app.plan_remote_routes(profile_sha256, task_file,
                                                 public_directory / 'plan.json', routes_file)
                self.assertFalse((public_directory / 'plan.json').exists())
                routes_file.write_text('\n'.join(reversed(plan.routes)) + '\n')
                with self.assertRaises(ValueError):
                    local_app.plan_remote_routes(profile_sha256, task_file,
                                                 root / 'reversed.json', routes_file)
                routes_file.write_text('\n'.join(plan.routes))
                with self.assertRaisesRegex(ValueError, 'one canonical URL per line'):
                    local_app.plan_remote_routes(profile_sha256, task_file,
                                                 root / 'unterminated.json', routes_file)
                routes_file.write_text('\n'.join(plan.routes) + '\n')
                linked_routes = root / 'linked-routes.txt'
                linked_routes.symlink_to(routes_file)
                with self.assertRaises(OSError):
                    local_app.plan_remote_routes(profile_sha256, task_file,
                                                 root / 'linked-source.json', linked_routes)
                routes_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'owner-only'):
                    local_app.plan_remote_routes(profile_sha256, task_file,
                                                 root / 'public-source.json', routes_file)
                routes_file.chmod(0o600)
                with (patch('sys.argv', ['aos-v1', 'start',
                                         '--remote-routes-source-file', str(routes_file)]),
                      patch('aos.local_app.prepare_base') as prepare,
                      patch('sys.stderr', new_callable=StringIO) as error,
                      self.assertRaises(SystemExit)):
                    local_app.main()
                prepare.assert_not_called()
                self.assertIn('only accepted', error.getvalue())
                prepared, plan_sha256 = local_app.prepare_remote_routes(
                    'real', profile_sha256, task_file, plan_file)
                self.assertEqual(plan_sha256, digest(plan.model_dump()))
                with (patch('sys.argv', ['aos-v1', 'preview-remote-routes',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-routes-plan-file', str(plan_file)]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                self.assertEqual(json.loads(output.getvalue()), {
                    'profile_sha256': profile_sha256, 'plan_sha256': plan_sha256,
                    'status': 'validated_input', 'execution_authorized': False,
                    'collection_authorized': False})
                prepare.assert_not_called()
                launch.assert_not_called()
                plan_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'owner-only'):
                    local_app.prepare_remote_routes('real', profile_sha256, task_file, plan_file)
                plan_file.chmod(0o600)
                running = local_app.LocalAppState(
                    session='app-' + 'c' * 32, mode='real', phase='running',
                    supervisor=process_identity(os.getpid()), backend=process_identity(os.getpid()),
                    started_at='synthetic', remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=hashlib.sha256(
                        canonical(task.model_dump(mode='json')).encode()).hexdigest(),
                    remote_routes_plan_sha256=plan_sha256)
                with (patch('aos.local_app.socket.socket') as socket_factory,
                      patch('aos.local_app.subprocess.Popen') as launch,
                      patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='c' * 32)),
                      patch('aos.local_app.read_state', side_effect=[None, running, running])):
                    listener = socket_factory.return_value.__enter__.return_value
                    listener.fileno.return_value = 23
                    launch.return_value.wait.return_value = 0
                    status = local_app.start('real', check=False,
                                             remote_entry_profile_sha256=profile_sha256,
                                             remote_entry_task_file=task_file,
                                             remote_routes_plan_file=plan_file)
                self.assertEqual(status['remote_routes_plan_sha256'], plan_sha256)
                copied = self.base / status['session'] / 'remote-routes-plan.json'
                self.assertEqual(local_app.private_read(copied, 20000), prepared)
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
                command = launch.call_args.args[0]
                self.assertEqual(command[command.index('--remote-routes-plan-sha256') + 1], plan_sha256)
                self.assertNotIn(str(plan_file), command)
                backend = local_app.managed_backend_command(
                    copied.parent, 'real', 23,
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=running.remote_entry_task_sha256,
                    remote_routes_plan_sha256=plan_sha256)
                self.assertEqual(backend[backend.index('--remote-routes-plan-file') + 1], str(copied))
                self.assertNotIn(str(plan_file), backend)
                rejected = subprocess.run([
                    local_app.sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                    '--port', '18765', '--engine', 'fixture', '--browser-tasks', '--desktop-browser',
                    '--remote-entry-mcp-manifest', str(local_app.MCP_MANIFEST),
                    '--remote-entry-profile-sha256', profile_sha256,
                    '--remote-entry-task-file', str(copied.parent / 'remote-entry-task.json'),
                    '--remote-entry-task-sha256', running.remote_entry_task_sha256,
                    '--remote-routes-plan-file', str(copied),
                    '--remote-routes-plan-sha256', '0' * 64],
                    capture_output=True, timeout=15)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'Read-only route plan file is unavailable or invalid', rejected.stderr)

    def test_managed_public_form_preview_private_copy_and_backend_rejections(self):
        from aos.contracts import canonical, digest
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract
        from aos.web_https_form_transport import plan_web_https_form

        with tempfile.TemporaryDirectory(prefix='managed-public-form-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            origin = 'https://crm.example.com'
            source = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
            profile = WebApplicationProfile.model_validate({**source,
                'entry_url': origin + '/app/', 'allowed_origins': [origin]})
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(root / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task_source = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())['task']
            task = WebTaskContract.model_validate({**task_source,
                'profile_sha256': profile_sha256, 'entry_url': profile.entry_url,
                'allowed_origins': [origin]})
            value = b'private-form-test-879'
            body = b'message=private-form-test-879'
            plan = plan_web_https_form(profiles, task, submit_url=origin + '/submit',
                                       receipt_url=origin + '/receipt',
                                       body_sha256=hashlib.sha256(body).hexdigest(),
                                       body_bytes=len(body))
            task_file = root / 'task.json'
            task_file.write_bytes(canonical(task.model_dump(mode='json')).encode())
            task_file.chmod(0o600)
            plan_file = root / 'form-plan.json'
            plan_file.write_bytes(canonical(plan.model_dump(mode='json')).encode())
            plan_file.chmod(0o600)
            value_file = root / 'form-value.txt'
            value_file.write_bytes(value)
            value_file.chmod(0o600)
            cookie = b'session=synthetic-managed-cookie'
            cookie_file = root / 'form-cookie.txt'
            cookie_file.write_bytes(cookie)
            cookie_file.chmod(0o600)
            cookie_sha256 = hashlib.sha256(cookie).hexdigest()
            plan_sha256 = digest(plan.model_dump())
            task_sha256 = digest(task.model_dump())
            with patch('aos.local_app.WEB_PROFILES', profiles.root):
                with (patch('sys.argv', ['aos-v1', 'plan-remote-form-cookie',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-public-plan-sha256', plan_sha256,
                                         '--remote-form-cookie-file', str(cookie_file)]),
                      patch('sys.stdout', new_callable=StringIO) as cookie_output):
                    local_app.main()
                self.assertEqual(json.loads(cookie_output.getvalue())['cookie_sha256'],
                                 cookie_sha256)
                self.assertNotIn(cookie.decode(), cookie_output.getvalue())
                with self.assertRaises(ValueError):
                    local_app.prepare_remote_form_cookie(
                        'real', profile_sha256, task_file, plan_file, 'message', value_file,
                        plan_sha256, cookie_file, '0' * 64)
                with (patch('sys.argv', ['aos-v1', 'preview-remote-form-cookie',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-public-plan-sha256', plan_sha256,
                                         '--remote-form-cookie-file', str(cookie_file),
                                         '--remote-form-cookie-sha256', cookie_sha256]),
                      patch('sys.stdout', new_callable=StringIO) as cookie_preview):
                    local_app.main()
                self.assertEqual(json.loads(cookie_preview.getvalue())['cookie_sha256'],
                                 cookie_sha256)
                self.assertNotIn(cookie.decode(), cookie_preview.getvalue())
                cookie_file.chmod(0o644)
                with self.assertRaises(ValueError):
                    local_app.prepare_remote_form_cookie(
                        'real', profile_sha256, task_file, plan_file, 'message', value_file,
                        plan_sha256, cookie_file, cookie_sha256)
                cookie_file.chmod(0o600)
                cookie_file.write_bytes(b'session=changed\r\nX-Other: injected')
                with self.assertRaises(ValueError):
                    local_app.prepare_remote_form_cookie(
                        'real', profile_sha256, task_file, plan_file, 'message', value_file,
                        plan_sha256, cookie_file, cookie_sha256)
                cookie_file.write_bytes(cookie)
                state_file = root / 'form-state-plan.json'
                state_report = local_app.plan_remote_form_state(
                    profile_sha256, task_file, plan_file, 'message', value_file,
                    plan_sha256, state_file, origin + '/state',
                    '1' * 64, '2' * 64)
                state_sha256 = state_report['state_plan_sha256']
                self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
                generated_state_file = root / 'generated-form-state-plan.json'
                with (patch('sys.argv', ['aos-v1', 'plan-remote-form-state',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-public-plan-sha256', plan_sha256,
                                         '--remote-form-state-plan-file', str(generated_state_file),
                                         '--remote-form-state-url', origin + '/state',
                                         '--remote-form-state-before-sha256', '1' * 64,
                                         '--remote-form-state-after-sha256', '2' * 64]),
                      patch('sys.stdout', new_callable=StringIO) as plan_output):
                    local_app.main()
                self.assertEqual(generated_state_file.read_bytes(), state_file.read_bytes())
                self.assertEqual(json.loads(plan_output.getvalue())['state_plan_sha256'],
                                 state_sha256)
                self.assertNotIn(value.decode(), plan_output.getvalue())
                marker_file = root / 'marker-form-state-plan.json'
                with (patch('sys.argv', ['aos-v1', 'plan-remote-form-state',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-public-plan-sha256', plan_sha256,
                                         '--remote-form-state-plan-file', str(marker_file),
                                         '--remote-form-state-url', origin + '/state',
                                         '--remote-form-state-before-sha256', '1' * 64,
                                         '--remote-form-state-after-sha256', '2' * 64,
                                         '--remote-form-state-marker-id', 'outcome',
                                         '--remote-form-state-before-marker-sha256', '3' * 64,
                                         '--remote-form-state-after-marker-sha256', '4' * 64]),
                      patch('sys.stdout', new_callable=StringIO) as marker_output):
                    local_app.main()
                marker_sha256 = json.loads(marker_output.getvalue())['state_plan_sha256']
                self.assertNotEqual(marker_sha256, state_sha256)
                self.assertEqual(json.loads(marker_file.read_text())['marker_id'], 'outcome')
                self.assertEqual(local_app.prepare_remote_form_state(
                    'real', profile_sha256, task_file, plan_file, 'message', value_file,
                    plan_sha256, marker_file, marker_sha256)[1], marker_sha256)
                self.assertEqual(local_app.prepare_remote_form_state(
                    'real', profile_sha256, task_file, plan_file, 'message', value_file,
                    plan_sha256, state_file, state_sha256)[1], state_sha256)
                with self.assertRaises(ValueError):
                    local_app.prepare_remote_form_state(
                        'real', profile_sha256, task_file, plan_file, 'message', value_file,
                        plan_sha256, state_file, '0' * 64)
                with (patch('sys.argv', ['aos-v1', 'preview-remote-form-state',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-public-plan-sha256', plan_sha256,
                                         '--remote-form-state-plan-file', str(state_file),
                                         '--remote-form-public-state-plan-sha256', state_sha256]),
                      patch('sys.stdout', new_callable=StringIO) as state_output):
                    local_app.main()
                self.assertEqual(json.loads(state_output.getvalue())['state_plan_sha256'],
                                 state_sha256)
                self.assertNotIn(value.decode(), state_output.getvalue())
                with (patch('sys.argv', ['aos-v1', 'preview-remote-form',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-public-plan-sha256', plan_sha256,
                                         '--remote-form-state-plan-file', str(state_file),
                                         '--remote-form-public-state-plan-sha256', state_sha256]),
                      patch('sys.stderr', new_callable=StringIO) as wrong_preview,
                      self.assertRaises(SystemExit)):
                    local_app.main()
                self.assertIn('HTTPS form state options require', wrong_preview.getvalue())
                generated_file = root / 'generated-form-plan.json'
                with (patch('sys.argv', ['aos-v1', 'plan-remote-form',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(generated_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-submit-url', plan.submit_url,
                                         '--remote-form-receipt-url', plan.receipt_url]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare_base,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                self.assertEqual(generated_file.read_bytes(), plan_file.read_bytes())
                self.assertEqual(generated_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(output.getvalue())['plan_sha256'], plan_sha256)
                self.assertNotIn(value.decode(), output.getvalue())
                prepare_base.assert_not_called()
                launch.assert_not_called()
                with self.assertRaises(FileExistsError):
                    local_app.plan_remote_form(profile_sha256, task_file, generated_file,
                                               'message', value_file, plan.submit_url,
                                               plan.receipt_url)
                shared_directory = root / 'shared'
                shared_directory.mkdir(mode=0o755)
                with self.assertRaisesRegex(ValueError, 'private'):
                    local_app.plan_remote_form(profile_sha256, task_file,
                                               shared_directory / 'form-plan.json',
                                               'message', value_file, plan.submit_url,
                                               plan.receipt_url)
                self.assertFalse((shared_directory / 'form-plan.json').exists())
                linked_plan = root / 'linked-form-plan.json'
                linked_plan.symlink_to(plan_file)
                with self.assertRaises(FileExistsError):
                    local_app.plan_remote_form(profile_sha256, task_file, linked_plan,
                                               'message', value_file, plan.submit_url,
                                               plan.receipt_url)
                self.assertEqual(plan_file.read_bytes(), generated_file.read_bytes())
                for grant in (None, '0' * 64):
                    with self.assertRaises(ValueError):
                        local_app.prepare_remote_form('real', profile_sha256, task_file,
                                                      plan_file, 'message', value_file, grant)
                prepared = local_app.prepare_remote_form(
                    'real', profile_sha256, task_file, plan_file, 'message', value_file,
                    plan_sha256)
                self.assertEqual(prepared, (plan_file.read_bytes(), plan_sha256, value))
                with (patch('sys.argv', ['aos-v1', 'preview-remote-form',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-field-name', 'message',
                                         '--remote-form-value-file', str(value_file),
                                         '--remote-form-public-plan-sha256', plan_sha256]),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.prepare_base') as prepare_base,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                report = json.loads(output.getvalue())
                self.assertEqual(report['plan_sha256'], plan_sha256)
                self.assertEqual(report['submit_url'], plan.submit_url)
                self.assertFalse(report['execution_authorized'])
                self.assertNotIn(value.decode(), output.getvalue())
                prepare_base.assert_not_called()
                launch.assert_not_called()
                value_file.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'differs'):
                    local_app.prepare_remote_form('real', profile_sha256, task_file,
                                                  plan_file, 'message', value_file, plan_sha256)
                value_file.write_bytes(value)
                value_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'owner-only'):
                    local_app.prepare_remote_form('real', profile_sha256, task_file,
                                                  plan_file, 'message', value_file, plan_sha256)
                value_file.chmod(0o600)
                running = local_app.LocalAppState(
                    session='app-' + 'd' * 32, mode='real', phase='running',
                    supervisor=process_identity(os.getpid()),
                    backend=process_identity(os.getpid()), started_at='synthetic',
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=task_sha256,
                    remote_form_plan_sha256=plan_sha256,
                    remote_form_field_name='message',
                    remote_form_state_plan_sha256=state_sha256,
                    remote_form_cookie_sha256=cookie_sha256)
                with (patch('aos.local_app.socket.socket') as socket_factory,
                      patch('aos.local_app.subprocess.Popen') as launch,
                      patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='d' * 32)),
                      patch('aos.local_app.read_state', side_effect=[None, running, running])):
                    listener = socket_factory.return_value.__enter__.return_value
                    listener.fileno.return_value = 23
                    launch.return_value.wait.return_value = 0
                    status = local_app.start(
                        'real', check=False, remote_entry_profile_sha256=profile_sha256,
                        remote_entry_task_file=task_file, remote_form_plan_file=plan_file,
                        remote_form_field_name='message', remote_form_value_file=value_file,
                        remote_form_public_plan_sha256=plan_sha256,
                        remote_form_state_plan_file=state_file,
                        remote_form_public_state_plan_sha256=state_sha256,
                        remote_form_cookie_file=cookie_file,
                        remote_form_cookie_sha256=cookie_sha256)
                self.assertEqual(status['remote_form_plan_sha256'], plan_sha256)
                self.assertEqual(status['remote_form_state_plan_sha256'], state_sha256)
                self.assertEqual(status['remote_form_cookie_sha256'], cookie_sha256)
                directory = self.base / status['session']
                self.assertEqual(local_app.private_read(directory / 'remote-form-plan.json', 20000),
                                 plan_file.read_bytes())
                self.assertEqual(local_app.private_read(directory / 'remote-form-value.txt', 4096), value)
                self.assertEqual(local_app.private_read(directory / 'remote-form-state-plan.json', 20000),
                                 state_file.read_bytes())
                self.assertEqual(local_app.private_read(directory / 'remote-form-cookie.txt', 2048),
                                 cookie)
                command = launch.call_args.args[0]
                self.assertNotIn(value.decode(), ' '.join(command))
                self.assertNotIn(str(value_file), command)
                self.assertEqual(command[command.index('--remote-form-plan-sha256') + 1],
                                 plan_sha256)
                self.assertEqual(command[command.index('--remote-form-state-plan-sha256') + 1],
                                 state_sha256)
                self.assertEqual(command[command.index('--remote-form-cookie-sha256') + 1],
                                 cookie_sha256)
                self.assertNotIn(cookie.decode(), ' '.join(command))
                backend = local_app.managed_backend_command(
                    directory, 'real', 23, remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=task_sha256,
                    remote_form_plan_sha256=plan_sha256,
                    remote_form_field_name='message',
                    remote_form_state_plan_sha256=state_sha256,
                    remote_form_cookie_sha256=cookie_sha256)
                self.assertEqual(backend[backend.index('--remote-form-value-file') + 1],
                                 str(directory / 'remote-form-value.txt'))
                self.assertNotIn(value.decode(), ' '.join(backend))
                self.assertNotIn(str(value_file), backend)
                self.assertEqual(backend[backend.index('--remote-form-state-plan-file') + 1],
                                 str(directory / 'remote-form-state-plan.json'))
                self.assertEqual(backend[backend.index('--remote-form-cookie-file') + 1],
                                 str(directory / 'remote-form-cookie.txt'))
                self.assertNotIn(cookie.decode(), ' '.join(backend))
                base = [local_app.sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                        '--port', '18765', '--engine', 'fixture', '--browser-tasks',
                        '--desktop-browser', '--web-profiles-root', str(profiles.root),
                        '--remote-entry-mcp-manifest', str(local_app.MCP_MANIFEST),
                        '--remote-entry-profile-sha256', profile_sha256,
                        '--remote-entry-task-file', str(directory / 'remote-entry-task.json'),
                        '--remote-entry-task-sha256', task_sha256,
                        '--remote-form-plan-file', str(directory / 'remote-form-plan.json'),
                        '--remote-form-field-name', 'message',
                        '--remote-form-value-file', str(directory / 'remote-form-value.txt'),
                        '--remote-form-public-plan-sha256', plan_sha256]
                rejected = subprocess.run(base + ['--remote-form-plan-sha256', '0' * 64],
                                          capture_output=True, timeout=15)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'Public HTTPS form plan or private value', rejected.stderr)
                (directory / 'remote-form-value.txt').chmod(0o644)
                rejected = subprocess.run(base + ['--remote-form-plan-sha256', plan_sha256],
                                          capture_output=True, timeout=15)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'Public HTTPS form plan or private value', rejected.stderr)
                cookie_base = [local_app.sys.executable,
                               str(REPO_ROOT / 'scripts/serve_desktop.py'),
                               '--port', '18765', '--engine', 'fixture', '--browser-tasks',
                               '--desktop-browser', '--web-profiles-root', str(profiles.root),
                               '--remote-entry-mcp-manifest', str(local_app.MCP_MANIFEST),
                               '--remote-entry-profile-sha256', profile_sha256,
                               '--remote-entry-task-file', str(task_file),
                               '--remote-entry-task-sha256', task_sha256,
                               '--remote-form-plan-file', str(plan_file),
                               '--remote-form-plan-sha256', plan_sha256,
                               '--remote-form-field-name', 'message',
                               '--remote-form-value-file', str(value_file),
                               '--remote-form-public-plan-sha256', plan_sha256,
                               '--remote-form-cookie-file', str(cookie_file),
                               '--remote-form-cookie-sha256', '0' * 64]
                rejected = subprocess.run(cookie_base, capture_output=True, timeout=15)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'HTTPS form cookie is unavailable or invalid', rejected.stderr)
                self.assertNotIn(cookie, rejected.stderr)
                with (patch('aos.local_app.socket.socket') as socket_factory,
                      patch('aos.local_app.subprocess.Popen', side_effect=OSError('launch failed')),
                      patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='e' * 32)),
                      patch('aos.local_app.read_state', return_value=None),
                      self.assertRaisesRegex(OSError, 'launch failed')):
                    socket_factory.return_value.__enter__.return_value.fileno.return_value = 23
                    local_app.start(
                        'real', check=False, remote_entry_profile_sha256=profile_sha256,
                        remote_entry_task_file=task_file, remote_form_plan_file=plan_file,
                        remote_form_field_name='message', remote_form_value_file=value_file,
                        remote_form_public_plan_sha256=plan_sha256,
                        remote_form_cookie_file=cookie_file,
                        remote_form_cookie_sha256=cookie_sha256)
                failed_directory = self.base / ('app-' + 'e' * 32)
                self.assertFalse((failed_directory / 'remote-form-value.txt').exists())
                self.assertFalse((failed_directory / 'remote-form-cookie.txt').exists())

    def test_symlink_hardlink_fifo_permissions_and_oversize_rejected(self):
        local_app.prepare_base()
        target = self.base / 'target'
        target.write_text('synthetic')
        target.chmod(0o600)
        alias = self.base / 'alias'
        alias.symlink_to(target)
        with self.assertRaises(OSError):
            local_app.private_read(alias)
        alias.unlink()
        os.link(target, alias)
        with self.assertRaises(ValueError):
            local_app.private_read(target)
        alias.unlink()
        os.mkfifo(alias)
        with self.assertRaises(ValueError):
            local_app.private_read(alias)
        with self.assertRaises(ValueError):
            local_app.private_read(target, 1)
        target.chmod(0o644)
        with self.assertRaises(ValueError):
            local_app.private_read(target)

    def test_base_symlink_or_public_directory_rejected(self):
        self.base.symlink_to(self.base.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            local_app.prepare_base()
        self.base.unlink()
        self.base.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            local_app.prepare_base()

    def test_active_instance_lock_and_unowned_pid_never_signalled(self):
        local_app.prepare_base()
        with local_app.instance_lock():
            with self.assertRaises(BlockingIOError):
                with local_app.instance_lock():
                    pass
        identity = process_identity(os.getpid()).model_copy(update={'start_ticks': 1})
        with patch('signal.pidfd_send_signal') as send:
            with self.assertRaises(ValueError):
                local_app.signal_owned(identity)
        send.assert_not_called()

    def test_dead_manager_requires_inspection_no_signal_or_new_process(self):
        local_app.prepare_base()
        local_app.write_state(self.state)
        with patch('aos.local_app.observe_process', return_value='not_observed'), patch('subprocess.Popen') as launch:
            self.assertEqual(local_app.current_status()['phase'], 'needs_inspection')
            with self.assertRaises(ValueError):
                local_app.stop()
            with self.assertRaises(ValueError):
                local_app.start(check=False)
        launch.assert_not_called()

    def test_previous_boot_recovery_requires_exact_stopped_container_and_preserves_audit(self):
        from aos.contracts import canonical, digest

        local_app.prepare_base()
        old_process = self.state.supervisor.model_copy(update={'boot_id': '00000000-0000-0000-0000-000000000000'})
        state = self.state.model_copy(update={
            'mode': 'real', 'phase': 'running', 'supervisor': old_process, 'backend': old_process,
            'remote_entry_profile_sha256': 'a' * 64, 'remote_entry_task_sha256': 'b' * 64,
            'remote_form_plan_sha256': 'c' * 64, 'remote_form_field_name': 'message'})
        local_app.write_state(state)
        session = self.base / state.session
        session.mkdir(mode=0o700)
        private_value = session / 'remote-form-value.txt'
        private_value.write_bytes(b'synthetic-private-value')
        private_value.chmod(0o600)
        workspace = session / 'workspace'
        workspace.mkdir(mode=0o700)
        journals = session / '.aos-lifecycle'
        journals.mkdir(mode=0o700)
        descriptor = open_existing_workspace(workspace)
        try:
            identity = workspace_identity(workspace, descriptor)
        finally:
            os.close(descriptor)
        runtime_id = 'desktop-' + 'b' * 32
        container_id = 'c' * 64
        image_id = 'sha256:' + 'd' * 64
        previous_boot_workspace = identity.model_copy(update={'device': identity.device + 1})
        birth = LifecycleBirth(runtime_id=runtime_id, container_name='aos-desktop-' + 'e' * 20,
                               image_id=image_id, source_sha256='f' * 64,
                               workspace=previous_boot_workspace, process=old_process)
        events = []
        for index, stage in enumerate(('intent', 'created', 'started')):
            events.append(LifecycleEvent(birth=birth, sequence=index,
                                         previous_sha256=digest(events[-1].model_dump()) if events else None,
                                         stage=stage, container_id=container_id if index else None,
                                         recorded_at='synthetic'))
        journal = journals / (runtime_id + '.jsonl')
        journal.write_text(''.join(canonical(event.model_dump()) + '\n' for event in events))
        journal.chmod(0o600)

        def inspection(status):
            fields = [container_id, '/' + birth.container_name, status, status == 'running',
                      1 if status == 'running' else 0, image_id, runtime_id]
            return subprocess.CompletedProcess(args=['docker'], returncode=0,
                                               stdout=json.dumps(fields).encode(), stderr=b'')

        with patch('aos.local_app.subprocess.run', return_value=inspection('running')):
            with self.assertRaisesRegex(ValueError, 'not safely stopped'):
                local_app.recover_reboot()
        self.assertEqual(local_app.read_state(), state)
        self.assertTrue(private_value.exists())
        self.assertFalse((self.base / ('.recovery-' + state.session + '.json')).exists())
        with (patch('aos.local_app.subprocess.run', return_value=inspection('exited')) as docker,
              patch('aos.local_app.socket.socket')):
            result = local_app.recover_reboot()
        self.assertEqual(result['phase'], 'stopped')
        self.assertEqual(local_app.read_state().backend, state.backend)
        self.assertEqual(local_app.read_state().phase, 'stopped')
        self.assertFalse(private_value.exists())
        self.assertEqual(json.loads((self.base / ('.recovery-' + state.session + '.json')).read_text())['previous_state'],
                         state.model_dump())
        self.assertEqual(len(docker.call_args.args[0]), 5)
        with self.assertRaisesRegex(ValueError, 'No interrupted session'):
            local_app.recover_reboot()

    def test_managed_ordered_form_plan_preview_copy_and_stale_admission(self):
        from aos.contracts import canonical, digest
        from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
        from aos.web_application_binding import WebTaskContract

        with tempfile.TemporaryDirectory(prefix='managed-multifield-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            origin = 'https://crm.example.com'
            profile_source = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
            profile = WebApplicationProfile.model_validate({**profile_source,
                'entry_url': origin + '/app/', 'allowed_origins': [origin]})
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(root / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task_source = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())['task']
            task = WebTaskContract.model_validate({**task_source,
                'profile_sha256': profile_sha256, 'entry_url': profile.entry_url,
                'allowed_origins': [origin]})
            task_file = root / 'task.json'
            task_file.write_bytes(canonical(task.model_dump(mode='json')).encode())
            task_file.chmod(0o600)
            fields = [{'name': 'subject', 'value': 'private-subject-971'},
                      {'name': 'message', 'value': 'private-message-971'}]
            body = b'subject=private-subject-971&message=private-message-971'
            fields_document = {'schema_version': '1.0', 'fields': fields,
                               'body_sha256': hashlib.sha256(body).hexdigest(),
                               'body_bytes': len(body), 'execution_authorized': False,
                               'collection_authorized': False}
            fields_content = canonical(fields_document).encode()
            fields_sha256 = hashlib.sha256(fields_content).hexdigest()
            fields_source = root / 'fields-source.json'
            fields_source.write_text(json.dumps(fields, indent=2))
            fields_source.chmod(0o600)
            fields_file = root / 'fields.json'
            with (patch('sys.argv', ['aos-v1', 'plan-remote-form-fields',
                                     '--remote-form-fields-source-file', str(fields_source),
                                     '--remote-form-fields-file', str(fields_file)]),
                  patch('sys.stdout', new_callable=StringIO) as fields_output,
                  patch('aos.local_app.subprocess.Popen') as fields_launch):
                local_app.main()
            fields_report = json.loads(fields_output.getvalue())
            self.assertEqual(fields_file.read_bytes(), fields_content)
            self.assertEqual(fields_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(fields_report['fields_sha256'], fields_sha256)
            self.assertEqual(fields_report['field_names'], ['subject', 'message'])
            self.assertNotIn('private-subject-971', fields_output.getvalue())
            fields_launch.assert_not_called()
            with (patch('sys.argv', ['aos-v1', 'plan-remote-form-fields',
                                     '--remote-form-fields-source-file', str(fields_source),
                                     '--remote-form-fields-file', str(root / 'mixed-fields.json'),
                                     '--remote-form-field-name', 'subject']),
                  patch('sys.stderr', new_callable=StringIO) as rejected_output,
                  self.assertRaises(SystemExit)):
                local_app.main()
            self.assertIn('only accepts private source', rejected_output.getvalue())
            with self.assertRaises(FileExistsError):
                local_app.plan_remote_form_fields(fields_source, fields_file)
            fields_source.write_text(json.dumps([fields[0], fields[0]]))
            with self.assertRaises(ValueError):
                local_app.plan_remote_form_fields(fields_source, root / 'duplicate-fields.json')
            fields_source.write_text('[{"name":"subject","name":"other","value":"demo"},'
                                     '{"name":"message","value":"hello"}]')
            with self.assertRaisesRegex(ValueError, 'duplicate JSON keys'):
                local_app.plan_remote_form_fields(fields_source, root / 'duplicate-key-fields.json')
            fields_source.write_bytes(json.dumps(fields).encode('utf-16'))
            with self.assertRaises(UnicodeError):
                local_app.plan_remote_form_fields(fields_source, root / 'non-utf8-fields.json')
            fields_source.write_text(json.dumps(fields, indent=2))
            fields_source.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'owner-only'):
                local_app.plan_remote_form_fields(fields_source, root / 'shared-fields.json')
            fields_source.chmod(0o600)
            plan_file = root / 'form-plan.json'
            task_sha256 = digest(task.model_dump())
            with patch('aos.local_app.WEB_PROFILES', profiles.root):
                with (patch('sys.argv', ['aos-v1', 'plan-remote-form',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-fields-file', str(fields_file),
                                         '--remote-form-submit-url', origin + '/submit',
                                         '--remote-form-receipt-url', origin + '/receipt']),
                      patch('sys.stdout', new_callable=StringIO) as output,
                      patch('aos.local_app.subprocess.Popen') as launch):
                    local_app.main()
                report = json.loads(output.getvalue())
                plan_sha256 = report['plan_sha256']
                self.assertEqual(report['field_names'], ['subject', 'message'])
                self.assertEqual(report['body_sha256'], fields_document['body_sha256'])
                self.assertNotIn('private-subject-971', output.getvalue())
                self.assertNotIn('private-message-971', output.getvalue())
                self.assertEqual(plan_file.stat().st_mode & 0o777, 0o600)
                launch.assert_not_called()
                prepared = local_app.prepare_remote_form(
                    'real', profile_sha256, task_file, plan_file, None, None,
                    plan_sha256, fields_file=fields_file)
                self.assertEqual(prepared[2], fields_content)
                state_file = root / 'form-state.json'
                state_report = local_app.plan_remote_form_state(
                    profile_sha256, task_file, plan_file, None, None,
                    plan_sha256, state_file, origin + '/state',
                    '1' * 64, '2' * 64, fields_file=fields_file)
                self.assertEqual(local_app.prepare_remote_form_state(
                    'real', profile_sha256, task_file, plan_file, None, None,
                    plan_sha256, state_file, state_report['state_plan_sha256'],
                    fields_file=fields_file)[1], state_report['state_plan_sha256'])
                cookie_file = root / 'cookie.txt'
                cookie = b'session=synthetic-ordered-form'
                cookie_file.write_bytes(cookie)
                cookie_file.chmod(0o600)
                cookie_sha256 = hashlib.sha256(cookie).hexdigest()
                self.assertEqual(local_app.prepare_remote_form_cookie(
                    'real', profile_sha256, task_file, plan_file, None, None,
                    plan_sha256, cookie_file, cookie_sha256,
                    fields_file=fields_file)[0], cookie)
                with (patch('sys.argv', ['aos-v1', 'preview-remote-form',
                                         '--remote-entry-profile-sha256', profile_sha256,
                                         '--remote-entry-task-file', str(task_file),
                                         '--remote-form-plan-file', str(plan_file),
                                         '--remote-form-fields-file', str(fields_file),
                                         '--remote-form-public-plan-sha256', plan_sha256]),
                      patch('sys.stdout', new_callable=StringIO) as preview):
                    local_app.main()
                self.assertEqual(json.loads(preview.getvalue())['field_names'], ['subject', 'message'])
                self.assertNotIn('private-subject-971', preview.getvalue())
                with self.assertRaisesRegex(ValueError, 'one private input mode'):
                    local_app.prepare_remote_form('real', profile_sha256, task_file,
                                                  plan_file, 'subject', None, plan_sha256,
                                                  fields_file=fields_file)
                with self.assertRaisesRegex(ValueError, 'fields_document_body_invalid'):
                    changed_document = {**fields_document, 'body_bytes': 1}
                    fields_file.write_bytes(canonical(changed_document).encode())
                    local_app.prepare_remote_form('real', profile_sha256, task_file,
                                                  plan_file, None, None, plan_sha256,
                                                  fields_file=fields_file)
                fields_file.write_bytes(fields_content)
                running = local_app.LocalAppState(
                    session='app-' + 'f' * 32, mode='real', phase='running',
                    supervisor=process_identity(os.getpid()),
                    backend=process_identity(os.getpid()), started_at='synthetic',
                    remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=task_sha256,
                    remote_form_plan_sha256=plan_sha256,
                    remote_form_fields_sha256=fields_sha256,
                    remote_form_state_plan_sha256=state_report['state_plan_sha256'],
                    remote_form_cookie_sha256=cookie_sha256)
                with (patch('aos.local_app.socket.socket') as socket_factory,
                      patch('aos.local_app.subprocess.Popen') as launch,
                      patch('aos.local_app.uuid4', return_value=SimpleNamespace(hex='f' * 32)),
                      patch('aos.local_app.read_state', side_effect=[None, running, running])):
                    socket_factory.return_value.__enter__.return_value.fileno.return_value = 23
                    launch.return_value.wait.return_value = 0
                    status = local_app.start(
                        'real', check=False, remote_entry_profile_sha256=profile_sha256,
                        remote_entry_task_file=task_file, remote_form_plan_file=plan_file,
                        remote_form_fields_file=fields_file,
                        remote_form_public_plan_sha256=plan_sha256,
                        remote_form_state_plan_file=state_file,
                        remote_form_public_state_plan_sha256=state_report['state_plan_sha256'],
                        remote_form_cookie_file=cookie_file,
                        remote_form_cookie_sha256=cookie_sha256)
                self.assertEqual(status['remote_form_fields_sha256'], fields_sha256)
                self.assertEqual(status['remote_form_state_plan_sha256'],
                                 state_report['state_plan_sha256'])
                self.assertEqual(status['remote_form_cookie_sha256'], cookie_sha256)
                session = self.base / running.session
                copied = session / 'remote-form-fields.json'
                self.assertEqual(local_app.private_read(copied, 8192), fields_content)
                self.assertEqual(local_app.private_read(session / 'remote-form-state-plan.json', 20000),
                                 state_file.read_bytes())
                self.assertEqual(local_app.private_read(session / 'remote-form-cookie.txt', 2048), cookie)
                self.assertFalse((session / 'remote-form-value.txt').exists())
                manager_command = launch.call_args.args[0]
                self.assertEqual(manager_command[manager_command.index('--remote-form-fields-sha256') + 1],
                                 fields_sha256)
                self.assertNotIn(str(fields_file), manager_command)
                self.assertNotIn('private-subject-971', ' '.join(manager_command))
                backend_command = local_app.managed_backend_command(
                    session, 'real', 23, remote_entry_profile_sha256=profile_sha256,
                    remote_entry_task_sha256=task_sha256,
                    remote_form_plan_sha256=plan_sha256,
                    remote_form_fields_sha256=fields_sha256,
                    remote_form_state_plan_sha256=state_report['state_plan_sha256'],
                    remote_form_cookie_sha256=cookie_sha256)
                self.assertEqual(backend_command[backend_command.index('--remote-form-fields-file') + 1],
                                 str(copied))
                self.assertEqual(backend_command[backend_command.index('--remote-form-state-plan-file') + 1],
                                 str(session / 'remote-form-state-plan.json'))
                self.assertEqual(backend_command[backend_command.index('--remote-form-cookie-file') + 1],
                                 str(session / 'remote-form-cookie.txt'))
                self.assertNotIn('private-message-971', ' '.join(backend_command))
                base_command = [local_app.sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'),
                                '--port', '18765', '--engine', 'fixture', '--browser-tasks',
                                '--desktop-browser', '--web-profiles-root', str(profiles.root),
                                '--remote-entry-mcp-manifest', str(local_app.MCP_MANIFEST),
                                '--remote-entry-profile-sha256', profile_sha256,
                                '--remote-entry-task-file', str(session / 'remote-entry-task.json'),
                                '--remote-entry-task-sha256', task_sha256,
                                '--remote-form-plan-file', str(session / 'remote-form-plan.json'),
                                '--remote-form-plan-sha256', plan_sha256,
                                '--remote-form-fields-file', str(copied),
                                '--remote-form-public-plan-sha256', plan_sha256]
                rejected = subprocess.run(base_command + ['--remote-form-fields-sha256', '0' * 64],
                                          capture_output=True, timeout=15)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b'Public HTTPS form plan or private value', rejected.stderr)
                self.assertNotIn(b'private-subject-971', rejected.stderr)
                changed_document = {**fields_document, 'fields': [
                    fields[0], {'name': 'message', 'value': 'changed'}]}
                copied.write_bytes(canonical(changed_document).encode())
                lock_descriptor = os.open(self.base / 'manager.lock', os.O_RDWR)
                with (patch('aos.local_app.signal.signal'),
                      patch('aos.local_app.subprocess.Popen') as backend_launch,
                      self.assertRaises(ValueError)):
                    local_app.supervise(running.session, 'real', lock_descriptor, -1,
                                        remote_entry_profile_sha256=profile_sha256,
                                        remote_entry_task_sha256=task_sha256,
                                        remote_form_plan_sha256=plan_sha256,
                                        remote_form_fields_sha256=fields_sha256,
                                        remote_form_state_plan_sha256=state_report['state_plan_sha256'],
                                        remote_form_cookie_sha256=cookie_sha256)
                backend_launch.assert_not_called()
                self.assertFalse(copied.exists())
                self.assertFalse((session / 'remote-form-cookie.txt').exists())
                self.assertEqual(local_app.read_state().phase, 'failed')

    def test_private_form_value_retirement_only_unlinks_owned_session_entry(self):
        local_app.prepare_base()
        session = self.base / self.state.session
        session.mkdir(mode=0o700)
        private_value = session / 'remote-form-value.txt'
        private_value.write_bytes(b'synthetic-private-value')
        private_value.chmod(0o644)
        private_fields = session / 'remote-form-fields.json'
        private_fields.write_bytes(b'synthetic-private-fields')
        private_fields.chmod(0o600)
        local_app.retire_remote_form_value(session)
        self.assertFalse(private_value.exists())
        self.assertFalse(private_fields.exists())
        local_app.retire_remote_form_value(session)
        outside = self.base / 'outside.txt'
        outside.write_bytes(b'keep')
        private_value.symlink_to(outside)
        private_fields.symlink_to(outside)
        local_app.retire_remote_form_value(session)
        self.assertEqual(outside.read_bytes(), b'keep')
        session.chmod(0o755)
        private_value.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'private_directory'):
            local_app.retire_remote_form_value(session)
        self.assertEqual(private_value.read_bytes(), b'keep')

    def test_failed_managed_form_admission_retires_copied_value(self):
        local_app.prepare_base()
        session = self.base / self.state.session
        session.mkdir(mode=0o700)
        task = session / 'remote-entry-task.json'
        task.write_bytes(b'{}')
        task.chmod(0o600)
        private_value = session / 'remote-form-value.txt'
        private_value.write_bytes(b'synthetic-private-value')
        private_value.chmod(0o600)
        lock = self.base / 'manager.lock'
        lock.touch(mode=0o600)
        lock_descriptor = os.open(lock, os.O_RDWR)
        with (patch('aos.local_app.signal.signal'),
              patch('aos.local_app.prepare_remote_form', side_effect=ValueError('rejected')),
              self.assertRaisesRegex(ValueError, 'rejected')):
            local_app.supervise(self.state.session, 'real', lock_descriptor, -1,
                                remote_entry_profile_sha256='a' * 64,
                                remote_entry_task_sha256=hashlib.sha256(b'{}').hexdigest(),
                                remote_form_plan_sha256='b' * 64,
                                remote_form_field_name='message')
        self.assertFalse(private_value.exists())
        self.assertEqual(local_app.read_state().phase, 'failed')

    def test_changed_managed_route_review_fails_before_backend(self):
        local_app.prepare_base()
        session = self.base / self.state.session
        session.mkdir(mode=0o700)
        task = session / 'remote-entry-task.json'
        task.write_bytes(b'{}')
        task.chmod(0o600)
        plan = session / 'remote-routes-plan.json'
        plan.write_bytes(b'{}')
        plan.chmod(0o600)
        lock = self.base / 'manager.lock'
        lock.touch(mode=0o600)
        lock_descriptor = os.open(lock, os.O_RDWR)
        source = REPO_ROOT / 'data' / 'synthetic-source.sqlite'
        site_store = REPO_ROOT / 'data' / 'synthetic-site'
        review_store = REPO_ROOT / 'data' / 'synthetic-review'
        with (patch('aos.local_app.signal.signal'),
              patch('aos.local_app.prepare_managed_remote_route_review',
                    return_value=(source, site_store, review_store,
                                  'c' * 64, 'd' * 64, 'e' * 64)),
              patch('aos.local_app.subprocess.Popen') as launch,
              self.assertRaisesRegex(ValueError, 'changed before managed launch')):
            local_app.supervise(
                self.state.session, 'real', lock_descriptor, -1,
                remote_entry_profile_sha256='a' * 64,
                remote_entry_task_sha256=hashlib.sha256(b'{}').hexdigest(),
                remote_routes_plan_sha256=hashlib.sha256(b'{}').hexdigest(),
                remote_route_review_source_database=source,
                remote_route_review_site_store=site_store,
                remote_route_review_store=review_store,
                remote_route_review_sha256='c' * 64,
                remote_route_review_source_sha256='f' * 64)
        launch.assert_not_called()
        self.assertEqual(local_app.read_state().phase, 'failed')

    def test_finished_managed_form_backend_retires_copied_value(self):
        local_app.prepare_base()
        session = self.base / self.state.session
        session.mkdir(mode=0o700)
        task = session / 'remote-entry-task.json'
        task.write_bytes(b'{}')
        task.chmod(0o600)
        private_value = session / 'remote-form-value.txt'
        private_value.write_bytes(b'synthetic-private-value')
        private_value.chmod(0o600)
        lock = self.base / 'manager.lock'
        lock.touch(mode=0o600)
        lock_descriptor = os.open(lock, os.O_RDWR)
        listen_descriptor = os.open(os.devnull, os.O_RDONLY)
        process = SimpleNamespace(pid=os.getpid(), poll=lambda: 0, wait=lambda timeout: 0)

        def request_shutdown(_signal, callback):
            callback(None, None)

        with (patch('aos.local_app.signal.signal', side_effect=request_shutdown),
              patch('aos.local_app.prepare_remote_form', return_value=(b'{}', 'b' * 64, b'value')),
              patch('aos.local_app.managed_backend_command', return_value=['mock']),
              patch('aos.local_app.subprocess.Popen', return_value=process),
              patch('aos.local_app.clean_shutdown', return_value=True)):
            local_app.supervise(self.state.session, 'real', lock_descriptor, listen_descriptor,
                                remote_entry_profile_sha256='a' * 64,
                                remote_entry_task_sha256=hashlib.sha256(b'{}').hexdigest(),
                                remote_form_plan_sha256='b' * 64,
                                remote_form_field_name='message')
        self.assertFalse(private_value.exists())
        self.assertEqual(local_app.read_state().phase, 'stopped')

    def test_owned_recipe_supervise_rejects_manifest_recipe_pin_mismatch(self):
        from aos.owned_form_invocation_session import provision_owned_synthetic_form_invocation

        local_app.prepare_base()
        session = self.base / self.state.session
        session.mkdir(mode=0o700)
        bundle = provision_owned_synthetic_form_invocation(
            session / 'owned-form', 19434, mode='owned_synthetic_form_recipe')
        lock = self.base / 'manager.lock'
        lock.touch(mode=0o600)
        lock_descriptor = os.open(lock, os.O_RDWR)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        self.addCleanup(os.close, lock_descriptor)
        with (patch('aos.local_app.signal.signal'),
              self.assertRaisesRegex(ValueError, 'recipe pin differs')):
            local_app.supervise(
                self.state.session, 'real', lock_descriptor, -1,
                owned_form_listener_fd=listener.fileno(),
                owned_form_manifest_sha256=bundle['manifest_sha256'],
                owned_form_recipe_sha256='f' * 64)

    def test_changed_managed_form_state_fails_before_backend_and_retires_value(self):
        local_app.prepare_base()
        session = self.base / self.state.session
        session.mkdir(mode=0o700)
        task = session / 'remote-entry-task.json'
        task.write_bytes(b'{}')
        task.chmod(0o600)
        private_value = session / 'remote-form-value.txt'
        private_value.write_bytes(b'synthetic-private-value')
        private_value.chmod(0o600)
        private_cookie = session / 'remote-form-cookie.txt'
        private_cookie.write_bytes(b'session=synthetic-secret')
        private_cookie.chmod(0o600)
        lock = self.base / 'manager.lock'
        lock.touch(mode=0o600)
        lock_descriptor = os.open(lock, os.O_RDWR)
        with (patch('aos.local_app.signal.signal'),
              patch('aos.local_app.prepare_remote_form',
                    return_value=(b'{}', 'b' * 64, b'value')),
              patch('aos.local_app.prepare_remote_form_state',
                    side_effect=ValueError('changed state plan')),
              patch('aos.local_app.subprocess.Popen') as launch,
              self.assertRaisesRegex(ValueError, 'changed state plan')):
            local_app.supervise(self.state.session, 'real', lock_descriptor, -1,
                                remote_entry_profile_sha256='a' * 64,
                                remote_entry_task_sha256=hashlib.sha256(b'{}').hexdigest(),
                                remote_form_plan_sha256='b' * 64,
                                remote_form_field_name='message',
                                remote_form_state_plan_sha256='c' * 64,
                                remote_form_cookie_sha256='d' * 64)
        launch.assert_not_called()
        self.assertFalse(private_value.exists())
        self.assertFalse(private_cookie.exists())
        self.assertEqual(local_app.read_state().phase, 'failed')

    def test_reboot_recovery_refuses_same_boot_without_docker_inspection(self):
        local_app.prepare_base()
        local_app.write_state(self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor}))
        with patch('aos.local_app.subprocess.run') as docker, self.assertRaisesRegex(ValueError, 'previous boot'):
            local_app.recover_reboot()
        docker.assert_not_called()

    def test_restart_running_session_preserves_mode_and_requires_clean_stop(self):
        local_app.prepare_base()
        running = self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor})
        local_app.write_state(running)
        with (patch('aos.local_app.require_local_preflight') as preflight,
              patch('aos.local_app.require_idle_restart') as idle,
              patch('aos.local_app.stop', return_value={'phase': 'stopped'}) as stop,
              patch('aos.local_app.start', return_value={'phase': 'running'}) as start):
            self.assertEqual(local_app.restart()['phase'], 'running')
        preflight.assert_called_once_with('fixture')
        idle.assert_called_once_with(running)
        stop.assert_called_once_with(expected_session=running.session)
        start.assert_called_once_with('fixture')
        with (patch('aos.local_app.require_local_preflight'),
              patch('aos.local_app.require_idle_restart'),
              patch('aos.local_app.stop', return_value={'phase': 'needs_inspection'}),
              patch('aos.local_app.start') as start,
              self.assertRaisesRegex(ValueError, 'shutdown incomplete')):
            local_app.restart()
        start.assert_not_called()
        with (patch('aos.local_app.require_local_preflight'),
              patch('aos.local_app.require_idle_restart', side_effect=ValueError('busy')),
              patch('aos.local_app.stop') as stop,
              patch('aos.local_app.start') as start,
              self.assertRaisesRegex(ValueError, 'busy')):
            local_app.restart()
        stop.assert_not_called()
        start.assert_not_called()

    def test_restart_previous_boot_uses_guarded_recovery_and_refuses_opt_ins(self):
        local_app.prepare_base()
        previous_boot = self.state.supervisor.model_copy(update={
            'boot_id': '00000000-0000-0000-0000-000000000000'})
        interrupted = self.state.model_copy(update={
            'mode': 'real', 'phase': 'running', 'supervisor': previous_boot,
            'backend': previous_boot})
        local_app.write_state(interrupted)
        with (patch('aos.local_app.require_local_preflight') as preflight,
              patch('aos.local_app.recover_reboot', return_value={'phase': 'stopped'}) as recover,
              patch('aos.local_app.start', return_value={'phase': 'running'}) as start):
            self.assertEqual(local_app.restart()['phase'], 'running')
        preflight.assert_called_once_with('real')
        recover.assert_called_once_with(expected_session=interrupted.session)
        start.assert_called_once_with('real')
        local_app.write_state(interrupted.model_copy(update={'synthetic_staging': True}))
        with (patch('aos.local_app.recover_reboot') as recover,
              patch('aos.local_app.start') as start,
              self.assertRaisesRegex(ValueError, 'plain local session')):
            local_app.restart()
        recover.assert_not_called()
        start.assert_not_called()
        local_app.write_state(interrupted.model_copy(update={
            'remote_entry_profile_sha256': 'a' * 64,
            'remote_entry_task_sha256': 'b' * 64}))
        with (patch('aos.local_app.recover_reboot') as recover,
              patch('aos.local_app.start') as start,
              self.assertRaisesRegex(ValueError, 'plain local session')):
            local_app.restart()
        recover.assert_not_called()
        start.assert_not_called()

    def test_restart_refuses_session_drift_and_same_boot_interruption(self):
        local_app.prepare_base()
        interrupted = self.state.model_copy(update={'phase': 'failed', 'backend': self.state.supervisor})
        local_app.write_state(interrupted)
        with (patch('aos.local_app.require_local_preflight'),
              patch('aos.local_app.start') as start,
              self.assertRaisesRegex(ValueError, 'previous boot')):
            local_app.restart()
        start.assert_not_called()
        with (patch('aos.local_app.signal_owned') as signal,
              self.assertRaisesRegex(ValueError, 'session changed')):
            local_app.stop(expected_session='app-' + 'b' * 32)
        signal.assert_not_called()
        with (patch('aos.local_app.subprocess.run') as docker,
              self.assertRaisesRegex(ValueError, 'session changed')):
            local_app.recover_reboot(expected_session='app-' + 'b' * 32)
        docker.assert_not_called()

    def test_restart_preflight_failure_preserves_running_session(self):
        local_app.prepare_base()
        running = self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor})
        local_app.write_state(running)
        report = {'ready': False, 'checks': [
            {'ok': False, 'detail': 'Stale UI build', 'action': 'Run pnpm build'}]}
        with (patch('aos.local_preflight.check_local', return_value=report),
              patch('aos.local_app.require_idle_restart') as idle,
              patch('aos.local_app.stop') as stop,
              patch('aos.local_app.recover_reboot') as recover,
              patch('aos.local_app.start') as start,
              self.assertRaisesRegex(ValueError, 'Stale UI build')):
            local_app.restart()
        stop.assert_not_called()
        idle.assert_not_called()
        recover.assert_not_called()
        start.assert_not_called()
        self.assertEqual(local_app.read_state(), running)

    def test_restart_idle_gate_refuses_busy_or_unverified_status(self):
        running = self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor})
        desktop_session_id = 'desktop-session-' + 'b' * 32
        base_tasks = {'busy': False, 'reserved': False, 'approval': None,
                      'auto_approval': None, 'jobs': []}
        control = {'control': {'session_id': desktop_session_id, 'owner': 'AGENT', 'status': 'running'}}
        def response(value):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
        with (patch('aos.local_app.token_value', return_value='synthetic-token'),
              patch('aos.local_app.httpx.Client') as client):
            requests = client.return_value.__enter__.return_value
            requests.post.return_value = response({})
            requests.get.side_effect = [response({**base_tasks, 'busy': True}), response(control)]
            with self.assertRaisesRegex(ValueError, 'idle AGENT'):
                local_app.require_idle_restart(running)
            requests.get.side_effect = [response(base_tasks), response(
                {'control': {'owner': 'HUMAN', 'status': 'running'}})]
            with self.assertRaisesRegex(ValueError, 'idle AGENT'):
                local_app.require_idle_restart(running)
            requests.get.side_effect = [response(base_tasks), response(control)]
            requests.post.side_effect = [response({}), response({'quiesced': True, 'session_id': desktop_session_id})]
            local_app.require_idle_restart(running)
            self.assertEqual(requests.post.call_args_list[-1].args, ('/api/restart/quiesce',))
            self.assertEqual(requests.post.call_args_list[-1].kwargs['json'], {'session_id': desktop_session_id})
            requests.post.side_effect = [response({}), response({'quiesced': False, 'session_id': desktop_session_id})]
            requests.get.side_effect = [response(base_tasks), response(control)]
            with self.assertRaisesRegex(ValueError, 'idle AGENT'):
                local_app.require_idle_restart(running)
            requests.post.side_effect = [response({}), httpx.Response(
                404, request=httpx.Request('POST', local_app.ORIGIN + '/api/restart/quiesce'))]
            requests.get.side_effect = [response(base_tasks), response(control)]
            with self.assertRaisesRegex(ValueError, 'idle AGENT'):
                local_app.require_idle_restart(running)

    def test_restart_quiesce_endpoint_failure_does_not_signal_process(self):
        running = self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor})
        local_app.prepare_base()
        local_app.write_state(running)
        with (patch('aos.local_app.require_local_preflight'),
              patch('aos.local_app.require_idle_restart', side_effect=ValueError('quiesce unavailable')),
              patch('aos.local_app.stop') as stop,
              patch('aos.local_app.start') as start,
              self.assertRaisesRegex(ValueError, 'quiesce unavailable')):
            local_app.restart()
        stop.assert_not_called()
        start.assert_not_called()

    def test_release_restart_requires_exact_running_session(self):
        with self.assertRaisesRegex(ValueError, 'Inspect the running'):
            local_app.release_restart_quiesce()
        running = self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor})
        desktop_session_id = 'desktop-session-' + 'b' * 32
        local_app.prepare_base()
        local_app.write_state(running)
        def response(value):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
        with (patch('aos.local_app.observe_process', return_value='same_process'),
              patch('aos.local_app.token_value', return_value='synthetic-token'),
              patch('aos.local_app.httpx.Client') as client):
            requests = client.return_value.__enter__.return_value
            requests.get.side_effect = [response({'restart_quiesced': True}),
                                        response({'control': {'session_id': desktop_session_id}})]
            requests.post.side_effect = [response({}), response({'quiesced': False, 'session_id': desktop_session_id})]
            self.assertEqual(local_app.release_restart_quiesce()['quiesced'], False)
            self.assertEqual(requests.post.call_args_list[-1].kwargs['json'], {'session_id': desktop_session_id})
            requests.get.side_effect = [response({'restart_quiesced': True}),
                                        response({'control': {'session_id': desktop_session_id}})]
            requests.post.side_effect = [response({}), response({'quiesced': False, 'session_id': 'other'})]
            with self.assertRaisesRegex(ValueError, 'release failed'):
                local_app.release_restart_quiesce()
            requests.get.side_effect = [response({'restart_quiesced': False}),
                                        response({'control': {'session_id': desktop_session_id}})]
            requests.post.side_effect = [response({})]
            with self.assertRaisesRegex(ValueError, 'release failed'):
                local_app.release_restart_quiesce()
            self.assertEqual(requests.post.call_count, 5)

    def test_restart_cli_rejects_mode_override(self):
        with (patch('sys.argv', ['aos-v1', 'restart', '--fixture']),
              patch('aos.local_app.restart') as restart,
              patch('sys.stderr', new_callable=StringIO) as error,
              self.assertRaises(SystemExit)):
            local_app.main()
        self.assertIn('preserves the existing mode', error.getvalue())
        restart.assert_not_called()
        with (patch('sys.argv', ['aos-v1', 'restart']),
              patch('aos.local_app.restart', return_value={'phase': 'running'}) as restart,
              patch('sys.stdout', new_callable=StringIO) as output):
            local_app.main()
        restart.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), {'phase': 'running'})

    def test_restart_stopped_session_starts_same_mode_and_requires_existing_session(self):
        with self.assertRaisesRegex(ValueError, 'No existing local application session'):
            local_app.restart()
        local_app.prepare_base()
        local_app.write_state(self.state.model_copy(update={'phase': 'stopped'}))
        with (patch('aos.local_app.stop') as stop,
              patch('aos.local_app.recover_reboot') as recover,
              patch('aos.local_app.start', return_value={'phase': 'running'}) as start):
            self.assertEqual(local_app.restart()['phase'], 'running')
        stop.assert_not_called()
        recover.assert_not_called()
        start.assert_called_once_with('fixture')

    def test_preflight_failure_cannot_start_backend(self):
        report = {'ready': False, 'checks': [{'ok': False, 'detail': 'Missing', 'action': 'Prepare explicitly'}]}
        with patch('aos.local_preflight.check_local', return_value=report), patch('subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'Missing'):
                local_app.start()
        launch.assert_not_called()
        self.assertIsNone(local_app.read_state())

    def test_live_port_is_not_reused_or_killed(self):
        with socket.socket() as listener:
            try:
                listener.bind(('127.0.0.1', 8765))
            except OSError:
                self.skipTest('The fixed pilot port is already in use')
            listener.listen()
            with patch('subprocess.Popen') as launch, self.assertRaises(OSError):
                local_app.start(check=False)
            launch.assert_not_called()
            self.assertIsNone(local_app.read_state())

    def test_only_live_state_can_reveal_token(self):
        with self.assertRaises(ValueError):
            local_app.token_value(self.state)
        running = self.state.model_copy(update={'phase': 'running', 'token_name': 'desktop-console-' + 'a' * 16 + '.token',
                                                'backend': self.state.supervisor})
        with patch('aos.local_app.private_read', return_value=b'a' * 43):
            self.assertEqual(local_app.token_value(running), 'a' * 43)
        with patch('aos.local_app.private_read', return_value=b'bad token'):
            with self.assertRaises(ValueError):
                local_app.token_value(running)
        with patch('aos.local_app.observe_process', return_value='different_process'):
            with self.assertRaises(ValueError):
                local_app.token_value(running)

    def test_growing_log_does_not_invalidate_private_state_reader(self):
        local_app.prepare_base()
        target = self.base / 'log'
        target.write_bytes(b'prefix')
        target.chmod(0o600)
        original = os.read

        def append(descriptor, limit):
            value = original(descriptor, limit)
            with target.open('ab') as stream:
                stream.write(b'new log line\n')
            return value

        with patch('aos.local_app.os.read', side_effect=append):
            self.assertEqual(local_app.private_read(target, append_log=True), b'prefix')
            with self.assertRaisesRegex(ValueError, 'Private file changed'):
                local_app.private_read(target)

    def test_schema_rejects_arbitrary_token_path_or_unknown_state(self):
        from aos.dataset import validator

        schema = json.loads((REPO_ROOT / 'schemas/local_app_state.schema.json').read_text())
        self.assertEqual(schema, local_app.LocalAppState.model_json_schema())
        validator('local_app_state').validate(self.state.model_dump())
        for change in ({'token_name': '/etc/passwd'}, {'phase': 'ready'}, {'url': 'http://example.com'},
                       {'synthetic_staging': 'yes'}, {'remote_entry_profile_sha256': 'wrong'},
                       {'remote_static_assets_plan_sha256': 'wrong'},
                       {'remote_form_plan_sha256': 'wrong'},
                       {'remote_form_field_name': 'not a field'},
                       {'remote_route_review_sha256': 'wrong'},
                       {'remote_json_review_sha256': 'wrong'},
                       {'raw_token': 'secret'}):
            with self.assertRaises(ValueError):
                local_app.LocalAppState.model_validate({**self.state.model_dump(), **change})
        with self.assertRaises(ValueError):
            local_app.LocalAppState.model_validate({**self.state.model_dump(),
                'remote_entry_profile_sha256': 'a' * 64,
                'remote_entry_task_sha256': 'b' * 64})
        with self.assertRaises(ValueError):
            local_app.LocalAppState.model_validate({**self.state.model_dump(),
                'remote_route_review_sha256': 'c' * 64,
                'remote_route_review_source_sha256': 'd' * 64})
        with self.assertRaises(ValueError):
            local_app.LocalAppState.model_validate({**self.state.model_dump(),
                'remote_static_assets_plan_sha256': 'c' * 64})

    def test_clean_shutdown_requires_token_removal_and_every_owned_journal(self):
        local_app.prepare_base()
        state = self.state.model_copy(update={'phase': 'running', 'backend': self.state.supervisor,
                                              'token_name': 'desktop-console-' + 'f' * 16 + '.token'})
        self.assertFalse(local_app.clean_shutdown(state))
        directory = self.base / state.session
        directory.mkdir(mode=0o700)
        journals = directory / '.aos-lifecycle'
        journals.mkdir(mode=0o700)
        (journals / 'synthetic.jsonl').write_text('not a journal')
        self.assertFalse(local_app.clean_shutdown(state))
        event = SimpleNamespace(stage='removed', birth=SimpleNamespace(process=state.backend))
        with patch('aos.local_app.read_journal', return_value=([event], 'synthetic')):
            self.assertTrue(local_app.clean_shutdown(state))
            event.stage = 'started'
            self.assertFalse(local_app.clean_shutdown(state))
            event.stage = 'removed'
            event.birth.process = state.backend.model_copy(update={'start_ticks': 1})
            self.assertFalse(local_app.clean_shutdown(state))
        self.assertFalse(local_app.clean_shutdown(state.model_copy(update={'token_name': None})))


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Real managed app requires AOS_DESKTOP_TESTS=1')
class LocalAppIntegrationTests(unittest.TestCase):
    def test_restart_admission_quiesce_with_live_backend_process(self):
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='restart-quiesce-', dir=REPO_ROOT / 'data') as temporary:
            with task_server(Path(temporary), 'fixture', ('--browser-tasks',)) as (origin, token, client, server):
                backend = process_identity(server.pid)
                manager_state = local_app.LocalAppState(
                    session='app-' + 'a' * 32, mode='fixture', phase='running',
                    supervisor=backend, backend=backend, started_at='synthetic')
                desktop_session_id = client.get('/api/state').json()['control']['session_id']
                self.assertNotEqual(desktop_session_id, manager_state.session)
                with (patch('aos.local_app.ORIGIN', origin),
                      patch('aos.local_app.token_value', return_value=token),
                      patch('aos.local_app.read_state', return_value=manager_state)):
                    local_app.require_idle_restart(manager_state)
                    self.assertTrue(client.get('/api/tasks').json()['restart_quiesced'])
                    state = client.get('/api/state').json()['control']
                    self.assertEqual(client.post('/api/tasks', json={
                        'kind': 'hello', 'lease_id': state['lease_id'],
                        'generation': state['generation']}).status_code, 409)
                    self.assertEqual(local_app.release_restart_quiesce(),
                                     {'quiesced': False, 'session_id': desktop_session_id})
                    self.assertFalse(client.get('/api/tasks').json()['restart_quiesced'])

    def test_managed_fixture_start_login_graceful_stop_and_fresh_restart(self):
        base = REPO_ROOT / 'data' / ('local-app-test-' + uuid4().hex)
        with patch('aos.local_app.BASE', base):
            state = None
            try:
                status = local_app.start('fixture', check=False)
                self.assertEqual(status['phase'], 'running')
                self.assertEqual(local_app.start('fixture', check=False)['session'], status['session'])
                state = local_app.read_state()
                token = local_app.token_value(state)
                with httpx.Client(base_url=local_app.ORIGIN, headers={'Origin': local_app.ORIGIN}, trust_env=False, timeout=10) as client:
                    self.assertEqual(client.get('/api/tasks').status_code, 401)
                    self.assertEqual(client.get('/ui/').status_code, 200)
                    self.assertEqual(client.post('/api/login', json={'token': token}).status_code, 200)
                    self.assertEqual(set(client.get('/api/tasks').json()['kinds']),
                                     {'hello', 'browser_form', 'vision_canvas', 'browser_local_navigation'})
                    self.assertEqual(client.get('/api/tasks').json()['browser_transport'], 'cdp')
                    self.assertEqual(client.get('/api/tasks').json()['navigation_transport'], 'playwright_mcp')
                    self.assertEqual(client.get('/api/tasks').json()['browser_display'], 'desktop')
                    self.assertEqual(client.get('/api/tasks').json()['vision_display'], 'desktop')
                    runtime = client.get('/api/state').json()['runtime']
                self.assertEqual(local_app.stop()['phase'], 'stopped')
                self.assertFalse((REPO_ROOT / 'runs' / state.token_name).exists())
                from aos.desktop import DOCKER

                listed = subprocess.run([*DOCKER, 'ps', '-aq', '--filter', 'id=' + runtime['container_id']], capture_output=True, text=True, check=True)
                self.assertEqual(listed.stdout.strip(), '')
                first = status['session']
                status = local_app.start('fixture', check=False)
                self.assertEqual(status['phase'], 'running')
                self.assertNotEqual(status['session'], first)
                self.assertTrue((base / first / 'store.sqlite').is_file())
            finally:
                current = local_app.read_state()
                if current is not None and local_app.observe_process(current.supervisor) == 'same_process':
                    local_app.stop()

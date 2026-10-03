import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from aos.contracts import REPO_ROOT, canonical, digest
from aos import desktop_console, remote_page_draft_seed
from aos.site_knowledge import SitePageDraft


specification = importlib.util.spec_from_file_location(
    'shared_desktop_entrypoint_paths', REPO_ROOT / 'scripts/serve_desktop.py')
SERVE = importlib.util.module_from_spec(specification)
specification.loader.exec_module(SERVE)

STORE_ARGUMENTS = (
    'site_knowledge_root', 'site_skills_root', 'page_seed_root',
    'route_review_root', 'json_review_root', 'json_page_seed_root',
)


class SharedDesktopEntrypointPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-entrypoint-paths-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / 'data'
        self.data.mkdir(mode=0o700)
        self.workspace = self.data / 'synthetic-session' / 'workspace'
        self.assets = self.data / 'synthetic-assets'
        self.assets.mkdir(mode=0o700)
        self.runtime = Mock()
        self.store = Mock()
        self.controller = SimpleNamespace(
            session_id='synthetic-session', store=self.store,
            state=lambda: {'status': 'stopped'})

    def run_main(self, extra_arguments=(), *, console_factory=None, scheduler=None):
        factory = console_factory or Mock(return_value=SimpleNamespace(router=SimpleNamespace()))
        arguments = [
            'serve_desktop.py', '--port', '48765',
            '--workspace', str(self.workspace),
            '--database', str(self.data / 'synthetic.sqlite'),
            '--trajectory-database', str(self.data / 'synthetic-trajectory.sqlite'),
            '--web-profiles-root', str(self.data / 'profiles'),
            '--knowledge-root', str(self.data / 'document-knowledge'),
            *extra_arguments,
        ]
        with (patch.object(SERVE, 'REPO_ROOT', self.root),
              patch.object(SERVE, 'DesktopRuntime', return_value=self.runtime),
              patch.object(SERVE, 'TrajectoryStore', return_value=self.store),
              patch.object(SERVE, 'DesktopController', return_value=self.controller),
              patch.object(SERVE, 'DesktopScheduler', return_value=scheduler),
              patch.object(SERVE, 'FixtureDecisionEngine', return_value=object()),
              patch.object(SERVE, 'prepare_console_assets', return_value=self.assets),
              patch.object(SERVE, 'create_console', factory),
              patch.object(SERVE.uvicorn, 'run') as server,
              patch.object(sys, 'argv', arguments), redirect_stdout(io.StringIO())):
            SERVE.main()
        server.assert_called_once()
        self.assertEqual(list((self.root / 'runs').iterdir()), [])
        return factory

    def test_all_explicit_paths_are_parsed_as_paths_and_forwarded(self):
        paths = {name: self.data / 'synthetic-session' / name.replace('_', '-')
                 for name in STORE_ARGUMENTS}
        arguments = [value for name, path in paths.items()
                     for value in ('--' + name.replace('_', '-'), str(path))]
        factory = self.run_main(arguments)
        factory.assert_called_once()
        for name, path in paths.items():
            with self.subTest(name=name):
                self.assertEqual(factory.call_args.kwargs[name], path)
                self.assertIsInstance(factory.call_args.kwargs[name], Path)
                self.assertFalse(path.exists())

    def test_omitted_paths_forward_none_preserving_console_fallback(self):
        factory = self.run_main()
        for name in STORE_ARGUMENTS:
            with self.subTest(name=name):
                self.assertIn(name, factory.call_args.kwargs)
                self.assertIsNone(factory.call_args.kwargs[name])

    def test_missing_path_values_fail_before_runtime_or_filesystem_effects(self):
        for name in STORE_ARGUMENTS:
            with self.subTest(name=name):
                with (patch.object(SERVE, 'DesktopRuntime') as runtime,
                      patch.object(sys, 'argv', ['serve_desktop.py', '--' + name.replace('_', '-')]),
                      redirect_stderr(io.StringIO())):
                    with self.assertRaises(SystemExit) as failure:
                        SERVE.main()
                self.assertEqual(failure.exception.code, 2)
                runtime.assert_not_called()
        self.assertFalse((self.root / 'runs').exists())

    def test_authenticated_seed_route_writes_only_explicit_private_store(self):
        manager = self.data / 'local-app-v1'
        manager.mkdir(mode=0o700)
        session = manager / ('app-' + 'a' * 32)
        session.mkdir(mode=0o700)
        self.workspace = session / 'workspace'
        connection = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE desktop_tasks '
                           '(session_id TEXT,kind TEXT,run_id TEXT,status TEXT)')
        connection.executemany('INSERT INTO desktop_tasks VALUES (?,?,?,?)', [
            ('synthetic-session', 'browser_remote_routes', 'before', 'succeeded'),
            ('synthetic-session', 'browser_remote_routes', 'after', 'succeeded'),
        ])
        self.store.connection = connection
        page = SitePageDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/site_page_draft.json').read_text())['page'])
        scheduler = SimpleNamespace(
            store=self.store, settings=SimpleNamespace(database=self.data / 'synthetic.sqlite'),
            remote_entry_profiles=SimpleNamespace(root=self.data / 'profiles'),
            remote_entry_profile_sha256=page.profile_sha256,
            remote_routes_plan=SimpleNamespace(routes=[object()], model_dump=lambda: {'synthetic': True}))
        paths = dict(zip(STORE_ARGUMENTS, [session / name for name in (
            'site-knowledge', 'site-skills', 'site-page-seeds',
            'route-reviews', 'json-reviews', 'json-page-seeds')]))
        arguments = ['--engine', 'fixture', *[
            value for name, path in paths.items()
            for value in ('--' + name.replace('_', '-'), str(path))]]
        default_seed = self.data / 'site-page-seeds'
        default_seed.mkdir(mode=0o700)
        sentinel = default_seed / 'active-user-sentinel'
        sentinel.write_text('synthetic untouched default')
        consoles = []

        def build_console(*arguments, **options):
            console = desktop_console.create_console(*arguments, **options)
            consoles.append(console)
            return console

        factory = Mock(side_effect=build_console)
        with patch.object(desktop_console, 'REPO_ROOT', self.root):
            self.run_main(arguments, console_factory=factory, scheduler=scheduler)
        token, origin = factory.call_args.args[1:3]
        client = TestClient(consoles[0], base_url=origin)
        self.addCleanup(client.close)
        report = {'profile_sha256': page.profile_sha256, 'page_key': page.page_key,
                  'draft_sha256': digest(page.model_dump()), 'execution_authorized': False}
        selection = {'before_run_id': 'before', 'after_run_id': 'after',
                     'route_index': 0, 'page_key': page.page_key}
        with (patch.object(remote_page_draft_seed, 'REPO_ROOT', self.root),
              patch.object(desktop_console, 'seed_remote_page_draft', return_value=(page, report)) as audit):
            headers = {'Origin': origin}
            self.assertEqual(client.post('/api/tasks/page-draft-seed',
                                         headers=headers, json=selection).status_code, 401)
            self.assertFalse(paths['page_seed_root'].exists())
            self.assertEqual(client.post('/api/login', headers=headers,
                                         json={'token': token}).status_code, 200)
            response = client.post('/api/tasks/page-draft-seed', headers=headers, json=selection)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(audit.call_args.kwargs['store'], paths['site_knowledge_root'])
        output = paths['page_seed_root'] / page.profile_sha256 / (page.page_key + '.json')
        self.assertEqual(output.read_bytes(), canonical(page.model_dump()).encode())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        self.assertEqual(sentinel.read_text(), 'synthetic untouched default')
        self.assertEqual(list(default_seed.iterdir()), [sentinel])


if __name__ == '__main__':
    unittest.main()

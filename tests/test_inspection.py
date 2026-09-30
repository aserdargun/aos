import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aos.computer import WorkspaceRuntime
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine
from aos.inspection import TrajectoryInspector
from aos.operator import Operator
from aos.storage import TrajectoryStore


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'trace.sqlite')
        self.store = TrajectoryStore(self.settings.database)
        self.addCleanup(self.store.close)
        self.runtime = WorkspaceRuntime(self.settings.workspace)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.result = asyncio.run(Operator(self.settings, self.store, self.runtime, FixtureDecisionEngine()).hello())
        self.inspector = TrajectoryInspector(self.settings.database)

    def test_summary_and_trace_match_verified_records_without_raw_payloads(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE tasks SET original_goal='synthetic-private-token' WHERE task_id IN (SELECT task_id FROM runs)")
            self.store.connection.execute("UPDATE observations SET payload_json='{" + '"secret":"synthetic-private-token"' + "}'")
        summary = self.inspector.overview()
        self.assertTrue(summary['available'])
        self.assertEqual(summary['runs'][0]['run_id'], self.result['run_id'])
        self.assertEqual(summary['runs'][0]['status'], 'succeeded')
        self.assertGreater(summary['runs'][0]['passed'], 0)
        self.assertEqual(summary['runs'][0]['model_calls'], 0)
        trace = self.inspector.trace(self.result['run_id'])
        self.assertEqual(trace['run']['status'], 'succeeded')
        self.assertEqual(trace['verifications'][0]['result'], 'passed')
        self.assertNotIn('synthetic-private-token', json.dumps([summary, trace]))
        self.assertNotIn('arguments_json', json.dumps(trace))

    def test_readonly_inspection_does_not_reconcile_or_create_database(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='running'")
        before = self.settings.database.read_bytes()
        self.assertEqual(self.inspector.overview()['runs'][0]['status'], 'running')
        self.inspector.trace(self.result['run_id'])
        self.assertEqual(self.settings.database.read_bytes(), before)
        with self.inspector.connection() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE runs SET status='paused'")
        connection.close()
        missing = self.root / 'absent.sqlite'
        self.assertFalse(TrajectoryInspector(missing).overview()['available'])
        self.assertFalse(missing.exists())
        self.assertIsNone(self.inspector.trace("' OR 1=1--"))
        self.assertIsNone(self.inspector.trace('unknown-run'))

    @unittest.skipUnless(importlib.util.find_spec('fastapi') and importlib.util.find_spec('httpx'), 'Install desktop extra')
    def test_authenticated_inspection_routes_do_not_accept_database_paths(self):
        import httpx
        from aos.desktop_console import create_console
        from aos.desktop_control import DesktopController
        from test_desktop import FakeDesktop

        controller = DesktopController(self.store, FakeDesktop())
        origin = 'http://127.0.0.1:8765'
        app = create_console(controller, 'synthetic-token', origin, self.root, self.settings.database)

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin, headers={'Origin': origin}) as client:
                for route in ['/api/overview', '/api/resources', '/api/runs/' + self.result['run_id']]:
                    self.assertEqual((await client.get(route)).status_code, 401)
                await client.post('/api/login', json={'token': 'synthetic-token'})
                self.assertEqual((await client.get('/api/overview')).json()['trajectory']['runs'][0]['run_id'], self.result['run_id'])
                self.assertEqual((await client.get('/api/runs/' + self.result['run_id'])).json()['run']['status'], 'succeeded')
                self.assertEqual((await client.get('/api/runs/missing')).status_code, 404)
                self.assertFalse((await client.get('/api/resources')).json()['available'])
                self.assertEqual((await client.post('/api/overview', json={'path': '/etc/passwd'})).status_code, 405)
                self.assertEqual((await client.post('/api/runs/' + self.result['run_id'], json={})).status_code, 405)
        asyncio.run(exercise())

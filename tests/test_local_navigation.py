import asyncio
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest

from aos.computer import SafetyPolicy, ToolRegistry
from aos.contracts import (AOSFault, Action, BROWSER_GOAL, ErrorCode, LOCAL_NAVIGATION_SCOPE,
                           Phase, REPO_ROOT, Settings, State, identifier)
from aos.decision import FixtureDecisionEngine
from aos.desktop_tasks import DesktopScheduler
from aos.local_navigation_operator import LocalNavigationOperator
from aos.storage import TrajectoryStore


class LocalNavigationMigrationTests(unittest.TestCase):
    def test_v8_upgrade_preserves_existing_job_and_approval_with_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'legacy.sqlite'
            connection = sqlite3.connect(database)
            self.addCleanup(connection.close)
            migrations = sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))
            for migration in migrations[:8]:
                connection.executescript(migration.read_text())
            connection.execute("INSERT INTO desktop_sessions VALUES('session','runtime','image','AGENT','lease',0,'running','synthetic','synthetic')")
            connection.execute("INSERT INTO desktop_tasks VALUES('job','session',NULL,'hello','lease',0,'succeeded',0,'synthetic','synthetic',NULL)")
            connection.execute("INSERT INTO desktop_approvals VALUES('approval','job','{}',?,123,'consumed','synthetic','synthetic')", ('0' * 64,))
            connection.commit()
            connection.executescript(migrations[8].read_text())
            self.assertEqual(connection.execute('SELECT job_id,kind,status FROM desktop_tasks').fetchall(),
                             [('job', 'hello', 'succeeded')])
            self.assertEqual(connection.execute('SELECT approval_id,status FROM desktop_approvals').fetchall(),
                             [('approval', 'consumed')])
            connection.execute("INSERT INTO desktop_tasks VALUES('navigation','session',NULL,'browser_local_navigation','lease',0,'queued',0,'synthetic','synthetic',NULL)")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO desktop_tasks VALUES('invalid','session',NULL,'arbitrary_web','lease',0,'cancelled',0,'synthetic','synthetic',NULL)")
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 9)


class LocalNavigationPolicyTests(unittest.TestCase):
    def state(self, *, kind='browser_local_navigation', scope=LOCAL_NAVIGATION_SCOPE, lease='current'):
        return State(task_id='task', run_id='run', step_id='step', runtime_id='runtime',
                     deployment_id='fixture-decision-v1', owner_lease_id=lease, task_kind=kind,
                     authorized_path=scope, authorized_content='DETAILS' if kind == 'browser_local_navigation' else 'Hello from the local agent.',
                     normalized_goal=BROWSER_GOAL, phase=Phase.EXECUTE)

    def action(self, *, tool='browser.fixture.open', option='open_start', arguments=None, lease='current'):
        return Action(task_id='task', run_id='run', step_id='step', action_id=identifier('action'),
                      runtime_id='runtime', state_version=0, owner_lease_id=lease, tool=tool,
                      arguments={} if arguments is None else arguments, expected_effect='fixed local page',
                      deadline=time.time() + 10, idempotency_key=identifier('intent'), selected_option=option)

    def test_only_exact_fixed_navigation_tools_and_choices_are_authorized(self):
        state = self.state()
        SafetyPolicy.check(self.action(), state, 'runtime')
        SafetyPolicy.check(self.action(tool='browser.fixture.follow', option='follow_details',
                                       arguments={'snapshot_id': 'a' * 32, 'element_id': 'b' * 32}), state, 'runtime')
        for action in (self.action(arguments={'url': 'https://example.invalid'}),
                       self.action(tool='browser.fill', option='open_start', arguments={}),
                       self.action(tool='browser.fixture.follow', option='open_start',
                                   arguments={'snapshot_id': 'a' * 32, 'element_id': 'b' * 32}),
                       self.action(lease='stale')):
            with self.subTest(action=action.tool, option=action.selected_option), self.assertRaises(AOSFault):
                SafetyPolicy.check(action, state, 'runtime')
        with self.assertRaises(AOSFault):
            SafetyPolicy.check(self.action(), self.state(scope='aos://other'), 'runtime')
        with self.assertRaises(AOSFault):
            SafetyPolicy.check(self.action(), self.state(kind='browser_form', scope='aos://synthetic/form'), 'runtime')

    def test_tool_registry_rejects_urls_and_unbounded_references(self):
        for arguments in ({'url': 'http://127.0.0.1/start'},
                          {'snapshot_id': 'a' * 32, 'element_id': 'b' * 32, 'url': 'file:///etc/passwd'},
                          {'snapshot_id': 'not-hex', 'element_id': 'b' * 32}):
            with self.subTest(arguments=arguments), self.assertRaises(AOSFault):
                ToolRegistry.validate(self.action(tool='browser.fixture.follow', option='follow_details', arguments=arguments))

    def test_scheduler_exposes_navigation_only_with_mcp_and_rejects_approve_all(self):
        settings = Settings(workspace=Path('synthetic-workspace'), database=Path('synthetic-store.sqlite'))
        controller = SimpleNamespace(store=SimpleNamespace())
        standard = DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                    browser_manifest=Path('synthetic'), desktop_browser=True)
        self.assertNotIn('browser_local_navigation', standard.kinds())
        mcp = DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                               browser_manifest=Path('synthetic'), desktop_browser=True,
                               desktop_mcp_manifest=Path('synthetic'))
        self.assertIn('browser_local_navigation', mcp.kinds())
        with self.assertRaises(AOSFault) as caught:
            mcp._start('lease', 0, 'browser_local_navigation', approve_all=True)
        self.assertEqual(caught.exception.code, ErrorCode.UNSAFE_ACTION)

    def test_operator_requires_external_live_lease_and_approval_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TrajectoryStore(root / 'store.sqlite')
            self.addCleanup(store.close)
            runtime = SimpleNamespace(runtime_id='runtime', status=lambda: {'running': True})
            operator = LocalNavigationOperator(Settings(workspace=root, database=root / 'store.sqlite'),
                                               store, runtime, FixtureDecisionEngine())
            with self.assertRaises(AOSFault):
                asyncio.run(operator.navigate(owner_lease_id='lease'))
            self.assertEqual(store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

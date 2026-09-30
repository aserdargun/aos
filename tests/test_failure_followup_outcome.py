import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aos.browser import BrowserRuntime
from aos.browser_operator import BrowserOperator
from aos.computer import WorkspaceRuntime
from aos.contracts import HELLO_CONTENT, REPO_ROOT, Settings, canonical, digest
from aos.decision import FixtureDecisionEngine
from aos.failure_followup_outcome import audit_followup_outcome
from aos.operator import Operator
from aos.storage import TrajectoryStore


class SyntheticFormRuntime(BrowserRuntime):
    def __init__(self):
        self.runtime_id = 'synthetic-outcome-browser'
        self.value = ''
        self.receipt = ''
        self.submissions = 0
        self.snapshot = 0

    def status(self):
        return {'runtime_id': self.runtime_id, 'running': True, 'real_execution': False}

    def read(self, path):
        self.snapshot += 1
        return canonical({**self.perform('browser.verify', {}),
                          'snapshot_id': f'{self.snapshot:032x}',
                          'elements': [{'element_id': 'a' * 32, 'role': 'textbox', 'label': 'Message'},
                                       {'element_id': 'b' * 32, 'role': 'button', 'label': 'Save locally'}]})

    def perform(self, tool, arguments):
        if tool == 'browser.fill':
            self.value = arguments['value']
        elif tool == 'browser.submit':
            self.receipt = self.value
            self.submissions += 1
        return {'value': self.value, 'receipt': self.receipt, 'submissions': self.submissions}


class FollowupOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(workspace=root / 'workspace', database=root / 'trace.sqlite')
        self.runtime = WorkspaceRuntime(self.settings.workspace)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.store = TrajectoryStore(self.settings.database)
        self.addCleanup(self.store.close)
        self.result = asyncio.run(Operator(self.settings, self.store, self.runtime, FixtureDecisionEngine()).hello())
        self.run_id = self.result['run_id']

    def copy(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        self.store.connection.backup(connection)
        for name, in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        return connection

    def test_real_workspace_fixture_engine_has_independent_fixed_outcome(self):
        before = self.store.connection.total_changes
        report = audit_followup_outcome(self.store.connection, self.run_id)
        self.assertEqual(report, {'status': 'verified', 'verification_count': 1,
                                 'scope': 'fixed_task_outcome', 'reason': 'independent_evidence_verified'})
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertFalse(self.result['real_model'])
        self.assertNotIn(HELLO_CONTENT.strip(), canonical(report))
        self.assertNotIn(self.run_id, canonical(report))

    def test_existing_exact_file_is_verified_by_a_distinct_read(self):
        result = asyncio.run(Operator(self.settings, self.store, self.runtime, FixtureDecisionEngine()).hello())
        self.assertEqual(audit_followup_outcome(self.store.connection, result['run_id'])['status'], 'verified')

    def test_equal_forged_expected_and_actual_are_not_a_contract(self):
        connection = self.copy()
        connection.execute('UPDATE verifications SET expected_json=?,actual_json=?', ('"private-forged"', '"private-forged"'))
        connection.execute("UPDATE observations SET payload_json=? WHERE kind='filesystem.read'", (canonical({'content': 'private-forged'}),))
        connection.execute("UPDATE actions SET result_json=? WHERE tool='filesystem.read'", (canonical({'content': 'private-forged'}),))
        report = audit_followup_outcome(connection, self.run_id)
        self.assertEqual(report['status'], 'not_verified')
        self.assertNotIn('private-forged', canonical(report))

    def test_action_decision_state_and_readback_tampering_fail_closed(self):
        mutations = (
            "UPDATE actions SET status='running' WHERE tool='filesystem.read'",
            "UPDATE actions SET arguments_json='{\"path\":\"/private\"}' WHERE tool='filesystem.read'",
            "UPDATE actions SET completed_at='1900-01-01T00:00:00+00:00' WHERE tool='filesystem.read'",
            "UPDATE action_envelopes SET payload_sha256='" + '0' * 64 + "'",
            "UPDATE decisions SET policy_result='deny'",
            "UPDATE decisions SET snapshot_id=(SELECT snapshot_id FROM state_snapshots WHERE state_version=0)",
            "UPDATE observations SET action_id=(SELECT action_id FROM actions WHERE tool='filesystem.write')",
            "UPDATE observations SET payload_json='{\"content\":\"private-changed\"}'",
            "UPDATE state_snapshots SET content_sha256='" + '0' * 64 + "' WHERE state_version=2",
            "DELETE FROM state_snapshots WHERE state_version=3",
            "UPDATE verifications SET evidence_refs_json='[]'",
            "UPDATE verifications SET verifier='unregistered'",
            "UPDATE runtime_states SET state_version=999",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                connection = self.copy()
                connection.execute(mutation)
                self.assertEqual(audit_followup_outcome(connection, self.run_id)['status'], 'not_verified')
        self.assertEqual(audit_followup_outcome(self.store.connection, self.run_id)['status'], 'verified')

    def test_rehashed_intermediate_identity_tampering_is_rejected(self):
        connection = self.copy()
        snapshot_id, encoded = connection.execute('SELECT snapshot_id,state_json FROM state_snapshots WHERE state_version=2').fetchone()
        changed = json.loads(encoded)
        changed['runtime_id'] = 'different-runtime'
        connection.execute('UPDATE state_snapshots SET state_json=?,content_sha256=? WHERE snapshot_id=?',
                           (canonical(changed), digest(changed), snapshot_id))
        self.assertEqual(audit_followup_outcome(connection, self.run_id)['status'], 'not_verified')

    def test_unsettled_unknown_and_malformed_sources_are_content_free(self):
        connection = self.copy()
        connection.execute("UPDATE runs SET status='failed'")
        self.assertEqual(audit_followup_outcome(connection, self.run_id)['reason'], 'run_not_settled_success')
        connection.execute("UPDATE runs SET status='succeeded',policy_version='future-private-policy'")
        self.assertEqual(audit_followup_outcome(connection, self.run_id), {
            'status': 'unsupported', 'verification_count': 0, 'scope': 'none', 'reason': 'task_contract_unsupported'})
        self.assertEqual(audit_followup_outcome(connection, 'missing-private-run')['reason'], 'run_not_settled_success')
        connection.close()
        self.assertEqual(audit_followup_outcome(connection, self.run_id)['reason'], 'evidence_missing_or_changed')

    def test_tuple_row_factory_and_query_only_connection_are_supported(self):
        with closing(sqlite3.connect(f'file:{self.settings.database}?mode=ro', uri=True)) as connection:
            connection.execute('PRAGMA query_only=ON')
            self.assertEqual(audit_followup_outcome(connection, self.run_id)['status'], 'verified')
            self.assertIsNone(connection.row_factory)

    def test_matching_boolean_submission_tamper_is_not_integer_outcome(self):
        result = asyncio.run(BrowserOperator(self.settings, self.store, SyntheticFormRuntime(), FixtureDecisionEngine()).form())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(audit_followup_outcome(self.store.connection, result['run_id'])['status'], 'verified')
        connection = self.copy()
        for table, primary, columns in (
                ('actions', 'action_id', ('result_json',)),
                ('observations', 'observation_id', ('payload_json',)),
                ('verifications', 'verification_id', ('expected_json', 'actual_json'))):
            for record in connection.execute(f'SELECT {primary},{",".join(columns)} FROM {table} WHERE run_id=?', (result['run_id'],)).fetchall():
                for column, encoded in zip(columns, record[1:], strict=True):
                    if encoded is None:
                        continue
                    payload = json.loads(encoded)
                    if isinstance(payload, dict) and payload.get('submissions') == 1:
                        payload['submissions'] = True
                        connection.execute(f'UPDATE {table} SET {column}=? WHERE {primary}=?', (canonical(payload), record[0]))
        self.assertEqual(audit_followup_outcome(connection, result['run_id'])['status'], 'not_verified')

    def test_missing_symbolic_source_target_is_not_an_outcome_proof(self):
        result = asyncio.run(BrowserOperator(self.settings, self.store, SyntheticFormRuntime(), FixtureDecisionEngine()).form())
        connection = self.copy()
        connection.execute("UPDATE observations SET payload_json='{}' WHERE run_id=? AND action_id IS NULL", (result['run_id'],))
        self.assertEqual(audit_followup_outcome(connection, result['run_id'])['status'], 'not_verified')

    def test_excessive_run_evidence_is_bounded(self):
        connection = self.copy()
        run_id, step_id = connection.execute('SELECT run_id,step_id FROM observations LIMIT 1').fetchone()
        connection.executemany('INSERT INTO observations(observation_id,run_id,step_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?)',
                               [(f'excess-{index}', run_id, step_id, 'synthetic', '{}', '2026-01-01T00:00:00+00:00') for index in range(501)])
        self.assertEqual(audit_followup_outcome(connection, run_id)['status'], 'not_verified')


@unittest.skipUnless(os.environ.get('AOS_BROWSER_TESTS') == '1', 'Opt in to isolated Chromium fixture operators')
class FollowupBrowserOutcomeTests(unittest.TestCase):
    def test_browser_and_vision_use_independent_application_readbacks(self):
        from aos.browser import BrowserRuntime
        from aos.browser_operator import BrowserOperator
        from aos.vision import FixtureVisionSupervisor, VisionRuntime
        from aos.vision_operator import VisionOperator

        for vision in (False, True):
            with self.subTest(vision=vision), tempfile.TemporaryDirectory() as temporary:
                settings = Settings(workspace=Path(temporary) / 'unused', database=Path(temporary) / 'trace.sqlite')
                runtime = (VisionRuntime if vision else BrowserRuntime)(REPO_ROOT / 'models/browser-manifest.json')
                store = TrajectoryStore(settings.database)
                try:
                    runtime.start()
                    operator = (VisionOperator(settings, store, runtime, FixtureDecisionEngine(), FixtureVisionSupervisor())
                                if vision else BrowserOperator(settings, store, runtime, FixtureDecisionEngine()))
                    result = asyncio.run(operator.canvas() if vision else operator.form())
                    self.assertEqual(result['status'], 'succeeded')
                    self.assertEqual(audit_followup_outcome(store.connection, result['run_id'])['status'], 'verified')
                finally:
                    runtime.stop()
                    store.close()


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and os.environ.get('AOS_HTTPS_RELAY_TESTS') == '1',
                     'Opt in to existing owned TLS/Chromium fixture scheduler harness')
class FollowupRemoteOutcomeTests(unittest.TestCase):
    def run_owned_case(self, method, database_name):
        from test_web_https_relay import WebHTTPSRelayTests

        fixture = WebHTTPSRelayTests(method)
        try:
            fixture.setUp()
            getattr(fixture, method)()
            with closing(sqlite3.connect(f'file:{fixture.root / database_name}?mode=ro', uri=True)) as connection:
                runs = connection.execute("SELECT run_id FROM runs WHERE status='succeeded'").fetchall()
                self.assertTrue(runs)
                for run_id, in runs:
                    report = audit_followup_outcome(connection, run_id)
                    self.assertEqual(report['status'], 'verified', report)
                    self.assertEqual(report['scope'], 'transport_readback')
                    with closing(sqlite3.connect(':memory:')) as changed:
                        connection.backup(changed)
                        changed.execute("UPDATE observations SET payload_json='{}' WHERE action_id IS NOT NULL")
                        self.assertEqual(audit_followup_outcome(changed, run_id)['status'], 'not_verified')
        finally:
            fixture.doCleanups()

    def test_owned_entry_outcome(self):
        self.run_owned_case('test_scheduler_approved_remote_entry_and_verified_readback', 'trajectory.sqlite')

    def test_owned_routes_outcome(self):
        self.run_owned_case('test_scheduler_canonical_query_route_requires_separate_approval', 'query-routes-trajectory.sqlite')

    def test_owned_static_outcome(self):
        self.run_owned_case('test_scheduler_static_bundle_requires_approval_and_readback', 'static-trajectory.sqlite')

    def test_owned_data_outcome(self):
        self.run_owned_case('test_scheduler_readonly_json_bundle_requires_approval_and_binds_run', 'readonly-data-trajectory.sqlite')

    def test_owned_form_outcome(self):
        self.run_owned_case('test_scheduled_form_needs_four_consumed_approvals_and_readback', 'form-task.sqlite')

    def test_owned_form_state_outcome(self):
        self.run_owned_case('test_scheduled_form_state_six_consumed_approvals_and_append_only_binding', 'form-state-task.sqlite')


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Opt in to existing owned desktop fixture scheduler harness')
class FollowupLocalDesktopOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_and_staging_outcomes(self):
        from test_desktop_mcp import MCPSchedulerIntegrationTests
        from test_staging_workflow import StagingWorkflowIntegrationTests

        for fixture_type, method, count in (
                (MCPSchedulerIntegrationTests, 'test_local_navigation_has_two_explicit_approvals_and_independent_details', 2),
                (StagingWorkflowIntegrationTests, 'test_four_manual_approvals_and_independent_receipt', 4)):
            with self.subTest(method=method):
                fixture = fixture_type(method)
                await fixture.asyncSetUp()
                try:
                    await getattr(fixture, method)()
                    run_id = fixture.store.connection.execute("SELECT run_id FROM runs WHERE status='succeeded'").fetchone()[0]
                    self.assertEqual(audit_followup_outcome(fixture.store.connection, run_id), {
                        'status': 'verified', 'verification_count': count,
                        'scope': 'fixed_task_outcome', 'reason': 'independent_evidence_verified'})
                finally:
                    await fixture.asyncTearDown()


if __name__ == '__main__':
    unittest.main()

import asyncio
import json
from pathlib import Path
import shutil
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aos.contracts import AOSFault, REPO_ROOT, Settings, canonical, digest, identifier
from aos.decision import FixtureDecisionEngine
from aos.desktop_control import DesktopController
from aos.desktop_mcp_bundle import PACKAGES, prepare_bundle
from aos.desktop_tasks import DesktopScheduler
from aos.owned_learning_workspace import OwnedLearningWorkspace
from aos.owned_parameter_project import (
    APPLICATIONS, MODE, owned_parameter_project_review_sha256, provision_owned_parameter_project,
)
from aos.owned_parameter_project_execution import OwnedParameterProjectExecutionJournal
from aos.owned_parameter_project_startup import load_owned_parameter_project_startup
from aos.remote_form_operator import RemoteFormOperator
from aos.storage import TrajectoryStore
from aos.web_application_binding import WebRuntimePin

from test_web_goal_desktop_execution import TLSFixtureBrowserBackend


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS certificate preparation')
class OwnedParameterProjectDesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aos-parameter-desktop-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        modules = self.root / 'synthetic-node-modules'
        for package, version in PACKAGES.items():
            directory = modules / package
            directory.mkdir(mode=0o700, parents=True)
            metadata = {'name': package, 'version': version}
            if package == '@playwright/mcp':
                metadata['dependencies'] = {name: PACKAGES[name]
                                            for name in ('playwright', 'playwright-core')}
            path = directory / 'package.json'
            path.write_text(canonical(metadata))
            path.chmod(0o600)
        self.mcp_manifest = prepare_bundle(modules, self.root / 'synthetic-mcp-bundle')

    def test_two_apps_four_authored_maps_actual_operator_tls_audit_and_receipts(self):
        receipts = []
        cases = (
            (APPLICATIONS[0], 'Synthetic Ada', 'Synthetic call tomorrow'),
            (APPLICATIONS[0], 'Synthetic Grace', 'Synthetic send brief'),
            (APPLICATIONS[1], 'Synthetic SKU-42', 'Synthetic inspect stock'),
            (APPLICATIONS[1], 'Synthetic SKU-73', 'Synthetic check shelf'))
        for index, (application, record_id, note) in enumerate(cases):
            with self.subTest(application=application, record_id=record_id):
                receipts.append(self.execute_case(index, application, record_id, note))
        self.assertEqual(len(receipts), 4)
        self.assertEqual(len({receipt['recipe_audit']['profile_sha256'] for receipt in receipts}), 4)
        self.assertEqual(len({receipt['run_identity']['run_id'] for receipt in receipts}), 4)
        self.assertEqual({receipt['observation']['scope']['application_id'] for receipt in receipts},
                         set(APPLICATIONS))
        self.assertEqual({tuple(sorted(receipt['observation']['record'])) for receipt in receipts},
                         {('contact_name', 'note'), ('item_code', 'note')})

    def execute_case(self, index, application, record_id, note, *, store=None, expected_run_count=1):
        root = self.root / ('case-' + str(index))
        root.mkdir(mode=0o700)
        workspace = root / 'workspace'
        workspace.mkdir(mode=0o700)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        parameters = {'record-id': record_id, 'note-text': note}
        bundle = provision_owned_parameter_project(
            root / 'project', listener.getsockname()[1], application_key=application,
            parameters=parameters,
            confirm_parameters_sha256=owned_parameter_project_review_sha256(application, parameters),
            human_confirmation=True)
        startup = load_owned_parameter_project_startup(
            bundle['directory'], bundle['manifest_sha256'], listener.fileno())
        self.addCleanup(startup.close)
        database = (Path(store.connection.execute('PRAGMA database_list').fetchone()[2])
                    if store is not None else root / 'trajectory.sqlite')
        settings = Settings(workspace=workspace, database=database)
        if store is None:
            store = TrajectoryStore(settings.database)
            self.addCleanup(store.close)
        parent = SimpleNamespace(runtime_id=identifier('desktop'),
                                 pins={'image_id': 'synthetic-image-not-a-native-runtime'})
        controller = DesktopController(store, parent)
        control = controller.state()
        events = []
        audits = []

        def independent_audit(run_id):
            report = startup.audit(settings.database, run_id)
            audits.append(report)
            events.append(('independent_audit', run_id))
            return report

        with OwnedLearningWorkspace.acquire(bundle['directory'], create=True) as source_lock:
            startup.retain_workspace(source_lock)
            fixture = startup.create_fixture(listener.fileno())
            self.addCleanup(fixture.close)
            scheduler = DesktopScheduler(
                controller, settings, FixtureDecisionEngine(),
                browser_manifest=root / 'synthetic-browser-not-started.json', desktop_browser=True,
                remote_entry_mcp_manifest=self.mcp_manifest,
                remote_entry_profiles=bundle['profiles'],
                remote_entry_profile_sha256=bundle['profile_sha256'], remote_entry_task=bundle['task'],
                remote_form_plan=bundle['form_plan'],
                remote_form_fields=[{'name': name, 'value': value} for name, value in bundle['fields']],
                remote_form_tls_context=fixture.target.tls_context,
                remote_form_state_plan=bundle['state_plan'],
                remote_form_skill_invocation=bundle['invocation'],
                remote_form_skill_invocation_sha256=bundle['invocation_sha256'],
                remote_form_skill_revalidator=startup.revalidate,
                remote_form_owned_target=fixture.target, remote_form_owned_fixture=fixture,
                remote_form_owned_manifest=bundle['manifest'],
                remote_form_owned_auditor=independent_audit,
                owned_skill_reuse_workspace_lock=source_lock)
            journal_directory = root / 'project-execution'
            scheduler.configure_owned_parameter_project_execution(
                bundle, journal_directory, current_source=startup.source,
                source_auditor=independent_audit)
            service = scheduler.owned_parameter_project_execution
            self.addCleanup(service.close)
            runtime_pin = WebRuntimePin.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_binding.json').read_text())['runtime'] | {
                    'parent_runtime_id': parent.runtime_id})
            backend = TLSFixtureBrowserBackend(runtime_pin, bundle, events)
            approvals = []

            async def execute():
                started = scheduler.start(control['lease_id'], control['generation'], 'browser_remote_form')
                self.assertEqual(started['job_id'], service.job_id)
                self.assertTrue(service.journal.reserved)
                intent_path, = journal_directory.glob('*.intent.json')
                intent = json.loads(intent_path.read_text())
                self.assertEqual(intent['source']['manifest_sha256'], bundle['manifest_sha256'])
                self.assertEqual(intent['source']['manifest']['mode'], MODE)
                self.assertEqual(intent['authority']['desktop_session_id'], controller.session_id)
                self.assertEqual(intent['authority']['runtime_id'], parent.runtime_id)
                self.assertFalse(any(event[0] == 'tool' for event in events))
                deadline = asyncio.get_running_loop().time() + 5
                while not scheduler.task.done():
                    if asyncio.get_running_loop().time() >= deadline:
                        scheduler.task.cancel()
                        await asyncio.gather(scheduler.task, return_exceptions=True)
                        self.fail('Synthetic approved task exceeded its five-second bound')
                    pending = store.connection.execute(
                        "SELECT * FROM desktop_approvals WHERE status='pending'").fetchone()
                    if pending is not None:
                        self.assertIsInstance(scheduler.operator, RemoteFormOperator)
                        bound_path, = journal_directory.glob('*.bound.json')
                        bound = json.loads(bound_path.read_text())
                        self.assertEqual(bound['run_identity']['runtime_id'], runtime_pin.runtime_id)
                        self.assertEqual(bound['run_identity']['skill_invocation_sha256'],
                                         bundle['invocation_sha256'])
                        approvals.append((pending['approval_id'], pending['action_sha256']))
                        scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
                    await asyncio.sleep(.001)
                await asyncio.wait_for(scheduler.task, 5)

            with patch('aos.desktop_form_mcp.DesktopHTTPSFormMCPRuntime', return_value=backend):
                asyncio.run(execute())
            row = store.connection.execute(
                'SELECT status,run_id,runtime_id,real_model FROM desktop_tasks WHERE job_id=?',
                (service.job_id,)).fetchone()
            self.assertEqual(row['status'], 'succeeded', dict(row))
            self.assertEqual(row['runtime_id'], runtime_pin.runtime_id)
            self.assertNotEqual(row['runtime_id'], parent.runtime_id)
            self.assertEqual(row['real_model'], 0)
            self.assertEqual(len(approvals), 6)
            self.assertEqual(len({approval[0] for approval in approvals}), 6)
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]['run_ref'], digest({'run_id': row['run_id']}))
            self.assertEqual(audits[0]['consumed_approval_count'], 6)
            self.assertEqual(audits[0]['submit_count'], 1)
            self.assertEqual(audits[0]['invocation_sha256'], bundle['invocation_sha256'])
            self.assertTrue(audits[0]['executable_recipe_executed'])
            accepted_path, = journal_directory.glob('*.accepted.json')
            receipt = json.loads(accepted_path.read_text())
            self.assertEqual(accepted_path.stat().st_mode & 0o777, 0o600)
            bound_path, = journal_directory.glob('*.bound.json')
            bound = json.loads(bound_path.read_text())
            self.assertEqual(receipt['run_identity'], bound['run_identity'])
            self.assertEqual(receipt['run_identity']['run_id'], row['run_id'])
            self.assertEqual(receipt['recipe_audit'], audits[0])
            self.assertEqual(receipt['recipe_audit_sha256'], digest(audits[0]))
            self.assertEqual(receipt['observation']['record'], dict(bundle['fields']))
            self.assertEqual(receipt['observation']['scope'], bundle['record_config']['scope'])
            self.assertTrue(receipt['record_outcome_verified'])
            self.assertTrue(receipt['task_terminal_verified'])
            self.assertTrue(receipt['recipe_execution_verified'])
            self.assertFalse(any(receipt[key] for key in (
                'native_model_verified', 'released_skill_verified', 'activation_authorized',
                'site_outcome_verified', 'training_ready', 'gpu_release_verified', 'replay_authorized')))
            self.assertFalse(service.journal.reserved)
            reopened = OwnedParameterProjectExecutionJournal(journal_directory)
            self.assertFalse(reopened.reserved)
            self.assertEqual(service.status()['entries'][0]['status'], 'accepted_verified')
            self.assertEqual(fixture._state.committed_record, dict(bundle['fields']))
            self.assertTrue(fixture._readback_used)
            self.assertEqual(sum(event == ('tool', 'browser.form.submit') for event in events), 1)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
            self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0],
                             7 * expected_run_count)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0],
                             6 * expected_run_count)
            self.assertEqual(store.connection.execute(
                "SELECT count(*) FROM human_interventions WHERE kind='approve'").fetchone()[0],
                             6 * expected_run_count)
            self.assertFalse(backend.status()['real_execution'])
            self.assertFalse(scheduler.engine.identity['real_model'])
            for kind in ('hello', 'browser_remote_form'):
                with self.subTest(replay_kind=kind), self.assertRaises(AOSFault):
                    scheduler.start(control['lease_id'], control['generation'], kind)
            self.assertEqual(service.finish(service.job_id), receipt)
            self.assertEqual(len(audits), 1)
            self.assertFalse(bundle['manifest']['execution_performed'])
            self.assertFalse(bundle['manifest']['source_evidence_verified'])
            return receipt


if __name__ == '__main__':
    unittest.main()

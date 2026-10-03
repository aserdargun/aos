"""CPU tests using explicitly mocked trajectory auditor and record reader."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from aos.contracts import Phase, REPO_ROOT, State, canonical, digest, identifier
from aos.dataset import validator
from aos.owned_parameter_project import (
    APPLICATIONS, owned_parameter_project_review_sha256,
    provision_owned_parameter_project, read_owned_parameter_project,
)
from aos.owned_parameter_project_execution import (
    MODELS, OwnedParameterProjectExecution, OwnedParameterProjectExecutionSource,
    OwnedParameterProjectExecutionStatus,
)


@unittest.skipUnless(shutil.which('openssl'), 'Requires local synthetic TLS authoring')
class OwnedParameterProjectExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        parameters = {'record-id': 'Synthetic Ada', 'note-text': 'Synthetic follow-up'}
        provision = provision_owned_parameter_project(
            self.root / 'project', 19443, application_key=APPLICATIONS[0], parameters=parameters,
            confirm_parameters_sha256=owned_parameter_project_review_sha256(APPLICATIONS[0], parameters),
            human_confirmation=True)
        self.loader = lambda: read_owned_parameter_project(
            self.root / 'project', provision['manifest_sha256'])
        self.bundle = self.loader()
        self.control = {'runtime_id': 'synthetic-parent-runtime', 'lease_id': 'synthetic-lease',
                        'generation': 1, 'owner': 'AGENT', 'status': 'running'}
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript('''CREATE TABLE desktop_tasks(
            job_id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, lease_id TEXT,
            generation INTEGER, runtime_id TEXT, run_id TEXT, status TEXT);
            CREATE TABLE runs(run_id TEXT PRIMARY KEY, task_id TEXT, status TEXT);''')
        self.state = State(task_kind='browser_remote_form', task_id=identifier('task'),
                           run_id=identifier('run'), step_id=identifier('step'),
                           runtime_id='synthetic-child-runtime', deployment_id='synthetic-fixture-deployment',
                           owner_lease_id=self.control['lease_id'],
                           skill_invocation_sha256=self.bundle['invocation_sha256'])
        self.states = {self.state.run_id: self.state}
        observation = {'schema_version': '1.0', 'scope': self.bundle['record_config']['scope'],
                       'record': dict(self.bundle['fields']), 'reported_post_count': 1,
                       'effect_status': 'committed'}
        self.reader = Mock(return_value=canonical(observation).encode())
        self.observation = observation
        self.auditor = Mock(side_effect=self.mock_audit)
        self.manager = SimpleNamespace(
            settings=SimpleNamespace(workspace=self.workspace), closed=False, restart_quiesced=False,
            controller=SimpleNamespace(session_id='synthetic-desktop-session', state=lambda: dict(self.control)),
            store=SimpleNamespace(connection=self.connection, state=lambda run_id: self.states[run_id]),
            remote_entry_profile_sha256=self.bundle['profile_sha256'], remote_entry_task=self.bundle['task'],
            remote_form_plan=self.bundle['form_plan'], remote_form_state_plan=self.bundle['state_plan'],
            remote_form_skill_invocation=self.bundle['invocation'],
            remote_form_skill_invocation_sha256=self.bundle['invocation_sha256'],
            remote_form_fields=self.bundle['fields'],
            remote_form_owned_fixture=SimpleNamespace(record_mode=True, read_whole_record=self.reader),
            remote_form_owned_target=SimpleNamespace(assert_plan=Mock(), assert_state_plan=Mock()),
            remote_form_owned_candidate_session=None, remote_form_public_plan_sha256=None,
            remote_form_cookie=None, _owned_skill_reuse=None)
        self.job_id = identifier('job')
        self.service = self.configure()

    def configure(self, directory=None):
        service = OwnedParameterProjectExecution(
            self.manager, self.bundle, directory or self.root / 'journal',
            current_source=self.loader, source_auditor=self.auditor)
        self.addCleanup(service.close)
        return service

    def mock_audit(self, run_id):
        report = deepcopy(json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_audit.json').read_text())['report'])
        invocation = self.bundle['invocation']
        for field in ('profile_sha256', 'task_sha256', 'skill_sha256', 'skill_plan_sha256',
                      'case_inputs_sha256', 'form_plan_sha256', 'state_plan_sha256',
                      'field_binding_sha256', 'recipe_sha256', 'case_key'):
            report[field] = invocation[field]
        report['symbolic_step_keys'] = [step['step_key'] for step in invocation['steps']]
        report['invocation_sha256'] = digest(invocation)
        report['run_ref'] = digest({'run_id': run_id})
        return report

    def reserve(self):
        return self.service.reserve(self.job_id, self.control['lease_id'], self.control['generation'])

    def bind(self):
        checksum = self.reserve()
        self.connection.execute('INSERT INTO desktop_tasks VALUES(?,?,?,?,?,?,?,?)',
            (self.job_id, self.manager.controller.session_id, 'browser_remote_form',
             self.control['lease_id'], self.control['generation'], self.state.runtime_id,
             self.state.run_id, 'running'))
        self.connection.execute('INSERT INTO runs VALUES(?,?,?)',
                                (self.state.run_id, self.state.task_id, 'running'))
        self.service.bind_run(self.job_id, self.state)
        return checksum

    def terminal(self):
        checksum = self.bind()
        self.states[self.state.run_id] = self.state.model_copy(update={'phase': Phase.SUCCEEDED})
        self.connection.execute("UPDATE desktop_tasks SET status='succeeded'")
        self.connection.execute("UPDATE runs SET status='succeeded'")
        return checksum

    def test_intent_precedes_row_and_new_instance_blocks_admission(self):
        self.reserve()
        self.assertEqual(self.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)
        self.assertTrue(self.service.reserved)
        fresh = self.configure()
        self.assertTrue(fresh.reserved)
        with self.assertRaises(ValueError):
            fresh.reserve(identifier('job'), self.control['lease_id'], 1)
        with self.assertRaises(ValueError):
            fresh.check_job(self.job_id)
        self.reader.assert_not_called()

    def test_status_is_read_only_and_never_creates_journal(self):
        self.assertEqual(self.service.status()['entries'], [])
        self.assertFalse((self.root / 'journal').exists())
        self.reserve()
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.root / 'journal').iterdir()}
        self.assertTrue(self.service.status()['reserved'])
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                                 for path in (self.root / 'journal').iterdir()})
        self.auditor.assert_not_called()
        self.reader.assert_not_called()

    def test_mocked_audit_receipt_has_no_release_or_native_claim_and_duplicate_does_not_read(self):
        self.terminal()
        receipt = self.service.finish(self.job_id)
        self.assertFalse(self.service.reserved)
        self.assertEqual(self.service.status()['entries'][0]['status'], 'accepted_verified')
        for flag in ('released_skill_verified', 'native_model_verified', 'gpu_release_verified',
                     'site_outcome_verified', 'training_ready', 'replay_authorized'):
            self.assertIs(receipt[flag], False)
        self.manager.remote_form_owned_fixture = None
        self.control['generation'] += 1
        self.assertEqual(self.service.finish(self.job_id), receipt)
        self.reader.assert_called_once()
        self.auditor.assert_called_once()
        fresh = self.configure_without_fixture_check()
        with self.assertRaises(ValueError):
            fresh.reserve(identifier('job'), self.control['lease_id'], self.control['generation'])

    def configure_without_fixture_check(self):
        self.manager.remote_form_owned_fixture = self.service.fixture
        return self.configure()

    def test_wrong_job_and_stale_control_fail_before_read(self):
        self.terminal()
        with self.assertRaises(ValueError):
            self.service.finish(identifier('job'))
        self.control['generation'] += 1
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.reader.assert_not_called()
        self.assertTrue(self.service.reserved)

    def test_source_and_configuration_mutation_fail_before_read(self):
        self.terminal()
        self.manager.remote_form_fields = [('contact_name', 'Changed'), ('note', 'Other')]
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.reader.assert_not_called()

    def test_failed_or_partial_run_never_accepts(self):
        self.bind()
        self.connection.execute("UPDATE desktop_tasks SET status='failed'")
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.connection.execute("UPDATE desktop_tasks SET status='succeeded'")
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.reader.assert_not_called()

    def test_replaced_original_run_and_store_fail_before_read(self):
        self.terminal()
        self.connection.execute('UPDATE desktop_tasks SET run_id=?', (identifier('run'),))
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.manager.store = SimpleNamespace(connection=self.connection)
        with self.assertRaises(ValueError):
            self.service.check_job(self.job_id)
        self.reader.assert_not_called()

    def test_all_recipe_audit_pins_checked_before_one_use_read(self):
        self.terminal()
        audit = self.mock_audit(self.state.run_id)
        for field in ('profile_sha256', 'task_sha256', 'skill_sha256', 'skill_plan_sha256',
                      'case_inputs_sha256', 'form_plan_sha256', 'state_plan_sha256',
                      'field_binding_sha256', 'recipe_sha256', 'invocation_sha256', 'run_ref'):
            self.auditor.side_effect = None
            self.auditor.return_value = audit | {field: '0' * 64}
            with self.subTest(pin=field), self.assertRaises(ValueError):
                self.service.finish(self.job_id)
        self.reader.assert_not_called()

    def test_wrong_full_record_does_not_accept_and_readback_is_never_retried(self):
        self.terminal()
        wrong = deepcopy(self.observation)
        wrong['record']['contact_name'] = 'Different record with same note'
        self.reader.return_value = canonical(wrong).encode()
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.reader.return_value = canonical(self.observation).encode()
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.reader.assert_called_once()
        self.assertTrue(self.service.reserved)

    def test_auditor_changing_current_authority_fails_before_reader(self):
        self.terminal()
        def changing_audit(run_id):
            self.control['generation'] += 1
            return self.mock_audit(run_id)
        self.auditor.side_effect = changing_audit
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.reader.assert_not_called()

    def test_reader_changes_terminal_status_and_cannot_be_retried(self):
        self.terminal()
        def changing_reader(**arguments):
            self.connection.execute("UPDATE desktop_tasks SET status='failed'")
            return canonical(self.observation).encode()
        self.reader.side_effect = changing_reader
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.connection.execute("UPDATE desktop_tasks SET status='succeeded'")
        with self.assertRaises(ValueError):
            self.service.finish(self.job_id)
        self.reader.assert_called_once()
        self.assertTrue(self.service.reserved)

    def test_persisted_intent_removed_blocks_next_effect_check(self):
        self.reserve()
        next((self.root / 'journal').glob('*.intent.json')).unlink()
        with self.assertRaises(ValueError):
            self.service.check_job(self.job_id)
        self.assertTrue(self.service.reserved)
        with self.assertRaises(ValueError):
            self.service.status()

    def test_changed_original_state_cannot_bind_a_different_run(self):
        self.bind()
        wrong = self.state.model_copy(update={'deployment_id': 'different-synthetic-deployment'})
        with self.assertRaises(ValueError):
            self.service.bind_run(self.job_id, wrong)
        self.reader.assert_not_called()

    def test_source_file_changed_fails_and_symlink_journal_blocks(self):
        self.reserve()
        source = self.bundle['directory'] / 'owned-record-config.json'
        source.write_bytes(source.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            self.service.check_job(self.job_id)
        journal = self.root / 'journal'
        journal.rename(self.root / 'old-journal')
        journal.symlink_to(self.root / 'old-journal', target_is_directory=True)
        self.assertTrue(self.service.reserved)

    def test_workspace_journal_and_boolean_control_rejected(self):
        with self.assertRaises(ValueError):
            self.configure(self.workspace / 'journal')
        with self.assertRaises(ValueError):
            self.service.reserve(self.job_id, self.control['lease_id'], True)
        self.assertFalse((self.root / 'journal').exists())

    def test_canonical_schemas_match_models_and_receipt_rejects_fake_flags(self):
        self.terminal()
        receipt = self.service.finish(self.job_id)
        for stage, model in MODELS.items():
            name = 'owned_parameter_project_execution_' + ('receipt' if stage == 'accepted'
                    else 'run_binding' if stage == 'bound' else 'intent')
            stored_schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            self.assertEqual(stored_schema, model.model_json_schema())
            for path in (self.root / 'journal').glob('*.' + stage + '.json'):
                self.assertTrue(validator(name).is_valid(json.loads(path.read_bytes())))
        for value in (True, 0, 1):
            with self.subTest(flag=value), self.assertRaises(ValueError):
                MODELS['accepted'].model_validate_json(canonical(receipt | {'native_model_verified': value}))
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/owned_parameter_project_execution_source.schema.json').read_text()),
                         OwnedParameterProjectExecutionSource.model_json_schema())
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/owned_parameter_project_execution_status.schema.json').read_text()),
                         OwnedParameterProjectExecutionStatus.model_json_schema())


if __name__ == '__main__':
    unittest.main()

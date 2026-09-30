import json
import os
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import Phase, REPO_ROOT, State, canonical, digest, now
from aos.decision import FixtureDecisionEngine
from aos.failure_followup import (ADMISSION, MARKER, FailureFollowupPreview, FailureFollowupReport,
                                  FailureFollowupService, FailureFollowupStart, task_contract_connection)
from aos.failure_improvement import derive_failure_connection
from tests import test_failure_improvement as failure_fixtures


SECRET = failure_fixtures.SECRET


class FailureFollowupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = failure_fixtures.FailureImprovementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.failures = self.fixture.service
        self.reviewed = self.fixture.review()
        self.service = FailureFollowupService(self.failures)
        self.target = {'session_id': 'session-fixture', 'parent_runtime_id': 'runtime-fixture',
                       'image_id': 'synthetic-image', 'lease_id': 'lease-fixture', 'generation': 0,
                       'task_kind': 'hello', 'configuration_sha256': 'a' * 64,
                       'system1_deployment_id': FixtureDecisionEngine.identity['deployment_id'],
                       'system2_deployment_id': None}

    def preview(self):
        return self.service.preview('job-fixture', self.reviewed['candidate_sha256'],
                                    self.reviewed['review']['receipt_sha256'], self.target)

    def commit(self):
        preview = self.preview()
        return self.service.commit(preview, preview['confirm_sha256'], True, self.target)

    def queued(self, intent, job_id='job-followup'):
        with self.store.connection:
            self.store.insert('desktop_tasks', job_id=job_id, session_id='session-fixture',
                              kind='hello', lease_id='lease-fixture', generation=0, status='queued',
                              real_model=0, created_at=now(), updated_at=now())
            result = self.service.admit(self.store, intent['intent_sha256'], job_id, self.target)
        return result

    def created(self, intent, *, state_changes=None):
        self.queued(intent)
        state = self.fixture.state.model_copy(update={'task_id': 'task-followup', 'run_id': 'run-followup',
                                                      'step_id': 'step-followup', **(state_changes or {})})
        self.store.create_run(state, FixtureDecisionEngine.identity, {'runtime_id': state.runtime_id})
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET run_id=?,runtime_id=?,status='running' WHERE job_id=?",
                                          (state.run_id, state.runtime_id, 'job-followup'))
            self.service.created(self.store, intent['intent_sha256'], 'job-followup', state, self.target)
        return state

    def test_preview_readonly_unique_nonce_and_false_authority(self):
        before = self.store.connection.total_changes
        first, second = self.preview(), self.preview()
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        self.assertEqual(before, self.store.connection.total_changes)
        self.assertFalse(self.service.directory.exists())
        self.assertNotIn(SECRET, canonical(first))
        self.assertFalse(first['causality_verified'])
        self.assertFalse(first['guidance_applied'])
        self.assertTrue(first['manual_approval_required'])

    def test_consent_exact_confirmation_and_replayed_attempt(self):
        preview = self.preview()
        for consent in (False, 1, 'true'):
            with self.subTest(consent=consent), self.assertRaises(ValueError):
                self.service.commit(preview, preview['confirm_sha256'], consent, self.target)
        with self.assertRaises(ValueError):
            self.service.commit(preview, 'f' * 64, True, self.target)
        committed = self.service.commit(preview, preview['confirm_sha256'], True, self.target)
        self.assertEqual(digest(committed['intent']), committed['intent_sha256'])
        with self.assertRaises(FileExistsError):
            self.service.commit(preview, preview['confirm_sha256'], True, self.target)
        for path in self.service.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_exact_transactional_job_and_created_binding(self):
        intent = self.commit()
        state = self.created(intent)
        self.service.guard(self.store.connection, intent['intent_sha256'], 'job-followup', self.target)
        self.assertEqual(task_contract_connection(self.store.connection, 'session-fixture', 'job-fixture'),
                         task_contract_connection(self.store.connection, 'session-fixture', 'job-followup'))
        marker = self.store.connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=?',
                                               (state.run_id, MARKER)).fetchone()
        self.assertIsNone(marker['action_id'])
        self.assertNotIn(SECRET, marker['payload_json'])
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET status='cancelled' WHERE job_id='job-followup'")
        with self.assertRaises(ValueError):
            self.queued(intent, 'job-other')
        self.assertIsNone(self.store.connection.execute("SELECT 1 FROM desktop_tasks WHERE job_id='job-other'").fetchone())

    def test_source_guard_never_opens_snapshot_under_writer(self):
        intent = self.commit()
        self.created(intent)
        with patch('aos.failure_followup.audit_snapshot', side_effect=AssertionError('must reuse writer connection')):
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_tasks SET updated_at=updated_at WHERE job_id='job-followup'")
                self.service.guard(self.store.connection, intent['intent_sha256'], 'job-followup', self.target)
                candidate = derive_failure_connection(self.store.connection, 'session-fixture', 'job-fixture')
                self.assertEqual(digest(candidate), self.reviewed['candidate_sha256'])

    def test_actual_created_scope_change_fails_before_model_action(self):
        intent = self.commit()
        with self.assertRaises(ValueError):
            self.created(intent, state_changes={'authorized_content': 'changed synthetic content'})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertIsNone(self.store.connection.execute('SELECT 1 FROM observations WHERE kind=?', (MARKER,)).fetchone())

    def test_current_control_configuration_and_foreign_kind_fail_closed(self):
        preview = self.preview()
        for key, value in (('generation', 1), ('configuration_sha256', 'b' * 64),
                           ('task_kind', 'browser_form'), ('system1_deployment_id', 'foreign')):
            changed = {**self.target, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.service.commit(preview, preview['confirm_sha256'], True, changed)

    def test_revoked_source_blocks_admission_and_pending_effect(self):
        intent = self.commit()
        self.created(intent)
        self.failures.revoke('job-fixture', self.reviewed['candidate_sha256'],
                             self.reviewed['review']['receipt_sha256'])
        with self.assertRaises(ValueError):
            self.service.guard(self.store.connection, intent['intent_sha256'], 'job-followup', self.target)
        report = self.service.inspect('job-followup')
        self.assertTrue(report['historical_binding_verified'])
        self.assertTrue(report['current_source_valid'])
        self.assertFalse(report['current_review_valid'])

    def test_missing_marker_not_retroactively_successful(self):
        intent = self.commit()
        self.created(intent)
        with self.store.connection:
            self.store.connection.execute('DELETE FROM observations WHERE kind=?', (MARKER,))
        with self.assertRaises(ValueError):
            self.service.guard(self.store.connection, intent['intent_sha256'], 'job-followup', self.target)
        report = self.service.inspect('job-followup')
        self.assertFalse(report['historical_binding_verified'])
        self.assertEqual(report['outcome']['status'], 'not_verified')

    def test_partial_publication_burns_nonce_without_repair(self):
        preview = self.preview()
        from aos.owned_form_candidate_execution import _write_private_child

        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('synthetic publication failure')
            return _write_private_child(*args)

        with patch('aos.failure_followup._write_private_child', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.service.commit(preview, preview['confirm_sha256'], True, self.target)
        with self.assertRaises(FileExistsError):
            self.service.commit(preview, preview['confirm_sha256'], True, self.target)

    def test_no_after_the_fact_existing_run_admission(self):
        intent = self.commit()
        self.store.connection.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(ValueError):
                self.service.admit(self.store, intent['intent_sha256'], 'job-fixture', self.target)
        finally:
            self.store.connection.rollback()

    def test_persisted_numeric_flags_and_link_aliases_fail_closed(self):
        intent = self.commit()
        path = self.service.directory / ('intent-' + intent['intent_sha256'] + '.json')
        original = path.read_text()
        changed = json.loads(original)
        changed['preview']['gold'] = 0
        path.write_text(canonical(changed))
        with self.assertRaises(ValueError):
            self.queued(intent)
        path.write_text(original)
        os.link(path, self.fixture.root / 'alias')
        with self.assertRaises(ValueError):
            self.queued(intent)

    def test_source_drift_blocks_guard_but_preserves_historical_link(self):
        intent = self.commit()
        self.created(intent)
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET generation=1 WHERE job_id='job-fixture'")
        with self.assertRaises(ValueError):
            self.service.guard(self.store.connection, intent['intent_sha256'], 'job-followup', self.target)
        report = self.service.inspect('job-followup')
        self.assertTrue(report['historical_binding_verified'])
        self.assertFalse(report['current_source_valid'])
        self.assertTrue(report['current_review_valid'])

    def test_marker_chronology_and_foreign_deployment_are_not_trusted(self):
        intent = self.commit()
        self.created(intent)
        with self.store.connection:
            self.store.connection.execute("UPDATE observations SET created_at='2000-01-01T00:00:00Z' WHERE kind=?", (MARKER,))
        with self.assertRaises(ValueError):
            self.service.inspect('job-followup')

    def test_remote_entry_normalizes_only_fresh_runtime_binding(self):
        from aos.web_application_binding import WebTaskAdmissionDraft

        template = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())['draft']
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_sessions SET runtime_id=?,image_id=?',
                                          (template['runtime']['parent_runtime_id'], template['runtime']['image_id']))
        contracts = []
        for ordinal in range(3):
            draft = json.loads(canonical(template))
            draft['runtime']['runtime_id'] = 'browser-' + str(ordinal + 1) * 32
            if ordinal == 2:
                draft['task']['max_actions'] -= 1
            draft['runtime_sha256'] = digest(draft['runtime'])
            draft['task_sha256'] = digest(draft['task'])
            draft['binding_sha256'] = digest({key: draft[key] for key in (
                'profile_sha256', 'task_sha256', 'runtime_sha256')})
            WebTaskAdmissionDraft.model_validate(draft)
            job_id = f'job-remote-{ordinal}'
            state = State(task_id=f'task-remote-{ordinal}', run_id=f'run-remote-{ordinal}',
                          step_id=f'step-remote-{ordinal}', runtime_id=draft['runtime']['runtime_id'],
                          deployment_id=FixtureDecisionEngine.identity['deployment_id'],
                          owner_lease_id='lease-fixture', task_kind='browser_remote_entry',
                          authorized_path='synthetic-remote-entry', authorized_content=draft['binding_sha256'])
            self.store.create_run(state, FixtureDecisionEngine.identity, {'runtime_id': state.runtime_id})
            with self.store.connection:
                self.store.insert('desktop_tasks', job_id=job_id, session_id='session-fixture', run_id=state.run_id,
                                  kind=state.task_kind, lease_id='lease-fixture', generation=0, status='failed', real_model=0,
                                  created_at=now(), updated_at=now(), runtime_id=state.runtime_id)
                self.store.insert('desktop_remote_entry_bindings', job_id=job_id, run_id=state.run_id,
                                  profile_sha256=draft['profile_sha256'], binding_sha256=draft['binding_sha256'],
                                  runtime_sha256=draft['runtime_sha256'], draft_json=canonical(draft),
                                  browser_runtime_id=state.runtime_id, created_at=now())
            contracts.append(task_contract_connection(self.store.connection, 'session-fixture', job_id))
        self.assertEqual(contracts[0], contracts[1])
        self.assertNotEqual(contracts[0], contracts[2])

    def test_canonical_schemas_and_examples(self):
        for suffix, model in (('preview', FailureFollowupPreview), ('start', FailureFollowupStart),
                              ('report', FailureFollowupReport)):
            schema = json.loads((REPO_ROOT / f'schemas/failure_followup_{suffix}.schema.json').read_text())
            self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                      **model.model_json_schema()})
            example = json.loads((REPO_ROOT / f'examples/failure_followup_{suffix}.json').read_text())
            jsonschema.Draft202012Validator(schema).validate(example)

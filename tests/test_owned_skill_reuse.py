"""Synthetic contract/guard tests, not model or cross-session execution evidence."""

import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.lifecycle import LifecycleBirth, LifecycleEvent, ProcessIdentity, process_identity
from aos.local_app import LocalAppState
from aos.owned_skill_reuse import (
    _paths, _selected_release, _stopped_source, _successful_job, _terminal_sessions,
    _unchanged_directory, preview_owned_skill_reuse, stable_review_evidence_audit_sha256,
    stable_source_audit_sha256)
from aos.workspace_identity import open_existing_workspace, workspace_identity


class OwnedSkillReuseContractTests(unittest.TestCase):
    def test_synthetic_example_is_strict_and_non_authorizing(self):
        example = json.loads((REPO_ROOT / 'examples/owned_skill_reuse_preview.json').read_text())
        schema = validator('owned_skill_reuse_preview')
        schema.validate(example)
        for field in ('execution_authorized', 'old_actions_replayed',
                      'activation_authorized', 'training_ready'):
            for invalid in (True, 0, 1, 'false'):
                with self.subTest(field=field, invalid=invalid):
                    changed = dict(example, **{field: invalid})
                    self.assertFalse(schema.is_valid(changed))
        for field in example:
            with self.subTest(missing=field):
                changed = dict(example)
                del changed[field]
                self.assertFalse(schema.is_valid(changed))
        self.assertFalse(schema.is_valid(dict(example, old_approval='synthetic')))

    def test_source_audit_projection_ignores_only_snapshot(self):
        report = {'status': 'fixed_template_invocation_execution_verified',
                  'source_snapshot_sha256': 'a' * 64, 'run_ref': 'b' * 64,
                  'invocation_sha256': 'c' * 64, 'submit_count': 1}
        original = stable_source_audit_sha256(report)
        self.assertEqual(original, stable_source_audit_sha256(
            dict(report, source_snapshot_sha256='d' * 64)))
        for field in ('run_ref', 'invocation_sha256', 'submit_count'):
            self.assertNotEqual(original, stable_source_audit_sha256(
                dict(report, **{field: 'changed'})))
        with self.assertRaisesRegex(ValueError, 'source_audit_invalid'):
            stable_source_audit_sha256(dict(report, status='unverified'))

    def test_evidence_projection_removes_transitive_snapshot_hash_only(self):
        report = {'status': 'source_bound_development_execution',
                  'source_snapshot_sha256': 'a' * 64, 'recipe_audit_sha256': 'b' * 64,
                  'source_run_ref': 'c' * 64, 'execution_run_ref': 'd' * 64,
                  'admission': {'recipe_sha256': 'e' * 64},
                  'admission_observation_ref': 'f' * 64}
        original = stable_review_evidence_audit_sha256(report)
        changed = dict(report, source_snapshot_sha256='1' * 64, recipe_audit_sha256='2' * 64)
        self.assertEqual(original, stable_review_evidence_audit_sha256(changed))
        for field in ('source_run_ref', 'execution_run_ref', 'admission_observation_ref'):
            self.assertNotEqual(original, stable_review_evidence_audit_sha256(
                dict(report, **{field: 'changed'})))
        changed = copy.deepcopy(report)
        changed['admission']['recipe_sha256'] = '3' * 64
        self.assertNotEqual(original, stable_review_evidence_audit_sha256(changed))
        with self.assertRaisesRegex(ValueError, 'evidence_audit_invalid'):
            stable_review_evidence_audit_sha256(dict(report, status='unverified'))

    def test_paths_require_same_base_and_original_source_pointer(self):
        session = 'app-' + 'a' * 32
        original = 'app-' + 'b' * 32
        base = REPO_ROOT / 'data' / ('local-app-test-' + 'c' * 32)
        state = SimpleNamespace(session=session, owned_skill_source_session=original)
        self.assertEqual(_paths(base / session, state, base / original / 'owned-form',
                                base / original / 'store.sqlite'),
                         (base / session, base / original / 'owned-form',
                          base / original / 'store.sqlite'))
        for root, database in (
                (base / session / 'owned-form', base / original / 'store.sqlite'),
                (base / original / 'owned-form', base / session / 'store.sqlite'),
                (base / original / 'owned-form', base / original / '../store.sqlite')):
            with self.assertRaisesRegex(ValueError, 'source_paths_invalid'):
                _paths(base / session, state, root, database)
        with self.assertRaisesRegex(ValueError, 'source_paths_invalid'):
            _paths(Path('/tmp') / session, state, Path('/tmp') / original / 'owned-form',
                   Path('/tmp') / original / 'store.sqlite')

    def test_invalid_pins_fail_before_disk_or_audit_access(self):
        with patch('aos.owned_skill_reuse._paths') as paths:
            for value in ('', '../source', 'A' * 64, 'a' * 63):
                with self.assertRaisesRegex(ValueError, 'pin_invalid'):
                    preview_owned_skill_reuse(
                        previous_session_directory=Path('/unavailable'), previous_state=None,
                        owned_root=Path('/unavailable'), database=Path('/unavailable'),
                        manifest_sha256=value, release_sha256='b' * 64,
                        selection_sha256='c' * 64)
            paths.assert_not_called()

    def test_exact_selection_and_review_no_fallback(self):
        selection = {'release_sha256': 'a' * 64}
        checksum = digest(selection)
        release = {'family_sha256': 'b' * 64, 'review_sha256': 'c' * 64}
        with patch('aos.owned_skill_reuse.load_owned_skill_release', return_value=release), \
                patch('aos.owned_skill_reuse.current_owned_skill_selection', return_value=(
                    {'selection_sha256': checksum}, selection)), \
                patch('aos.owned_skill_reuse.inspect_owned_candidate_review', return_value={
                    'status': 'revoked', 'receipt': {}}) as review:
            with self.assertRaisesRegex(ValueError, 'selection_changed'):
                _selected_release(Path('/unused'), 'a' * 64, 'd' * 64)
            review.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'selection_changed'):
                _selected_release(Path('/unused'), 'e' * 64, checksum)
            with self.assertRaisesRegex(ValueError, 'review_not_accepted'):
                _selected_release(Path('/unused'), 'a' * 64, checksum)


class OwnedSkillReuseTerminalTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE desktop_sessions(session_id TEXT, runtime_id TEXT, status TEXT);
            CREATE TABLE desktop_tasks(job_id TEXT, run_id TEXT, kind TEXT, status TEXT,
                                       real_model INTEGER);
            CREATE TABLE runs(run_id TEXT, status TEXT);
            CREATE TABLE actions(status TEXT);
            CREATE TABLE desktop_approvals(status TEXT);
            CREATE TABLE desktop_inputs(status TEXT);
            INSERT INTO desktop_sessions VALUES('synthetic-session','synthetic-runtime','stopped');
        ''')

    def test_only_stopped_sessions_and_terminal_work_are_accepted(self):
        for status in ('succeeded', 'failed', 'cancelled'):
            self.connection.execute('INSERT INTO runs VALUES(?,?)', (status, status))
        self.assertEqual(len(_terminal_sessions(self.connection, {'synthetic-runtime'})), 1)
        for status in ('running', 'paused'):
            self.connection.execute('UPDATE desktop_sessions SET status=?', (status,))
            with self.assertRaisesRegex(ValueError, 'prior_sessions_not_stopped'):
                _terminal_sessions(self.connection, {'synthetic-runtime'})

    def test_missing_previous_runtime_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'prior_sessions_not_stopped'):
            _terminal_sessions(self.connection, {'foreign-runtime'})

    def test_unresolved_actions_approvals_inputs_and_runs_never_resume(self):
        cases = {'actions': ('intent', 'running', 'uncertain'),
                 'desktop_approvals': ('pending', 'approved'),
                 'desktop_inputs': ('queued', 'running', 'uncertain'),
                 'runs': ('created', 'running', 'paused', 'waiting_human'),
                 'desktop_tasks': ('queued', 'running', 'paused', 'waiting_approval', 'waiting_human')}
        for table, statuses in cases.items():
            for status in statuses:
                with self.subTest(table=table, status=status):
                    self.connection.execute(f'INSERT INTO {table}(status) VALUES(?)', (status,))
                    with self.assertRaisesRegex(ValueError, 'unresolved_' + table):
                        _terminal_sessions(self.connection, {'synthetic-runtime'})
                    self.connection.execute(f'DELETE FROM {table}')

    def test_source_job_is_exact_unique_successful_real_form(self):
        self.connection.execute("INSERT INTO runs VALUES('source','succeeded')")
        self.connection.execute("INSERT INTO desktop_tasks VALUES('job','source','browser_remote_form','succeeded',1)")
        self.assertEqual(_successful_job(self.connection, 'source')['job_id'], 'job')
        for column, value in (('kind', 'hello'), ('status', 'failed'), ('real_model', 0)):
            original = self.connection.execute(f'SELECT {column} FROM desktop_tasks').fetchone()[0]
            self.connection.execute(f'UPDATE desktop_tasks SET {column}=?', (value,))
            with self.assertRaisesRegex(ValueError, 'source_job_invalid'):
                _successful_job(self.connection, 'source')
            self.connection.execute(f'UPDATE desktop_tasks SET {column}=?', (original,))
        self.connection.execute('INSERT INTO desktop_tasks SELECT * FROM desktop_tasks')
        with self.assertRaisesRegex(ValueError, 'source_job_invalid'):
            _successful_job(self.connection, 'source')


class OwnedSkillReuseStoppedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([
            sys.executable, '-c',
            'import os; from aos.lifecycle import process_identity; '
            'print(process_identity(os.getpid()).model_dump_json())'],
            check=True, capture_output=True, text=True)
        cls.dead_process = ProcessIdentity.model_validate_json(result.stdout)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.workspace = self.directory / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.journals = self.directory / '.aos-lifecycle'
        self.journals.mkdir(mode=0o700)
        descriptor = open_existing_workspace(self.workspace)
        try:
            identity = workspace_identity(self.workspace, descriptor)
        finally:
            os.close(descriptor)
        self.birth = LifecycleBirth(
            runtime_id='desktop-' + 'a' * 32, container_name='aos-desktop-' + 'b' * 20,
            image_id='sha256:' + 'c' * 64, source_sha256='d' * 64,
            workspace=identity, process=self.dead_process)
        self.path = self.journals / (self.birth.runtime_id + '.jsonl')
        self.state = LocalAppState(
            session='app-' + 'e' * 32, mode='real', owned_synthetic_form_invocation=True,
            owned_form_manifest_sha256='f' * 64, owned_form_invocation_sha256='1' * 64,
            phase='stopped', supervisor=self.dead_process, backend=self.dead_process,
            token_name=None, started_at='2026-09-27T00:00:00Z')
        self.write_events(('intent', 'created', 'started', 'removed'))

    def write_events(self, stages, *, birth=None):
        events = []
        for sequence, stage in enumerate(stages):
            events.append(LifecycleEvent(
                birth=birth or self.birth, sequence=sequence,
                previous_sha256=digest(events[-1].model_dump()) if events else None,
                stage=stage, container_id=None if stage == 'intent' else '2' * 64,
                recorded_at='2026-09-27T00:00:00Z'))
        self.path.write_text(''.join(canonical(event.model_dump()) + '\n' for event in events))
        self.path.chmod(0o600)

    def test_real_exited_process_and_synthetic_removed_journal_are_stable(self):
        first = _stopped_source(self.directory, self.state)
        self.assertEqual(first, _stopped_source(self.directory, self.state))
        self.assertEqual(first[1], {self.birth.runtime_id})

    def test_running_supervisor_is_not_a_stopped_source(self):
        state = self.state.model_copy(update={'supervisor': process_identity(os.getpid())})
        with self.assertRaisesRegex(ValueError, 'source_process_not_stopped'):
            _stopped_source(self.directory, state)

    def test_indeterminate_process_observation_fails_closed(self):
        for status in ('incomparable', 'unavailable', 'same_process'):
            with patch('aos.owned_skill_reuse.observe_process', return_value=status):
                with self.assertRaisesRegex(ValueError, 'source_process_not_stopped'):
                    _stopped_source(self.directory, self.state)

    def test_unremoved_and_foreign_workspace_journals_are_rejected(self):
        self.write_events(('intent', 'created', 'started'))
        with self.assertRaisesRegex(ValueError, 'lifecycle_not_stopped'):
            _stopped_source(self.directory, self.state)
        foreign = self.birth.model_copy(update={'workspace': self.birth.workspace.model_copy(
            update={'path_sha256': '3' * 64})})
        self.write_events(('intent', 'created', 'started', 'removed'), birth=foreign)
        with self.assertRaisesRegex(ValueError, 'lifecycle_not_stopped'):
            _stopped_source(self.directory, self.state)

    def test_retained_token_and_changed_state_fail_closed(self):
        for changes in ({'phase': 'failed'}, {'token_name': 'desktop-console-' + 'a' * 16 + '.token'},
                        {'backend': None}, {'owned_synthetic_form_invocation': False}):
            with self.assertRaisesRegex(ValueError, 'stopped_source_required'):
                _stopped_source(self.directory, self.state.model_copy(update=changes))
        token = self.directory / ('desktop-console-' + 'a' * 16 + '.token')
        token.write_text('synthetic-not-a-real-token')
        with self.assertRaisesRegex(ValueError, 'source_token_remaining'):
            _stopped_source(self.directory, self.state)

    def test_partial_symlinked_or_unknown_lifecycle_inventory_fails_closed(self):
        original = self.path.read_bytes()
        self.path.write_bytes(original[:-1])
        with self.assertRaises(ValueError):
            _stopped_source(self.directory, self.state)
        self.path.write_bytes(original)
        other = self.directory / 'synthetic-journal'
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaises((OSError, ValueError)):
            _stopped_source(self.directory, self.state)
        self.path.unlink()
        other.rename(self.path)
        (self.journals / 'unknown').write_text('synthetic')
        with self.assertRaisesRegex(ValueError, 'lifecycle_inventory_invalid'):
            _stopped_source(self.directory, self.state)

    def test_replaced_descriptor_directory_is_detected(self):
        descriptor = open_existing_workspace(self.workspace)
        try:
            before = os.fstat(descriptor)
            self.workspace.rename(self.directory / 'prior-workspace')
            self.workspace.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, 'directory_changed'):
                _unchanged_directory(self.workspace, descriptor, before)
        finally:
            os.close(descriptor)

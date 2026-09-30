import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.learning_events import learning_events_from_database, review_learning_events
from aos.learning_candidate import review_learning_candidates
from aos.site_page_evidence import FINGERPRINT_VERSION, review_site_page_evidence
from aos.storage import TrajectoryStore


DECIDER = {"deployment_id": "decider-test", "kind": "decider_native_worker", "real_model": True,
           "pins": {"revision": "synthetic"}}
BONSAI = {"deployment_id": "bonsai-test", "kind": "bonsai_native_supervisor", "real_model": True,
          "pins": {"revision": "synthetic"}}


class LearningEventTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "trajectory.sqlite"
        self.store = TrajectoryStore(self.path)
        self.addCleanup(lambda: self.store.close())
        connection = self.store.connection
        with connection:
            self.store.insert("tasks", task_id="task-test", original_goal="private-token-do-not-export",
                              normalized_goal="synthetic", success_criteria_json="[]",
                              workspace_scope_json="[]", created_at="2026-01-01T00:00:00Z")
            self.store.insert("runs", run_id="run-test", task_id="task-test", status="running", outcome="unknown",
                              policy_version="hello-policy-v1", environment_json='{"secret":"do-not-export"}',
                              deployment_snapshot_json=json.dumps({**DECIDER, "supervisor": BONSAI}),
                              started_at="2026-01-01T00:00:00Z")
            self.store.insert("steps", step_id="step-test", run_id="run-test", ordinal=0, state="DECIDE",
                              started_at="2026-01-01T00:00:00Z")

    def call(self, role="system1", call_id="call-test", deployment_id=None, status="ok", response='{"secret":"do-not-export"}'):
        identity = DECIDER if role == "system1" else BONSAI
        with self.store.connection:
            self.store.insert("model_calls", call_id=call_id, run_id="run-test", step_id="step-test",
                              deployment_id=deployment_id or identity["deployment_id"], role=role,
                              request_json='{"password":"do-not-export"}', response_json=response,
                              status=status, created_at="2026-01-01T00:00:00Z")

    def decision(self, call_id="call-test"):
        with self.store.connection:
            self.store.insert("state_snapshots", snapshot_id="snapshot-test", run_id="run-test", step_id="step-test",
                              state_version=1, state_json="{}", content_sha256="0" * 64,
                              created_at="2026-01-01T00:00:00Z")
            self.store.insert("decisions", decision_id="decision-test", run_id="run-test", step_id="step-test",
                              snapshot_id="snapshot-test", call_id=call_id, question="private-token-do-not-export",
                              options_json='[{"id":"a"},{"id":"b"}]', probabilities_json='{"a":1,"b":0}',
                              selected_option="a", confidence=1, policy_result="allow",
                              created_at="2026-01-01T00:00:00Z")

    def verification(self, actual='"same"', verifier="aos-exact-bytes-v1"):
        with self.store.connection:
            self.store.insert("actions", action_id="action-test", run_id="run-test", step_id="step-test",
                              decision_id="decision-test", idempotency_key="action-key", tool="filesystem.write",
                              arguments_json='{"password":"do-not-export"}', status="ok", actual_option="a",
                              created_at="2026-01-01T00:00:00Z")
            self.store.insert("actions", action_id="readback-test", run_id="run-test", step_id="step-test",
                              decision_id="decision-test", idempotency_key="readback-key", tool="filesystem.read",
                              arguments_json="{}", status="ok", result_json='{"content":"same"}',
                              actual_option="a", created_at="2026-01-01T00:00:00Z")
            self.store.insert("observations", observation_id="observation-test", run_id="run-test",
                              step_id="step-test", action_id="readback-test", kind="filesystem.read",
                              payload_json='{"content":"same"}',
                              created_at="2026-01-01T00:00:00Z")
            self.store.insert("verifications", verification_id="verification-test", run_id="run-test",
                              step_id="step-test", action_id="action-test", criterion="matches",
                              method="independent_read_equals", result="passed", expected_json='"same"',
                              actual_json=actual, evidence_refs_json='["observation-test"]', verifier=verifier,
                              created_at="2026-01-01T00:00:00Z")

    def events(self):
        return learning_events_from_database(self.path, "run-test")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "aos.learning_events_cli", "--database", str(self.path),
                               "--run-id", "run-test", *arguments], capture_output=True, text=True, timeout=10)

    def test_no_real_call_means_no_bonsai_or_decider_event(self):
        self.assertEqual(self.events(), [])
        report = review_learning_events(self.path, "run-test")
        self.assertEqual(report["event_count"], 0)
        self.assertEqual(report["events"], [])

    def test_canonical_fixture_is_valid_but_rejects_payloads(self):
        fixture = json.loads((REPO_ROOT / "examples/learning_event_v2.json").read_text())
        validator("learning_event_v2").validate(fixture)
        self.assertFalse(validator("learning_event_v2").is_valid({**fixture, "raw_screenshot": "private"}))
        self.assertFalse(validator("learning_event_v2").is_valid({**fixture, "verified_outcome": True,
                                                                    "source": {**fixture["source"], "verification_ids": []}}))

    def test_verified_synthetic_page_fingerprint_is_content_free_and_fails_closed(self):
        fixture = json.loads((REPO_ROOT / 'examples/site_page_evidence.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('site_page_evidence').validate(fixture['page'])
        self.assertFalse(validator('site_page_evidence').is_valid(
            {**fixture['page'], 'collection_authorized': True}))
        self.call()
        self.decision()
        self.verification()
        outcome = {'page': 'start', 'heading': 'Synthetic start',
                   'links': [{'role': 'link', 'label': 'Details'}]}
        observed = {**outcome, 'snapshot_id': 'a' * 32,
                    'elements': [{'role': 'link', 'label': 'Details', 'element_id': 'b' * 32}]}
        observed.pop('links')
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='browser-local-navigation-policy-v1'")
            self.store.connection.execute("UPDATE actions SET tool='browser.fixture.open' WHERE action_id='action-test'")
            self.store.connection.execute("UPDATE actions SET tool='browser.fixture.snapshot',result_json=? WHERE action_id='readback-test'",
                                          (json.dumps(observed),))
            self.store.connection.execute("UPDATE observations SET kind='browser.local_navigation',payload_json=? WHERE observation_id='observation-test'",
                                          (json.dumps(observed),))
            self.store.connection.execute("UPDATE verifications SET method='independent_local_page_equals',verifier='aos-local-navigation-v1',expected_json=?,actual_json=? WHERE verification_id='verification-test'",
                                          (json.dumps(outcome), json.dumps(outcome)))
        report = review_site_page_evidence(self.path, 'run-test')
        self.assertEqual(report['page_count'], 1)
        page = report['pages'][0]
        validator('site_page_evidence').validate(page)
        self.assertEqual(page['page_key'], 'start')
        self.assertEqual(page['page_fingerprint_sha256'],
                         digest({'version': FINGERPRINT_VERSION, 'outcome': outcome}))
        self.assertEqual(page['page_fingerprint_sha256'], fixture['page']['page_fingerprint_sha256'])
        self.assertEqual(page['source']['verification_id'], 'verification-test')
        self.assertFalse(page['profile_bound'])
        self.assertFalse(page['training_ready'])
        self.assertNotIn('Synthetic start', json.dumps(report))
        self.assertNotIn('do-not-export', json.dumps(report))
        self.assertEqual(report, review_site_page_evidence(self.path, 'run-test'))
        cli = subprocess.run([sys.executable, '-m', 'aos.site_page_evidence', '--database', str(self.path),
                              '--run-id', 'run-test'], capture_output=True, text=True, timeout=10)
        self.assertEqual(cli.returncode, 0)
        self.assertEqual(json.loads(cli.stdout), report)
        self.assertEqual(cli.stderr, '')
        for database in (self.path.parent / 'missing.sqlite', self.path.parent / 'linked.sqlite'):
            if database.name == 'linked.sqlite':
                database.symlink_to(self.path)
            rejected = subprocess.run([sys.executable, '-m', 'aos.site_page_evidence',
                                       '--database', str(database), '--run-id', 'run-test'],
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(rejected.stdout, '')
            self.assertNotIn(str(database), rejected.stderr)
        for table, column, record_id, replacement in (
            ('runs', 'policy_version', 'run-test', 'hello-policy-v1'),
            ('model_calls', 'deployment_id', 'call-test', 'wrong-deployment'),
            ('observations', 'payload_json', 'observation-test', '{}'),
            ('verifications', 'actual_json', 'verification-test', '{}'),
        ):
            id_column = {'runs': 'run_id', 'model_calls': 'call_id',
                         'observations': 'observation_id', 'verifications': 'verification_id'}[table]
            original = self.store.connection.execute(
                f'SELECT {column} FROM {table} WHERE {id_column}=?', (record_id,)).fetchone()[0]
            with self.subTest(table=table, column=column):
                with self.store.connection:
                    self.store.connection.execute(
                        f'UPDATE {table} SET {column}=? WHERE {id_column}=?', (replacement, record_id))
                self.assertEqual(review_site_page_evidence(self.path, 'run-test')['pages'], [])
                with self.store.connection:
                    self.store.connection.execute(
                        f'UPDATE {table} SET {column}=? WHERE {id_column}=?', (original, record_id))

    def test_candidate_gap_review_keeps_s1_and_s2_separate_without_training_authority(self):
        self.call()
        self.decision()
        self.verification()
        self.call(role='system2', call_id='bonsai-call')
        report = review_learning_candidates(self.path, 'run-test')
        self.assertEqual((report['system1_count'], report['system2_count']), (1, 1))
        self.assertEqual([row['role'] for row in report['rows']], ['system1', 'system2'])
        self.assertEqual([row['has_independent_outcome'] for row in report['rows']], [True, False])
        self.assertTrue(all(row['status'] == 'blocked' and row['label_ready'] is False
                            and row['training_ready'] is False for row in report['rows']))
        self.assertEqual(report, review_learning_candidates(self.path, 'run-test'))
        self.assertNotIn('do-not-export', json.dumps(report))

    def test_s1_is_metadata_only_role_bound_and_restart_deterministic(self):
        self.call()
        self.decision()
        self.verification()
        first = self.events()
        self.assertEqual(len(first), 1)
        event = first[0]
        validator("learning_event_v2").validate(event)
        fixture = json.loads((REPO_ROOT / "examples/learning_event_v2.json").read_text())
        self.assertEqual(event, fixture)
        self.assertEqual(event["role"], "system1")
        self.assertTrue(event["verified_outcome"])
        self.assertEqual(event["source"]["verification_ids"], ["verification-test"])
        self.assertFalse(event["training_ready"])
        self.assertIn("synthetic_task_excluded", event["blockers"])
        self.assertNotIn("do-not-export", json.dumps(event))
        self.assertEqual(first, self.events())
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM model_calls").fetchone()[0], 1)
        self.store.close()
        self.store = TrajectoryStore(self.path, readonly=True)
        self.assertEqual(first, self.events())

    def test_s2_requires_actual_bonsai_call_and_never_inherits_s1_verification(self):
        self.call()
        self.decision()
        self.verification()
        self.call(role="system2", call_id="bonsai-call")
        with self.store.connection:
            self.store.insert("supervisor_escalations", escalation_id="escalation-test", run_id="run-test",
                              step_id="step-test", call_id="bonsai-call", reason="test",
                              evidence_refs_json="[]", outcome="pending", created_at="2026-01-01T00:00:00Z")
        events = self.events()
        self.assertEqual([event["role"] for event in events], ["system2", "system1"] if "bonsai-call" < "call-test" else ["system1", "system2"])
        system2 = next(event for event in events if event["role"] == "system2")
        self.assertEqual(system2["source"]["escalation_id"], "escalation-test")
        self.assertEqual(system2["source"]["verification_ids"], [])
        self.assertFalse(system2["verified_outcome"])
        self.assertFalse(system2["training_ready"])
        self.assertIsNone(system2["source"]["scene_observation_id"])
        self.assertEqual(system2["source"]["downstream_verification_ids"], [])

    def test_bonsai_scene_is_bound_to_capture_and_downstream_decider_verification_without_gold(self):
        self.call()
        self.decision()
        self.verification()
        scene = json.loads((REPO_ROOT / 'examples/vision_scene.json').read_text())['scene']
        capture = {'capture_id': scene['capture_id'], 'width': 640, 'height': 360,
                   'sha256': 'a' * 64, 'state_version': scene['state_version'],
                   'artifact_id': 'artifact-' + 'b' * 32}
        request = {'purpose': 'synthetic_canvas_vision',
                   'capture': {key: value for key, value in capture.items()
                               if key not in {'state_version', 'artifact_id'}},
                   'state_version': capture['state_version'], 'artifact_id': capture['artifact_id']}
        outcome = {'selected': 'SAVE', 'clicks': 1}
        self.call(role='system2', call_id='bonsai-call', response=json.dumps(scene))
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET request_json=? WHERE call_id='bonsai-call'",
                                          (json.dumps(request),))
            self.store.insert('artifacts', artifact_id=capture['artifact_id'], run_id='run-test',
                              step_id='step-test', relative_path='private-capture.png', media_type='image/png',
                              sha256=capture['sha256'], size_bytes=1, redaction_status='raw',
                              created_at='2026-01-01T00:00:00Z')
            self.store.insert('observations', observation_id='capture-observation-test', run_id='run-test',
                              step_id='step-test', kind='vision.capture', payload_json=json.dumps(capture),
                              created_at='2026-01-01T00:00:00Z')
            self.store.insert('observations', observation_id='scene-observation-test', run_id='run-test',
                              step_id='step-test', kind='vision.scene', payload_json=json.dumps(scene),
                              created_at='2026-01-01T00:00:00Z')
            self.store.connection.execute("UPDATE state_snapshots SET state_json=? WHERE snapshot_id='snapshot-test'",
                (json.dumps({'task_kind': 'vision_canvas', 'capture_id': scene['capture_id'],
                             'scene_sha256': digest(scene)}),))
            self.store.connection.execute("UPDATE actions SET tool='vision.click' WHERE action_id='action-test'")
            self.store.connection.execute("UPDATE actions SET tool='vision.verify',result_json=? WHERE action_id='readback-test'",
                                          (json.dumps(outcome),))
            self.store.connection.execute("UPDATE observations SET kind='vision.outcome',payload_json=? WHERE observation_id='observation-test'",
                                          (json.dumps(outcome),))
            self.store.connection.execute("UPDATE verifications SET method='independent_canvas_equals',verifier='aos-visual-canvas-v1',expected_json=?,actual_json=? WHERE verification_id='verification-test'",
                                          (json.dumps(outcome), json.dumps(outcome)))
        system2 = next(event for event in self.events() if event['role'] == 'system2')
        self.assertEqual(system2['source']['scene_observation_id'], 'scene-observation-test')
        self.assertEqual(system2['source']['downstream_verification_ids'], ['verification-test'])
        self.assertFalse(system2['verified_outcome'])
        self.assertFalse(system2['training_ready'])
        self.assertNotIn('private-capture.png', json.dumps(system2))
        for table, column, record_id, replacement, expected_scene in (
            ('observations', 'payload_json', 'scene-observation-test', '{}', None),
            ('model_calls', 'request_json', 'bonsai-call', '{}', None),
            ('state_snapshots', 'state_json', 'snapshot-test', '{}', 'scene-observation-test'),
            ('model_calls', 'deployment_id', 'call-test', 'wrong-deployment', 'scene-observation-test'),
            ('verifications', 'actual_json', 'verification-test', '{"selected":"MISS","clicks":1}', 'scene-observation-test'),
        ):
            id_column = {'observations': 'observation_id', 'model_calls': 'call_id',
                         'state_snapshots': 'snapshot_id', 'verifications': 'verification_id'}[table]
            with self.subTest(table=table, column=column):
                original = self.store.connection.execute(
                    f'SELECT {column} FROM {table} WHERE {id_column}=?', (record_id,)).fetchone()[0]
                with self.store.connection:
                    self.store.connection.execute(
                        f'UPDATE {table} SET {column}=? WHERE {id_column}=?', (replacement, record_id))
                changed = next(event for event in self.events() if event['role'] == 'system2')
                self.assertEqual(changed['source']['scene_observation_id'], expected_scene)
                self.assertEqual(changed['source']['downstream_verification_ids'], [])
                with self.store.connection:
                    self.store.connection.execute(
                        f'UPDATE {table} SET {column}=? WHERE {id_column}=?', (original, record_id))

    def test_fixture_or_mismatched_deployment_cannot_claim_real_model(self):
        self.call(deployment_id="wrong-deployment")
        self.decision()
        self.assertEqual(self.events(), [])
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET deployment_id=?", (DECIDER["deployment_id"],))
            self.store.connection.execute("UPDATE runs SET deployment_snapshot_json=?",
                                          (json.dumps({**DECIDER, "real_model": False, "supervisor": BONSAI}),))
        self.assertEqual(self.events(), [])

    def test_failed_or_unlinked_calls_are_not_learning_events(self):
        self.call(status="error", response=None)
        self.decision()
        self.assertEqual(self.events(), [])
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET status='ok',response_json='{}'")
            self.store.connection.execute("DELETE FROM decisions")
        self.assertEqual(self.events(), [])

    def test_invalid_independent_evidence_does_not_generate_verified_outcome(self):
        self.call()
        self.decision()
        self.verification(actual='"different"')
        event = self.events()[0]
        self.assertFalse(event["verified_outcome"])
        self.assertEqual(event["source"]["verification_ids"], [])
        self.assertIn("outcome_not_attributed", event["blockers"])

    def test_readback_attribution_rejects_wrong_tool_kind_result_and_same_action(self):
        self.call()
        self.decision()
        self.verification()
        self.assertTrue(self.events()[0]["verified_outcome"])
        for table, column, record_id, replacement in (
            ("actions", "tool", "action-test", "browser.fill"),
            ("actions", "tool", "readback-test", "browser.verify"),
            ("actions", "result_json", "readback-test", '{"content":"different"}'),
            ("actions", "status", "readback-test", "error"),
            ("actions", "decision_id", "readback-test", None),
            ("observations", "kind", "observation-test", "browser.dom"),
            ("observations", "payload_json", "observation-test", '{"content":"different"}'),
            ("observations", "action_id", "observation-test", "action-test"),
        ):
            id_column = {"actions": "action_id", "observations": "observation_id"}[table]
            with self.subTest(table=table, column=column):
                original = self.store.connection.execute(
                    f"SELECT {column} FROM {table} WHERE {id_column}=?", (record_id,)).fetchone()[0]
                with self.store.connection:
                    self.store.connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE {id_column}=?", (replacement, record_id))
                self.assertFalse(self.events()[0]["verified_outcome"])
                with self.store.connection:
                    self.store.connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE {id_column}=?", (original, record_id))
        self.assertTrue(self.events()[0]["verified_outcome"])

    def test_dom_and_canvas_attribution_require_their_own_readback_tools(self):
        self.call()
        self.decision()
        self.verification()
        for method, verifier, effect, readback, kind, result in (
            ("independent_dom_equals", "aos-browser-form-v1", "browser.fill", "browser.verify",
             "browser.dom", {"value": "same", "receipt": "", "submissions": 0}),
            ("independent_canvas_equals", "aos-visual-canvas-v1", "vision.click", "vision.verify",
             "vision.outcome", {"selected": "SAVE", "clicks": 1}),
        ):
            with self.subTest(method=method), self.store.connection:
                self.store.connection.execute("UPDATE actions SET tool=? WHERE action_id='action-test'", (effect,))
                self.store.connection.execute("UPDATE actions SET tool=?,result_json=? WHERE action_id='readback-test'",
                                              (readback, json.dumps(result)))
                self.store.connection.execute("UPDATE observations SET kind=?,payload_json=? WHERE observation_id='observation-test'",
                                              (kind, json.dumps(result)))
                self.store.connection.execute("UPDATE verifications SET method=?,verifier=?,expected_json=?,actual_json=? WHERE verification_id='verification-test'",
                                              (method, verifier, json.dumps(result), json.dumps(result)))
            self.assertTrue(self.events()[0]["verified_outcome"])
            with self.store.connection:
                self.store.connection.execute("UPDATE observations SET kind='unrelated' WHERE observation_id='observation-test'")
            self.assertFalse(self.events()[0]["verified_outcome"])

    def test_local_navigation_outcome_requires_matching_independent_snapshot(self):
        self.call()
        self.decision()
        expected = {"page": "start", "heading": "Synthetic start",
                    "links": [{"role": "link", "label": "Details"}]}
        observed = {"page": "start", "heading": "Synthetic start", "snapshot_id": "a" * 32,
                    "elements": [{"element_id": "b" * 32, "role": "link", "label": "Details"}]}
        payload = json.dumps(observed)
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='browser-local-navigation-policy-v1' WHERE run_id='run-test'")
            self.store.insert("actions", action_id="effect-test", run_id="run-test", step_id="step-test",
                              decision_id="decision-test", idempotency_key="effect-key",
                              tool="browser.fixture.open", arguments_json="{}", status="ok",
                              actual_option="open_start", created_at="2026-01-01T00:00:00Z")
            self.store.insert("actions", action_id="snapshot-action-test", run_id="run-test", step_id="step-test",
                              decision_id="decision-test", idempotency_key="snapshot-key",
                              tool="browser.fixture.snapshot", arguments_json="{}", status="ok",
                              result_json=payload, actual_option="open_start", created_at="2026-01-01T00:00:00Z")
            self.store.insert("observations", observation_id="page-observation-test", run_id="run-test",
                              step_id="step-test", action_id="snapshot-action-test", kind="browser.local_navigation",
                              payload_json=payload, created_at="2026-01-01T00:00:00Z")
            self.store.insert("verifications", verification_id="page-verification-test", run_id="run-test",
                              step_id="step-test", action_id="effect-test", criterion="Exact synthetic Start page",
                              method="independent_local_page_equals", result="passed",
                              expected_json=json.dumps(expected), actual_json=json.dumps(expected),
                              evidence_refs_json='["page-observation-test"]', verifier="aos-local-navigation-v1",
                              created_at="2026-01-01T00:00:00Z")
        event = self.events()[0]
        self.assertTrue(event["verified_outcome"])
        self.assertEqual(event["source"]["verification_ids"], ["page-verification-test"])
        self.assertFalse(event["training_ready"])
        self.assertIn("synthetic_task_excluded", event["blockers"])
        with self.store.connection:
            self.store.connection.execute("UPDATE observations SET payload_json=? WHERE observation_id='page-observation-test'",
                                          (json.dumps({**observed, "heading": "private-token-do-not-export"}),))
        changed = self.events()[0]
        self.assertFalse(changed["verified_outcome"])
        self.assertEqual(changed["source"]["verification_ids"], [])
        self.assertNotIn("private-token-do-not-export", json.dumps(changed))
        with self.store.connection:
            self.store.connection.execute("UPDATE observations SET payload_json=? WHERE observation_id='page-observation-test'",
                                          (payload,))
        for table, column, record_id, replacement in (
            ("actions", "tool", "snapshot-action-test", "browser.fixture.open"),
            ("actions", "result_json", "snapshot-action-test", json.dumps({**observed, "page": "details"})),
            ("observations", "kind", "page-observation-test", "browser.form"),
            ("verifications", "actual_json", "page-verification-test", json.dumps({**expected, "page": "details"})),
        ):
            id_column = {"actions": "action_id", "observations": "observation_id",
                         "verifications": "verification_id"}[table]
            with self.subTest(table=table, column=column):
                original = self.store.connection.execute(
                    f"SELECT {column} FROM {table} WHERE {id_column}=?", (record_id,)).fetchone()[0]
                with self.store.connection:
                    self.store.connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE {id_column}=?", (replacement, record_id))
                self.assertFalse(self.events()[0]["verified_outcome"])
                with self.store.connection:
                    self.store.connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE {id_column}=?", (original, record_id))
        self.assertTrue(self.events()[0]["verified_outcome"])

    def test_cli_review_is_deterministic_metadata_only_and_does_not_write(self):
        self.call()
        self.decision()
        self.verification()
        database_before = self.path.read_bytes()
        first = self.cli()
        second = self.cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report["mode"], "read_only_metadata_review")
        self.assertEqual(report["event_count"], 1)
        self.assertEqual(report["events"], self.events())
        self.assertFalse(report["collection_authorized"])
        self.assertFalse(report["training_ready"])
        self.assertNotIn("do-not-export", first.stdout)
        self.assertEqual(self.path.read_bytes(), database_before)

    def test_cli_missing_run_database_symlink_and_corruption_fail_closed(self):
        for arguments in (("--run-id", "missing-run"),
                          ("--database", str(self.path.parent / "missing-private.sqlite")),
                          ("--run-id", "private/token"),
                          ("--secret", "do-not-export")):
            with self.subTest(arguments=arguments):
                failed = self.cli(*arguments)
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(failed.stdout, "")
                self.assertNotIn("do-not-export", failed.stderr)
                self.assertNotIn("private", failed.stderr)
                self.assertNotIn("Traceback", failed.stderr)
        corrupt = self.path.parent / "corrupt-private.sqlite"
        corrupt.write_bytes(b"not a sqlite database")
        shortcut = self.path.parent / "symlink-private.sqlite"
        shortcut.symlink_to(self.path)
        for path in (corrupt, shortcut):
            with self.subTest(path=path):
                failed = self.cli("--database", str(path))
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(failed.stdout, "")
                self.assertNotIn("private", failed.stderr)

    def test_cli_rejects_schema_drift_without_partial_output(self):
        with self.store.connection:
            self.store.connection.execute("CREATE TABLE unauthorized_source (secret TEXT)")
        failed = self.cli()
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")
        self.assertNotIn("Traceback", failed.stderr)

    def test_review_limits_all_recorded_calls_without_truncation(self):
        self.call()
        with patch("aos.learning_events.MAX_REVIEW_EVENTS", 0):
            with self.assertRaisesRegex(ValueError, "event_count_limit"):
                review_learning_events(self.path, "run-test")


if __name__ == "__main__":
    unittest.main()

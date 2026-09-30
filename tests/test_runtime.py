import asyncio
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import jsonschema

from aos.computer import ComputerGateway, SafetyPolicy, WorkspaceRuntime
from aos.contracts import AOSFault, Action, HELLO_CONTENT, HELLO_PATH, Phase, Prediction, REPO_ROOT, Settings, State
from aos.decision import FixtureDecisionEngine
from aos.export import export_hello
from aos.operator import Operator
from aos.storage import TrajectoryStore


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / "workspace", database=self.root / "trace.sqlite")
        self.runtime = WorkspaceRuntime(self.settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(self.settings.database)

    def tearDown(self):
        self.store.close()
        self.runtime.stop()
        self.temporary.cleanup()

    def run_hello(self, engine=None):
        return asyncio.run(Operator(self.settings, self.store, self.runtime, engine or FixtureDecisionEngine()).hello())

    def test_hello_persists_and_exports_canonical_trajectory(self):
        result = self.run_hello()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["real_model"])
        self.assertEqual((self.settings.workspace / "hello.txt").read_bytes(), HELLO_CONTENT.encode())
        exported = export_hello(self.store, result["run_id"])
        schema = json.loads((REPO_ROOT / "schemas/trajectory_export.schema.json").read_text())
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(exported)
        self.assertEqual(len(exported["steps"][0]["actions"]), 2)
        self.assertEqual(exported["steps"][0]["model_calls"], [])
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.store.connection.execute("SELECT training_eligible FROM runs").fetchone()[0], 0)

    def test_existing_exact_content_is_only_read(self):
        self.run_hello()
        result = self.run_hello()
        actions = self.store.connection.execute("SELECT tool FROM actions WHERE run_id=?", (result["run_id"],)).fetchall()
        self.assertTrue(all(row[0] == "filesystem.read" for row in actions))

    def test_conflicting_file_is_preserved(self):
        (self.settings.workspace / "hello.txt").write_text("preserve me")
        result = self.run_hello()
        self.assertEqual(result["status"], "waiting_human")
        self.assertEqual((self.settings.workspace / "hello.txt").read_text(), "preserve me")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions").fetchone()[0], 0)

    def test_readback_mismatch_does_not_succeed(self):
        original_read = self.runtime.read

        def changed_read(path):
            if (self.settings.workspace / "hello.txt").exists():
                return "tampered"
            return original_read(path)

        with patch.object(self.runtime, "read", side_effect=changed_read):
            result = self.run_hello()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.connection.execute("SELECT result FROM verifications").fetchone()[0], "failed")

    def test_symlink_hardlink_and_traversal_are_denied(self):
        outside = self.root / "outside"
        outside.write_text("private")
        target = self.settings.workspace / "hello.txt"
        target.symlink_to(outside)
        self.assertEqual(self.run_hello()["status"], "failed")
        target.unlink()
        os.link(outside, target)
        with self.assertRaises(AOSFault):
            self.runtime.read(HELLO_PATH)
        with self.assertRaises(AOSFault):
            self.runtime.read("/workspace/../outside")
        with self.assertRaises(FileExistsError):
            self.runtime.write(HELLO_PATH, HELLO_CONTENT)
        self.assertEqual(outside.read_text(), "private")

    def test_invalid_predictions_never_execute(self):
        for probabilities in ({"write_file": math.nan, "ask_supervisor": 0.0, "ask_human": 0.0},
                              {"write_file": 0.2, "ask_supervisor": 0.2, "ask_human": 0.2},
                              {"write_file": 1.0, "unknown": 0.0}):
            class InvalidEngine(FixtureDecisionEngine):
                async def decide(self, state, options):
                    return Prediction(selected_option="write_file", probabilities=probabilities)

            self.assertEqual(self.run_hello(InvalidEngine())["status"], "failed")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())

    def test_timeout_does_not_execute(self):
        class SlowEngine(FixtureDecisionEngine):
            async def decide(self, state, options):
                await asyncio.sleep(1)

        self.settings = self.settings.model_copy(update={"model_timeout_seconds": 0.01})
        self.assertEqual(self.run_hello(SlowEngine())["error"]["code"], "TIMEOUT")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())

    def test_low_confidence_abstains(self):
        class UncertainEngine(FixtureDecisionEngine):
            async def decide(self, state, options):
                return Prediction(selected_option="write_file", probabilities={"write_file": 0.7, "ask_supervisor": 0.2, "ask_human": 0.1})

        self.assertEqual(self.run_hello(UncertainEngine())["status"], "waiting_human")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())

    def test_state_machine_rejects_unverified_success(self):
        state = State(task_id="task", run_id="run", step_id="step", runtime_id="runtime", deployment_id="fixture", owner_lease_id="lease")
        with self.assertRaises(AOSFault):
            state.advance(Phase.SUCCEEDED)

    def test_policy_rejects_stale_lease_scope_and_deadline(self):
        state = State(task_id="task", run_id="run", step_id="step", runtime_id=self.runtime.runtime_id,
                      deployment_id="fixture", owner_lease_id="lease", phase=Phase.EXECUTE)
        action = Operator(self.settings, self.store, self.runtime, FixtureDecisionEngine()).action(state, "filesystem.write", "write_file")
        for changes in ({"state_version": 2}, {"owner_lease_id": "old"}, {"runtime_id": "other"},
                        {"deadline": time.time() - 1}, {"arguments": {"path": "/etc/passwd", "content": HELLO_CONTENT}}):
            with self.assertRaises(AOSFault):
                SafetyPolicy.check(action.model_copy(update=changes), state, self.runtime.runtime_id)
        with self.assertRaises(AOSFault):
            SafetyPolicy.check(action, state.model_copy(update={"owner": "HUMAN"}), self.runtime.runtime_id)

    def test_durable_intent_precedes_write_and_replay_is_cached(self):
        original_write = self.runtime.write

        def audited_write(path, content):
            with closing(sqlite3.connect(self.settings.database)) as other:
                self.assertEqual(other.execute("SELECT status FROM actions").fetchone()[0], "running")
                self.assertEqual(other.execute("SELECT count(*) FROM action_envelopes").fetchone()[0], 1)
            return original_write(path, content)

        with patch.object(self.runtime, "write", side_effect=audited_write):
            self.run_hello()
        row = self.store.connection.execute("SELECT * FROM actions ORDER BY created_at LIMIT 1").fetchone()
        envelope = self.store.connection.execute("SELECT envelope_json FROM action_envelopes WHERE action_id=?", (row["action_id"],)).fetchone()[0]
        action = Action.model_validate_json(envelope)
        gateway = ComputerGateway(self.store, self.runtime)
        with patch.object(self.runtime, "write", side_effect=AssertionError("must not replay")):
            self.assertEqual(gateway.execute(action, row["decision_id"])["bytes_written"], len(HELLO_CONTENT))
        with self.assertRaises(AOSFault):
            gateway.execute(action.model_copy(update={"expected_effect": "different"}), row["decision_id"])

    def test_restart_marks_inflight_uncertain_and_revokes_lease(self):
        def interrupted_write(path, content):
            raise KeyboardInterrupt()

        with patch.object(self.runtime, "write", side_effect=interrupted_write):
            with self.assertRaises(KeyboardInterrupt):
                self.run_hello()
        self.store.close()
        self.store = TrajectoryStore(self.settings.database)
        self.assertEqual(self.store.connection.execute("SELECT status FROM actions").fetchone()[0], "uncertain")
        row = self.store.connection.execute("SELECT run_id,status FROM runs").fetchone()
        self.assertEqual(row["status"], "paused")
        self.assertEqual(self.store.state(row["run_id"]).owner, "PAUSED")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())

    def test_cross_run_links_and_concurrent_writer_are_rejected(self):
        first = self.run_hello()
        second = self.run_hello()
        action = self.store.connection.execute("SELECT action_id FROM actions WHERE run_id=?", (first["run_id"],)).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection:
                self.store.connection.execute("UPDATE actions SET run_id=? WHERE action_id=?", (second["run_id"], action))
        with self.assertRaises(AOSFault):
            TrajectoryStore(self.settings.database)

    def test_denied_gateway_intent_has_no_actual_action(self):
        original_action = Operator.action

        def stale_action(operator, state, tool, option):
            return original_action(operator, state, tool, option).model_copy(update={"owner_lease_id": "revoked"})

        with patch.object(Operator, "action", stale_action):
            result = self.run_hello()
        self.assertEqual(result["status"], "failed")
        row = self.store.connection.execute("SELECT status,actual_option FROM actions").fetchone()
        self.assertEqual(row["status"], "denied")
        self.assertIsNone(row["actual_option"])
        self.assertFalse((self.settings.workspace / "hello.txt").exists())

    def test_crash_after_effect_is_not_replayed(self):
        original_write = self.runtime.write

        def crash_after_write(path, content):
            original_write(path, content)
            raise KeyboardInterrupt()

        with patch.object(self.runtime, "write", side_effect=crash_after_write):
            with self.assertRaises(KeyboardInterrupt):
                self.run_hello()
        self.store.close()
        self.store = TrajectoryStore(self.settings.database)
        row = self.store.connection.execute("SELECT action_id,decision_id,status FROM actions").fetchone()
        self.assertEqual(row["status"], "uncertain")
        envelope = self.store.connection.execute("SELECT envelope_json FROM action_envelopes WHERE action_id=?", (row["action_id"],)).fetchone()[0]
        with self.assertRaises(AOSFault):
            ComputerGateway(self.store, self.runtime).execute(Action.model_validate_json(envelope), row["decision_id"])
        self.assertEqual(self.runtime.read(HELLO_PATH), HELLO_CONTENT)

    def test_checksum_uses_isolated_fixed_argv(self):
        self.runtime.write(HELLO_PATH, HELLO_CONTENT)
        with patch("aos.computer.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"fixed checksum output"
            run.return_value.stderr = b""
            self.runtime.checksum(HELLO_PATH)
            command = run.call_args.args[0]
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertEqual(command[-2:], ["/usr/bin/sha256sum", HELLO_PATH])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_readonly_export_does_not_reconcile_or_write(self):
        result = self.run_hello()
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='running' WHERE run_id=?", (result["run_id"],))
        reader = TrajectoryStore(self.settings.database, readonly=True)
        try:
            self.assertEqual(reader.connection.execute("SELECT status FROM runs").fetchone()[0], "running")
            with self.assertRaises(sqlite3.OperationalError):
                reader.connection.execute("UPDATE runs SET status='paused'")
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()

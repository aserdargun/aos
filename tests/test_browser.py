import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
from pydantic import ValidationError

from aos.browser import BROWSER_EXPECTED, BROWSER_SCOPE, BROWSER_VALUE, BrowserRuntime, DomObservation
from aos.browser_operator import BrowserOperator
from aos.contracts import AOSFault, ErrorCode, Phase, Prediction, REPO_ROOT, Settings, canonical, identifier
from aos.decision import FixtureDecisionEngine
from aos.export import export_hello, export_run
from aos.storage import TrajectoryStore


class BrowserContractTests(unittest.TestCase):
    def test_canonical_dom_schema_and_synthetic_example(self):
        schema = json.loads((REPO_ROOT / "schemas/browser_observation.schema.json").read_text())
        self.assertEqual(schema, {"$schema": "https://json-schema.org/draft/2020-12/schema", **DomObservation.model_json_schema()})
        example = json.loads((REPO_ROOT / "examples/browser_observation.json").read_text())
        self.assertTrue(example["synthetic"])
        jsonschema.Draft202012Validator(schema).validate(example["observation"])
        DomObservation.model_validate(example["observation"])
        bad = {**example["observation"], "submissions": True}
        with self.assertRaises(ValidationError):
            DomObservation.model_validate(bad)
        bad = {**example["observation"], "elements": example["observation"]["elements"][::-1]}
        with self.assertRaises(ValidationError):
            DomObservation.model_validate(bad)


@unittest.skipUnless(os.environ.get("AOS_BROWSER_TESTS") == "1", "Opt in to real isolated Chromium with AOS_BROWSER_TESTS=1")
class BrowserIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(workspace=root / "unused", database=root / "browser.sqlite")
        self.store = TrajectoryStore(self.settings.database)
        self.runtime = BrowserRuntime(REPO_ROOT / "models/browser-manifest.json")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.stop)
        self.runtime.start()

    def operator(self, engine=None):
        return BrowserOperator(self.settings, self.store, self.runtime, engine or FixtureDecisionEngine())

    def test_real_browser_fixture_decision_and_export(self):
        result = asyncio.run(self.operator().form())
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["real_model"])
        self.assertFalse(result["vision"])
        self.assertEqual(result["system2_calls"], 0)
        self.assertEqual(self.runtime.perform("browser.verify", {}), BROWSER_EXPECTED)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE status='ok'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM model_calls").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 2)
        exported = export_run(self.store, result["run_id"])
        schema = json.loads((REPO_ROOT / "schemas/trajectory_export.schema.json").read_text())
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(exported)
        self.assertEqual(exported["redaction_version"], "bounded-browser-form-v1")
        with self.assertRaises(AOSFault):
            export_hello(self.store, result["run_id"])
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.store.connection.execute("SELECT training_eligible FROM runs").fetchone()[0], 0)

    def test_stale_snapshot_and_unknown_target_have_no_effect(self):
        observed = json.loads(self.runtime.read(BROWSER_SCOPE))
        self.runtime.read(BROWSER_SCOPE)
        arguments = {"snapshot_id": observed["snapshot_id"], "element_id": observed["elements"][0]["element_id"], "value": BROWSER_VALUE}
        with self.assertRaises(AOSFault) as caught:
            self.runtime.perform("browser.fill", arguments)
        self.assertEqual(caught.exception.code, ErrorCode.UI_CHANGED)
        observed = json.loads(self.runtime.read(BROWSER_SCOPE))
        with self.assertRaises(AOSFault) as caught:
            self.runtime.perform("browser.fill", {**arguments, "snapshot_id": observed["snapshot_id"]})
        self.assertEqual(caught.exception.code, ErrorCode.ELEMENT_MISSING)
        self.assertEqual(self.runtime.perform("browser.verify", {})["value"], "")

    def test_navigation_evaluation_and_arbitrary_values_are_denied(self):
        for tool, arguments in [("browser.navigate", {"url": "file:///etc/shadow"}),
                                ("browser.evaluate", {"script": "fetch('https://example.invalid')"}),
                                ("browser.fill", {"selector": "body", "value": "private"})]:
            with self.subTest(tool=tool), self.assertRaises(AOSFault) as caught:
                self.runtime.perform(tool, arguments)
            self.assertEqual(caught.exception.code, ErrorCode.UNSAFE_ACTION)
        with self.assertRaises(AOSFault):
            self.runtime.read("file:///etc/shadow")

    def test_invalid_output_low_confidence_and_takeover_stop_input(self):
        store = self.store

        class ControlledEngine(FixtureDecisionEngine):
            mode = "invalid"

            async def decide(self, state, options):
                if self.mode == "takeover":
                    store.save_state(state, state.advance(Phase.PAUSED, owner="HUMAN", owner_lease_id=identifier("human")))
                    return await super().decide(state, options)
                if self.mode == "timeout":
                    raise TimeoutError
                confidence = 1.2 if self.mode == "invalid" else 0.6
                return Prediction(selected_option=options[0].id, probabilities={options[0].id: confidence, "ask_human": 1 - confidence})

        engine = ControlledEngine()
        for mode, expected in [("invalid", "failed"), ("low", "waiting_human"), ("takeover", "paused"), ("timeout", "failed")]:
            engine.mode = mode
            result = asyncio.run(self.operator(engine).form())
            self.assertEqual(result["status"], expected)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions").fetchone()[0], 0)

    def test_gateway_idempotency_and_intent_before_effect(self):
        operator = self.operator()
        original = operator.gateway.execute
        observed_intent = []

        def execute_twice(action, decision_id):
            result = original(action, decision_id)
            self.assertEqual(original(action, decision_id), result)
            return result

        perform = self.runtime.perform

        def check_intent(tool, arguments):
            if tool in {"browser.fill", "browser.submit"}:
                row = self.store.connection.execute("SELECT status FROM actions ORDER BY rowid DESC LIMIT 1").fetchone()
                observed_intent.append(row[0])
            return perform(tool, arguments)

        with patch.object(operator.gateway, "execute", side_effect=execute_twice), patch.object(self.runtime, "perform", side_effect=check_intent):
            self.assertEqual(asyncio.run(operator.form())["status"], "succeeded")
        self.assertEqual(observed_intent, ["running", "running"])
        self.assertEqual(perform("browser.verify", {})["submissions"], 1)

    def test_cancellation_revokes_lease_without_input(self):
        class CancelledEngine(FixtureDecisionEngine):
            async def decide(self, state, options):
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.operator(CancelledEngine()).form())
        row = self.store.connection.execute("SELECT run_id,status FROM runs").fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(self.store.state(row["run_id"]).owner, "PAUSED")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions").fetchone()[0], 0)

    def test_crash_after_effect_is_uncertain_not_replayed(self):
        original = self.runtime.perform

        def crash(tool, arguments):
            result = original(tool, arguments)
            if tool == "browser.fill":
                self.runtime.stop()
                raise AOSFault(ErrorCode.RUNTIME_CRASH, "Synthetic lost acknowledgement after real effect")
            return result

        with patch.object(self.runtime, "perform", side_effect=crash):
            result = asyncio.run(self.operator().form())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.connection.execute("SELECT status FROM actions").fetchone()[0], "uncertain")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM verifications").fetchone()[0], 0)

    def test_mismatch_fails_independent_verification(self):
        original = self.runtime.perform

        def mismatch(tool, arguments):
            result = original(tool, arguments)
            return {**result, "receipt": "unexpected"} if tool == "browser.verify" else result

        with patch.object(self.runtime, "perform", side_effect=mismatch):
            result = asyncio.run(self.operator().form())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.connection.execute("SELECT result FROM verifications").fetchone()[0], "failed")

    def test_dom_replacement_and_network_boundary_in_real_chromium(self):
        command = self.runtime.command(Path(self.runtime.pins["browser_root"]))
        boundary = command.index("--")
        command[boundary:boundary] = ["--ro-bind", str(REPO_ROOT / "tests/browser_sandbox_checks.py"), "/checks.py",
                                     "--setenv", "AOS_HOST_NETNS", os.readlink("/proc/self/ns/net")]
        command[-1] = "/checks.py"
        result = subprocess.run(command, capture_output=True, timeout=30, env={})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(len(json.loads(result.stdout)["passed"]), 7)

    def test_timeout_stops_only_owned_worker(self):
        process = self.runtime.process
        with patch("aos.browser.select.select", return_value=([], [], [])), self.assertRaises(AOSFault) as caught:
            self.runtime.receive(0.01)
        self.assertEqual(caught.exception.code, ErrorCode.TIMEOUT)
        self.assertIsNone(self.runtime.process)
        self.assertIsNotNone(process.poll())

    def test_browser_pin_mismatch_fails_before_launch(self):
        pins = {**self.runtime.pins, "files": {"chrome-headless-shell": "0" * 64}}
        path = Path(self.temporary.name) / "bad-manifest.json"
        path.write_text(canonical(pins))
        runtime = BrowserRuntime(path)
        with self.assertRaises(AOSFault):
            runtime.start()
        self.assertIsNone(runtime.process)

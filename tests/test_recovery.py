import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import jsonschema
from pydantic import ValidationError

from aos.computer import WorkspaceRuntime
from aos.contracts import AOSFault, ErrorCode, HELLO_CONTENT, Phase, REPO_ROOT, Settings
from aos.decision import FixtureDecisionEngine
from aos.export import export_hello
from aos.operator import Operator
from aos.storage import TrajectoryStore
from aos.supervisor import BonsaiSupervisor, RecoveryPlan, verify_artifacts


FIXTURE = json.loads((REPO_ROOT / "examples/supervisor_plan.json").read_text())


class FixtureSupervisor:
    identity = {"deployment_id": "fixture-supervisor-v1", "kind": "deterministic_fixture", "real_model": False}
    last_metrics = {}

    async def plan(self, problem, evidence):
        value = copy.deepcopy(FIXTURE["plan"])
        value["evidence_refs"] = [item["id"] for item in evidence]
        return RecoveryPlan.model_validate(value)


class RecoveryTests(unittest.TestCase):
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

    def recover(self, supervisor=None):
        operator = Operator(self.settings, self.store, self.runtime, FixtureDecisionEngine(), supervisor or FixtureSupervisor())
        return asyncio.run(operator.hello(recovery_probe=True))

    def test_recovery_records_real_missing_read_and_verified_outcome(self):
        result = self.recover()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["real_supervisor"])
        self.assertEqual(result["system2_calls"], 0)
        self.assertEqual(result["recovery_attempts"], 1)
        self.assertEqual(self.runtime.read("/workspace/hello.txt"), HELLO_CONTENT)
        actions = self.store.connection.execute("SELECT tool,status,error_code FROM actions ORDER BY created_at").fetchall()
        self.assertEqual(tuple(actions[0]), ("filesystem.read", "error", "ELEMENT_MISSING"))
        self.assertEqual(len(actions), 3)
        self.assertEqual(self.store.connection.execute("SELECT outcome FROM supervisor_escalations").fetchone()[0], "recovered")
        state = self.store.state(result["run_id"])
        self.assertEqual(state.plan_version, 1)
        self.assertEqual(state.recovery_plan[-1], "verify_exact_content")
        exported = export_hello(self.store, result["run_id"])
        schema = json.loads((REPO_ROOT / "schemas/trajectory_export.schema.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(exported)
        self.assertEqual(exported["steps"][0]["escalations"][0]["outcome"], "recovered")
        self.assertEqual(len(exported["run"]["deployment_ids"]), 2)

    def test_human_required_plan_does_not_write(self):
        class HumanSupervisor(FixtureSupervisor):
            async def plan(self, problem, evidence):
                value = (await super().plan(problem, evidence)).model_dump()
                return RecoveryPlan.model_validate({**value, "needs_human": True, "revised_plan": []})

        result = self.recover(HumanSupervisor())
        self.assertEqual(result["status"], "waiting_human")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())
        self.assertEqual(self.store.connection.execute("SELECT outcome FROM supervisor_escalations").fetchone()[0], "human_required")

    def test_unknown_evidence_blocks_execution(self):
        class FabricatedSupervisor(FixtureSupervisor):
            async def plan(self, problem, evidence):
                return RecoveryPlan.model_validate(FIXTURE["plan"])

        self.assertEqual(self.recover(FabricatedSupervisor())["status"], "failed")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())

    def test_unsafe_or_incomplete_plans_are_rejected(self):
        for action in ("shell.exec", "read_host_key"):
            value = copy.deepcopy(FIXTURE["plan"])
            value["revised_plan"][0]["action"] = action
            with self.assertRaises(ValidationError):
                RecoveryPlan.model_validate(value)
        value = copy.deepcopy(FIXTURE["plan"])
        value["revised_plan"] = value["revised_plan"][:2]
        with self.assertRaises(AOSFault):
            RecoveryPlan.model_validate(value).validate_evidence(FIXTURE["evidence"])

    def test_takeover_during_supervisor_call_prevents_write(self):
        store = self.store

        class TakeoverSupervisor(FixtureSupervisor):
            async def plan(self, problem, evidence):
                run_id = store.connection.execute("SELECT run_id FROM runs").fetchone()[0]
                state = store.state(run_id)
                current = state.advance(Phase.PAUSED, owner="HUMAN", owner_lease_id="revoked")
                store.save_state(state, current)
                return await super().plan(problem, evidence)

        self.assertEqual(self.recover(TakeoverSupervisor())["status"], "failed")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())

    def test_supervisor_timeout_and_exhausted_budget(self):
        class SlowSupervisor(FixtureSupervisor):
            async def plan(self, problem, evidence):
                await asyncio.sleep(1)

        self.settings = self.settings.model_copy(update={"supervisor_timeout_seconds": 0.01})
        result = self.recover(SlowSupervisor())
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertFalse((self.settings.workspace / "hello.txt").exists())
        self.settings = self.settings.model_copy(update={"max_supervisor_recoveries": 0})
        self.assertEqual(self.recover()["error"]["code"], "STUCK")

    def test_existing_file_is_preserved_and_probe_refused(self):
        target = self.settings.workspace / "hello.txt"
        target.write_text("existing content")
        self.assertEqual(self.recover()["status"], "failed")
        self.assertEqual(target.read_text(), "existing content")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM supervisor_escalations").fetchone()[0], 0)

    def test_schema_and_synthetic_fixture_match_runtime(self):
        schema = json.loads((REPO_ROOT / "schemas/supervisor_plan.schema.json").read_text())
        self.assertEqual(schema, {"$schema": "https://json-schema.org/draft/2020-12/schema", **RecoveryPlan.model_json_schema()})
        jsonschema.Draft202012Validator(schema).validate(FIXTURE["plan"])
        RecoveryPlan.model_validate(FIXTURE["plan"]).validate_evidence(FIXTURE["evidence"])

    def test_native_api_receives_nested_schema_and_disables_reasoning(self):
        manifest = self.root / "bonsai.json"
        manifest.write_text(json.dumps({"temperature": 0.0, "max_output_tokens": 512}))
        supervisor = BonsaiSupervisor(manifest)
        body = supervisor.request_body("Synthetic missing-file probe", FIXTURE["evidence"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], RecoveryPlan.model_json_schema())
        self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("tools", body)

    def test_bonsai_stage_metrics_are_bounded_to_successful_call(self):
        manifest = self.root / "bonsai.json"
        manifest.write_text(json.dumps({
            "model_path": str(self.root), "weights_file": "synthetic.gguf",
            "projector_file": "synthetic-projector.gguf", "server_path": str(self.root / "llama-server"),
            "context_tokens": 16384, "gpu_layers": 99,
            "temperature": 0.0, "max_output_tokens": 512}))
        supervisor = BonsaiSupervisor(manifest)
        process = AsyncMock()
        process.returncode = None
        process.pid = 12345

        def response(port, token, route, *arguments):
            if route == "/v1/models":
                return {"data": [{"id": supervisor.identity["deployment_id"]}]}
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(FIXTURE["plan"])}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 34}}

        with patch.object(supervisor, "verify_pins"), \
                patch("aos.supervisor.request_json", side_effect=response), \
                patch("aos.supervisor.asyncio.create_subprocess_exec", return_value=process), \
                patch("aos.supervisor.os.killpg") as terminate:
            result = asyncio.run(supervisor.plan("synthetic", FIXTURE["evidence"]))
        self.assertIsInstance(result, RecoveryPlan)
        metrics = supervisor.last_metrics
        stages = ("pin_verify_ms", "server_start_ms", "request_build_ms",
                  "inference_http_ms", "response_parse_ms")
        self.assertTrue(all(metrics[name] >= 0 for name in stages))
        self.assertAlmostEqual(sum(metrics[name] for name in stages), metrics["latency_ms"])
        self.assertEqual((metrics["input_tokens"], metrics["output_tokens"]), (12, 34))
        terminate.assert_called_once_with(process.pid, signal.SIGTERM)
        process.wait.assert_awaited_once()

        with patch.object(supervisor, "verify_pins"), \
                patch("aos.supervisor.request_json", side_effect=ValueError("synthetic bad response")), \
                patch("aos.supervisor.asyncio.create_subprocess_exec", return_value=process), \
                patch("aos.supervisor.os.killpg"):
            with self.assertRaises(AOSFault):
                asyncio.run(supervisor.plan("synthetic", FIXTURE["evidence"]))
        self.assertEqual(supervisor.last_metrics, {})

    def test_failed_readback_does_not_mark_recovery_successful(self):
        original_read = self.runtime.read

        def mismatched_read(path):
            if (self.settings.workspace / "hello.txt").exists():
                return "wrong"
            return original_read(path)

        with patch.object(self.runtime, "read", side_effect=mismatched_read):
            self.assertEqual(self.recover()["status"], "failed")
        self.assertEqual(self.store.connection.execute("SELECT outcome FROM supervisor_escalations").fetchone()[0], "failed")

    def test_bonsai_artifact_hash_and_external_symlink_are_rejected(self):
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / "model").write_bytes(b"modified")
        with self.assertRaises(AOSFault):
            verify_artifacts(artifacts, {"model": "0" * 64})
        (artifacts / "model").unlink()
        (artifacts / "model").symlink_to(self.settings.database)
        with self.assertRaises(AOSFault):
            verify_artifacts(artifacts, {"model": "0" * 64})

    def test_bonsai_pin_cache_rehashes_changed_artifacts(self):
        model_root = self.root / "models"
        runtime_root = self.root / "runtime"
        model_root.mkdir()
        runtime_root.mkdir()
        weights = model_root / "model.gguf"
        weights.write_bytes(b"synthetic weights")
        projector = model_root / "projector.gguf"
        projector.write_bytes(b"synthetic projector")
        server = runtime_root / "llama-server"
        server.write_bytes(b"synthetic server")
        library = self.root / "library"
        library.write_bytes(b"synthetic library")
        checksum = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        pins = {"model_path": str(model_root), "model_files": {
                    "model.gguf": checksum(weights), "projector.gguf": checksum(projector)},
                "runtime_path": str(runtime_root), "runtime_files": {"llama-server": checksum(server)},
                "server_path": str(server), "native_libraries": {str(library): checksum(library)}}
        manifest = self.root / "bonsai-cache.json"
        manifest.write_text(json.dumps(pins))
        supervisor = BonsaiSupervisor(manifest)
        with patch("aos.supervisor.verify_artifacts", wraps=verify_artifacts) as hashes:
            async def prepared_call():
                supervisor.prewarm_pins_idle()
                await supervisor.verify_pins_for_call()
                await supervisor.verify_pins_for_call()
                await supervisor.close_pin_prewarm()

            asyncio.run(prepared_call())
            self.assertEqual(hashes.call_count, 2)
            original = weights.stat()
            weights.write_bytes(b"synthetic changed")
            os.utime(weights, ns=(original.st_atime_ns, original.st_mtime_ns))
            with self.assertRaises(AOSFault):
                supervisor.verify_pins()
            self.assertEqual(hashes.call_count, 3)
            weights.write_bytes(b"synthetic weights")
            supervisor.verify_pins()
            self.assertEqual(hashes.call_count, 5)
            (runtime_root / "unplanned").write_bytes(b"new")
            with self.assertRaises(AOSFault):
                supervisor.verify_pins()
            self.assertEqual(hashes.call_count, 7)
            (runtime_root / "unplanned").unlink()
            supervisor.verify_pins()
            library.write_bytes(b"modified library")
            with self.assertRaises(AOSFault):
                supervisor.verify_pins()
            weights.unlink()
            weights.symlink_to(self.settings.database)
            with self.assertRaises(AOSFault):
                supervisor.verify_pins()
            weights.unlink()
            weights.write_bytes(b"synthetic weights")
            library.write_bytes(b"synthetic library")

            def mutate_after_model(root, expected):
                verify_artifacts(root, expected)
                if root == model_root:
                    projector.write_bytes(b"modified projector")

            with patch("aos.supervisor.verify_artifacts", side_effect=mutate_after_model):
                with self.assertRaisesRegex(AOSFault, "changed during verification"):
                    supervisor.verify_pins()

        async def failed_preparation():
            with patch.object(supervisor, "verify_pins",
                    side_effect=AOSFault(ErrorCode.MODEL_FAILURE, "synthetic pin failure")):
                supervisor.prewarm_pins_idle()
                with self.assertRaises(AOSFault):
                    await supervisor.verify_pins_for_call()
                await supervisor.close_pin_prewarm()

        asyncio.run(failed_preparation())

    def test_native_timeout_stops_owned_process_and_removes_key(self):
        library = self.root / "library"
        library.write_bytes(b"synthetic library")
        (self.root / "models").mkdir()
        (self.root / "runtime").mkdir()
        (self.root / "runtime/llama-server").write_bytes(b"synthetic server")
        pins = {"model_path": str(self.root / "models"), "model_files": {},
                "runtime_path": str(self.root / "runtime"), "runtime_files": {},
                "server_path": str(self.root / "runtime/llama-server"),
                "native_libraries": {str(library): hashlib.sha256(library.read_bytes()).hexdigest()},
                "weights_file": "model.gguf", "projector_file": "projector.gguf", "context_tokens": 16384, "gpu_layers": 99}
        manifest = self.root / "timeout.json"
        manifest.write_text(json.dumps(pins))
        supervisor = BonsaiSupervisor(manifest, timeout=0.01)
        process = AsyncMock()
        process.returncode = None
        process.pid = 12345
        with patch("aos.supervisor.verify_artifacts"), patch("aos.supervisor.request_json", side_effect=ConnectionRefusedError), \
                patch("aos.supervisor.asyncio.create_subprocess_exec", return_value=process) as launch, \
                patch("aos.supervisor.os.killpg") as terminate:
            with self.assertRaises(AOSFault) as failure:
                asyncio.run(supervisor.plan("synthetic", FIXTURE["evidence"]))
            self.assertEqual(failure.exception.code, "TIMEOUT")
            terminate.assert_called_once()
            process.wait.assert_awaited_once()
        arguments = launch.call_args.args
        self.assertFalse(Path(arguments[arguments.index("--api-key-file") + 1]).exists())


if __name__ == "__main__":
    unittest.main()

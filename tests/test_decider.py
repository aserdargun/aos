import asyncio
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aos.contracts import AOSFault, Option, REPO_ROOT, State, digest
from aos.decision import DeciderEngine


class DeciderTests(unittest.TestCase):
    def test_artifact_manifest_rejects_changed_and_extra_files(self):
        worker = runpy.run_path(str(REPO_ROOT / "services/decider/worker.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "weights"
            artifact.write_bytes(b"synthetic artifact")
            expected = {"weights": worker["sha256"](artifact)}
            self.assertEqual(worker["verify_files"](root, expected), root)
            artifact.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                worker["verify_files"](root, expected)
            with self.assertRaises(ValueError):
                worker["verify_files"](root, {})

    def test_adapter_rejects_wrong_loaded_identity_and_unknown_options(self):
        state = State(task_id="task", run_id="run", step_id="step", runtime_id="runtime",
                      deployment_id="fixture", owner_lease_id="lease")
        options = [Option(id="write", label="Write"), Option(id="ask", label="Ask")]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text("{}")
            engine = DeciderEngine(manifest, Path("/synthetic/python"))
            for identity, selected in (("wrong", "write"), (digest({}), "unknown")):
                response = {"deployment_digest": identity, "metrics": {},
                            "prediction": {"selected_option": selected, "probabilities": {"write": 0.9, "ask": 0.1}}}
                process = AsyncMock()
                process.returncode = 0
                process.communicate.return_value = (json.dumps(response).encode(), None)
                with patch("aos.decision.asyncio.create_subprocess_exec", return_value=process):
                    with self.assertRaises(AOSFault):
                        asyncio.run(engine.decide(state, options))


if __name__ == "__main__":
    unittest.main()

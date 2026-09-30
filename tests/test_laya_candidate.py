import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest

from aos.contracts import digest
from aos.laya_candidate import LayaCandidateEngine
from aos.registries import DeploymentRegistry
from aos.storage import TrajectoryStore


WORKER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "services/laya/worker.py"))


class LayaCandidateTests(unittest.TestCase):
    def test_file_pin_rejects_symlink_extra_and_changed_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            model.mkdir()
            artifact = model / "weights"
            artifact.write_bytes(b"synthetic")
            expected = {"weights": hashlib.sha256(b"synthetic").hexdigest()}
            self.assertEqual(WORKER["verify_files"](model, expected), model)
            with self.assertRaises(ValueError):
                WORKER["verify_files"](model / "weights", expected)
            alias = root / "alias"
            alias.symlink_to(model, target_is_directory=True)
            with self.assertRaises(ValueError):
                WORKER["verify_files"](alias, expected)
            artifact.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                WORKER["verify_files"](model, expected)

    def test_request_rejects_duplicate_labels_and_unbounded_text(self):
        request = {"state": "A", "question": "Next?", "options": [
            {"id": "first", "label": "Write"}, {"id": "second", "label": "Ask"}]}
        self.assertEqual(len(WORKER["validate_request"](request)), 2)
        with self.assertRaises(ValueError):
            WORKER["validate_request"]({**request, "options": [request["options"][0],
                                                           {"id": "second", "label": "Write"}]})
        with self.assertRaises(ValueError):
            WORKER["validate_request"]({**request, "state": "X" * 4097})

    def test_registry_records_laya_as_disabled_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TrajectoryStore(root / "test.db")
            pins = {"source": "convaiinnovations/laya-typed-decisions",
                    "checkpoint_revision": "a" * 40, "tokenizer_revision": "a" * 40,
                    "model_files": {"model.safetensors": "b" * 64}}
            identity = {"deployment_id": "laya-candidate-" + digest(pins),
                        "kind": "laya_candidate", "real_model": True, "pins": pins}
            DeploymentRegistry(store).record_experiment(identity)
            model = store.connection.execute("SELECT source,enabled FROM models").fetchone()
            deployment = store.connection.execute("SELECT status FROM deployments").fetchone()
            self.assertEqual((model["source"], model["enabled"], deployment["status"]),
                             (pins["source"], 0, "EXPERIMENTAL"))
            store.close()

    def test_engine_uses_separate_identity_and_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"source": "synthetic"}))
            engine = LayaCandidateEngine(manifest, Path("/synthetic/python"))
            self.assertEqual(engine.identity["kind"], "laya_candidate")
            self.assertEqual(engine.identity["deployment_id"], "laya-candidate-" + digest(engine.pins))


if __name__ == "__main__":
    unittest.main()

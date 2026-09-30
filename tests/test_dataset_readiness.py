import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import build_fixture_dataset, validator
from aos.dataset_readiness import readiness


class DatasetReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = self.root / "dataset"
        build_fixture_dataset(self.dataset, include_synthetic=True)

    def evidence(self):
        tokenizer = json.loads((REPO_ROOT / "examples/dataset_tokenizer_probe.json").read_text())["report"]
        loss = json.loads((REPO_ROOT / "examples/dataset_loss_probe.json").read_text())["report"]
        pins = {key: tokenizer[key] for key in ("checkpoint_revision", "tokenizer_revision", "code_revision", "code_files", "dependencies")}
        pins["model_files"] = {**tokenizer["tokenizer_files"], "model.safetensors": "0" * 64}
        deployment = self.root / "synthetic-manifest.json"
        deployment.write_text(canonical(pins))
        loss["manifest_sha256"] = hashlib.sha256(deployment.read_bytes()).hexdigest()
        token_path, loss_path = self.root / "token.json", self.root / "loss.json"
        token_path.write_text(canonical(tokenizer))
        loss_path.write_text(canonical(loss))
        return {"deployment": deployment, "tokenizer_report": token_path, "loss_report": loss_path}

    def test_fixture_counts_and_missing_gates_are_not_training_authority(self):
        before = {path: path.read_bytes() for path in self.dataset.rglob("*") if path.is_file()}
        report = readiness(self.dataset)
        self.assertEqual(report["records"], 2)
        self.assertEqual(report["real_records"], 0)
        self.assertFalse(report["training_ready"])
        for reason in ("empty_validation_split", "empty_test_split", "tokenizer_report_missing", "forward_loss_report_missing"):
            self.assertIn(reason, report["blockers"])
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_matching_report_pins_do_not_unlock_training_or_claim_dataset_bound_probe(self):
        report = readiness(self.dataset, **self.evidence())
        self.assertEqual(report["reported_tokenizer_binding"], "matches")
        self.assertEqual(report["reported_forward_loss_binding"], "matches")
        self.assertIn("dataset_bound_preflight_missing", report["blockers"])
        self.assertIn("training_authorization_missing", report["blockers"])
        self.assertFalse(report["training_ready"])

    def test_changed_pin_or_report_is_rejected(self):
        evidence = self.evidence()
        report = json.loads(evidence["tokenizer_report"].read_text())
        report["tokenizer_files"]["tokenizer.json"] = "f" * 64
        evidence["tokenizer_report"].write_text(canonical(report))
        with self.assertRaisesRegex(ValueError, "tokenizer_deployment_binding_differs"):
            readiness(self.dataset, **evidence)
        evidence = self.evidence()
        report = json.loads(evidence["loss_report"].read_text())
        report["manifest_sha256"] = "f" * 64
        evidence["loss_report"].write_text(canonical(report))
        with self.assertRaisesRegex(ValueError, "loss_deployment_binding_differs"):
            readiness(self.dataset, **evidence)
        with self.assertRaisesRegex(ValueError, "deployment_pin_required"):
            readiness(self.dataset, tokenizer_report=evidence["tokenizer_report"])

    def test_corrupt_dataset_is_not_a_readiness_report(self):
        path = self.dataset / "system1_choice/train.jsonl"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "dataset_file_mismatch"):
            readiness(self.dataset)

    def test_rehashed_semantically_changed_converter_is_rejected(self):
        path = self.dataset / "system1_choice/train.converted.jsonl"
        payload = json.loads(path.read_text())
        payload["qs"][0]["gold"] = (payload["qs"][0]["gold"] + 1) % len(payload["qs"][0]["options"])
        path.write_text(canonical(payload) + "\n")
        manifest_path = self.dataset / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["system1_choice/train.converted.jsonl"].update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        manifest["dataset_id"] = "fixture-" + digest({key: value for key, value in manifest.items() if key != "dataset_id"})
        manifest_path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError, "readiness_converter_mismatch"):
            readiness(self.dataset)

    def test_report_schema_and_cli_have_no_raw_content(self):
        report = readiness(self.dataset)
        for change in ({"training_ready": True}, {"real_records": 1}, {"raw_state": "not allowed"}):
            self.assertFalse(validator("dataset_readiness").is_valid({**report, **change}))
        result = subprocess.run([sys.executable, "-m", "aos.dataset_readiness", "--dataset", str(self.dataset)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), report)
        self.assertNotIn(str(self.dataset), result.stdout)
        self.assertNotIn("inventory@", result.stdout)


if __name__ == "__main__":
    unittest.main()

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT
from aos.dataset_tokenizer import converted_fixtures, tokenizer_preflight, validate_probe_report


class DatasetTokenizerTests(unittest.TestCase):
    def test_probe_is_opt_in_and_has_bounded_budget(self):
        absent = Path("not-a-local-artifact")
        with self.assertRaisesRegex(ValueError, "synthetic_opt_in_required"), patch("subprocess.run") as command:
            tokenizer_preflight(absent, absent, absent)
        command.assert_not_called()
        for budget in (True, 63, 1537):
            with self.assertRaisesRegex(ValueError, "tokenizer_budget_invalid"):
                tokenizer_preflight(absent, absent, absent, include_synthetic=True, max_tokens=budget)

    def test_converter_exposes_no_target_rationale_or_provenance(self):
        examples = converted_fixtures()
        self.assertEqual(len(examples), 4)
        for example in examples:
            self.assertEqual(set(example), {"context", "qs", "task"})
            self.assertEqual(set(example["qs"][0]), {"text", "options", "gold"})

    def test_schema_and_semantics_reject_training_claims_and_bad_counts(self):
        fixture = json.loads((REPO_ROOT / "examples/dataset_tokenizer_probe.json").read_text())
        self.assertTrue(fixture["synthetic"])
        report = fixture["report"]
        validate_probe_report(report, 4, 1536)
        for change in ({"training_ready": True}, {"weights_loaded": True}, {"loss_verified": True}, {"raw_input": "not allowed"},
                       {"variants": 40}, {"token_budget": 64}, {"max_padded_tokens": 32}, {"max_context_tokens": 70},
                       {"tokenizer_revision": "f" * 40}):
            with self.assertRaises(ValueError):
                validate_probe_report({**report, **change}, 4, 1536)

    def test_worker_rejects_changed_or_symlinked_artifacts(self):
        spec = importlib.util.spec_from_file_location("aos_tokenizer_probe_test", REPO_ROOT / "services/decider/tokenizer_probe.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "synthetic.json"
            path.write_text("synthetic tokenizer placeholder")
            checksum = worker.checksum(path)
            self.assertEqual(worker.checked_file(root, path.name, checksum), path)
            with self.assertRaises(ValueError):
                worker.checked_file(root, path.name, "0" * 64)
            link = root / "link.json"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                worker.checked_file(root, link.name, checksum)

    def test_cli_does_not_start_without_opt_in(self):
        command = [sys.executable, "-m", "aos.dataset_tokenizer", "--manifest", "missing", "--model-python", "missing", "--upstream-source", "missing"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")


@unittest.skipUnless(os.environ.get("AOS_TOKENIZER_TESTS") == "1", "Real local tokenizer probe requires AOS_TOKENIZER_TESTS=1")
class DatasetRealTokenizerTests(unittest.TestCase):
    def setUp(self):
        self.manifest = Path(os.environ.get("AOS_DECIDER_MANIFEST", REPO_ROOT / "models/decider-manifest.json"))
        self.python = Path(os.environ["AOS_MODEL_PYTHON"])
        self.source = Path(os.environ["AOS_DECIDER_DATA_SOURCE"])

    def test_real_pinned_tokenizer_and_collate_without_weights(self):
        first = tokenizer_preflight(self.manifest, self.python, self.source, include_synthetic=True)
        second = tokenizer_preflight(self.manifest, self.python, self.source, include_synthetic=True)
        self.assertEqual(first, second)
        self.assertEqual(first["variants"], 160)
        self.assertEqual(first["batches"], 40)
        self.assertFalse(first["weights_loaded"])
        self.assertFalse(first["cuda_initialized"])

    def test_context_truncation_is_rejected_not_silently_applied(self):
        examples = copy.deepcopy(converted_fixtures())
        examples[0]["context"] = "Synthetic long context. " * 3000
        with patch("aos.dataset_tokenizer.converted_fixtures", return_value=examples), self.assertRaisesRegex(ValueError, "tokenizer_preflight_failed"):
            tokenizer_preflight(self.manifest, self.python, self.source, include_synthetic=True, max_tokens=64)

    def test_changed_pin_is_rejected_without_model_loading(self):
        pins = json.loads(self.manifest.read_text())
        pins["model_files"]["tokenizer.json"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-invalid-manifest.json"
            path.write_text(json.dumps(pins))
            with self.assertRaisesRegex(ValueError, "tokenizer_preflight_failed"):
                tokenizer_preflight(path, self.python, self.source, include_synthetic=True)


if __name__ == "__main__":
    unittest.main()

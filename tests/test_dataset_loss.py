import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT
from aos.dataset_loss import loss_preflight, validate_loss_report


class DatasetLossTests(unittest.TestCase):
    def test_both_explicit_flags_required_before_subprocess(self):
        missing = Path("missing")
        for synthetic, forward in ((False, False), (True, False), (False, True)):
            with patch("subprocess.run") as command, self.assertRaises(ValueError):
                loss_preflight(missing, missing, missing, include_synthetic=synthetic, forward_only=forward)
            command.assert_not_called()

    def test_schema_semantics_and_nonfinite_metrics_fail_closed(self):
        fixture = json.loads((REPO_ROOT / "examples/dataset_loss_probe.json").read_text())
        self.assertTrue(fixture["synthetic"])
        report = fixture["report"]
        validate_loss_report(report)
        for change in ({"training_ready": True}, {"optimizer_run": True}, {"gradient_run": True}, {"parameters_unchanged": False},
                       {"raw_prediction": "not allowed"}, {"elapsed_seconds": float("nan")},
                       {"elapsed_seconds": float("inf")}, {"peak_vram_allocated_bytes": 2}):
            with self.assertRaises(ValueError):
                validate_loss_report({**report, **change})
        modified = copy.deepcopy(report)
        modified["layouts"][0]["mean_nll"] = float("nan")
        with self.assertRaises(ValueError):
            validate_loss_report(modified)
        with self.assertRaises(ValueError):
            validate_loss_report({**report, "layouts": list(reversed(report["layouts"]))})

    def test_cli_without_opt_in_does_not_load_model(self):
        command = [sys.executable, "-m", "aos.dataset_loss", "--manifest", "missing", "--model-python", "missing", "--upstream-source", "missing"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")


@unittest.skipUnless(os.environ.get("AOS_LOSS_TESTS") == "1", "Real model forward-only GPU probe requires AOS_LOSS_TESTS=1")
class DatasetRealLossTests(unittest.TestCase):
    def test_pinned_real_forward_finite_loss_without_parameter_updates(self):
        report = loss_preflight(Path(os.environ.get("AOS_DECIDER_MANIFEST", REPO_ROOT / "models/decider-manifest.json")),
                                Path(os.environ["AOS_MODEL_PYTHON"]), Path(os.environ["AOS_DECIDER_DATA_SOURCE"]),
                                include_synthetic=True, forward_only=True)
        self.assertEqual(sum(item["decisions"] for item in report["layouts"]), 8)
        self.assertTrue(report["parameters_unchanged"])
        self.assertFalse(report["gradient_run"])
        self.assertFalse(report["optimizer_run"])
        self.assertFalse(report["training_ready"])


if __name__ == "__main__":
    unittest.main()

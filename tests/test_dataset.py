import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset import (KINDS, SPLITS, assign_split, build_fixture_dataset, convert_record, curate,
                         leakage_groups, normalize_reviewed, select_record, validate_record, validator, verify_fixture_dataset)


def fixture_records():
    return [(kind, json.loads(line)) for kind in KINDS
            for line in (REPO_ROOT / f"examples/{kind}.jsonl").read_text().splitlines()]


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.records = fixture_records()
        self.reviews = {entry["sample_id"]: entry for entry in json.loads((REPO_ROOT / "examples/dataset_review.json").read_text())["reviews"]}
        self.evidence = {entry["id"]: entry for entry in json.loads((REPO_ROOT / "examples/evidence_catalog.json").read_text())["evidence"]}

    def test_build_is_reproducible_private_and_never_training_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            manifest = build_fixture_dataset(first, include_synthetic=True)
            self.assertEqual(manifest, build_fixture_dataset(second, include_synthetic=True))
            validator("dataset_manifest").validate(manifest)
            self.assertFalse(manifest["training_ready"])
            self.assertEqual(first.stat().st_mode & 0o777, 0o700)
            self.assertEqual(len(manifest["memberships"]), 2)
            self.assertEqual(manifest["dataset_id"], "fixture-" + digest({key: value for key, value in manifest.items() if key != "dataset_id"}))
            for name, info in manifest["files"].items():
                content = (first / name).read_bytes()
                self.assertEqual(content, (second / name).read_bytes())
                self.assertEqual(hashlib.sha256(content).hexdigest(), info["sha256"])
                self.assertEqual(len(content), info["bytes"])
                self.assertEqual(content.count(b"\n"), info["lines"])
                self.assertNotIn(b"inventory@fixture.invalid", content)
            for kind in KINDS:
                for split in SPLITS:
                    for line in (first / kind / (split + ".jsonl")).read_text().splitlines():
                        record = json.loads(line)
                        validate_record(kind, record)
                        membership = next(item for item in manifest["memberships"] if item["sample_id"] == record["sample_id"])
                        self.assertEqual(record["split_group"], "group-" + membership["group_sha256"])
                        self.assertEqual(digest(record), membership["record_sha256"])
            report = json.loads((first / "quality_report.json").read_text())
            self.assertEqual(report["excluded_counts"], {"benchmark_overlap": 4})
            self.assertEqual(report["input_records"], 6)
            self.assertEqual(report["real_records"], 0)
            self.assertEqual(verify_fixture_dataset(first), manifest)

    def test_verifier_rejects_tampering_and_unsafe_file_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dataset"
            manifest = build_fixture_dataset(output, include_synthetic=True)
            report = output / "quality_report.json"
            content = report.read_bytes()
            report.write_bytes(content + b" ")
            with self.assertRaisesRegex(ValueError, "dataset_file_mismatch"):
                verify_fixture_dataset(output)
            report.write_bytes(content)
            report.unlink()
            report.symlink_to(output / "quarantine.jsonl")
            with self.assertRaisesRegex(ValueError, "symlink_dataset_file"):
                verify_fixture_dataset(output)
            report.unlink()
            report.write_bytes(content)
            manifest["files"]["../outside"] = manifest["files"].pop("quality_report.json")
            manifest["dataset_id"] = "fixture-" + digest({key: value for key, value in manifest.items() if key != "dataset_id"})
            (output / "manifest.json").write_text(canonical(manifest))
            with self.assertRaisesRegex(ValueError, "unexpected_dataset_files"):
                verify_fixture_dataset(output)
            manifest["seed"] = 43
            (output / "manifest.json").write_text(canonical(manifest))
            with self.assertRaisesRegex(ValueError, "invalid_manifest"):
                verify_fixture_dataset(output)

    def test_opt_in_existing_directory_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dataset"
            with self.assertRaisesRegex(ValueError, "synthetic_requires_explicit_opt_in"):
                build_fixture_dataset(output)
            self.assertFalse(output.exists())
            build_fixture_dataset(output, include_synthetic=True)
            original = (output / "manifest.json").read_bytes()
            with self.assertRaises(FileExistsError):
                build_fixture_dataset(output, include_synthetic=True)
            self.assertEqual((output / "manifest.json").read_bytes(), original)
            link = Path(directory) / "link"
            link.symlink_to(output, target_is_directory=True)
            with self.assertRaises(FileExistsError):
                build_fixture_dataset(link, include_synthetic=True)
            self.assertEqual((output / "manifest.json").read_bytes(), original)

    def test_failed_publication_removes_only_new_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dataset"
            sibling = Path(directory) / "existing.txt"
            sibling.write_text("keep")
            with patch("aos.dataset.os.fsync", side_effect=OSError("simulated disk failure")):
                with self.assertRaises(OSError):
                    build_fixture_dataset(output, include_synthetic=True)
            self.assertFalse(output.exists())
            self.assertEqual(sibling.read_text(), "keep")

    def test_modified_content_is_quarantined_without_raw_payload(self):
        record = copy.deepcopy(self.records[0][1])
        record["state"] = "Authorization: Bearer synthetic-test-secret"
        retained, memberships, quarantine = curate([("system1_choice", record)], self.reviews, self.evidence, set(), 42)
        self.assertEqual((retained, memberships), ([], []))
        self.assertEqual(quarantine, [{"source_sha256": digest(record), "reason": "unreviewed_content"}])
        self.assertNotIn("synthetic-test-secret", canonical(quarantine))
        record["sample_id"] = "sensitive-id"
        self.assertNotIn("sensitive-id", canonical(curate([("system1_choice", record)], {}, self.evidence, set(), 42)))

    def test_selection_denies_real_unknown_rights_and_unreviewed_records(self):
        for field, value, reason in [("synthetic", False, "real_data_not_supported"),
                                     ("usage_rights", "unknown", "unknown_usage_rights"),
                                     ("redaction_version", "unknown", "unknown_redaction_version")]:
            with self.subTest(field=field):
                record = copy.deepcopy(self.records[0][1])
                record["provenance"][field] = value
                review = {**self.reviews[record["sample_id"]], "record_sha256": digest(record)}
                with self.assertRaisesRegex(ValueError, reason):
                    select_record("system1_choice", record, review, self.evidence)
        record = self.records[0][1]
        for status in ("pending", "rejected"):
            with self.assertRaisesRegex(ValueError, "review_not_accepted"):
                select_record("system1_choice", record, {**self.reviews[record["sample_id"]], "review_status": status}, self.evidence)

    def test_verification_must_match_run_step_outcome_and_policy_review(self):
        record = self.records[2][1]
        for field, value in [("run_id", "wrong-run"), ("step_id", "wrong-step"), ("result", "failed"), ("method", "model_self_review")]:
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                evidence[record["provenance"]["verification_ref"]][field] = value
                with self.assertRaisesRegex(ValueError, "invalid_verification"):
                    select_record("system1_choice", record, self.reviews[record["sample_id"]], evidence)
        with self.assertRaisesRegex(ValueError, "invalid_verification"):
            select_record("system1_choice", record, self.reviews[record["sample_id"]], {})

    def test_semantic_and_schema_failures_are_rejected(self):
        source = self.records[0][1]
        changes = [lambda record: record["target"].update(correct_option="absent"),
                   lambda record: record["prediction"]["probabilities"].update(write_file=0.1),
                   lambda record: record["prediction"]["probabilities"].update(write_file=float("nan")),
                   lambda record: record["prediction"]["probabilities"].update(write_file=True),
                   lambda record: record["options"][1].update(id="write_file"),
                   lambda record: record.update(created_at="not-a-date"),
                   lambda record: record.update(chain_of_thought="not allowed")]
        for change in changes:
            record = copy.deepcopy(source)
            change(record)
            with self.assertRaises(ValueError):
                validate_record("system1_choice", record)
        supervisor = copy.deepcopy(self.records[-1][1])
        supervisor["target"]["needs_human"] = False
        with self.assertRaisesRegex(ValueError, "invalid_supervisor"):
            validate_record("system2_supervisor", supervisor)

    def test_normalization_preserves_order_filenames_and_only_masks_reviewed_literal(self):
        value = {"state": " /workspace/İçerik.txt\r\n  inventory@fixture.invalid ", "options": ["second", "first"]}
        normalized = normalize_reviewed(value)
        self.assertEqual(normalized, {"state": " /workspace/İçerik.txt\n  [REDACTED_EMAIL] ", "options": ["second", "first"]})
        self.assertIn("inventory@fixture.invalid", value["state"])

    def test_corrected_gold_not_prediction_and_no_target_leakage(self):
        record = self.records[1][1]
        converted = convert_record("system1_choice", record, "fixture")
        self.assertEqual(converted["qs"][0]["gold"], 1)
        self.assertEqual(converted["qs"][0]["options"], [option["label"] for option in record["options"]])
        self.assertEqual(set(converted), {"context", "qs", "task"})
        self.assertNotIn(record["target"]["rationale"], converted["context"])
        for index in range(2):
            reordered = copy.deepcopy(record)
            if index:
                reordered["options"].reverse()
            question = convert_record("system1_choice", reordered, "fixture")["qs"][0]
            self.assertEqual(question["options"][question["gold"]], "Request diagnosis and a revised plan")
        supervisor = self.records[-1][1]
        pair = convert_record("system2_supervisor", supervisor, "fixture")
        self.assertEqual(pair["target"], supervisor["target"])
        self.assertEqual(set(pair["prompt"]), {"problem", "evidence", "failed_trajectory"})
        self.assertNotIn(supervisor["target"]["diagnosis"], canonical(pair["prompt"]))

    def test_group_split_unions_run_family_cluster_and_duplicate_inputs(self):
        base = self.records[0][1]
        items = []
        for index in range(5):
            record = copy.deepcopy(base)
            record.update(sample_id=f"sample-{index}", split_group=f"group-{index}", state=f"state-{index}")
            record["provenance"]["run_id"] = f"run-{index}"
            review = {"task_family": f"family-{index}", "near_duplicate_cluster": f"cluster-{index}"}
            items.append({"kind": "system1_choice", "record": record, "review": review})
        items[1]["record"]["provenance"]["run_id"] = "run-0"
        items[2]["review"]["task_family"] = "family-1"
        items[3]["review"]["near_duplicate_cluster"] = "cluster-2"
        items[4]["record"]["state"] = "state-3"
        groups = leakage_groups(items)
        self.assertEqual(len(set(groups.values())), 1)
        self.assertEqual(groups, leakage_groups(list(reversed(items))))
        self.assertEqual(len({assign_split(group, 42) for group in groups.values()}), 1)
        self.assertEqual({assign_split(f"group-{index}", 42) for index in range(100)}, set(SPLITS))

    def test_benchmark_overlap_propagates_across_linked_systems(self):
        reviews = copy.deepcopy(self.reviews)
        reviews["s2-recovery"]["task_family"] = "different-family"
        retained, memberships, quarantine = curate(self.records, reviews, self.evidence, {"recovery-path"}, 42)
        self.assertEqual(len(retained), 4)
        self.assertEqual(sum(item["reason"] == "benchmark_overlap" for item in quarantine), 2)
        self.assertFalse({"s1-escalate", "s2-recovery"} & {item["sample_id"] for item in memberships})
        self.assertEqual((retained, memberships, quarantine), curate(list(reversed(self.records)), reviews, self.evidence, {"recovery-path"}, 42))

    def test_exact_duplicates_keep_one_and_conflicting_labels_fail_closed(self):
        record = copy.deepcopy(self.records[0][1])
        duplicate = copy.deepcopy(record)
        duplicate["sample_id"] = "s1-copy"
        reviews = copy.deepcopy(self.reviews)
        reviews["s1-copy"] = {**reviews[record["sample_id"]], "sample_id": "s1-copy", "record_sha256": digest(duplicate)}
        records = [("system1_choice", item) for item in (record, duplicate)]
        retained, memberships, quarantine = curate(records, reviews, self.evidence, set(), 42)
        self.assertEqual(len(retained), 1)
        self.assertEqual(quarantine[0]["reason"], "duplicate")
        duplicate["target"]["correct_option"] = "ask_supervisor"
        reviews["s1-copy"]["record_sha256"] = digest(duplicate)
        with self.assertRaisesRegex(ValueError, "conflicting_labels"):
            curate(records, reviews, self.evidence, set(), 42)
        with self.assertRaisesRegex(ValueError, "duplicate_sample_id"):
            curate([("system1_choice", record)] * 2, reviews, self.evidence, set(), 42)

    def test_cli_default_denies_and_explicit_build_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            command = [sys.executable, "-m", "aos.dataset", "--output", str(output)]
            denied = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse(output.exists())
            completed = subprocess.run(command + ["--include-synthetic"], capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(json.loads(completed.stdout)["training_ready"])


@unittest.skipUnless(os.environ.get("AOS_DATASET_UPSTREAM_TESTS") == "1", "Pinned upstream converter probe is opt-in")
class DatasetUpstreamTests(unittest.TestCase):
    def test_pinned_example_question_and_prompt_shuffle(self):
        root = Path(os.environ["AOS_DECIDER_DATA_SOURCE"])
        expected = json.loads((REPO_ROOT / "examples/dataset_converter_pin.json").read_text())
        modules = {}
        for name, sha256 in expected["files"].items():
            path = root / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha256)
            module_name = "aos_probe_" + path.stem
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            self.addCleanup(sys.modules.pop, module_name, None)
            spec.loader.exec_module(module)
            modules[path.stem] = module

        class ByteTokenizer:
            def encode(self, text, add_special_tokens=False):
                return list(text.encode())

        for kind, record in fixture_records():
            if kind != "system1_choice":
                continue
            payload = convert_record(kind, record, "aos-fixture")
            questions = [modules["core"].Q(**question) for question in payload["qs"]]
            example = modules["core"].Example(context=payload["context"], qs=questions, task=payload["task"])
            self.assertIsNone(example.image)
            for seed in range(20):
                for layout in ("state_first", "schema_first"):
                    item = modules["prompt"].build(example, ByteTokenizer(), rng=random.Random(seed), layout=layout)
                    original_index = item["perms"][0][item["golds"][0]]
                    self.assertEqual(record["options"][original_index]["id"], record["target"]["correct_option"])


if __name__ == "__main__":
    unittest.main()

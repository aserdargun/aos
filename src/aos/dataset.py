import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import shutil

import jsonschema

from .contracts import REPO_ROOT, canonical, digest


KINDS = ("system1_choice", "system2_supervisor")
SPLITS = ("train", "validation", "test")
REDACTION_VERSION = "reviewed-fixture-v1"
DECIDER_REVISION = "75b00fade2dd7f353106e3f4683e56fa2481ec28"


@lru_cache
def validator(name: str):
    schema = json.loads((REPO_ROOT / "schemas" / (name + ".schema.json")).read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validate_record(kind: str, record: dict) -> None:
    if kind not in KINDS:
        raise ValueError("unknown_record_kind")
    if not validator(kind).is_valid(record):
        raise ValueError("invalid_schema")
    if kind == "system1_choice":
        options = [option["id"] for option in record["options"]]
        probabilities = record["prediction"]["probabilities"]
        if (len(set(options)) != len(options) or set(probabilities) != set(options)
                or any(isinstance(value, bool) or not math.isfinite(value) for value in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > 1e-6
                or record["prediction"]["selected_option"] not in options
                or record["target"]["correct_option"] not in options):
            raise ValueError("invalid_choice")
    else:
        target = record["target"]
        if ([item["order"] for item in target["corrected_plan"]] != list(range(1, len(target["corrected_plan"]) + 1))
                or (target["outcome"] == "human_required") != target["needs_human"]
                or len({item["id"] for item in record["evidence"]}) != len(record["evidence"])
                or record["provenance"]["step_id"] not in {item["step_id"] for item in record["failed_trajectory"]}):
            raise ValueError("invalid_supervisor")


def model_input(kind: str, record: dict) -> dict:
    fields = ("state", "question", "options") if kind == "system1_choice" else ("problem", "evidence", "failed_trajectory")
    return {field: record[field] for field in fields}


def convert_record(kind: str, record: dict, family: str) -> dict:
    validate_record(kind, record)
    if kind == "system1_choice":
        options = record["options"]
        return {"context": record["state"], "qs": [{"text": record["question"],
                "options": [option["label"] for option in options],
                "gold": [option["id"] for option in options].index(record["target"]["correct_option"])}], "task": family}
    return {"format": "aos-supervisor-pair-v1", "prompt": model_input(kind, record), "target": record["target"]}


def normalize_reviewed(value):
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("inventory@fixture.invalid", "[REDACTED_EMAIL]")
    if isinstance(value, list):
        return [normalize_reviewed(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_reviewed(item) for key, item in value.items()}
    return value


def select_record(kind: str, record: dict, review: dict | None, evidence: dict) -> dict:
    validate_record(kind, record)
    if review is None or review["record_sha256"] != digest(record):
        raise ValueError("unreviewed_content")
    if review["review_status"] != "accepted":
        raise ValueError("review_not_accepted")
    provenance = record["provenance"]
    if provenance["synthetic"] is not True:
        raise ValueError("real_data_not_supported")
    if provenance["usage_rights"] != "synthetic fixture authored for AOS":
        raise ValueError("unknown_usage_rights")
    if provenance["redaction_version"] != "fixture-v1":
        raise ValueError("unknown_redaction_version")
    verification = evidence.get(provenance["verification_ref"])
    if (verification is None or verification["run_id"] != provenance["run_id"]
            or verification["step_id"] != provenance["step_id"]
            or verification["result"] != record["target"]["outcome"]
            or (record["target"]["outcome"] in {"policy_reviewed", "human_required"}
                and (provenance["source"] != "human_review" or verification["method"] != "human_review"))):
        raise ValueError("invalid_verification")
    normalized = normalize_reviewed(record)
    normalized["provenance"]["redaction_version"] = REDACTION_VERSION
    validate_record(kind, normalized)
    return normalized


def leakage_groups(items: list[dict]) -> dict[str, str]:
    parents = {item["record"]["sample_id"]: item["record"]["sample_id"] for item in items}

    def root(sample):
        while parents[sample] != sample:
            parents[sample] = parents[parents[sample]]
            sample = parents[sample]
        return sample

    owners = {}
    keys_by_sample = {}
    for item in sorted(items, key=lambda entry: entry["record"]["sample_id"]):
        record, review = item["record"], item["review"]
        sample = record["sample_id"]
        keys = [("run", record["provenance"]["run_id"]), ("group", record["split_group"]),
                ("family", review["task_family"]), ("cluster", review["near_duplicate_cluster"]),
                ("input", digest({"kind": item["kind"], "input": model_input(item["kind"], record)}))]
        keys_by_sample[sample] = keys
        for key in keys:
            if key in owners:
                first, second = sorted((root(sample), root(owners[key])))
                parents[second] = first
            owners[key] = sample
    components = {}
    for sample, keys in keys_by_sample.items():
        components.setdefault(root(sample), set()).update(keys)
    return {sample: digest(sorted(components[root(sample)])) for sample in parents}


def assign_split(group: str, seed: int) -> str:
    bucket = int(digest({"group": group, "seed": seed})[:16], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def curate(records: list[tuple[str, dict]], reviews: dict, evidence: dict, benchmark_families: set[str], seed: int) -> tuple:
    accepted, quarantine = [], []
    sample_ids = set()
    for kind, record in records:
        source_hash = digest(record)
        try:
            review = reviews.get(record.get("sample_id")) if isinstance(record, dict) else None
            normalized = select_record(kind, record, review, evidence)
        except ValueError as error:
            quarantine.append({"source_sha256": source_hash, "reason": str(error)})
            continue
        if normalized["sample_id"] in sample_ids:
            raise ValueError("duplicate_sample_id")
        sample_ids.add(normalized["sample_id"])
        accepted.append({"kind": kind, "record": normalized, "review": review, "source_sha256": source_hash})
    groups = leakage_groups(accepted)
    blocked_groups = {groups[item["record"]["sample_id"]] for item in accepted if item["review"]["task_family"] in benchmark_families}
    seen, retained, memberships = {}, [], []
    for item in sorted(accepted, key=lambda entry: entry["record"]["sample_id"]):
        record = item["record"]
        group = groups[record["sample_id"]]
        input_hash = digest({"kind": item["kind"], "input": model_input(item["kind"], record)})
        target_hash = digest(record["target"])
        if group in blocked_groups:
            quarantine.append({"source_sha256": item["source_sha256"], "reason": "benchmark_overlap"})
            continue
        if input_hash in seen:
            if seen[input_hash] != target_hash:
                raise ValueError("conflicting_labels")
            quarantine.append({"source_sha256": item["source_sha256"], "reason": "duplicate"})
            continue
        seen[input_hash] = target_hash
        split = assign_split(group, seed)
        record = {**record, "split_group": "group-" + group}
        retained.append({**item, "record": record, "split": split})
        memberships.append({"sample_id": record["sample_id"], "kind": item["kind"], "split": split,
                            "group_sha256": group, "source_sha256": item["source_sha256"],
                            "record_sha256": digest(record)})
    return retained, memberships, sorted(quarantine, key=canonical)


def build_fixture_dataset(output: Path, *, include_synthetic: bool = False) -> dict:
    if not include_synthetic:
        raise ValueError("synthetic_requires_explicit_opt_in")
    paths = [f"examples/{kind}.jsonl" for kind in KINDS] + ["examples/evidence_catalog.json",
             "examples/dataset_review.json", "examples/dataset_converter_pin.json", "benchmarks/tasks.json",
             "training/recipes/fixture-dataset-v001.json"]
    sources = {name: (REPO_ROOT / name).read_bytes() for name in paths}
    if json.loads(sources["examples/dataset_converter_pin.json"])["revision"] != DECIDER_REVISION:
        raise ValueError("converter_revision_mismatch")
    recipe = json.loads(sources[paths[-1]])
    if recipe != {"schema_version": "1.0", "recipe_id": "fixture-dataset-v001", "mode": "synthetic_smoke_only",
                  "seed": 42, "split": {"train": 0.8, "validation": 0.1, "test": 0.1}, "training_ready": False}:
        raise ValueError("unsupported_recipe")
    review_catalog = json.loads(sources["examples/dataset_review.json"])
    if not validator("dataset_review").is_valid(review_catalog):
        raise ValueError("invalid_review_catalog")
    reviews = {entry["sample_id"]: entry for entry in review_catalog["reviews"]}
    if len(reviews) != len(review_catalog["reviews"]):
        raise ValueError("duplicate_review_id")
    catalog = json.loads(sources["examples/evidence_catalog.json"])
    evidence = {entry["id"]: entry for entry in catalog["evidence"]}
    if catalog["synthetic"] is not True or len(evidence) != len(catalog["evidence"]):
        raise ValueError("invalid_evidence_catalog")
    records = [(kind, json.loads(line)) for kind in KINDS
               for line in sources[f"examples/{kind}.jsonl"].decode().splitlines() if line.strip()]
    families = {entry["id"] for entry in json.loads(sources["benchmarks/tasks.json"])["tasks"]}
    retained, memberships, quarantine = curate(records, reviews, evidence, families, recipe["seed"])
    files = {}
    for kind in KINDS:
        for split in SPLITS:
            selected = [item for item in retained if item["kind"] == kind and item["split"] == split]
            files[f"{kind}/{split}.jsonl"] = "".join(canonical(item["record"]) + "\n" for item in selected).encode()
            files[f"{kind}/{split}.converted.jsonl"] = "".join(
                canonical(convert_record(kind, item["record"], item["review"]["task_family"])) + "\n" for item in selected).encode()
    files["quarantine.jsonl"] = "".join(canonical(item) + "\n" for item in quarantine).encode()
    report = {"input_records": len(records), "retained_records": len(retained),
              "excluded_counts": dict(sorted(Counter(item["reason"] for item in quarantine).items())),
              "training_ready": False, "real_records": 0,
              "limitations": ["synthetic_only", "insufficient_data", "reviewed_fixture_redaction_only",
                              "manual_near_duplicate_clusters", "no_tokenizer_or_loss_validation", "no_training_or_promotion"],
              "split_counts": {kind: {split: sum(item["kind"] == kind and item["split"] == split for item in retained)
                                      for split in SPLITS} for kind in KINDS}}
    files["quality_report.json"] = (canonical(report) + "\n").encode()
    manifest = {"schema_version": "1.0", "recipe_id": recipe["recipe_id"], "synthetic": True,
                "training_ready": False, "seed": recipe["seed"], "redaction_version": REDACTION_VERSION,
                "group_algorithm": "connected-run-family-reviewed-cluster-input-v1",
                "split_algorithm": "sha256-mod100-80-10-10-v1", "memberships": memberships,
                "sources": {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()},
                "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "schemas": {kind: hashlib.sha256((REPO_ROOT / "schemas" / (kind + ".schema.json")).read_bytes()).hexdigest()
                            for kind in (*KINDS, "dataset_review", "dataset_manifest")},
                "converters": {"system1_choice": "decider-Example-Q-" + DECIDER_REVISION,
                               "system2_supervisor": "aos-supervisor-pair-v1-not-tokenized"},
                "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                                  "lines": content.count(b"\n")} for name, content in sorted(files.items())}}
    manifest["dataset_id"] = "fixture-" + digest(manifest)
    if not validator("dataset_manifest").is_valid(manifest):
        raise ValueError("invalid_manifest")
    output = Path(output)
    output.mkdir(mode=0o700)
    try:
        for name, content in files.items():
            destination = output / name
            destination.parent.mkdir(exist_ok=True, mode=0o700)
            with destination.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        temporary = output / ".manifest.json.tmp"
        with temporary.open("xb") as stream:
            stream.write((canonical(manifest) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(output / "manifest.json")
    except BaseException:
        shutil.rmtree(output)
        raise
    return manifest


def verify_fixture_dataset(output: Path) -> dict:
    output = Path(output)
    if output.is_symlink() or (output / "manifest.json").is_symlink():
        raise ValueError("symlink_dataset")
    manifest = json.loads((output / "manifest.json").read_text())
    if not validator("dataset_manifest").is_valid(manifest):
        raise ValueError("invalid_manifest")
    if manifest["dataset_id"] != "fixture-" + digest({key: value for key, value in manifest.items() if key != "dataset_id"}):
        raise ValueError("manifest_hash_mismatch")
    expected = {f"{kind}/{split}{suffix}.jsonl" for kind in KINDS for split in SPLITS for suffix in ("", ".converted")}
    expected.update({"quarantine.jsonl", "quality_report.json"})
    if set(manifest["files"]) != expected:
        raise ValueError("unexpected_dataset_files")
    for name, info in manifest["files"].items():
        path = output / name
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("symlink_dataset_file")
        content = path.read_bytes()
        if (hashlib.sha256(content).hexdigest() != info["sha256"] or len(content) != info["bytes"]
                or content.count(b"\n") != info["lines"]):
            raise ValueError("dataset_file_mismatch")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS offline sentetik dataset provası; eğitim çalıştırmaz")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument("--verify", type=Path)
    parser.add_argument("--include-synthetic", action="store_true")
    arguments = parser.parse_args()
    try:
        manifest = (verify_fixture_dataset(arguments.verify) if arguments.verify else
                    build_fixture_dataset(arguments.output, include_synthetic=arguments.include_synthetic))
    except (ValueError, OSError):
        parser.exit(1, "Dataset oluşturulamadı; fixture sözleşmesini, opt-in ve yeni çıktı dizinini kontrol edin.\n")
    print(canonical({"dataset_id": manifest["dataset_id"], "records": len(manifest["memberships"]), "training_ready": False}))


if __name__ == "__main__":
    main()

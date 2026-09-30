import argparse
import hashlib
from pathlib import Path

from .contracts import canonical, digest
from .dataset import KINDS, SPLITS, convert_record, validate_record, validator, verify_fixture_dataset
from .dataset_loss import validate_loss_report
from .dataset_reviewer import decode_json
from .dataset_tokenizer import validate_probe_report


def small_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("readiness_file_unavailable")
    with path.open("rb") as stream:
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("readiness_file_size_limit")
    return decode_json(payload)


def readiness(dataset: Path, *, deployment: Path | None = None, tokenizer_report: Path | None = None,
              loss_report: Path | None = None, dataset_tokenizer_report: Path | None = None,
              dataset_loss_report: Path | None = None) -> dict:
    manifest = verify_fixture_dataset(dataset)
    counts = {kind: {split: 0 for split in SPLITS} for kind in KINDS}
    memberships = {item["sample_id"]: item for item in manifest["memberships"]}
    if len(memberships) != len(manifest["memberships"]):
        raise ValueError("readiness_membership_conflict")
    seen = set()
    for kind in KINDS:
        for split in SPLITS:
            records = [decode_json(line.encode()) for line in (dataset / kind / (split + ".jsonl")).read_text().splitlines()]
            converted = [decode_json(line.encode()) for line in (dataset / kind / (split + ".converted.jsonl")).read_text().splitlines()]
            if len(records) != len(converted):
                raise ValueError("readiness_converter_count_mismatch")
            for record, payload in zip(records, converted):
                validate_record(kind, record)
                membership = memberships.get(record["sample_id"])
                if (record["provenance"]["synthetic"] is not True or record["sample_id"] in seen or membership is None
                        or membership["kind"] != kind or membership["split"] != split or membership["record_sha256"] != digest(record)):
                    raise ValueError("readiness_membership_mismatch")
                family = payload.get("task", "") if kind == "system1_choice" else ""
                if payload != convert_record(kind, record, family):
                    raise ValueError("readiness_converter_mismatch")
                seen.add(record["sample_id"])
            counts[kind][split] = len(records)
    quality = small_json(dataset / "quality_report.json")
    if (seen != set(memberships) or quality["split_counts"] != counts or quality["real_records"] != 0
            or quality["training_ready"] is not False or quality["retained_records"] != len(seen)):
        raise ValueError("readiness_quality_mismatch")
    report = {"schema_version": "1.0", "mode": "fixture_readiness_v1", "dataset_id": manifest["dataset_id"],
              "dataset_integrity_verified": True, "training_ready": False, "real_records": 0, "records": len(seen),
              "split_counts": counts, "deployment_manifest_sha256": None, "reported_tokenizer_binding": "missing",
              "reported_forward_loss_binding": "missing", "reported_dataset_tokenizer_binding": "missing",
              "reported_dataset_loss_binding": "missing", "blockers": []}
    blockers = {"synthetic_dataset_only", "dataset_bound_preflight_missing", "general_redaction_unverified",
                "real_source_review_pipeline_missing", "licensed_replay_missing", "gradient_optimizer_unverified",
                "training_authorization_missing", "promotion_unavailable"}
    for split in ("validation", "test"):
        if any(counts[kind][split] == 0 for kind in KINDS):
            blockers.add("empty_" + split + "_split")
    if tokenizer_report is not None or loss_report is not None:
        if deployment is None:
            raise ValueError("deployment_pin_required")
        pins = small_json(deployment)
        report["deployment_manifest_sha256"] = hashlib.sha256(deployment.read_bytes()).hexdigest()
        if tokenizer_report is not None:
            tokenizer = small_json(tokenizer_report)
            validate_probe_report(tokenizer, 4, 1536)
            if (any(tokenizer[key] != pins[key] for key in ("checkpoint_revision", "tokenizer_revision", "code_revision"))
                    or any(pins["model_files"].get(key) != value for key, value in tokenizer["tokenizer_files"].items())
                    or any(pins["code_files"].get(key) != value for key, value in tokenizer["code_files"].items())
                    or any(pins["dependencies"].get(key) != value for key, value in tokenizer["dependencies"].items())):
                raise ValueError("tokenizer_deployment_binding_differs")
            report["reported_tokenizer_binding"] = "matches"
        if loss_report is not None:
            loss = small_json(loss_report)
            validate_loss_report(loss)
            if (loss["manifest_sha256"] != report["deployment_manifest_sha256"] or loss["checkpoint_revision"] != pins["checkpoint_revision"]
                    or loss["code_revision"] != pins["code_revision"] or loss["weights_sha256"] != pins["model_files"]["model.safetensors"]):
                raise ValueError("loss_deployment_binding_differs")
            report["reported_forward_loss_binding"] = "matches"
    if report["reported_tokenizer_binding"] == "missing":
        blockers.add("tokenizer_report_missing")
    if report["reported_forward_loss_binding"] == "missing":
        blockers.add("forward_loss_report_missing")
    if dataset_tokenizer_report is not None:
        from .dataset_preflight import verify_dataset_tokenizer_report

        if deployment is None:
            raise ValueError("deployment_pin_required")
        verified = verify_dataset_tokenizer_report(small_json(dataset_tokenizer_report), dataset, deployment)
        if (verified.dataset_id != manifest['dataset_id']
                or verified.split_counts != counts['system1_choice']
                or report['deployment_manifest_sha256'] not in {None, verified.deployment_manifest_sha256}):
            raise ValueError('readiness_dataset_preflight_changed')
        report["deployment_manifest_sha256"] = verified.deployment_manifest_sha256
        report["reported_dataset_tokenizer_binding"] = "matches"
        blockers.discard("tokenizer_report_missing")
        blockers.discard("dataset_bound_preflight_missing")
        blockers.update({"dataset_bound_forward_loss_missing", "supervisor_tokenizer_missing"})
    if dataset_loss_report is not None:
        from .dataset_bound_loss import verify_dataset_loss_report

        if deployment is None:
            raise ValueError('deployment_pin_required')
        verified_loss = verify_dataset_loss_report(small_json(dataset_loss_report), dataset, deployment)
        if (verified_loss.dataset_id != manifest['dataset_id']
                or verified_loss.split_counts != counts['system1_choice']
                or report['deployment_manifest_sha256'] not in {None, verified_loss.deployment_manifest_sha256}):
            raise ValueError('readiness_dataset_loss_changed')
        report['deployment_manifest_sha256'] = verified_loss.deployment_manifest_sha256
        report['reported_dataset_loss_binding'] = 'matches'
        blockers.discard('forward_loss_report_missing')
        blockers.discard('dataset_bound_forward_loss_missing')
        blockers.add('supervisor_tokenizer_missing')
    report["blockers"] = sorted(blockers)
    if not validator("dataset_readiness").is_valid(report):
        raise ValueError("readiness_report_invalid")
    return report


def main():
    parser = argparse.ArgumentParser(description="Offline fixture readiness; eğitim veya izin üretmez")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--deployment-manifest", type=Path)
    parser.add_argument("--tokenizer-report", type=Path)
    parser.add_argument("--loss-report", type=Path)
    parser.add_argument("--dataset-tokenizer-report", type=Path)
    parser.add_argument("--dataset-loss-report", type=Path)
    arguments = parser.parse_args()
    try:
        report = readiness(arguments.dataset, deployment=arguments.deployment_manifest,
                           tokenizer_report=arguments.tokenizer_report, loss_report=arguments.loss_report,
                           dataset_tokenizer_report=arguments.dataset_tokenizer_report,
                           dataset_loss_report=arguments.dataset_loss_report)
        print(canonical(report))
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        parser.exit(1, "Readiness denetimi tamamlanamadı; dataset bütünlüğü ve rapor/pin bağlarını kontrol edin.\n")


if __name__ == "__main__":
    main()

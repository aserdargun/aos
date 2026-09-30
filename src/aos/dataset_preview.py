import argparse
import json
from pathlib import Path
import sqlite3

from .contracts import Option, Phase, State, canonical, digest
from .dataset import validate_record, validator
from .dataset_audit import audit_snapshot, valid_choice_label
from .dataset_reviews import _stored_receipt, source_fingerprint
from .decision import decision_request


MAX_CANDIDATE_BYTES = 1024 * 1024
REDACTION_UNREVIEWED = "preview-unreviewed-v1"
SUPPORTED_POLICIES = {"hello-policy-v1": "hello", "browser-form-policy-v1": "browser_form"}


class PreviewBlocked(ValueError):
    pass


def derive_choice(connection: sqlite3.Connection, run: sqlite3.Row, label: sqlite3.Row) -> dict:
    if label["review_status"] != "accepted":
        raise PreviewBlocked("label_not_accepted")
    if label["label_type"] != "choice":
        raise PreviewBlocked("label_kind_unsupported")
    if label["run_id"] != run["run_id"] or not valid_choice_label(connection, label):
        raise PreviewBlocked("label_evidence_invalid")
    target = json.loads(label["label_json"])
    if set(target) != {"correct_option", "outcome", "rationale"} or target["outcome"] != "passed":
        raise PreviewBlocked("label_target_incomplete")
    decision = connection.execute("SELECT * FROM decisions WHERE decision_id=?", (label["decision_id"],)).fetchone()
    snapshot = connection.execute("SELECT * FROM state_snapshots WHERE snapshot_id=? AND run_id=? AND step_id=?",
                                  (decision["snapshot_id"], run["run_id"], label["step_id"])).fetchone()
    if snapshot is None:
        raise PreviewBlocked("decision_snapshot_invalid")
    state = State.model_validate_json(snapshot["state_json"])
    if (snapshot["content_sha256"] != digest(state.model_dump(mode="json")) or state.phase != Phase.DECIDE
            or state.run_id != run["run_id"] or state.task_id != run["task_id"] or state.step_id != label["step_id"]
            or state.state_version != snapshot["state_version"] or state.task_kind != SUPPORTED_POLICIES[run["policy_version"]]):
        raise PreviewBlocked("decision_snapshot_invalid")
    options = [Option.model_validate(item) for item in json.loads(decision["options_json"])]
    request = decision_request(state, options)
    if decision["question"] != request["question"]:
        raise PreviewBlocked("decision_question_differs")
    prediction = {"selected_option": decision["selected_option"], "probabilities": json.loads(decision["probabilities_json"])}
    if decision["call_id"] is not None:
        call = connection.execute("SELECT * FROM model_calls WHERE call_id=? AND run_id=? AND step_id=?",
                                  (decision["call_id"], run["run_id"], label["step_id"])).fetchone()
        if (call is None or call["status"] != "ok" or call["role"] != "system1" or call["deployment_id"] != state.deployment_id
                or canonical(json.loads(call["request_json"])) != canonical(request)):
            raise PreviewBlocked("model_request_differs")
        if canonical(json.loads(call["response_json"])) != canonical(prediction):
            raise PreviewBlocked("model_prediction_differs")
    elif json.loads(run["deployment_snapshot_json"]).get("real_model") is not False:
        raise PreviewBlocked("model_call_missing")
    candidate = {"schema_version": "1.0", "sample_id": "candidate-" + digest({"run_id": run["run_id"], "label_id": label["label_id"]}),
                 "created_at": label["created_at"], "split_group": run["run_id"],
                 "provenance": {"run_id": run["run_id"], "step_id": label["step_id"], "source": label["source"],
                                "verification_ref": label["verification_id"], "synthetic": True,
                                "redaction_version": REDACTION_UNREVIEWED, "usage_rights": "unreviewed"},
                 **request, "prediction": prediction, "target": target}
    validate_record("system1_choice", candidate)
    return candidate


def receipt_preflight(connection: sqlite3.Connection, receipt_id: str, candidate: dict, source_hash: str) -> str:
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version=8").fetchone() is None:
        return "unavailable_v7"
    row = connection.execute("SELECT * FROM dataset_reviews WHERE receipt_id=?", (receipt_id,)).fetchone()
    if row is None:
        return "missing"
    receipt = _stored_receipt(row)
    revoked = connection.execute("SELECT * FROM dataset_reviews WHERE revokes=?", (receipt_id,)).fetchone()
    if revoked is not None:
        revocation = _stored_receipt(revoked)
        fields = ("run_id", "source_sha256", "candidate_sha256", "kind", "purpose", "provenance", "usage_rights", "redaction_version")
        if revocation["decision"] != "revoke" or any(revocation[field] != receipt[field] for field in fields):
            raise ValueError("stored_review_integrity_failure")
    if receipt["decision"] != "accept" or revoked is not None:
        return "revoked"
    if receipt["candidate_sha256"] != digest(candidate):
        return "candidate_changed"
    if receipt["source_sha256"] != source_hash:
        return "source_changed"
    rights = "synthetic fixture authored for AOS" if receipt["usage_rights"] == "synthetic_authored" else "user_authorized"
    if (receipt["run_id"] != candidate["provenance"]["run_id"] or receipt["kind"] != "system1_choice"
            or receipt["purpose"] != "offline_training_text" or receipt["provenance"] != "synthetic"
            or receipt["redaction_version"] != candidate["provenance"]["redaction_version"]
            or candidate["provenance"]["usage_rights"] != rights):
        return "binding_mismatch"
    return "current"


def inspect_preview(connection: sqlite3.Connection, identity: dict, run_id: str, *, label_id: str | None = None,
                    candidate: dict | None = None, receipt_id: str | None = None) -> dict:
    if (candidate is not None and label_id is None) or (receipt_id is not None and candidate is None):
        raise ValueError("preview_arguments_incomplete")
    report = {"schema_version": "1.0", "preview_version": "choice-source-preflight-v1", "mode": "hash_only",
              "training_ready": False, "metadata_claims_verified": False, "run_ref": digest({"run_id": run_id}),
              "label_ref": digest({"label_id": label_id}) if label_id is not None else None,
              "snapshot_sha256": identity["sha256"], "source_sha256": None, "projection_sha256": None,
              "candidate_sha256": None, "accepted_choice_labels": 0, "derivation_matches": None,
              "receipt_status": "not_requested", "blockers": []}
    blockers = {"training_not_authorized", "reviewer_authentication_unverified", "redaction_unverified", "usage_rights_unverified"}
    try:
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise PreviewBlocked("run_missing")
        try:
            report["source_sha256"] = source_fingerprint(connection, run_id)
        except ValueError:
            raise PreviewBlocked("source_unsettled") from None
        if run["policy_version"] not in SUPPORTED_POLICIES:
            raise PreviewBlocked("task_kind_unsupported")
        blockers.add("synthetic_content_excluded")
        if run["status"] != "succeeded" or run["outcome"] != "passed":
            raise PreviewBlocked("run_not_verified")
        if connection.execute("SELECT 1 FROM artifacts WHERE run_id=? AND lower(media_type) LIKE 'image/%' LIMIT 1", (run_id,)).fetchone():
            raise PreviewBlocked("image_artifacts_excluded")
        report["accepted_choice_labels"] = connection.execute(
            "SELECT count(*) FROM trajectory_labels WHERE run_id=? AND review_status='accepted' AND label_type='choice'", (run_id,)).fetchone()[0]
        if label_id is None:
            raise PreviewBlocked("label_selection_required" if report["accepted_choice_labels"] else "no_accepted_choice_labels")
        label = connection.execute("SELECT * FROM trajectory_labels WHERE label_id=? AND run_id=?", (label_id, run_id)).fetchone()
        if label is None:
            raise PreviewBlocked("label_unavailable")
        derived = derive_choice(connection, run, label)
        report["projection_sha256"] = digest(derived)
        if candidate is None:
            blockers.add("candidate_not_supplied")
        else:
            try:
                validate_record("system1_choice", candidate)
            except ValueError:
                raise PreviewBlocked("candidate_invalid") from None
            report["candidate_sha256"] = digest(candidate)
            compared = {**candidate, "provenance": {**candidate["provenance"],
                        "redaction_version": REDACTION_UNREVIEWED, "usage_rights": "unreviewed"}}
            report["derivation_matches"] = canonical(compared) == canonical(derived)
            if not report["derivation_matches"]:
                blockers.add("candidate_derivation_differs")
            if receipt_id is not None:
                try:
                    report["receipt_status"] = receipt_preflight(connection, receipt_id, candidate, report["source_sha256"])
                except ValueError:
                    report["receipt_status"] = "invalid"
                if report["receipt_status"] != "current":
                    blockers.add("receipt_not_current")
            else:
                blockers.add("receipt_not_supplied")
    except PreviewBlocked as error:
        blockers.add(str(error))
    except (ValueError, TypeError, KeyError, RecursionError):
        blockers.add("source_record_invalid")
    report["blockers"] = sorted(blockers)
    if not validator("dataset_preview").is_valid(report):
        raise ValueError("invalid_preview_report")
    return report


def preview_database(path: Path, run_id: str, *, label_id: str | None = None, candidate: dict | None = None,
                     receipt_id: str | None = None) -> dict:
    with audit_snapshot(path) as (snapshot, identity):
        return inspect_preview(snapshot, identity, run_id, label_id=label_id, candidate=candidate, receipt_id=receipt_id)


def load_candidate(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("candidate_path_unavailable")
    with path.open("rb") as stream:
        payload = stream.read(MAX_CANDIDATE_BYTES + 1)
    if len(payload) > MAX_CANDIDATE_BYTES:
        raise ValueError("candidate_size_limit")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    candidate = json.loads(payload, object_pairs_hook=unique_pairs)
    validate_record("system1_choice", candidate)
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS salt okunur source-candidate hash önizlemesi; içerik veya eğitim izni üretmez")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--label-id")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--receipt-id")
    arguments = parser.parse_args()
    try:
        candidate = load_candidate(arguments.candidate) if arguments.candidate else None
        report = preview_database(arguments.database, arguments.run_id, label_id=arguments.label_id,
                                  candidate=candidate, receipt_id=arguments.receipt_id)
    except (ValueError, OSError, sqlite3.Error, RecursionError):
        parser.exit(1, "Önizleme tamamlanamadı: argüman, dosya, şema, bütünlük veya kaynak sınırını kontrol edin.\n")
    print(canonical(report))


if __name__ == "__main__":
    main()

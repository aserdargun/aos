"""Read-only, content-free learning evidence projection from a trajectory snapshot."""

import json
from pathlib import Path
import re
import sqlite3

from .contracts import canonical, digest
from .dataset import validator
from .dataset_audit import SYNTHETIC_POLICIES, VERIFIERS, audit_snapshot
from .vision import CaptureEvidence, VisionScene


DERIVATION_VERSION = "learning-evidence-v2"
MAX_REVIEW_EVENTS = 500
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
MODEL_KINDS = {"system1": {"decider_native_worker", "laya_candidate", "owned_episode_adapter_runtime"},
               "system2": {"bonsai_native_supervisor"}}
LOCAL_PAGE_OUTCOMES = {
    "browser.fixture.open": {"page": "start", "heading": "Synthetic start",
                             "links": [{"role": "link", "label": "Details"}]},
    "browser.fixture.follow": {"page": "details", "heading": "Synthetic details", "links": []},
}


def _identity(run: sqlite3.Row, role: str) -> dict | None:
    try:
        snapshot = json.loads(run["deployment_snapshot_json"])
        identity = ({key: value for key, value in snapshot.items() if key != "supervisor"}
                    if role == "system1" else snapshot["supervisor"])
        if (not isinstance(identity, dict) or identity.get("real_model") is not True
                or identity.get("kind") not in MODEL_KINDS[role]
                or not isinstance(identity.get("deployment_id"), str)
                or not isinstance(identity.get("pins"), dict)):
            return None
        return identity
    except (KeyError, TypeError, ValueError):
        return None


def _independent_evidence(connection: sqlite3.Connection, row: sqlite3.Row, evidence: list[str]) -> bool:
    if len(evidence) != 1:
        return False
    try:
        observation = connection.execute("""SELECT o.kind,o.payload_json,o.action_id,a.tool,a.status,
            a.decision_id,a.result_json FROM observations o JOIN actions a
            ON a.action_id=o.action_id AND a.run_id=o.run_id AND a.step_id=o.step_id
            WHERE o.observation_id=? AND o.run_id=? AND o.step_id=?""",
            (evidence[0], row["run_id"], row["step_id"])).fetchone()
        if (observation is None or observation["action_id"] == row["action_id"]
                or observation["status"] != "ok"
                or observation["decision_id"] != row["decision_id"]
                or observation["result_json"] is None):
            return False
        payload = json.loads(observation["payload_json"])
        if payload != json.loads(observation["result_json"]):
            return False
        method = row["method"]
        if method == "independent_read_equals":
            return (row["tool"] in {"filesystem.write", "filesystem.read"}
                    and observation["tool"] == "filesystem.read"
                    and observation["kind"] == "filesystem.read"
                    and isinstance(payload, dict) and set(payload) == {"content"}
                    and isinstance(payload["content"], str)
                    and payload["content"] == json.loads(row["actual_json"]))
        if method == "independent_dom_equals":
            return (row["tool"] in {"browser.fill", "browser.submit"}
                    and observation["tool"] == "browser.verify"
                    and observation["kind"] == "browser.dom"
                    and payload == json.loads(row["actual_json"]))
        if method == "independent_canvas_equals":
            return (row["tool"] == "vision.click" and observation["tool"] == "vision.verify"
                    and observation["kind"] == "vision.outcome"
                    and payload == json.loads(row["actual_json"]))
        expected = LOCAL_PAGE_OUTCOMES.get(row["tool"])
        if (method != "independent_local_page_equals" or expected is None
                or observation["tool"] != "browser.fixture.snapshot"
                or observation["kind"] != "browser.local_navigation"
                or json.loads(row["actual_json"]) != expected
                or not isinstance(payload, dict)
                or set(payload) != {"page", "heading", "snapshot_id", "elements"}
                or not isinstance(payload["snapshot_id"], str)
                or re.fullmatch("[a-f0-9]{32}", payload["snapshot_id"]) is None):
            return False
        measured = {"page": payload["page"], "heading": payload["heading"],
                    "links": [{"role": element["role"], "label": element["label"]}
                              for element in payload["elements"]]}
        return measured == expected
    except (KeyError, TypeError, ValueError, RecursionError):
        return False


def _verified_decision_refs(connection: sqlite3.Connection, run_id: str, step_id: str, decision_id: str) -> list[str]:
    refs = []
    rows = connection.execute("""SELECT v.*,a.tool,a.decision_id FROM verifications v JOIN actions a
        ON a.action_id=v.action_id AND a.run_id=v.run_id AND a.step_id=v.step_id
        WHERE v.run_id=? AND v.step_id=? AND a.decision_id=? AND a.status='ok'
        ORDER BY v.verification_id""", (run_id, step_id, decision_id))
    for row in rows:
        if row["result"] != "passed" or VERIFIERS.get(row["method"]) != row["verifier"]:
            continue
        try:
            if canonical(json.loads(row["expected_json"])) != canonical(json.loads(row["actual_json"])):
                continue
            evidence = json.loads(row["evidence_refs_json"])
            if not isinstance(evidence, list) or not evidence or len(evidence) != len(set(evidence)):
                continue
            if any(not isinstance(reference, str) or not connection.execute("""SELECT 1 FROM observations
                WHERE observation_id=? AND run_id=? AND step_id=? UNION ALL SELECT 1 FROM artifacts
                WHERE artifact_id=? AND run_id=? AND step_id=?""",
                (reference, run_id, step_id, reference, run_id, step_id)).fetchone() for reference in evidence):
                continue
            if not _independent_evidence(connection, row, evidence):
                continue
            refs.append(row["verification_id"])
        except (TypeError, ValueError, RecursionError):
            continue
    return refs


def _scene_evidence(connection: sqlite3.Connection, call: sqlite3.Row,
                    system1_deployment_id: str | None) -> tuple[str | None, list[str]]:
    try:
        scene = VisionScene.model_validate_json(call["response_json"])
        request = json.loads(call["request_json"])
        if not isinstance(request, dict) or request.get("purpose") != "synthetic_canvas_vision":
            return None, []
        captures = []
        for row in connection.execute("""SELECT observation_id,payload_json FROM observations
                WHERE run_id=? AND step_id=? AND kind='vision.capture' AND action_id IS NULL""",
                (call["run_id"], call["step_id"])):
            capture = CaptureEvidence.model_validate_json(row["payload_json"])
            metadata = capture.model_dump(exclude={"state_version", "artifact_id"})
            if (request.get("capture") != metadata or request.get("state_version") != capture.state_version
                    or request.get("artifact_id") != capture.artifact_id
                    or scene.capture_id != capture.capture_id or scene.state_version != capture.state_version):
                continue
            artifact = connection.execute("""SELECT sha256,media_type FROM artifacts
                WHERE artifact_id=? AND run_id=? AND step_id=?""",
                (capture.artifact_id, call["run_id"], call["step_id"])).fetchone()
            if artifact is not None and artifact["sha256"] == capture.sha256 and artifact["media_type"] == "image/png":
                captures.append(row["observation_id"])
        if len(captures) != 1:
            return None, []
        scenes = []
        for row in connection.execute("""SELECT observation_id,payload_json FROM observations
                WHERE run_id=? AND step_id=? AND kind='vision.scene' AND action_id IS NULL""",
                (call["run_id"], call["step_id"])):
            if json.loads(row["payload_json"]) == scene.model_dump():
                scenes.append(row["observation_id"])
        if len(scenes) != 1:
            return None, []
        decisions = []
        for row in connection.execute("""SELECT d.decision_id,s.state_json,m.deployment_id FROM decisions d
                JOIN state_snapshots s ON s.snapshot_id=d.snapshot_id AND s.run_id=d.run_id AND s.step_id=d.step_id
                JOIN model_calls m ON m.call_id=d.call_id AND m.run_id=d.run_id AND m.step_id=d.step_id
                WHERE d.run_id=? AND d.step_id=? AND m.role='system1' AND m.status='ok'""",
                (call["run_id"], call["step_id"])):
            state = json.loads(row["state_json"])
            if (row["deployment_id"] == system1_deployment_id and isinstance(state, dict)
                    and state.get("task_kind") == "vision_canvas"
                    and state.get("capture_id") == scene.capture_id
                    and state.get("scene_sha256") == digest(scene.model_dump())):
                decisions.append(row["decision_id"])
        downstream = (_verified_decision_refs(connection, call["run_id"], call["step_id"], decisions[0])
                      if len(decisions) == 1 else [])
        return scenes[0], downstream
    except (KeyError, TypeError, ValueError, RecursionError):
        return None, []


def project_learning_events(connection: sqlite3.Connection, run_id: str) -> list[dict]:
    """Project stable metadata only; this does not create labels, skills or trainer input."""
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        return []
    events = []
    calls = connection.execute("SELECT * FROM model_calls WHERE run_id=? ORDER BY call_id", (run_id,))
    for call in calls:
        role = call["role"]
        identity = _identity(run, role) if role in MODEL_KINDS else None
        if (identity is None or call["deployment_id"] != identity["deployment_id"]
                or call["status"] != "ok" or call["response_json"] is None):
            continue
        if identity['kind'] == 'owned_episode_adapter_runtime':
            from .owned_adapter_identity import verify_adapter_inference

            if not verify_adapter_inference(connection, call, identity):
                continue
        decision = None
        escalation = None
        if role == "system1":
            decision = connection.execute("""SELECT decision_id FROM decisions
                WHERE run_id=? AND step_id=? AND call_id=?""",
                (run_id, call["step_id"], call["call_id"])).fetchone()
            if decision is None:
                continue
        else:
            escalation = connection.execute("""SELECT escalation_id FROM supervisor_escalations
                WHERE run_id=? AND step_id=? AND call_id=?""",
                (run_id, call["step_id"], call["call_id"])).fetchone()
        verification_ids = (_verified_decision_refs(connection, run_id, call["step_id"], decision["decision_id"])
                            if decision is not None else [])
        system1_identity = _identity(run, "system1") if role == "system2" else None
        scene_observation_id, downstream_verification_ids = (_scene_evidence(
            connection, call, system1_identity["deployment_id"] if system1_identity else None)
                                                              if role == "system2" else (None, []))
        source = {"run_id": run_id, "step_id": call["step_id"], "call_id": call["call_id"],
                  "deployment_id": call["deployment_id"], "deployment_sha256": digest(identity),
                  "decision_id": decision["decision_id"] if decision else None,
                  "escalation_id": escalation["escalation_id"] if escalation else None,
                  "verification_ids": verification_ids,
                  "scene_observation_id": scene_observation_id,
                  "downstream_verification_ids": downstream_verification_ids}
        blockers = ["collection_rights_unverified", "redaction_unverified", "site_scope_unbound",
                    "review_required", "training_not_authorized"]
        if run["policy_version"] in SYNTHETIC_POLICIES:
            blockers.append("synthetic_task_excluded")
        if not verification_ids:
            blockers.append("outcome_not_attributed")
        if role == "system2" and escalation is None:
            blockers.append("supervisor_plan_not_linked")
        event = {"schema_version": "2.0", "derivation_version": DERIVATION_VERSION,
                 "event_id": "learning-" + digest({"version": DERIVATION_VERSION, "role": role,
                                                    "run_id": run_id, "call_id": call["call_id"]}),
                 "role": role, "model_kind": identity["kind"], "source": source,
                 "run_status": run["status"], "verified_outcome": bool(verification_ids),
                 "metadata_only": True, "training_ready": False, "blockers": sorted(blockers)}
        if not validator("learning_event_v2").is_valid(event):
            raise ValueError("invalid_learning_event")
        events.append(event)
    return events


def learning_events_from_database(path: Path, run_id: str) -> list[dict]:
    with audit_snapshot(path) as (snapshot, _snapshot_identity):
        return project_learning_events(snapshot, run_id)


def review_learning_events_snapshot(connection: sqlite3.Connection, identity: dict,
                                    run_id: str) -> dict:
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise ValueError("invalid_run_id")
    if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        raise ValueError("run_missing")
    count = connection.execute("SELECT count(*) FROM model_calls WHERE run_id=?", (run_id,)).fetchone()[0]
    if count > MAX_REVIEW_EVENTS:
        raise ValueError("event_count_limit")
    events = project_learning_events(connection, run_id)
    return {"schema_version": "1.0", "mode": "read_only_metadata_review",
            "run_ref": digest({"run_id": run_id}), "snapshot_sha256": identity["sha256"],
            "event_count": len(events), "events": events,
            "collection_authorized": False, "training_ready": False}


def review_learning_events(path: Path, run_id: str) -> dict:
    """Return a bounded, deterministic, content-free review of one existing run."""
    with audit_snapshot(path) as (snapshot, identity):
        return review_learning_events_snapshot(snapshot, identity, run_id)

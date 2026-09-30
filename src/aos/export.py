import json

from .contracts import AOSFault, ErrorCode, HELLO_CONTENT, MISSING_FILE_SUMMARY, State, now
from .browser import BROWSER_VALUE, BrowserOutcome, DomObservation
from .storage import TrajectoryStore
from .write_receipts import WriteReceipt
from .vision import CaptureEvidence, VisionOutcome, VisionScene


def export_hello(store: TrajectoryStore, run_id: str) -> dict:
    policy = store.connection.execute("SELECT policy_version FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if policy is None or policy[0] != "hello-policy-v1":
        raise AOSFault(ErrorCode.UNSAFE_ACTION, "Hello export requires a hello run")
    return export_run(store, run_id)


def export_run(store: TrajectoryStore, run_id: str) -> dict:
    connection = store.connection
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if (run is None or run["status"] != "succeeded"
            or run["policy_version"] not in {"hello-policy-v1", "browser-form-policy-v1", "vision-canvas-policy-v1"}):
        raise AOSFault(ErrorCode.UNSAFE_ACTION, "Only verified bounded synthetic runs can use this exporter")
    browser = run["policy_version"] == "browser-form-policy-v1"
    vision = run["policy_version"] == "vision-canvas-policy-v1"
    task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (run["task_id"],)).fetchone()
    deployment = json.loads(run["deployment_snapshot_json"])
    environment = json.loads(run["environment_json"])
    environment.pop('workspace_identity', None)
    steps = []
    for step in connection.execute("SELECT * FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,)):
        def rows(table):
            return connection.execute(f"SELECT * FROM {table} WHERE run_id=? AND step_id=?",
                                      (run_id, step["step_id"])).fetchall()

        observations = []
        artifacts = []
        capture_artifacts = {row["artifact_id"]: row for row in rows("artifacts")}
        for observation in rows("observations"):
            payload = json.loads(observation["payload_json"])
            if observation['kind'] == 'filesystem.write_receipt':
                WriteReceipt.model_validate(payload)
                continue
            content = payload.get("content")
            if vision and observation["kind"].startswith("vision."):
                models = {"vision.capture": CaptureEvidence, "vision.scene": VisionScene, "vision.outcome": VisionOutcome}
                if observation["kind"] not in models:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, "Unsupported visual evidence")
                bounded = models[observation["kind"]].model_validate(payload)
                content = bounded.model_dump_json()
                if isinstance(bounded, CaptureEvidence):
                    artifact = capture_artifacts.get(bounded.artifact_id)
                    if (artifact is None or artifact["sha256"] != bounded.sha256 or artifact["media_type"] != "image/png"
                            or artifact["relative_path"] != "vision-captures/" + bounded.capture_id + ".png"):
                        raise AOSFault(ErrorCode.UNSAFE_ACTION, "Visual capture artifact binding differs")
                    artifacts.append({key: artifact[key] for key in ("artifact_id", "relative_path", "sha256", "media_type")} | {"redacted": False})
            elif browser and observation["kind"] == "browser.dom":
                model = DomObservation if "snapshot_id" in payload else BrowserOutcome
                bounded = model.model_validate(payload)
                if bounded.value not in {"", BROWSER_VALUE} or bounded.receipt not in {"", BROWSER_VALUE} or bounded.submissions not in {0, 1}:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, "Non-fixture DOM requires a reviewed redactor")
                content = bounded.model_dump_json()
            elif observation["kind"] == "filesystem.error" and payload == {"error": "ELEMENT_MISSING", "summary": MISSING_FILE_SUMMARY}:
                content = MISSING_FILE_SUMMARY
            elif content != HELLO_CONTENT:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Non-fixture content requires a reviewed redactor")
            observations.append({"observation_id": observation["observation_id"],
                                 "action_id": observation["action_id"], "kind": observation["kind"], "summary": content})
        steps.append({
            "step_id": step["step_id"], "ordinal": step["ordinal"],
            "snapshots": [{"snapshot_id": row["snapshot_id"], "state_version": row["state_version"],
                           "state": State.model_validate_json(row["state_json"]).normalized_goal} for row in rows("state_snapshots")],
            "decisions": [{"decision_id": row["decision_id"], "snapshot_id": row["snapshot_id"],
                           "question": row["question"], "options": json.loads(row["options_json"]),
                           "selected_option": row["selected_option"], "probabilities": json.loads(row["probabilities_json"]),
                           "policy_result": row["policy_result"]} for row in rows("decisions")],
            "actions": [{"action_id": row["action_id"], "decision_id": row["decision_id"], "tool": row["tool"],
                         "arguments": json.loads(row["arguments_json"]), "idempotency_key": row["idempotency_key"],
                         "status": row["status"], "actual_option": row["actual_option"]} for row in rows("actions")],
            "observations": observations,
            "verifications": [{"verification_id": row["verification_id"], "action_id": row["action_id"],
                               "criterion": row["criterion"], "method": row["method"], "result": row["result"],
                               "expected": json.loads(row["expected_json"]), "actual": json.loads(row["actual_json"]),
                               "evidence_refs": json.loads(row["evidence_refs_json"])} for row in rows("verifications")],
            "model_calls": [{key: row[key] for key in ("call_id", "deployment_id", "role", "status", "latency_ms")}
                            for row in rows("model_calls")],
            "escalations": [{"escalation_id": row["escalation_id"], "reason": row["reason"], "diagnosis": row["diagnosis"],
                             "plan": [item["action"] for item in json.loads(row["corrected_plan_json"])["revised_plan"]],
                             "outcome": row["outcome"]} for row in rows("supervisor_escalations")],
            "human_interventions": [{"intervention_id": row["intervention_id"], "kind": row["kind"],
                                     "summary": "Recorded human intervention; raw payload retained only in the local audit store."}
                                    for row in rows("human_interventions")],
            "artifacts": artifacts, "labels": [],
        })
    return {"schema_version": "1.0", "exported_at": now(), "synthetic": True,
            "redaction_version": "bounded-vision-metadata-v1" if vision else "bounded-browser-form-v1" if browser else "bounded-hello-v1",
            "task": {"task_id": task["task_id"], "goal_original": task["original_goal"],
                     "goal_normalized": task["normalized_goal"], "success_criteria": json.loads(task["success_criteria_json"])},
            "run": {"run_id": run_id, "status": run["status"], "outcome": run["outcome"],
                    "policy_version": run["policy_version"], "deployment_ids": [deployment["deployment_id"]] +
                    ([deployment["supervisor"]["deployment_id"]] if "supervisor" in deployment else []),
                    "environment": {**environment, "decision_engine": deployment["kind"],
                                    "real_model": deployment["real_model"],
                                    "real_supervisor": deployment.get("supervisor", {}).get("real_model", False)}}, "steps": steps}

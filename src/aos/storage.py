import fcntl
import os
import sqlite3
from pathlib import Path

from .contracts import AOSFault, ErrorCode, Phase, REPO_ROOT, State, canonical, digest, identifier, now


class TrajectoryStore:
    def __init__(self, path: Path, readonly: bool = False):
        self.lock = None
        if readonly:
            self.connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("BEGIN")
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Another writer owns this database") from None
        if path.is_symlink():
            os.close(self.lock)
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Database symlinks are forbidden")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA synchronous=FULL")
        exists = self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        applied = {row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")} if exists else set()
        for migration in sorted((REPO_ROOT / "database/migrations").glob("*.sql")):
            if int(migration.name.split("_", 1)[0]) not in applied:
                self.connection.executescript(migration.read_text())
        self.reconcile()

    def insert(self, table: str, **values) -> None:
        self.connection.execute(
            f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            list(values.values()),
        )

    def create_run(self, state: State, deployment: dict, runtime: dict) -> None:
        with self.connection:
            self.insert("tasks", task_id=state.task_id, original_goal=state.original_goal,
                        normalized_goal=state.normalized_goal, success_criteria_json=canonical(state.success_criteria),
                        workspace_scope_json=canonical([state.authorized_path]), created_at=now())
            self.insert("runs", run_id=state.run_id, task_id=state.task_id, status="running",
                        policy_version={"browser_form": "browser-form-policy-v1", "vision_canvas": "vision-canvas-policy-v1", "hello": "hello-policy-v1", "browser_local_navigation": "browser-local-navigation-policy-v1", "browser_staging_workflow": "browser-staging-workflow-policy-v1", "browser_remote_entry": "browser-remote-entry-policy-v1", "browser_remote_routes": "browser-remote-routes-policy-v1", "browser_remote_static_assets": "browser-remote-static-assets-policy-v1", "browser_remote_form": "browser-remote-form-policy-v1"}[state.task_kind],
                        environment_json=canonical(runtime),
                        deployment_snapshot_json=canonical(deployment), started_at=now())
            self.insert("steps", step_id=state.step_id, run_id=state.run_id, ordinal=0,
                        state=state.phase.value, started_at=now())
            self.insert("runtime_states", run_id=state.run_id, state_version=state.state_version,
                        state_json=state.model_dump_json())
            self.snapshot(state)

    def snapshot(self, state: State) -> str:
        snapshot_id = identifier("snapshot")
        self.insert("state_snapshots", snapshot_id=snapshot_id, run_id=state.run_id, step_id=state.step_id,
                    state_version=state.state_version, state_json=state.model_dump_json(),
                    content_sha256=digest(state.model_dump(mode="json")), created_at=now())
        return snapshot_id

    def state(self, run_id: str) -> State:
        row = self.connection.execute("SELECT state_json FROM runtime_states WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, "Unknown run")
        return State.model_validate_json(row[0])

    def save_state(self, previous: State, current: State, *, resume: bool = False) -> None:
        with self.connection:
            if resume:
                if previous.phase != Phase.PAUSED or current.phase != Phase.PAUSED or current.owner != 'AGENT':
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only a paused run can resume')
                resumed = self.connection.execute(
                    "UPDATE runs SET status='running',outcome='unknown',ended_at=NULL WHERE run_id=? AND status='paused'", (current.run_id,)).rowcount
                if resumed != 1:
                    raise AOSFault(ErrorCode.UI_CHANGED, 'Run changed before resume')
                self.connection.execute('UPDATE steps SET ended_at=NULL WHERE step_id=?', (current.step_id,))
            changed = self.connection.execute(
                "UPDATE runtime_states SET state_version=?,state_json=? WHERE run_id=? AND state_version=?",
                (current.state_version, current.model_dump_json(), current.run_id, previous.state_version),
            ).rowcount
            if changed != 1:
                raise AOSFault(ErrorCode.UI_CHANGED, "State changed before persistence")
            self.connection.execute("UPDATE steps SET state=? WHERE step_id=?", (current.phase.value, current.step_id))
            self.snapshot(current)

    def finish(self, state: State, status: str, outcome: str) -> None:
        if status == "succeeded":
            rows = self.connection.execute("SELECT * FROM verifications WHERE run_id=?", (state.run_id,)).fetchall()
            if (state.phase != Phase.SUCCEEDED or not rows
                    or any(row["result"] != "passed" or row["expected_json"] != row["actual_json"] for row in rows)):
                raise AOSFault(ErrorCode.INVALID_OUTPUT, "Success requires independent matching verification")
        with self.connection:
            self.connection.execute("UPDATE runs SET status=?,outcome=?,ended_at=? WHERE run_id=?",
                                    (status, outcome, now(), state.run_id))
            self.connection.execute("UPDATE steps SET ended_at=? WHERE step_id=?", (now(), state.step_id))

    def reconcile(self) -> None:
        with self.connection:
            self.connection.execute("UPDATE desktop_approvals SET status='revoked',updated_at=? WHERE status IN ('pending','approved')", (now(),))
            for row in self.connection.execute("SELECT run_id FROM desktop_tasks WHERE status='paused' AND run_id IS NOT NULL").fetchall():
                current = self.state(row[0])
                if current.phase == Phase.PAUSED:
                    cancelled = current.advance(Phase.CANCELLED, owner='PAUSED', owner_lease_id=identifier('revoked'))
                    self.connection.execute('UPDATE runtime_states SET state_json=?,state_version=? WHERE run_id=?',
                                            (cancelled.model_dump_json(), cancelled.state_version, cancelled.run_id))
                    self.snapshot(cancelled)
                    self.connection.execute("UPDATE steps SET state='CANCELLED',ended_at=? WHERE step_id=?", (now(), cancelled.step_id))
                    self.connection.execute("UPDATE runs SET status='cancelled',outcome='unknown',ended_at=? WHERE run_id=?", (now(), cancelled.run_id))
            self.connection.execute("UPDATE desktop_tasks SET status='cancelled',updated_at=? WHERE status IN ('queued','running','waiting_approval','paused')", (now(),))
            self.connection.execute("UPDATE desktop_inputs SET status='cancelled' WHERE status='queued'")
            self.connection.execute("UPDATE desktop_inputs SET status='uncertain' WHERE status='running'")
            for session in self.connection.execute("SELECT session_id FROM desktop_sessions WHERE status!='stopped'").fetchall():
                self.connection.execute("UPDATE desktop_sessions SET owner='PAUSED',status='paused',lease_id=?,generation=generation+1,updated_at=? WHERE session_id=?",
                                        (identifier('revoked'), now(), session[0]))
            self.connection.execute("UPDATE actions SET status='uncertain',error_code='RUNTIME_CRASH' WHERE status IN ('intent','running')")
            for row in self.connection.execute("SELECT run_id FROM runs WHERE status='running'").fetchall():
                state = self.state(row[0])
                paused = state.model_copy(update={"phase": Phase.PAUSED, "owner": "PAUSED",
                                                  "owner_lease_id": identifier("revoked"),
                                                  "state_version": state.state_version + 1})
                self.connection.execute("UPDATE runtime_states SET state_json=?,state_version=? WHERE run_id=?",
                                        (paused.model_dump_json(), paused.state_version, state.run_id))
                self.connection.execute("UPDATE steps SET state='PAUSED' WHERE step_id=?", (state.step_id,))
                self.snapshot(paused)
                self.connection.execute("UPDATE runs SET status='paused',outcome='unknown' WHERE run_id=?", (state.run_id,))
                self.connection.execute("UPDATE supervisor_escalations SET outcome='human_required' WHERE run_id=? AND outcome='pending'", (state.run_id,))

    def close(self) -> None:
        self.connection.close()
        if self.lock is not None:
            os.close(self.lock)

import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aos.computer import WorkspaceRuntime
from aos.contracts import REPO_ROOT, Settings, canonical, digest, now
from aos.dataset import validator
from aos.dataset_preview import derive_choice
from aos.dataset_reviewer import ATTESTATIONS, ReviewerGrant, peer_uid, send_message, serve_review_session
from aos.dataset_reviews import ReviewAuthority, append_review, source_fingerprint
from aos.decision import FixtureDecisionEngine
from aos.operator import Operator
from aos.storage import TrajectoryStore


def read_reply(peer):
    def exact(size):
        payload = bytearray()
        while len(payload) < size:
            chunk = peer.recv(size - len(payload))
            if not chunk:
                raise EOFError("review peer closed")
            payload.extend(chunk)
        return bytes(payload)

    return json.loads(exact(struct.unpack("!I", exact(4))[0]))


class DatasetReviewerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "synthetic.sqlite"
        self.store = TrajectoryStore(self.path)
        self.addCleanup(self.store.close)
        runtime = WorkspaceRuntime(self.root / "workspace")
        runtime.start()
        self.addCleanup(runtime.stop)
        result = asyncio.run(Operator(Settings(workspace=runtime.root, database=self.path), self.store, runtime, FixtureDecisionEngine()).hello())
        self.run_id = result["run_id"]
        verification = self.store.connection.execute("SELECT * FROM verifications LIMIT 1").fetchone()
        action = self.store.connection.execute("SELECT * FROM actions WHERE action_id=?", (verification["action_id"],)).fetchone()
        with self.store.connection:
            self.store.insert("trajectory_labels", label_id="synthetic-label", run_id=self.run_id, step_id=verification["step_id"],
                              decision_id=action["decision_id"], verification_id=verification["verification_id"], label_type="choice",
                              label_json=canonical({"correct_option": action["actual_option"], "outcome": "passed", "rationale": "Synthetic review only."}),
                              source="verified_outcome", review_status="accepted", created_at=now())
        run = self.store.connection.execute("SELECT * FROM runs").fetchone()
        label = self.store.connection.execute("SELECT * FROM trajectory_labels").fetchone()
        self.candidate = derive_choice(self.store.connection, run, label)
        self.candidate["provenance"].update(usage_rights="synthetic fixture authored for AOS", redaction_version="synthetic-review-v1")
        self.request = {"operation": "prepare_accept", "run_id": self.run_id, "label_id": "synthetic-label",
                        "source_sha256": source_fingerprint(self.store.connection, self.run_id), "candidate": self.candidate,
                        "attestations": sorted(ATTESTATIONS)}
        self.events = []

    def grant(self, **changes):
        return ReviewerGrant(**{"uid": os.getuid(), "run_ids": frozenset({self.run_id}),
                                "decisions": frozenset({"accept", "revoke"}), "expires_at": time.time() + 60, **changes})

    def exchange(self, request=None, *, grants=None, change_confirmation=None, audit=None, raw=None, repeat_confirmation=False):
        server, client = socket.socketpair()
        client.settimeout(5)
        replies = []
        errors = []

        def communicate():
            try:
                if raw is None:
                    send_message(client, request or self.request, time.monotonic() + 5)
                else:
                    client.sendall(raw)
                replies.append(read_reply(client))
                if replies[-1]["status"] == "confirmation_required":
                    prepared = replies[-1]
                    confirmation = {"operation": "confirm", "event_sha256": prepared["event_sha256"],
                                    "challenge": prepared["challenge"], "approved": True}
                    if change_confirmation:
                        change_confirmation(confirmation)
                    send_message(client, confirmation, time.monotonic() + 5)
                    if repeat_confirmation:
                        send_message(client, confirmation, time.monotonic() + 5)
                    replies.append(read_reply(client))
            except (OSError, EOFError) as error:
                errors.append(type(error).__name__)
            finally:
                client.close()

        thread = threading.Thread(target=communicate)
        thread.start()
        try:
            serve_review_session(self.store, server, (self.grant(),) if grants is None else grants, audit or self.events.append)
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        return replies

    def count(self):
        return self.store.connection.execute("SELECT count(*) FROM dataset_reviews").fetchone()[0]

    def test_accept_binds_kernel_uid_exact_event_and_minimized_audit(self):
        replies = self.exchange()
        self.assertEqual(replies[-1]["status"], "recorded")
        receipt = replies[0]["receipt"]
        self.assertEqual(receipt["reviewer_id"], f"linux-uid-{os.getuid()}")
        self.assertEqual(receipt["source_sha256"], self.request["source_sha256"])
        self.assertEqual(receipt["candidate_sha256"], digest(self.candidate))
        self.assertEqual(replies[-1]["event_sha256"], digest(receipt))
        self.assertEqual([event["stage"] for event in self.events], ["authenticated", "prepared", "confirmation", "committed"])
        for event in self.events:
            validator("dataset_reviewer_audit").validate(event)
            self.assertNotIn(replies[0]["challenge"], canonical(event))
            self.assertNotIn(self.candidate["state"], canonical(event))
        self.assertEqual(self.store.connection.execute("SELECT training_eligible FROM runs").fetchone()[0], 0)
        self.assertEqual(source_fingerprint(self.store.connection, self.run_id), self.request["source_sha256"])

    def test_revoke_works_after_source_change_without_candidate(self):
        accepted = self.exchange()[0]["receipt"]
        with self.store.connection:
            self.store.connection.execute("UPDATE tasks SET original_goal='synthetic changed source'")
        replies = self.exchange({"operation": "prepare_revoke", "receipt_id": accepted["receipt_id"]},
                                grants=(self.grant(decisions=frozenset({"revoke"})),))
        self.assertEqual(replies[-1]["status"], "recorded")
        self.assertEqual(replies[0]["receipt"]["revokes"], accepted["receipt_id"])
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.exchange({"operation": "prepare_revoke", "receipt_id": accepted["receipt_id"]})[-1]["status"], "denied")

    def test_wrong_uid_empty_duplicate_expired_or_wrong_scope_grants(self):
        cases = [(), (self.grant(uid=os.getuid() + 1),), (self.grant(expires_at=time.time() - 1),),
                 (self.grant(), self.grant()), (self.grant(run_ids=frozenset({"different-run"})),),
                 (self.grant(decisions=frozenset({"revoke"})),)]
        for grants in cases:
            with self.subTest(grants=grants):
                self.exchange(grants=grants)
                self.assertEqual(self.count(), 0)
                self.assertEqual(self.events[-1]["stage"], "denied")

    def test_identity_scope_and_attestation_cannot_come_from_json(self):
        for change in ({"uid": os.getuid()}, {"reviewer_id": "linux-uid-0"}, {"purpose": "remote_upload"},
                       {"attestations": []}, {"attestations": ["content_reviewed"] * 3}):
            self.assertEqual(self.exchange({**self.request, **change})[-1]["status"], "denied")
        self.assertEqual(self.count(), 0)

    def test_changed_source_or_candidate_and_missing_label_are_denied(self):
        for field, value in (("source_sha256", "0" * 64), ("label_id", "missing"), ("run_id", "missing")):
            self.assertEqual(self.exchange({**self.request, field: value})[-1]["status"], "denied")
        changed = copy.deepcopy(self.request)
        changed["candidate"]["state"] = "Different synthetic input"
        self.assertEqual(self.exchange(changed)[-1]["status"], "denied")
        changed = copy.deepcopy(self.request)
        changed["candidate"]["provenance"]["synthetic"] = False
        self.assertEqual(self.exchange(changed)[-1]["status"], "denied")
        self.assertEqual(self.count(), 0)

    def test_rejection_and_confirmation_tampering_write_nothing(self):
        for change, expected in (({"approved": False}, "cancelled"), ({"challenge": "x" * 43}, "denied"),
                                 ({"event_sha256": "0" * 64}, "denied"), ({"approved": "yes"}, "denied")):
            self.assertEqual(self.exchange(change_confirmation=lambda value: value.update(change))[-1]["status"], expected)
        self.assertEqual(self.count(), 0)

    def test_replay_is_rejected_in_same_and_new_sessions(self):
        first = self.exchange(repeat_confirmation=True)[0]
        old = {"operation": "confirm", "challenge": first["challenge"], "event_sha256": first["event_sha256"], "approved": True}
        self.assertEqual(self.exchange(old)[-1]["status"], "denied")
        self.assertEqual(self.exchange(change_confirmation=lambda value: value.update(old))[-1]["status"], "denied")
        self.assertEqual(self.count(), 1)

    def test_source_mutation_between_prepare_and_confirm_fails_atomically(self):
        def audit(event):
            self.events.append(event)
            if event["stage"] == "confirmation":
                with self.store.connection:
                    self.store.connection.execute("UPDATE tasks SET original_goal='synthetic concurrent edit'")

        self.assertEqual(self.exchange(audit=audit)[-1]["status"], "denied")
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.store.connection.in_transaction)

    def test_expiry_after_confirmation_audit_prevents_write(self):
        current = time.time()
        grant = self.grant(expires_at=current + 60)
        with patch("aos.dataset_reviewer.time.time", return_value=current) as clock:
            def audit(event):
                self.events.append(event)
                if event["stage"] == "confirmation":
                    clock.return_value = current + 120

            self.exchange(grants=(grant,), audit=audit)
        self.assertEqual(self.count(), 0)
        self.assertIn("confirmation", [event["stage"] for event in self.events])
        self.assertEqual(self.events[-1]["stage"], "denied")

    def test_stalled_peer_times_out_without_writing(self):
        server, client = socket.socketpair()
        try:
            with patch("aos.dataset_reviewer.SESSION_SECONDS", 0.02):
                serve_review_session(self.store, server, (self.grant(),), self.events.append)
            self.assertEqual(self.events[-1]["stage"], "denied")
            self.assertEqual(self.count(), 0)
        finally:
            client.close()

    def test_caller_transaction_is_not_committed_or_rolled_back(self):
        self.store.connection.execute("UPDATE tasks SET original_goal='pending synthetic edit'")
        try:
            self.assertEqual(self.exchange()[-1]["reason"], "writer_unavailable")
            self.assertTrue(self.store.connection.in_transaction)
            self.assertEqual(self.count(), 0)
        finally:
            self.store.connection.rollback()

    def test_monotonic_expiry_is_checked_inside_receipt_transaction(self):
        receipt = self.exchange(change_confirmation=lambda value: value.update(approved=False))[0]["receipt"]
        authority = ReviewAuthority(digest(receipt), time.time() + 60, time.monotonic() + 5)
        with patch("aos.dataset_reviews.time.monotonic", side_effect=[authority.monotonic_deadline - 1, authority.monotonic_deadline + 1]):
            with self.assertRaisesRegex(ValueError, "explicit_review_authority_required"):
                append_review(self.store, receipt, self.candidate, authority=authority)
        self.assertEqual(self.count(), 0)

    def test_audit_failure_before_and_after_commit_is_not_false_success(self):
        def fail_confirmation(event):
            if event["stage"] == "confirmation":
                raise OSError("synthetic private audit failure")
            self.events.append(event)

        self.assertEqual(self.exchange(audit=fail_confirmation)[-1]["status"], "denied")
        self.assertEqual(self.count(), 0)

        def fail_committed(event):
            if event["stage"] == "committed":
                raise OSError("synthetic private audit failure")
            self.events.append(event)

        self.assertEqual(self.exchange(audit=fail_committed)[-1]["status"], "delivery_uncertain")
        self.assertEqual(self.count(), 1)
        self.assertNotIn("private audit failure", canonical(self.events))

    def test_bounded_frames_duplicate_json_and_disconnect(self):
        invalid = b'{"operation":"prepare_revoke","operation":"confirm"}'
        for raw in (struct.pack("!I", 0), struct.pack("!I", 1024 * 1024 + 1), struct.pack("!I", len(invalid)) + invalid):
            self.exchange(raw=raw)
            self.assertEqual(self.events[-1]["stage"], "denied")
        server, client = socket.socketpair()
        client.close()
        serve_review_session(self.store, server, (self.grant(),), self.events.append)
        self.assertEqual(self.events[-1]["reason"], "peer_disconnected")
        self.assertEqual(self.count(), 0)

    def test_tcp_and_invalid_host_policy_are_rejected(self):
        with socket.socket() as peer, self.assertRaises(ValueError):
            peer_uid(peer)
        for change in ({"uid": True}, {"expires_at": float("nan")}, {"expires_at": float("inf")},
                       {"run_ids": set([self.run_id])}, {"decisions": frozenset({"upload"})}):
            with self.assertRaises(ValueError):
                self.grant(**change)

    def test_real_local_subprocess_peer_is_authenticated_without_client_identity(self):
        socket_path = self.root / "review.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            socket_path.chmod(0o600)
            listener.listen(1)
            listener.settimeout(5)
            code = """
import json, socket, struct, sys
def receive(peer):
    def exact(size):
        data = b''
        while len(data) < size:
            part = peer.recv(size - len(data))
            if not part:
                raise EOFError()
            data += part
        return data
    return json.loads(exact(struct.unpack('!I', exact(4))[0]))
def send(peer, value):
    data = json.dumps(value).encode()
    peer.sendall(struct.pack('!I', len(data)) + data)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
    peer.settimeout(5)
    peer.connect(sys.argv[1])
    send(peer, json.load(sys.stdin))
    prepared = receive(peer)
    send(peer, {'operation':'confirm','challenge':prepared['challenge'],'event_sha256':prepared['event_sha256'],'approved':True})
    print(json.dumps(receive(peer)))
"""
            process = subprocess.Popen([sys.executable, "-c", code, str(socket_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                process.stdin.write(canonical(self.request))
                process.stdin.close()
                process.stdin = None
                peer, _ = listener.accept()
                self.assertEqual(peer_uid(peer), os.getuid())
                serve_review_session(self.store, peer, (self.grant(),), self.events.append)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["status"], "recorded")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_synthetic_audit_fixture_and_schema(self):
        fixture = json.loads((REPO_ROOT / "examples/dataset_reviewer_audit.json").read_text())
        self.assertTrue(fixture["synthetic"])
        validator("dataset_reviewer_audit").validate(fixture["event"])
        for change in ({"challenge": "private"}, {"training_ready": True}, {"reason": "raw exception"}):
            self.assertFalse(validator("dataset_reviewer_audit").is_valid({**fixture["event"], **change}))


if __name__ == "__main__":
    unittest.main()

from dataclasses import dataclass
import json
import math
import secrets
import socket
import sqlite3
import struct
import time
from typing import Callable

from .contracts import canonical, digest, now
from .dataset import validate_record, validator
from .dataset_preview import REDACTION_UNREVIEWED, SUPPORTED_POLICIES, derive_choice
from .dataset_reviews import ReviewAuthority, _stored_receipt, append_review, source_fingerprint
from .storage import TrajectoryStore


MAX_MESSAGE_BYTES = 1024 * 1024
SESSION_SECONDS = 120
ATTESTATIONS = {"content_reviewed", "usage_rights_reviewed", "redaction_reviewed"}


class ReviewDenied(ValueError):
    pass


@dataclass(frozen=True)
class ReviewerGrant:
    uid: int
    run_ids: frozenset[str]
    decisions: frozenset[str]
    expires_at: float

    def __post_init__(self):
        if (type(self.uid) is not int or self.uid < 0 or not isinstance(self.run_ids, frozenset)
                or not self.run_ids or any(not isinstance(run_id, str) or not run_id for run_id in self.run_ids)
                or not isinstance(self.decisions, frozenset) or not self.decisions
                or not self.decisions <= {"accept", "revoke"}
                or type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at)):
            raise ValueError("invalid_reviewer_grant")


def peer_uid(peer: socket.socket) -> int:
    if peer.family != socket.AF_UNIX or peer.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise ReviewDenied("peer_transport_denied")
    process_id, uid, group_id = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    if process_id <= 0 or uid < 0 or group_id < 0:
        raise ReviewDenied("peer_identity_denied")
    return uid


def decode_json(payload: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReviewDenied("invalid_request")
            result[key] = value
        return result

    return json.loads(payload, object_pairs_hook=unique_pairs)


def receive_message(peer: socket.socket, deadline: float, schema: str = "dataset_reviewer_request") -> dict:
    def receive_exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReviewDenied("session_expired")
            peer.settimeout(remaining)
            chunk = peer.recv(size - len(chunks))
            if not chunk:
                raise ReviewDenied("peer_disconnected")
            chunks.extend(chunk)
        return bytes(chunks)

    size = struct.unpack("!I", receive_exact(4))[0]
    if not 0 < size <= MAX_MESSAGE_BYTES:
        raise ReviewDenied("message_size_denied")

    message = decode_json(receive_exact(size))
    if not validator(schema).is_valid(message):
        raise ReviewDenied("invalid_request")
    return message


def send_message(peer: socket.socket, message: dict, deadline: float) -> None:
    payload = canonical(message).encode()
    remaining = deadline - time.monotonic()
    if remaining <= 0 or len(payload) > MAX_MESSAGE_BYTES:
        raise ReviewDenied("session_expired")
    peer.settimeout(remaining)
    peer.sendall(struct.pack("!I", len(payload)) + payload)


def prepare_review(store: TrajectoryStore, request: dict, grant: ReviewerGrant, authorization_ref: str) -> tuple[dict, dict | None]:
    decision = {"prepare_accept": "accept", "prepare_revoke": "revoke"}.get(request["operation"])
    if decision not in grant.decisions or grant.expires_at <= time.time():
        raise ReviewDenied("scope_or_expiry_denied")
    connection = store.connection
    if store.lock is None or connection.in_transaction:
        raise ReviewDenied("writer_unavailable")
    connection.execute("BEGIN")
    try:
        candidate = None
        if decision == "accept":
            run_id = request["run_id"]
            if run_id not in grant.run_ids or set(request["attestations"]) != ATTESTATIONS:
                raise ReviewDenied("scope_or_attestation_denied")
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if (run is None or run["policy_version"] not in SUPPORTED_POLICIES
                    or run["status"] != "succeeded" or run["outcome"] != "passed"
                    or connection.execute("SELECT 1 FROM artifacts WHERE run_id=? AND lower(media_type) LIKE 'image/%'", (run_id,)).fetchone()):
                raise ReviewDenied("source_not_supported")
            source_hash = source_fingerprint(connection, run_id)
            if source_hash != request["source_sha256"]:
                raise ReviewDenied("source_changed")
            label = connection.execute("SELECT * FROM trajectory_labels WHERE run_id=? AND label_id=?", (run_id, request["label_id"])).fetchone()
            if label is None:
                raise ReviewDenied("label_unavailable")
            derived = derive_choice(connection, run, label)
            candidate = request["candidate"]
            validate_record("system1_choice", candidate)
            provenance = candidate["provenance"]
            compared = {**candidate, "provenance": {**provenance, "usage_rights": "unreviewed", "redaction_version": REDACTION_UNREVIEWED}}
            if (canonical(compared) != canonical(derived) or provenance["usage_rights"] != "synthetic fixture authored for AOS"
                    or provenance["redaction_version"] == REDACTION_UNREVIEWED):
                raise ReviewDenied("candidate_not_reviewed_or_derived")
            binding = {"run_id": run_id, "source_sha256": source_hash, "candidate_sha256": digest(candidate),
                       "kind": "system1_choice", "purpose": "offline_training_text", "provenance": "synthetic",
                       "usage_rights": "synthetic_authored", "redaction_version": provenance["redaction_version"], "revokes": None}
        else:
            row = connection.execute("SELECT * FROM dataset_reviews WHERE receipt_id=?", (request["receipt_id"],)).fetchone()
            if row is None:
                raise ReviewDenied("receipt_unavailable")
            parent = _stored_receipt(row)
            if (parent["run_id"] not in grant.run_ids or parent["decision"] != "accept" or parent["kind"] != "system1_choice"
                    or parent["provenance"] != "synthetic" or parent["usage_rights"] != "synthetic_authored"
                    or connection.execute("SELECT 1 FROM dataset_reviews WHERE revokes=?", (parent["receipt_id"],)).fetchone()):
                raise ReviewDenied("scope_or_receipt_denied")
            binding = {key: parent[key] for key in ("run_id", "source_sha256", "candidate_sha256", "kind", "purpose",
                                                   "provenance", "usage_rights", "redaction_version")}
            binding["revokes"] = parent["receipt_id"]
        receipt = {"schema_version": "1.0", "receipt_id": "review-" + secrets.token_hex(16), **binding,
                   "reviewer_id": f"linux-uid-{grant.uid}", "authorization_ref": authorization_ref,
                   "decision": decision, "created_at": now()}
        if not validator("dataset_review_receipt").is_valid(receipt):
            raise ReviewDenied("invalid_request")
        return receipt, candidate
    finally:
        connection.rollback()


def serve_review_session(store: TrajectoryStore, peer: socket.socket, grants: tuple[ReviewerGrant, ...],
                         audit: Callable[[dict], None]) -> None:
    deadline = time.monotonic() + SESSION_SECONDS
    authorization_ref = "peercred-" + secrets.token_hex(16)
    uid = None
    event_hash = None
    grant_hash = None
    grant_expiry = None
    committed = False

    def record(stage, reason):
        event = {"schema_version": "1.0", "authentication": "linux_peercred_v1", "authorization_ref": authorization_ref,
                 "peer_uid": uid, "event_sha256": event_hash, "grant_sha256": grant_hash, "grant_expires_at": grant_expiry,
                 "stage": stage, "reason": reason,
                 "created_at": now(), "training_ready": False}
        if not validator("dataset_reviewer_audit").is_valid(event):
            raise ReviewDenied("audit_unavailable")
        try:
            audit(event)
        except Exception:
            raise ReviewDenied("audit_unavailable") from None

    try:
        uid = peer_uid(peer)
        matching = [grant for grant in grants if grant.uid == uid and grant.expires_at > time.time()]
        if len(matching) != 1:
            raise ReviewDenied("peer_not_authorized")
        grant = matching[0]
        grant_expiry = grant.expires_at
        grant_hash = digest({"uid": grant.uid, "run_ids": sorted(grant.run_ids), "decisions": sorted(grant.decisions),
                             "expires_at": grant.expires_at})
        record("authenticated", "ok")
        request = receive_message(peer, deadline)
        receipt, candidate = prepare_review(store, request, grant, authorization_ref)
        event_hash = digest(receipt)
        challenge = secrets.token_urlsafe(32)
        record("prepared", "ok")
        send_message(peer, {"status": "confirmation_required", "receipt": receipt, "event_sha256": event_hash,
                            "challenge": challenge, "expires_in_seconds": max(0, min(deadline - time.monotonic(), grant.expires_at - time.time())),
                            "training_ready": False}, deadline)
        confirmation = receive_message(peer, deadline)
        if (confirmation["operation"] != "confirm" or confirmation["event_sha256"] != event_hash
                or not secrets.compare_digest(confirmation["challenge"], challenge)):
            raise ReviewDenied("confirmation_binding_denied")
        if not confirmation["approved"]:
            record("cancelled", "explicit_rejection")
            send_message(peer, {"status": "cancelled", "training_ready": False}, deadline)
            return
        record("confirmation", "ok")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or grant.expires_at <= time.time():
            raise ReviewDenied("session_expired")
        authority = ReviewAuthority(event_hash, min(grant.expires_at, time.time() + remaining), deadline)
        append_review(store, receipt, candidate, authority=authority)
        committed = True
        record("committed", "ok")
        send_message(peer, {"status": "recorded", "receipt_id": receipt["receipt_id"], "event_sha256": event_hash,
                            "training_ready": False}, deadline)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RecursionError) as error:
        reason = str(error) if type(error) is ReviewDenied else "request_or_storage_denied"
        try:
            record("delivery_uncertain" if committed else "denied", reason)
        finally:
            try:
                send_message(peer, {"status": "delivery_uncertain" if committed else "denied", "reason": reason,
                                    "training_ready": False}, deadline)
            except (ValueError, OSError):
                pass
    finally:
        peer.close()

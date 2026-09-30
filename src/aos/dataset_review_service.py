import argparse
from contextlib import closing
import errno
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import sys
import time

from .contracts import canonical, digest
from .dataset import validate_record, validator
from .dataset_audit import audit_snapshot
from .dataset_review_journal import ReceiptStore, ReviewJournal, database_binding, exclusive_file, private_directory, read_private_json
from .dataset_reviewer import MAX_MESSAGE_BYTES, ReviewerGrant, peer_uid, receive_message, send_message, serve_review_session
from .dataset_reviews import _stored_receipt


def load_policy(path: Path) -> tuple[ReviewerGrant, ...]:
    policy = read_private_json(path, 65536)
    if not validator("dataset_reviewer_policy").is_valid(policy):
        raise ValueError("invalid_reviewer_policy")
    grants = tuple(ReviewerGrant(item["uid"], frozenset(item["run_ids"]), frozenset(item["decisions"]), item["expires_at"])
                   for item in policy["grants"])
    if len({grant.uid for grant in grants}) != len(grants) or any(grant.uid != os.geteuid() for grant in grants):
        raise ValueError("owner_only_unique_policy_required")
    return grants


def initialize_journal(database: Path, state: Path) -> dict:
    state.mkdir(mode=0o700, exist_ok=True)
    private_directory(state)
    with exclusive_file(state / "service.lock"), closing(ReceiptStore(database)) as store:
        hashes = [digest(_stored_receipt(row)) for row in store.connection.execute("SELECT * FROM dataset_reviews")]
        with closing(ReviewJournal(state / "audit.jsonl", database_binding(database), baseline=digest(sorted(hashes)))) as journal:
            return journal.reconcile(store.connection)


def reconcile_journal(database: Path, state: Path) -> dict:
    private_directory(state)
    with closing(ReviewJournal(state / "audit.jsonl", database_binding(database), readonly=True)) as journal:
        with audit_snapshot(database) as (snapshot, identity):
            return journal.reconcile(snapshot)


def remove_stale_socket(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("existing_socket_not_owned")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(path))
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise ValueError("existing_socket_not_stale") from None
        else:
            raise ValueError("review_service_already_running")
    if path.lstat().st_ino != info.st_ino:
        raise ValueError("socket_changed")
    path.unlink()


def serve(database: Path, policy: Path, state: Path, *, once: bool = False) -> None:
    state = private_directory(state)
    socket_path = state / "reviewer.sock"
    load_policy(policy)
    with exclusive_file(state / "service.lock"), closing(ReceiptStore(database)) as store, \
            closing(ReviewJournal(state / "audit.jsonl", database_binding(database))) as journal:
        journal.reconcile(store.connection)
        remove_stale_socket(socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            socket_path.chmod(0o600)
            inode = socket_path.lstat().st_ino
            try:
                listener.listen(4)
                print(canonical({"status": "ready", "training_ready": False}), flush=True)
                while True:
                    peer, _ = listener.accept()
                    try:
                        grants = load_policy(policy)
                        serve_review_session(store, peer, grants, journal)
                    finally:
                        peer.close()
                    if journal.poisoned:
                        raise ValueError("journal_unavailable")
                    journal.reconcile(store.connection)
                    if once:
                        return
            finally:
                if socket_path.exists() and socket_path.lstat().st_ino == inode:
                    socket_path.unlink()


def check_prepared(prepared: dict, request: dict) -> None:
    receipt = prepared["receipt"]
    if (not validator("dataset_review_receipt").is_valid(receipt) or digest(receipt) != prepared["event_sha256"]
            or receipt["reviewer_id"] != f"linux-uid-{os.geteuid()}" or not receipt["authorization_ref"].startswith("peercred-")
            or receipt["kind"] != "system1_choice" or receipt["provenance"] != "synthetic"
            or receipt["usage_rights"] != "synthetic_authored"):
        raise ValueError("review_reply_binding_differs")
    if request["operation"] == "prepare_accept":
        candidate = request["candidate"]
        provenance = candidate["provenance"]
        if (receipt["decision"] != "accept" or receipt["run_id"] != request["run_id"]
                or receipt["source_sha256"] != request["source_sha256"] or receipt["candidate_sha256"] != digest(candidate)
                or receipt["redaction_version"] != provenance["redaction_version"]):
            raise ValueError("review_reply_binding_differs")
    elif receipt["decision"] != "revoke" or receipt["revokes"] != request["receipt_id"]:
        raise ValueError("review_reply_binding_differs")


def review_client(socket_path: Path, request_path: Path) -> dict:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("interactive_terminal_required")
    private_directory(socket_path.absolute().parent)
    info = socket_path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("private_socket_required")
    request = read_private_json(request_path, MAX_MESSAGE_BYTES)
    if not validator("dataset_reviewer_request").is_valid(request) or request["operation"] not in {"prepare_accept", "prepare_revoke"}:
        raise ValueError("invalid_review_request")
    if request["operation"] == "prepare_accept":
        validate_record("system1_choice", request["candidate"])
    print("Yalnız sentetik inceleme. Eğitim izni verilmez. İçerik/hak/redaction beyanlarını kontrol edin.")
    print(json.dumps(request, ensure_ascii=True, indent=2, allow_nan=False))
    deadline = time.monotonic() + 120
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(5)
        peer.connect(str(socket_path))
        if peer_uid(peer) != os.geteuid():
            raise ValueError("review_server_identity_denied")
        send_message(peer, request, deadline)
        prepared = receive_message(peer, deadline, "dataset_reviewer_response")
        if prepared["status"] == "denied":
            return prepared
        if prepared["status"] != "confirmation_required":
            raise ValueError("review_response_order_invalid")
        check_prepared(prepared, request)
        print(json.dumps(prepared["receipt"], ensure_ascii=True, indent=2))
        print("Bağlantı kesilirse bu receipt_id ile journal/DB sonucunu denetleyin; otomatik tekrar yoktur.")
        approved = input("Onay için ONAY " + prepared["event_sha256"] + " yazın; diğer yanıtlar iptal eder: ") == "ONAY " + prepared["event_sha256"]
        send_message(peer, {"operation": "confirm", "challenge": prepared["challenge"], "event_sha256": prepared["event_sha256"],
                            "approved": approved}, deadline)
        result = receive_message(peer, deadline, "dataset_reviewer_response")
        if result["status"] == "confirmation_required":
            raise ValueError("review_response_order_invalid")
        if result["status"] == "recorded" and (not approved or result["receipt_id"] != prepared["receipt"]["receipt_id"]
                                                or result["event_sha256"] != prepared["event_sha256"]):
            raise ValueError("review_result_binding_differs")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS private yerel reviewer; sentetik kapsam, eğitim kapalı")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "serve", "reconcile"):
        child = commands.add_parser(command)
        child.add_argument("--database", type=Path, required=True)
        child.add_argument("--state-dir", type=Path, required=True)
        if command == "serve":
            child.add_argument("--policy", type=Path, required=True)
            child.add_argument("--once", action="store_true")
    client = commands.add_parser("review")
    client.add_argument("--socket", type=Path, required=True)
    client.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        if arguments.command == "init":
            result = initialize_journal(arguments.database, arguments.state_dir)
        elif arguments.command == "reconcile":
            result = reconcile_journal(arguments.database, arguments.state_dir)
        elif arguments.command == "review":
            result = review_client(arguments.socket, arguments.request)
        else:
            serve(arguments.database, arguments.policy, arguments.state_dir, once=arguments.once)
            return
        print(canonical(result))
        if arguments.command == "review" and result["status"] not in {"recorded", "cancelled"}:
            parser.exit(1)
    except KeyboardInterrupt:
        parser.exit(130, "Reviewer durduruldu; belirsiz yazma sonucunu reconcile ile kontrol edin.\n")
    except (ValueError, OSError, sqlite3.Error, RecursionError, EOFError):
        parser.exit(1, "Reviewer işlemi tamamlanamadı; izin, şema, journal ve bağlantı durumunu kontrol edin. Sonucu varsaymayın.\n")


if __name__ == "__main__":
    main()

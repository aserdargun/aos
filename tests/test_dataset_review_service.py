import copy
import json
import os
from pathlib import Path
import pty
import re
import select
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_dataset_reviewer as fixtures
from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset_review_journal import ReceiptStore, ReviewJournal, database_binding, private_directory, read_private_json
from aos.dataset_review_service import check_prepared, initialize_journal, load_policy, reconcile_journal
from aos.dataset_reviewer import receive_message, send_message


class DatasetReviewServiceTests(unittest.TestCase):
    setUp = fixtures.DatasetReviewerTests.setUp
    grant = fixtures.DatasetReviewerTests.grant
    exchange = fixtures.DatasetReviewerTests.exchange
    count = fixtures.DatasetReviewerTests.count

    def write_json(self, path, value):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(canonical(value))

    def journal(self):
        journal = ReviewJournal(self.root / "audit.jsonl", database_binding(self.path), baseline=digest([]))
        self.addCleanup(journal.close)
        return journal

    def release_writer(self):
        if self.store.lock is not None:
            self.store.close()
            self.store.lock = None

    def start_service(self, *, initialize=True, once=True, crash_stage=None):
        self.release_writer()
        state = self.root / "service"
        if initialize:
            initialize_journal(self.path, state)
        policy = self.root / "policy.json"
        self.write_json(policy, {"schema_version": "1.0", "grants": [{"uid": os.geteuid(), "run_ids": [self.run_id],
                              "decisions": ["accept", "revoke"], "expires_at": time.time() + 300}]})
        command = [sys.executable, "-m", "aos.dataset_review_service", "serve", "--database", str(self.path),
                   "--state-dir", str(state), "--policy", str(policy)]
        if once:
            command.append("--once")
        if crash_stage:
            code = """
import os, sys
from pathlib import Path
from aos.dataset_review_journal import ReviewJournal
from aos.dataset_review_service import serve
original = ReviewJournal.__call__
def crash(self, event):
    if event['stage'] == 'committed' and sys.argv[4] == 'committed':
        os._exit(77)
    original(self, event)
    if event['stage'] == 'confirmation' and sys.argv[4] == 'confirmation':
        os._exit(77)
ReviewJournal.__call__ = crash
serve(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), once=True)
"""
            command = [sys.executable, "-c", code, str(self.path), str(policy), str(state), crash_stage]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()

        self.addCleanup(cleanup)
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, "service did not become ready")
        line = process.stdout.readline()
        if not line:
            self.fail(process.communicate(timeout=5)[1])
        self.assertEqual(json.loads(line)["status"], "ready")
        return process, state

    def connect(self, state):
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(peer.close)
        peer.settimeout(5)
        peer.connect(str(state / "reviewer.sock"))
        return peer

    def prepare(self, peer):
        send_message(peer, self.request, time.monotonic() + 5)
        result = receive_message(peer, time.monotonic() + 5, "dataset_reviewer_response")
        self.assertEqual(result["status"], "confirmation_required")
        return result

    def confirm(self, peer, prepared):
        send_message(peer, {"operation": "confirm", "challenge": prepared["challenge"],
                           "event_sha256": prepared["event_sha256"], "approved": True}, time.monotonic() + 5)

    def test_durable_hash_chain_reopens_and_reconciles(self):
        journal = self.journal()
        replies = self.exchange(audit=journal)
        self.assertEqual(replies[-1]["status"], "recorded")
        report = journal.reconcile(self.store.connection)
        self.assertEqual(report["recorded"], 1)
        self.assertEqual(report["commit_audit_missing"], 0)
        self.assertFalse(report["training_ready"])
        payload = (self.root / "audit.jsonl").read_text()
        self.assertNotIn(replies[0]["challenge"], payload)
        self.assertNotIn(self.candidate["state"], payload)
        self.assertEqual((self.root / "audit.jsonl").stat().st_mode & 0o777, 0o600)

    def test_corruption_partial_tail_and_wrong_database_are_rejected(self):
        path = self.root / "audit.jsonl"
        journal = ReviewJournal(path, database_binding(self.path), baseline=digest([]))
        journal.close()
        original = path.read_bytes()
        for payload in (original + b'{"partial":', original.replace(b'"sequence":0', b'"sequence":1')):
            path.write_bytes(payload)
            with self.assertRaises(ValueError):
                ReviewJournal(path, database_binding(self.path))
        path.write_bytes(original)
        with self.assertRaises(ValueError):
            ReviewJournal(path, "0" * 64)
        with self.assertRaises(FileExistsError):
            ReviewJournal(path, database_binding(self.path), baseline=digest([]))

    def test_missing_audit_suffix_is_detected_against_receipts(self):
        journal = self.journal()
        self.exchange(audit=journal)
        original = list(journal.records)
        journal.records = original[:1]
        with self.assertRaisesRegex(ValueError, "journal_receipt_history_differs"):
            journal.reconcile(self.store.connection)
        journal.records = original[:3]
        with self.assertRaisesRegex(ValueError, "journal_receipt_integrity_failure"):
            journal.reconcile(self.store.connection)
        journal.records = original[:-1]
        self.assertEqual(journal.reconcile(self.store.connection)["commit_audit_missing"], 1)

    def test_fsync_failure_poisons_journal_and_prevents_receipt(self):
        journal = self.journal()
        with patch("aos.dataset_review_journal.os.fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(ValueError):
                self.exchange(audit=journal)
        self.assertTrue(journal.poisoned)
        self.assertEqual(self.count(), 0)
        with self.assertRaises(ValueError):
            journal.append({"kind": "event", "event": {}})

    def test_private_files_symlinks_hardlinks_and_policy_scope(self):
        policy = self.root / "policy.json"
        value = {"schema_version": "1.0", "grants": [{"uid": os.geteuid(), "run_ids": [self.run_id],
                 "decisions": ["accept"], "expires_at": time.time() + 60}]}
        self.write_json(policy, value)
        self.assertEqual(len(load_policy(policy)), 1)
        policy.chmod(0o644)
        with self.assertRaises(ValueError):
            load_policy(policy)
        policy.chmod(0o600)
        link = self.root / "link.json"
        link.symlink_to(policy)
        with self.assertRaises(OSError):
            load_policy(link)
        hard = self.root / "hard.json"
        os.link(policy, hard)
        with self.assertRaises(ValueError):
            load_policy(policy)
        hard.unlink()
        value["grants"][0]["uid"] += 1
        self.write_json(policy, value)
        with self.assertRaises(ValueError):
            load_policy(policy)
        self.root.chmod(0o755)
        try:
            with self.assertRaises(ValueError):
                private_directory(self.root)
        finally:
            self.root.chmod(0o700)

    def test_duplicate_json_and_size_limit(self):
        path = self.root / "request.json"
        self.write_json(path, {"synthetic": True})
        with self.assertRaises(ValueError):
            read_private_json(path, 1)
        path.write_text('{"uid":1,"uid":2}')
        with self.assertRaises(ValueError):
            read_private_json(path, 65536)

    def test_receipt_writer_does_not_reconcile_create_or_migrate(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='paused',outcome='unknown'")
        self.release_writer()
        before = self.path.read_bytes()
        store = ReceiptStore(self.path)
        try:
            self.assertEqual(store.connection.execute("SELECT status FROM runs").fetchone()[0], "paused")
            with self.assertRaises(ValueError):
                store.insert("tasks", original_goal="no")
        finally:
            store.close()
        self.assertEqual(self.path.read_bytes(), before)
        missing = self.root / "missing.sqlite"
        with self.assertRaises(FileNotFoundError):
            ReceiptStore(missing)
        self.assertFalse(missing.exists())

    def test_existing_writer_and_journal_lock_are_respected(self):
        with self.assertRaises(BlockingIOError):
            ReceiptStore(self.path)
        journal = self.journal()
        with self.assertRaises(BlockingIOError):
            ReviewJournal(self.root / "audit.jsonl", database_binding(self.path), readonly=True)
        self.assertFalse(journal.poisoned)

    def test_legacy_database_is_rejected_without_migration(self):
        legacy = self.root / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        for path in sorted((REPO_ROOT / "database/migrations").glob("*.sql"))[:7]:
            connection.executescript(path.read_text())
        connection.close()
        legacy.chmod(0o600)
        before = legacy.read_bytes()
        with self.assertRaises(ValueError):
            ReceiptStore(legacy)
        self.assertEqual(legacy.read_bytes(), before)

    def test_existing_v8_database_remains_supported_without_migration(self):
        legacy = self.root / 'v8.sqlite'
        connection = sqlite3.connect(legacy)
        for path in sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))[:8]:
            connection.executescript(path.read_text())
        connection.close()
        legacy.chmod(0o600)
        before = legacy.read_bytes()
        writer = ReceiptStore(legacy)
        writer.close()
        self.assertEqual(legacy.read_bytes(), before)

    def test_uninitialized_or_live_service_socket_is_not_replaced(self):
        self.release_writer()
        state = self.root / "service"
        state.mkdir(mode=0o700)
        with self.assertRaises(FileNotFoundError):
            ReviewJournal(state / "audit.jsonl", database_binding(self.path))
        from aos.dataset_review_service import remove_stale_socket

        socket_path = state / "reviewer.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            socket_path.chmod(0o600)
            listener.listen(1)
            with self.assertRaises(ValueError):
                remove_stale_socket(socket_path)
            self.assertTrue(socket_path.exists())

    def test_service_accept_and_readonly_reconciliation(self):
        process, state = self.start_service()
        peer = self.connect(state)
        prepared = self.prepare(peer)
        self.confirm(peer, prepared)
        result = receive_message(peer, time.monotonic() + 5, "dataset_reviewer_response")
        self.assertEqual(result["status"], "recorded")
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertFalse((state / "reviewer.sock").exists())
        before = self.path.read_bytes()
        report = reconcile_journal(self.path, state)
        self.assertEqual(report["recorded"], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_crash_before_and_after_receipt_commit_and_stale_socket_restart(self):
        for stage, recorded, missing in (("confirmation", 0, 0), ("committed", 1, 1)):
            with self.subTest(stage=stage):
                process, state = self.start_service(initialize=stage == "confirmation", crash_stage=stage)
                peer = self.connect(state)
                self.confirm(peer, self.prepare(peer))
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, 77)
                report = reconcile_journal(self.path, state)
                self.assertEqual(report["recorded"], recorded)
                self.assertEqual(report["commit_audit_missing"], missing)
        restarted, state = self.start_service(initialize=False)
        peer = self.connect(state)
        send_message(peer, {"operation": "prepare_revoke", "receipt_id": "missing"}, time.monotonic() + 5)
        self.assertEqual(receive_message(peer, time.monotonic() + 5, "dataset_reviewer_response")["status"], "denied")
        restarted.communicate(timeout=5)
        self.assertEqual(restarted.returncode, 0)

    def test_sigterm_while_waiting_for_confirmation_writes_no_receipt(self):
        process, state = self.start_service()
        self.prepare(self.connect(state))
        process.terminate()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130)
        self.assertFalse((state / "reviewer.sock").exists())
        report = reconcile_journal(self.path, state)
        self.assertEqual(report["recorded"], 0)
        self.assertEqual(report["not_recorded"], 1)

    def test_noninteractive_client_refuses_before_reading_content(self):
        command = [sys.executable, "-m", "aos.dataset_review_service", "review", "--socket", str(self.root / "missing.sock"),
                   "--request", str(self.root / "missing.json")]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_real_terminal_client_requires_exact_event_confirmation(self):
        process, state = self.start_service()
        request_path = self.root / "request.json"
        self.write_json(request_path, self.request)
        master, slave = pty.openpty()
        client = subprocess.Popen([sys.executable, "-m", "aos.dataset_review_service", "review", "--socket", str(state / "reviewer.sock"),
                                   "--request", str(request_path)], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        output = bytearray()
        approved = False
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    match = re.search(rb'ONAY ([a-f0-9]{64})', output)
                    if match and not approved:
                        os.write(master, b'ONAY ' + match.group(1) + b'\n')
                        approved = True
                if client.poll() is not None and not ready:
                    break
            client.wait(timeout=2)
            process.communicate(timeout=5)
            self.assertTrue(approved)
            self.assertEqual(client.returncode, 0, output.decode(errors="replace"))
            self.assertIn(b'"status":"recorded"', output)
            self.assertNotIn(b'"challenge"', output)
            self.assertEqual(reconcile_journal(self.path, state)["recorded"], 1)
        finally:
            if client.poll() is None:
                client.kill()
                client.wait()
            os.close(master)

    def test_client_rejects_modified_receipt_before_confirmation(self):
        prepared = self.exchange(change_confirmation=lambda value: value.update(approved=False))[0]
        check_prepared(prepared, self.request)
        for field, value in (("source_sha256", "0" * 64), ("reviewer_id", "linux-uid-0"), ("kind", "system2_supervisor")):
            modified = copy.deepcopy(prepared)
            modified["receipt"][field] = value
            modified["event_sha256"] = digest(modified["receipt"])
            with self.assertRaises(ValueError):
                check_prepared(modified, self.request)

    def test_client_rejects_recorded_reply_before_explicit_confirmation(self):
        from aos.dataset_review_service import review_client

        path = self.root / "request.json"
        self.write_json(path, self.request)
        socket_path = self.root / "reviewer.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            socket_path.chmod(0o600)
            with patch("aos.dataset_review_service.socket.socket"), patch("aos.dataset_review_service.peer_uid", return_value=os.geteuid()), \
                    patch("aos.dataset_review_service.sys.stdin"), patch("aos.dataset_review_service.sys.stdout"), \
                    patch("aos.dataset_review_service.receive_message", return_value={"status": "recorded"}), \
                    patch("builtins.input") as confirmation, self.assertRaisesRegex(ValueError, "review_response_order_invalid"):
                review_client(socket_path, path)
            confirmation.assert_not_called()
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()

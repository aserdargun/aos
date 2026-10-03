from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time

from .contracts import canonical, digest, now
from .dataset import validator
from .dataset_audit import AUDIT_TIMEOUT_SECONDS, MAX_SNAPSHOT_BYTES, SUPPORTED_SCHEMA_VERSIONS, expected_schema, schema_signature
from .dataset_reviewer import decode_json
from .dataset_reviews import _stored_receipt


MAX_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_JOURNAL_LINE = 16384


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("private_path_required")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private_directory_required")
    return path


def private_file(path: Path, flags: int, *, create: bool = False) -> int:
    private_directory(path.absolute().parent)
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("private_regular_file_required")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_private_json(path: Path, limit: int) -> dict:
    descriptor = private_file(path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("private_file_size_limit")
    return decode_json(payload)


@contextmanager
def exclusive_file(path: Path):
    descriptor = private_file(path, os.O_RDWR, create=True)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptor
    finally:
        os.close(descriptor)


def database_binding(path: Path) -> str:
    descriptor = private_file(path, os.O_RDONLY)
    try:
        info = os.fstat(descriptor)
        return digest({"path": str(path.absolute()), "device": info.st_dev, "inode": info.st_ino})
    finally:
        os.close(descriptor)


class ReceiptStore:
    def __init__(self, path: Path):
        self.connection = None
        self.lock = None
        database_binding(path)
        descriptor = private_file(Path(str(path) + ".lock"), os.O_RDWR, create=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock = descriptor
            self.connection = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True, timeout=1)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.execute("PRAGMA synchronous=FULL")
            deadline = time.monotonic() + AUDIT_TIMEOUT_SECONDS
            self.connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            self.connection.execute("BEGIN")
            if self.connection.execute("PRAGMA page_size").fetchone()[0] * self.connection.execute("PRAGMA page_count").fetchone()[0] > MAX_SNAPSHOT_BYTES:
                raise ValueError("review_database_size_limit")
            actual_signature = schema_signature(self.connection)
            actual_versions = [tuple(row) for row in self.connection.execute(
                "SELECT version,name FROM schema_migrations ORDER BY version")]
            known_schema = any(actual_signature == signature and actual_versions == versions
                               for signature, versions, _ in (expected_schema(version)
                                                              for version in SUPPORTED_SCHEMA_VERSIONS if version >= 8))
            if (not known_schema
                    or [tuple(row) for row in self.connection.execute("PRAGMA integrity_check")] != [("ok",)]
                    or self.connection.execute("PRAGMA foreign_key_check").fetchone()):
                raise ValueError("review_requires_valid_database")
            self.connection.rollback()
            self.connection.set_progress_handler(None, 0)
        except BaseException:
            if self.connection is not None:
                self.connection.close()
            os.close(descriptor)
            self.lock = None
            raise

    def insert(self, table: str, **values) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(dataset_reviews)")}
        if table != "dataset_reviews" or set(values) != columns:
            raise ValueError("receipt_write_only")
        self.connection.execute(f"INSERT INTO dataset_reviews ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))

    def close(self) -> None:
        self.connection.close()
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None


class ReviewJournal:
    def __init__(self, path: Path, binding: str, *, readonly: bool = False, baseline: str | None = None):
        flags = os.O_RDONLY if readonly else os.O_RDWR | os.O_APPEND
        self.descriptor = private_file(path, flags | (os.O_EXCL if baseline is not None else 0), create=baseline is not None)
        self.readonly = readonly
        self.poisoned = False
        self.records = []
        self.size = 0
        try:
            fcntl.flock(self.descriptor, (fcntl.LOCK_SH if readonly else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            if os.fstat(self.descriptor).st_size > MAX_JOURNAL_BYTES:
                raise ValueError("journal_size_limit")
            with os.fdopen(os.dup(self.descriptor), "rb") as stream:
                while line := stream.readline(MAX_JOURNAL_LINE + 1):
                    self.size += len(line)
                    if len(line) > MAX_JOURNAL_LINE or not line.endswith(b"\n") or self.size > MAX_JOURNAL_BYTES:
                        raise ValueError("journal_incomplete_or_oversized")
                    record = decode_json(line)
                    self.validate(record)
                    self.records.append(record)
            if not self.records:
                if readonly or baseline is None:
                    raise ValueError("journal_missing_header")
                self.append({"kind": "header", "database_binding": binding, "baseline_receipts_sha256": baseline, "created_at": now()})
                directory = os.open(path.absolute().parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            if self.records[0]["kind"] != "header" or self.records[0]["database_binding"] != binding:
                raise ValueError("journal_database_binding_differs")
        except BaseException:
            os.close(self.descriptor)
            raise

    def validate(self, record: dict) -> None:
        previous = self.records[-1]["sha256"] if self.records else "0" * 64
        if (not validator("dataset_review_journal").is_valid(record) or record["sequence"] != len(self.records)
                or record["previous_sha256"] != previous
                or record["sha256"] != digest({key: value for key, value in record.items() if key != "sha256"})):
            raise ValueError("journal_integrity_failure")
        if record["kind"] == "event" and not validator("dataset_reviewer_audit").is_valid(record["event"]):
            raise ValueError("journal_event_invalid")

    def append(self, payload: dict) -> None:
        if self.readonly or self.poisoned:
            raise ValueError("journal_readonly")
        record = {"journal_version": "1.0", "sequence": len(self.records),
                  "previous_sha256": self.records[-1]["sha256"] if self.records else "0" * 64, **payload}
        record["sha256"] = digest(record)
        self.validate(record)
        line = (canonical(record) + "\n").encode()
        if len(line) > MAX_JOURNAL_LINE or self.size + len(line) > MAX_JOURNAL_BYTES:
            raise ValueError("journal_size_limit")
        try:
            if os.write(self.descriptor, line) != len(line):
                raise OSError("journal_partial_write")
            os.fsync(self.descriptor)
        except BaseException:
            self.poisoned = True
            raise
        self.records.append(record)
        self.size += len(line)

    def __call__(self, event: dict) -> None:
        self.append({"kind": "event", "event": event})

    def reconcile(self, connection: sqlite3.Connection) -> dict:
        sessions = {}
        for record in self.records[1:]:
            event = record["event"]
            sessions.setdefault(event["authorization_ref"], []).append(event)
        counts = {"recorded": 0, "not_recorded": 0, "commit_audit_missing": 0}
        untracked = []
        for row in connection.execute("SELECT * FROM dataset_reviews"):
            receipt = _stored_receipt(row)
            if receipt["authorization_ref"] not in sessions:
                untracked.append(digest(receipt))
        if digest(sorted(untracked)) != self.records[0]["baseline_receipts_sha256"]:
            raise ValueError("journal_receipt_history_differs")
        for reference, events in sessions.items():
            rows = connection.execute("SELECT * FROM dataset_reviews WHERE authorization_ref=?", (reference,)).fetchall()
            confirmations = [event for event in events if event["stage"] == "confirmation"]
            committed = [event for event in events if event["stage"] == "committed"]
            if len(rows) > 1 or len(confirmations) > 1 or len(committed) > 1:
                raise ValueError("journal_receipt_integrity_failure")
            if rows:
                receipt = _stored_receipt(rows[0])
                if (not confirmations or confirmations[0]["event_sha256"] != digest(receipt)
                        or receipt["reviewer_id"] != f'linux-uid-{confirmations[0]["peer_uid"]}'
                        or any(event["event_sha256"] not in (None, digest(receipt)) for event in events)):
                    raise ValueError("journal_receipt_integrity_failure")
                counts["recorded"] += 1
                counts["commit_audit_missing"] += int(not committed)
            else:
                if committed:
                    raise ValueError("journal_receipt_missing")
                counts["not_recorded"] += 1
        return {"journal_version": "1.0", "sessions": len(sessions), **counts, "training_ready": False}

    def close(self) -> None:
        os.close(self.descriptor)

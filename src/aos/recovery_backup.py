import argparse
from contextlib import closing
import hashlib
import fcntl
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, canonical, digest
from .dataset_audit import AUDIT_TIMEOUT_SECONDS, MAX_SNAPSHOT_BYTES, audit_snapshot, expected_schema, schema_signature
from .dataset_review_journal import private_directory, private_file, read_private_json


class BackupManifest(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    format: Literal['aos_sqlite_snapshot_v1'] = 'aos_sqlite_snapshot_v1'
    database_file: Literal['store.sqlite'] = 'store.sqlite'
    database_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    source_snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    journal_mode: Literal['rollback'] = 'rollback'
    database_bytes: int = Field(gt=0, le=MAX_SNAPSHOT_BYTES)
    schema_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    schema_version_number: Literal[7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
    migrations: dict[str, str]
    captured_at: str
    execution_authorized: Literal[False] = False
    resume_authorized: Literal[False] = False


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new(path: Path, payload: bytes) -> None:
    descriptor = private_file(path, os.O_WRONLY | os.O_EXCL, create=True)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    sync_directory(path.absolute().parent)


def backup_database(database: Path, output: Path) -> dict:
    output = output.absolute()
    private_directory(output.parent)
    with audit_snapshot(database) as (snapshot, identity):
        payload = snapshot.serialize()
        if hashlib.sha256(payload).hexdigest() != identity['sha256']:
            raise ValueError('snapshot_changed')
        if len(payload) < 100 or payload[:16] != b'SQLite format 3\x00' or payload[18:20] not in (b'\x01\x01', b'\x02\x02'):
            raise ValueError('snapshot_header_unsupported')
        payload = payload[:18] + b'\x01\x01' + payload[20:]
        manifest = BackupManifest(database_sha256=hashlib.sha256(payload).hexdigest(), source_snapshot_sha256=identity['sha256'],
                                  database_bytes=len(payload), schema_sha256=identity['schema_sha256'],
                                  schema_version_number=len(identity['migrations']), migrations=identity['migrations'], captured_at=identity['captured_at'])
    output.mkdir(mode=0o700)
    sync_directory(output.parent)
    write_new(output / 'store.sqlite', payload)
    write_new(output / 'manifest.json', (canonical(manifest.model_dump()) + '\n').encode())
    return summary(manifest, 'backup_created')


def validated_backup(directory: Path) -> tuple[BackupManifest, bytes]:
    directory = private_directory(directory)
    if {path.name for path in directory.iterdir()} != {'manifest.json', 'store.sqlite'}:
        raise ValueError('backup_file_set_invalid')
    manifest = BackupManifest.model_validate(read_private_json(directory / 'manifest.json', 65536))
    descriptor = private_file(directory / 'store.sqlite', os.O_RDONLY)
    with os.fdopen(descriptor, 'rb') as stream:
        payload = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if (len(payload) != manifest.database_bytes or hashlib.sha256(payload).hexdigest() != manifest.database_sha256
            or payload[:16] != b'SQLite format 3\x00' or payload[18:20] != b'\x01\x01'):
        raise ValueError('backup_content_mismatch')
    if manifest.source_snapshot_sha256 not in {
            manifest.database_sha256, hashlib.sha256(payload[:18] + b'\x02\x02' + payload[20:]).hexdigest()}:
        raise ValueError('backup_source_binding_mismatch')
    signature, versions, migrations = expected_schema(manifest.schema_version_number)
    if manifest.schema_sha256 != signature or manifest.migrations != migrations:
        raise ValueError('backup_schema_pin_mismatch')
    deadline = time.monotonic() + AUDIT_TIMEOUT_SECONDS
    with closing(sqlite3.connect(':memory:')) as connection:
        connection.deserialize(payload)
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if (schema_signature(connection) != signature
                or connection.execute('SELECT version,name FROM schema_migrations ORDER BY version').fetchall() != versions
                or connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                or connection.execute('PRAGMA foreign_key_check').fetchone()):
            raise ValueError('backup_integrity_failure')
    return manifest, payload


def summary(manifest: BackupManifest, status: str) -> dict:
    return {'schema_version': '1.0', 'status': status, 'manifest_sha256': digest(manifest.model_dump()),
            'source_snapshot_sha256': manifest.source_snapshot_sha256,
            'database_sha256': manifest.database_sha256, 'database_bytes': manifest.database_bytes,
            'schema_version_number': manifest.schema_version_number, 'execution_authorized': False,
            'resume_authorized': False, 'runtime_assets_included': False, 'review_journal_included': False}


def verify_backup(directory: Path) -> dict:
    manifest, _ = validated_backup(directory)
    return summary(manifest, 'backup_verified')


def restore_backup(directory: Path, destination: Path) -> dict:
    destination = destination.absolute()
    private_directory(destination.parent)
    for suffix in ('', '-wal', '-shm', '-journal', '.lock'):
        path = Path(str(destination) + suffix)
        if path.exists() or path.is_symlink():
            raise ValueError('restore_destination_not_new')
    manifest, payload = validated_backup(directory)
    descriptor = private_file(Path(str(destination) + '.lock'), os.O_RDWR | os.O_EXCL, create=True)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_new(destination, payload)
    finally:
        os.close(descriptor)
    return summary(manifest, 'database_copy_restored_requires_reconciliation')


def main():
    parser = argparse.ArgumentParser(description='Private SQLite yedeği ve yeni dosyaya restore; runtime/lease/onay başlatmaz')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--database', type=Path, required=True)
    create.add_argument('--output', type=Path, required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--backup', type=Path, required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--backup', type=Path, required=True)
    restore.add_argument('--destination', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == 'create':
            result = backup_database(arguments.database, arguments.output)
        elif arguments.command == 'verify':
            result = verify_backup(arguments.backup)
        else:
            result = restore_backup(arguments.backup, arguments.destination)
        print(canonical(result))
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(1, 'Yedek işlemi başarısız; private izinleri, bütünlüğü ve yeni hedef koşulunu kontrol edin.\n')


if __name__ == '__main__':
    main()

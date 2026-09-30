"""Explicit host-side revocation and logical purge of one remote metadata outbox."""

import argparse
import os
from pathlib import Path
import sqlite3
import stat

from .contracts import canonical
from .dataset import validator
from .learning_event_outbox import _directory
from .learning_event_stream import _private_file
from .remote_learning_consent import RemoteLearningConsents
from .remote_learning_stream import STORE_NAME, _open_store, _store_for_consent
from .remote_route_evidence import CHECKSUM


def purge_revoked_remote_learning(*, consents: Path, consent_sha256: str,
                                  outbox_dir: Path) -> tuple[bool, bool]:
    RemoteLearningConsents(consents).revocation(consent_sha256)
    base = Path(outbox_dir)
    if base.is_symlink():
        raise ValueError('remote_learning_outbox_alias')
    if not base.exists():
        return False, False
    store_root = _store_for_consent(base, consent_sha256)
    if store_root.is_symlink():
        raise ValueError('remote_learning_outbox_alias')
    if not store_root.exists():
        return False, False
    try:
        connection, directory = _open_store(store_root, create=False)
    except FileNotFoundError:
        return False, False
    try:
        try:
            binding = connection.execute(
                'SELECT consent_sha256 FROM source_binding WHERE singleton=1').fetchone()
            if ((binding is None and (store_root == base or connection.execute(
                    'SELECT 1 FROM entries LIMIT 1').fetchone() is not None))
                    or (binding is not None and binding['consent_sha256'] != consent_sha256)):
                raise ValueError('remote_learning_purge_binding_changed')
        finally:
            connection.close()
        extra = set(os.listdir(directory)) - {STORE_NAME}
        if store_root != base and extra:
            raise ValueError('remote_learning_purge_sidecar_or_extra_file')
        if store_root == base:
            for name in extra:
                if CHECKSUM.fullmatch(name) is None:
                    raise ValueError('remote_learning_purge_sidecar_or_extra_file')
                child = _directory(base / name, create=False)
                os.close(child)
        metadata = _private_file(store_root / STORE_NAME)
        linked = os.stat(STORE_NAME, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(linked.st_mode) or linked.st_uid != os.getuid()
                or stat.S_IMODE(linked.st_mode) != 0o600 or linked.st_nlink != 1
                or (linked.st_dev, linked.st_ino, linked.st_size)
                != (metadata.st_dev, metadata.st_ino, metadata.st_size)):
            raise ValueError('remote_learning_purge_file_changed')
        os.unlink(STORE_NAME, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
    if store_root != base:
        parent = _directory(base, create=False)
        try:
            os.rmdir(store_root.name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
    return True, True


def revoke_remote_learning(*, consents: Path, consent_sha256: str,
                           confirm_sha256: str, outbox_dir: Path) -> dict:
    marker = RemoteLearningConsents(consents).revoke(
        consent_sha256, confirm_sha256=confirm_sha256)
    purged, present = purge_revoked_remote_learning(
        consents=consents, consent_sha256=consent_sha256, outbox_dir=outbox_dir)
    report = {'schema_version': '1.0', 'mode': 'explicit_remote_metadata_revocation',
              'consent_sha256': consent_sha256, 'revoked': True,
              'revoked_at': marker.revoked_at, 'outbox_present_before': present,
              'outbox_purged': purged, 'collection_authorized': False,
              'training_ready': False}
    validator('remote_learning_revocation_report').validate(report)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Revoke one local metadata consent and purge its exact outbox')
    parser.add_argument('--consents', type=Path, required=True)
    parser.add_argument('--consent-sha256', required=True)
    parser.add_argument('--confirm-sha256', required=True)
    parser.add_argument('--outbox-dir', type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = revoke_remote_learning(
            consents=arguments.consents, consent_sha256=arguments.consent_sha256,
            confirm_sha256=arguments.confirm_sha256, outbox_dir=arguments.outbox_dir)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, 'Remote learning revocation or purge incomplete; inspect the exact private stores.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()

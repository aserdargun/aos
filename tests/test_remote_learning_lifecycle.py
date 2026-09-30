import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.remote_learning_consent import RemoteLearningConsent, RemoteLearningConsents
from aos.remote_learning_lifecycle import revoke_remote_learning
from aos.remote_learning_stream import _open_store
from aos.remote_learning_stream import poll_remote_learning_stream


class RemoteLearningLifecycleTests(unittest.TestCase):
    def setUp(self):
        example = json.loads((REPO_ROOT / 'examples/remote_learning_consent.json').read_text())['consent']
        self.consent = RemoteLearningConsent.model_validate({
            **example, 'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()})
        self.checksum = digest(self.consent.model_dump())

    def register(self, root: Path) -> RemoteLearningConsents:
        store = RemoteLearningConsents(root)
        with patch('aos.remote_learning_consent.preview_remote_learning_consent',
                   return_value=self.consent):
            store.register(self.consent, confirm_sha256=self.checksum,
                           database=root.parent / 'unused.sqlite', profiles=root.parent / 'profiles')
        return store

    @staticmethod
    def bound_outbox(root: Path, checksum: str) -> Path:
        root.parent.mkdir(mode=0o700, exist_ok=True)
        connection, directory = _open_store(root)
        try:
            connection.execute('INSERT INTO source_binding VALUES(1,?,?,?,?,?,?,?,?)',
                               ('1' * 64, '2' * 64, 'synthetic-run', '3' * 64,
                                '4' * 64, '5' * 64, checksum, '6' * 64))
            connection.commit()
        finally:
            connection.close()
            os.close(directory)
        return root / 'remote-learning-stream.sqlite'

    def test_revocation_schema_and_exact_private_purge(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_learning_revocation.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name in ('revocation', 'report'):
            schema = json.loads((REPO_ROOT / f'schemas/remote_learning_{name if name == "revocation" else "revocation_report"}.schema.json').read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.Draft202012Validator(schema).validate(fixture[name])
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(schema).validate(
                    {**fixture[name], 'training_authorized' if name == 'revocation'
                     else 'training_ready': True})

        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            outbox = root / 'outbox'
            store = self.register(consents)
            path = self.bound_outbox(outbox / self.checksum, self.checksum)
            with self.assertRaises(ValueError):
                revoke_remote_learning(consents=consents, consent_sha256=self.checksum,
                                       confirm_sha256='0' * 64, outbox_dir=outbox)
            self.assertTrue(path.is_file())
            self.assertFalse((consents / (self.checksum + '.revoked.json')).exists())
            report = revoke_remote_learning(consents=consents, consent_sha256=self.checksum,
                                            confirm_sha256=self.checksum, outbox_dir=outbox)
            self.assertTrue(report['revoked'])
            self.assertTrue(report['outbox_present_before'])
            self.assertTrue(report['outbox_purged'])
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            marker = consents / (self.checksum + '.revoked.json')
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                store.get(self.checksum)
            with patch('aos.remote_learning_consent.preview_remote_learning_consent',
                       return_value=self.consent), self.assertRaises(ValueError):
                store.register(self.consent, confirm_sha256=self.checksum,
                               database=root / 'unused.sqlite', profiles=root / 'profiles')
            again = revoke_remote_learning(consents=consents, consent_sha256=self.checksum,
                                           confirm_sha256=self.checksum, outbox_dir=outbox)
            self.assertFalse(again['outbox_present_before'])
            self.assertFalse(again['outbox_purged'])
            self.assertEqual(again['revoked_at'], report['revoked_at'])

    def test_legacy_purge_preserves_other_consent_and_foreign_binding(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            outbox = root / 'outbox'
            self.register(consents)
            legacy = self.bound_outbox(outbox, self.checksum)
            other_checksum = 'b' * 64
            other = self.bound_outbox(outbox / other_checksum, other_checksum)
            report = revoke_remote_learning(consents=consents, consent_sha256=self.checksum,
                                            confirm_sha256=self.checksum, outbox_dir=outbox)
            self.assertTrue(report['outbox_purged'])
            self.assertFalse(legacy.exists())
            self.assertTrue(other.exists())

        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            outbox = root / 'outbox'
            store = self.register(consents)
            foreign = self.bound_outbox(outbox / self.checksum, 'b' * 64)
            with self.assertRaises(ValueError):
                revoke_remote_learning(consents=consents, consent_sha256=self.checksum,
                                       confirm_sha256=self.checksum, outbox_dir=outbox)
            self.assertTrue(foreign.exists())
            self.assertEqual(store.revocation(self.checksum).consent_sha256, self.checksum)
            with self.assertRaises(ValueError):
                store.get(self.checksum)

    def test_active_poll_lease_serializes_revocation(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            store = self.register(consents)
            polling = threading.Event()
            release = threading.Event()
            revoking = threading.Event()
            revoked = threading.Event()
            errors = []

            def blocked_poll(*arguments, **keywords):
                polling.set()
                if not release.wait(timeout=5):
                    raise AssertionError('poll_release_timeout')
                return {'metadata_only': True}

            def poll():
                try:
                    poll_remote_learning_stream(root / 'unused.sqlite', profiles=root / 'profiles',
                                                consents=consents, consent_sha256=self.checksum,
                                                outbox_dir=root / 'outbox')
                except BaseException as error:
                    errors.append(error)

            def revoke():
                revoking.set()
                try:
                    store.revoke(self.checksum, confirm_sha256=self.checksum)
                except BaseException as error:
                    errors.append(error)
                finally:
                    revoked.set()

            with patch('aos.remote_learning_stream._poll_authorized_remote_learning_stream',
                       side_effect=blocked_poll):
                poll_thread = threading.Thread(target=poll)
                revoke_thread = threading.Thread(target=revoke)
                poll_thread.start()
                self.assertTrue(polling.wait(timeout=5))
                revoke_thread.start()
                self.assertTrue(revoking.wait(timeout=5))
                self.assertFalse(revoked.wait(timeout=0.1))
                release.set()
                poll_thread.join(timeout=5)
                revoke_thread.join(timeout=5)
                self.assertFalse(poll_thread.is_alive())
                self.assertFalse(revoke_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(store.revocation(self.checksum).consent_sha256, self.checksum)

    def test_cli_requires_exact_confirmation_and_purges(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            consents = root / 'consents'
            outbox = root / 'outbox'
            self.register(consents)
            private_file = self.bound_outbox(outbox / self.checksum, self.checksum)
            command = [sys.executable, '-m', 'aos.remote_learning_lifecycle',
                       '--consents', str(consents), '--consent-sha256', self.checksum,
                       '--confirm-sha256', '0' * 64, '--outbox-dir', str(outbox)]
            denied = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                                    timeout=10, check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertTrue(private_file.exists())
            command[command.index('--confirm-sha256') + 1] = self.checksum
            accepted = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                                      timeout=10, check=False)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            report = json.loads(accepted.stdout)
            self.assertTrue(report['outbox_purged'])
            self.assertFalse(private_file.exists())


if __name__ == '__main__':
    unittest.main()

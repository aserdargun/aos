import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from aos.contracts import canonical, digest
from aos import native_inhibit as inhibit


class NativeInhibitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.store = inhibit.provision_native_inhibit(self.root / 'inhibit', scope_id='native-default-v1')
        self.source = self.root / 'synthetic-source'
        self.source.write_text('Synthetic CPU source pin; not a native worker')
        self.source.chmod(0o600)
        files = {str(self.source): hashlib.sha256(self.source.read_bytes()).hexdigest()}
        current = time.clock_gettime(time.CLOCK_BOOTTIME)
        self.request = inhibit.NativeInhibitRequest(
            request_id='native-inhibit-' + 'a' * 32, scope_id='native-default-v1', principal_id='synthetic-reviewer',
            expected_session='app-' + 'b' * 32, controller_session_id='desktop-session-' + 'c' * 32,
            generation=3, handover_sha256='1' * 64, shared_plan_sha256='2' * 64,
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            source_files=files, source_sha256=digest(files), config_files=files, config_sha256=digest(files),
            issued_boottime=current, expires_boottime=current + 600)

    def test_explicit_private_provision_no_adoption_and_external_binding(self):
        self.assertEqual((self.root / 'inhibit').stat().st_mode & 0o777, 0o700)
        self.assertEqual({path.name for path in (self.root / 'inhibit').iterdir()}, {inhibit.LOCK_NAME, inhibit.STORE_NAME})
        for path in (self.root / 'inhibit').iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(inhibit.read_native_inhibit(self.store))
        with self.assertRaises(FileExistsError):
            inhibit.provision_native_inhibit(self.root / 'inhibit', scope_id='native-default-v1')
        with self.assertRaises(ValueError):
            inhibit.acquire_native_lease(self.store.model_copy(update={'content_sha256': 'f' * 64}))

    def test_shared_native_leases_block_publication_and_recheck(self):
        with inhibit.acquire_native_lease(self.store) as first, inhibit.acquire_native_lease(self.store) as second:
            self.assertTrue(os.get_inheritable(first.fd))
            self.assertEqual(fcntl.fcntl(first.fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
            with self.assertRaisesRegex(ValueError, 'contention'):
                inhibit.publish_native_inhibit(self.store, self.request)
            first.assert_native_allowed()
            second.assert_native_allowed()
            self.assertFalse((self.root / 'inhibit' / inhibit.RECEIPT_NAME).exists())
        receipt = inhibit.publish_native_inhibit(self.store, self.request)
        self.assertFalse(receipt.worker_absence_verified)
        self.assertFalse(receipt.gpu_release_verified)
        with self.assertRaises(ValueError):
            inhibit.acquire_native_lease(self.store)

    def test_exclusive_contention_denies_native_without_wait(self):
        directory, descriptor = inhibit._open(self.store, writable=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'contention'):
                inhibit.acquire_native_lease(self.store)
            with self.assertRaisesRegex(ValueError, 'contention'):
                inhibit.read_native_inhibit(self.store)
        finally:
            os.close(descriptor)
            os.close(directory)

    def test_persistent_receipt_replay_conflict_and_expiry_never_reopen(self):
        receipt = inhibit.publish_native_inhibit(self.store, self.request)
        original = (self.root / 'inhibit' / inhibit.RECEIPT_NAME).read_bytes()
        for request in (self.request, self.request.model_copy(update={'generation': 4})):
            with self.assertRaises(ValueError):
                inhibit.publish_native_inhibit(self.store, request)
        with patch.object(inhibit.time, 'clock_gettime', return_value=self.request.expires_boottime + 100):
            self.assertEqual(inhibit.read_native_inhibit(self.store), receipt)
            with self.assertRaises(ValueError):
                inhibit.acquire_native_lease(self.store)
        self.assertEqual((self.root / 'inhibit' / inhibit.RECEIPT_NAME).read_bytes(), original)

    def test_missing_receipt_and_truncated_marker_do_not_reopen(self):
        inhibit.publish_native_inhibit(self.store, self.request)
        (self.root / 'inhibit' / inhibit.RECEIPT_NAME).unlink()
        with self.assertRaises(FileNotFoundError):
            inhibit.read_native_inhibit(self.store)
        with self.assertRaises(ValueError):
            inhibit.acquire_native_lease(self.store)
        (self.root / 'inhibit' / inhibit.LOCK_NAME).write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'publication altered'):
            inhibit.acquire_native_lease(self.store)
        with self.assertRaises(ValueError):
            inhibit.read_native_inhibit(self.store)

    def test_partial_publication_is_latched_without_cleanup_or_retry(self):
        original_write = inhibit._write
        def fail_receipt(descriptor, content):
            if b'"native_admission_inhibited"' in content:
                os.write(descriptor, b'{')
                os.fsync(descriptor)
                raise OSError('Synthetic publication crash')
            return original_write(descriptor, content)
        with patch.object(inhibit, '_write', side_effect=fail_receipt):
            with self.assertRaises(OSError):
                inhibit.publish_native_inhibit(self.store, self.request)
        self.assertGreater((self.root / 'inhibit' / inhibit.LOCK_NAME).stat().st_size, 0)
        for operation in (lambda: inhibit.read_native_inhibit(self.store),
                          lambda: inhibit.acquire_native_lease(self.store),
                          lambda: inhibit.publish_native_inhibit(self.store, self.request)):
            with self.assertRaises(ValueError):
                operation()

    def test_replaced_lock_directory_metadata_and_malformed_store_deny(self):
        operations = ['lock', 'directory', 'metadata', 'malformed', 'symlink', 'hardlink', 'mode']
        for operation in operations:
            with self.subTest(operation=operation):
                path = self.root / ('scope-' + operation)
                store = inhibit.provision_native_inhibit(path, scope_id='native-default-v1')
                if operation == 'directory':
                    path.rename(self.root / 'old-directory')
                    inhibit.provision_native_inhibit(path, scope_id='native-default-v1')
                elif operation in {'lock', 'metadata', 'symlink'}:
                    name = inhibit.STORE_NAME if operation == 'metadata' else inhibit.LOCK_NAME
                    content = (path / name).read_bytes()
                    (path / name).rename(path / 'old-file')
                    if operation == 'symlink':
                        (path / name).symlink_to(path / 'old-file')
                    else:
                        (path / name).write_bytes(content)
                        (path / name).chmod(0o600)
                elif operation == 'malformed':
                    (path / inhibit.STORE_NAME).write_text('{"version":"1","version":"1"}')
                elif operation == 'hardlink':
                    os.link(path / inhibit.LOCK_NAME, self.root / 'extra-link')
                else:
                    (path / inhibit.LOCK_NAME).chmod(0o644)
                with self.assertRaises((ValueError, OSError)):
                    inhibit.acquire_native_lease(store)

    def test_expired_future_oldboot_and_stale_source_denied_before_intent(self):
        invalid = [self.request.model_copy(update={'boot_id': '00000000-0000-0000-0000-000000000000'}),
                   self.request.model_copy(update={'issued_boottime': self.request.expires_boottime,
                                                   'expires_boottime': self.request.expires_boottime + 100}),
                   self.request.model_copy(update={'issued_boottime': 0.0, 'expires_boottime': 1.0})]
        for request in invalid:
            with self.assertRaises(ValueError):
                inhibit.publish_native_inhibit(self.store, request)
            self.assertIsNone(inhibit.read_native_inhibit(self.store))
        self.source.write_text('Synthetic changed pin')
        with self.assertRaises(ValueError):
            inhibit.publish_native_inhibit(self.store, self.request)
        self.assertIsNone(inhibit.read_native_inhibit(self.store))

    def test_expiry_during_source_reads_does_not_publish(self):
        expired = False
        original_read = inhibit.read_pinned_file
        def expire_after_read(*arguments, **keywords):
            nonlocal expired
            result = original_read(*arguments, **keywords)
            expired = True
            return result
        with patch.object(inhibit.time, 'clock_gettime', side_effect=lambda *_args: self.request.expires_boottime if expired else self.request.issued_boottime), \
                patch.object(inhibit, 'read_pinned_file', side_effect=expire_after_read) as read:
            with self.assertRaises(ValueError):
                inhibit.publish_native_inhibit(self.store, self.request)
        self.assertEqual(read.call_count, 1)
        self.assertIsNone(inhibit.read_native_inhibit(self.store))

    def test_synthetic_source_over_16_mib_fits_explicit_64_mib_bound(self):
        path = self.root / 'synthetic-large-source'
        with path.open('wb') as stream:
            stream.truncate(17 * 1024 * 1024)
        path.chmod(0o600)
        with path.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        sources = {str(path): checksum}
        request = self.request.model_copy(update={'source_files': sources, 'source_sha256': digest(sources)})
        self.assertEqual(inhibit.SOURCE_FILE_LIMIT, 64 * 1024 * 1024)
        receipt = inhibit.publish_native_inhibit(self.store, request)
        self.assertEqual(inhibit.read_native_inhibit(self.store), receipt)
        self.assertFalse(receipt.source_coverage_verified)

    def test_synthetic_source_over_64_mib_rejected_before_intent(self):
        path = self.root / 'synthetic-oversize-source'
        with path.open('wb') as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        path.chmod(0o600)
        sources = {str(path): '0' * 64}
        request = self.request.model_copy(update={'source_files': sources, 'source_sha256': digest(sources)})
        with self.assertRaises(ValueError):
            inhibit.publish_native_inhibit(self.store, request)
        self.assertEqual((self.root / 'inhibit' / inhibit.LOCK_NAME).stat().st_size, 0)
        self.assertFalse((self.root / 'inhibit' / inhibit.RECEIPT_NAME).exists())
        self.assertIsNone(inhibit.read_native_inhibit(self.store))

    def test_original_deadline_checked_before_each_source_and_config_read(self):
        additional = self.root / 'synthetic-additional-pin'
        additional.write_text('Synthetic second file pin')
        additional.chmod(0o600)
        additional_pin = {str(additional): hashlib.sha256(additional.read_bytes()).hexdigest()}
        for kind in ('source', 'config'):
            with self.subTest(kind=kind):
                files = dict(self.request.source_files if kind == 'source' else self.request.config_files) | additional_pin
                request = self.request.model_copy(update={kind + '_files': files, kind + '_sha256': digest(files)})
                readings = [request.issued_boottime] * (2 if kind == 'source' else 3) + [request.expires_boottime]
                with patch.object(inhibit.time, 'clock_gettime', side_effect=readings), \
                        patch.object(inhibit, 'read_pinned_file', wraps=inhibit.read_pinned_file) as read:
                    with self.assertRaises(ValueError):
                        inhibit.publish_native_inhibit(self.store, request)
                self.assertEqual(read.call_count, 1 if kind == 'source' else 2)
                self.assertIsNone(inhibit.read_native_inhibit(self.store))

    def test_expiry_after_durable_publication_requires_original_readback(self):
        committed = False
        original_write = inhibit._write
        def expire_after_receipt(descriptor, content):
            nonlocal committed
            original_write(descriptor, content)
            if b'"native_admission_inhibited"' in content:
                committed = True
        with patch.object(inhibit.time, 'clock_gettime', side_effect=lambda *_args: self.request.expires_boottime if committed else self.request.issued_boottime), \
                patch.object(inhibit, '_write', side_effect=expire_after_receipt):
            with self.assertRaisesRegex(ValueError, 'boot/time'):
                inhibit.publish_native_inhibit(self.store, self.request)
        receipt = inhibit.read_native_inhibit(self.store)
        self.assertEqual(receipt.request, self.request)
        self.assertTrue(receipt.native_admission_inhibited)
        self.assertFalse(receipt.shared_launch_authorized)
        with self.assertRaises(ValueError):
            inhibit.acquire_native_lease(self.store)
        with self.assertRaises(ValueError):
            inhibit.publish_native_inhibit(self.store, self.request)

    def test_readback_failure_after_publication_preserves_committed_inhibit(self):
        original_read = inhibit._read

        def unavailable_receipt(directory, name, expected_identity=None):
            if name == inhibit.RECEIPT_NAME:
                raise OSError('Synthetic postcommit readback failure')
            return original_read(directory, name, expected_identity)

        with patch.object(inhibit, '_read', side_effect=unavailable_receipt):
            with self.assertRaises(OSError):
                inhibit.publish_native_inhibit(self.store, self.request)
        self.assertEqual(inhibit.read_native_inhibit(self.store).request, self.request)
        with self.assertRaises(ValueError):
            inhibit.acquire_native_lease(self.store)
        with self.assertRaises(ValueError):
            inhibit.publish_native_inhibit(self.store, self.request)

    def test_parent_close_preserves_inherited_child_lease(self):
        lease = inhibit.acquire_native_lease(self.store)
        child = subprocess.Popen([sys.executable, '-c',
                                  'import os,sys; print("ready",flush=True); sys.stdin.buffer.read(1); os.close(int(sys.argv[1]))',
                                  str(lease.fd)], pass_fds=(lease.fd,), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline(), b'ready\n')
            lease.close()
            with self.assertRaisesRegex(ValueError, 'contention'):
                inhibit.publish_native_inhibit(self.store, self.request)
            child.communicate(b'x', timeout=5)
            self.assertEqual(child.returncode, 0)
            inhibit.publish_native_inhibit(self.store, self.request)
        finally:
            lease.close()
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_parent_crash_surviving_grandchild_retains_lock(self):
        program = '''import ctypes,json,os,subprocess,sys
from aos.native_inhibit import NativeInhibitStore,NativeInhibitRequest,acquire_native_lease,publish_native_inhibit
library=ctypes.CDLL(None,use_errno=True)
assert library.prctl(36,1,0,0,0)==0
store=NativeInhibitStore.model_validate_json(sys.argv[1])
request=NativeInhibitRequest.model_validate_json(sys.argv[2])
read_fd,write_fd=os.pipe()
parent=os.fork()
if parent==0:
    os.close(write_fd)
    lease=acquire_native_lease(store)
    code='import os,sys; print(os.getpid(),flush=True); os.read(int(sys.argv[2]),1); os.close(int(sys.argv[1]))'
    subprocess.Popen([sys.executable,'-c',code,str(lease.fd),str(read_fd)],pass_fds=(lease.fd,read_fd))
    os._exit(17)
os.close(read_fd)
assert os.waitpid(parent,0)[1]>>8==17
try:
    publish_native_inhibit(store,request)
except ValueError as error:
    assert 'contention' in str(error)
else:
    raise AssertionError('Surviving child lease lost')
os.write(write_fd,b'x')
os.close(write_fd)
child,status=os.waitpid(-1,0)
assert status==0
publish_native_inhibit(store,request)
print('parent-crash-inherited-lock-ok',flush=True)
'''
        result = subprocess.run([sys.executable, '-c', program, self.store.model_dump_json(), self.request.model_dump_json()],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('parent-crash-inherited-lock-ok', result.stdout)

    def test_canonical_receipt_schema_rejects_gpu_authority(self):
        receipt = inhibit.publish_native_inhibit(self.store, self.request)
        models = [('native_inhibit_store', self.store), ('native_inhibit_request', self.request),
                  ('native_inhibit_receipt', receipt)]
        for name, model in models:
            schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas' / (name + '.schema.json')).read_text())
            self.assertEqual(schema, type(model).model_json_schema() | {'$schema': 'https://json-schema.org/draft/2020-12/schema'})
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.Draft202012Validator(schema).validate(model.model_dump(mode='json'))
        validator = jsonschema.Draft202012Validator(schema)
        value = receipt.model_dump(mode='json')
        validator.validate(value)
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(value | {'gpu_release_verified': True})


if __name__ == '__main__':
    unittest.main()

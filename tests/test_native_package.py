import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT
from aos.dataset import validator
from scripts.package_native import (LOCK_FILES, SOURCE_FILES, NativePackageManifest,
                                    create_package, verify_package)


class NativePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aos-native-package-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'runs').mkdir()
        self.binary = bytearray(128)
        self.binary[:7] = b'\x7fELF\x02\x01\x01'
        self.binary[16:18] = (3).to_bytes(2, 'little')
        self.binary[18:20] = (62).to_bytes(2, 'little')
        self.binary[52:54] = (64).to_bytes(2, 'little')
        self.binary_path = self.root / 'ui/src-tauri/target/debug/aos-console'
        self.binary_path.parent.mkdir(parents=True)
        self.binary_path.write_bytes(self.binary)
        for name in (*SOURCE_FILES, *LOCK_FILES):
            source = self.root / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('explicitly synthetic source or dependency lock\n')
        self.output = self.root / 'runs/package-one'

    def create(self):
        return create_package(self.root, self.output, 'debug', synthetic=True)

    def rewrite_archive(self, transform):
        target = self.output / 'bundle.tar'
        with tarfile.open(target, 'r:') as archive:
            members = [(member, archive.extractfile(member).read()) for member in archive.getmembers()]
        members = transform(members)
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for member, payload in members:
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        payload = output.getvalue()
        target.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def test_private_deterministic_archive_and_read_only_verification(self):
        first = self.create()
        second = create_package(self.root, self.root / 'runs/package-two', 'debug', synthetic=True)
        self.assertEqual(first, second)
        self.assertTrue(first['synthetic'])
        self.assertFalse(first['standalone_inference'])
        self.assertFalse(first['build_provenance_verified'])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        target = self.output / 'bundle.tar'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        before = target.read_bytes()
        report = verify_package(self.output, first['archive_sha256'])
        self.assertEqual(report['status'], 'native_shell_package_integrity_verified')
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(set(self.output.iterdir()), {target})
        with tarfile.open(target, 'r:') as archive:
            self.assertEqual(archive.getnames(), ['manifest.json', 'aos-console'])
            manifest = json.load(archive.extractfile('manifest.json'))
            self.assertEqual(set(manifest['source_sha256']), set(SOURCE_FILES))
            self.assertEqual(set(manifest['dependency_lock_sha256']), set(LOCK_FILES))

    def test_existing_target_never_overwritten(self):
        report = self.create()
        with self.assertRaises(FileExistsError):
            self.create()
        verify_package(self.output, report['archive_sha256'])

    def test_output_scope_parent_traversal_and_symlink_refused(self):
        for output in (self.root / 'data/package', self.root / 'runs/../escape', self.root / 'runs/nested/package'):
            with self.assertRaises(ValueError):
                create_package(self.root, output, 'debug', synthetic=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'runs').rmdir()
        (self.root / 'runs').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.create()
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_and_binary_symlinks_refused(self):
        for path in (self.binary_path, self.root / SOURCE_FILES[0]):
            original = path.read_bytes()
            path.unlink()
            source = self.root / 'linked-source'
            source.write_bytes(original)
            path.symlink_to(source)
            with self.assertRaises(OSError):
                self.create()
            self.assertFalse(self.output.exists())
            path.unlink()
            path.write_bytes(original)

    def test_cargo_hardlinked_input_binary_allowed_but_archive_hardlink_refused(self):
        os.link(self.binary_path, self.root / 'cargo-binary-link')
        report = self.create()
        os.link(self.output / 'bundle.tar', self.root / 'archive-link')
        with self.assertRaises(ValueError):
            verify_package(self.output, report['archive_sha256'])

    def test_invalid_elf_profile_and_missing_build_are_not_packages(self):
        for payload in (b'synthetic shell text', bytes(128), b'\x7fELF' + bytes(124)):
            self.binary_path.write_bytes(payload)
            with self.assertRaises(ValueError):
                self.create()
        self.binary_path.write_bytes(self.binary)
        with self.assertRaises(ValueError):
            create_package(self.root, self.output, 'unknown', synthetic=True)
        with self.assertRaises(FileNotFoundError):
            create_package(self.root, self.output, 'release', synthetic=True)
        self.assertFalse(self.output.exists())

    def test_external_digest_required_and_tamper_detected(self):
        report = self.create()
        for digest in ('', 'f' * 64, '../outside'):
            with self.assertRaises(ValueError):
                verify_package(self.output, digest)
        target = self.output / 'bundle.tar'
        target.write_bytes(target.read_bytes() + b'synthetic appended bytes')
        with self.assertRaises(ValueError):
            verify_package(self.output, report['archive_sha256'])
        with self.assertRaisesRegex(ValueError, 'archive_not_canonical'):
            verify_package(self.output, hashlib.sha256(target.read_bytes()).hexdigest())

    def test_private_modes_extra_files_and_archive_symlink_refused(self):
        report = self.create()
        target = self.output / 'bundle.tar'
        for path, mode, original in ((self.output, 0o755, 0o700), (target, 0o644, 0o600)):
            path.chmod(mode)
            with self.assertRaises(ValueError):
                verify_package(self.output, report['archive_sha256'])
            path.chmod(original)
        extra = self.output / 'token'
        extra.write_text('explicit synthetic token')
        with self.assertRaises(ValueError):
            verify_package(self.output, report['archive_sha256'])
        extra.unlink()
        moved = self.root / 'moved.tar'
        target.rename(moved)
        target.symlink_to(moved)
        with self.assertRaises(OSError):
            verify_package(self.output, report['archive_sha256'])

    def test_rehashed_path_escape_duplicate_and_link_archive_refused(self):
        report = self.create()
        original = (self.output / 'bundle.tar').read_bytes()
        for name, member_type in (('../escape', tarfile.REGTYPE), ('manifest.json', tarfile.REGTYPE),
                                  ('aos-console', tarfile.SYMTYPE)):
            def transform(members):
                members[1][0].name = name
                members[1][0].type = member_type
                return members
            digest = self.rewrite_archive(transform)
            with self.assertRaises(ValueError):
                verify_package(self.output, digest)
            (self.output / 'bundle.tar').write_bytes(original)
        verify_package(self.output, report['archive_sha256'])
        self.assertFalse((self.root / 'escape').exists())

    def test_rehashed_authority_provenance_missing_pin_and_binary_tamper_refused(self):
        self.create()
        original = (self.output / 'bundle.tar').read_bytes()
        for change in ({'promotion_authorized': True}, {'build_provenance_verified': True},
                       {'source_sha256': {}}, {'artifact_sha256': 'f' * 64}, {'raw_token': 'synthetic'}):
            def transform(members):
                manifest = {**json.loads(members[0][1]), **change}
                return [(members[0][0], json.dumps(manifest).encode()), members[1]]
            digest = self.rewrite_archive(transform)
            with self.assertRaises(ValueError):
                verify_package(self.output, digest)
            (self.output / 'bundle.tar').write_bytes(original)

    def test_fsync_failure_leaves_non_success_and_no_overwrite(self):
        with patch('scripts.package_native.os.fsync', side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(OSError):
                self.create()
        self.assertTrue(self.output.exists())
        with self.assertRaises(FileExistsError):
            self.create()
        with self.assertRaises(ValueError):
            verify_package(self.output, 'f' * 64)

    def test_world_writable_output_parent_refused(self):
        (self.root / 'runs').chmod(0o777)
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.output.exists())

    def test_cli_missing_input_does_not_create_or_leak_path(self):
        missing = self.root / 'missing-private-path'
        result = subprocess.run([sys.executable, str(REPO_ROOT / 'scripts/package_native.py'), 'verify',
                                 '--package', str(missing), '--sha256', 'f' * 64],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(str(missing), result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertFalse(missing.exists())

    def test_canonical_schema_and_explicit_synthetic_fixture(self):
        schema = json.loads((REPO_ROOT / 'schemas/native_package.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                  **NativePackageManifest.model_json_schema()})
        fixture = json.loads((REPO_ROOT / 'examples/native_package.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('native_package').validate(fixture['manifest'])
        self.assertTrue(NativePackageManifest.model_validate(fixture['manifest']).synthetic)
        for change in ({'source_sha256': {}}, {'dependency_lock_sha256': {'../private': 'a' * 64}},
                       {'installation_authorized': True}, {'promotion_authorized': True},
                       {'build_provenance_verified': True}, {'raw_token': 'synthetic'}):
            self.assertFalse(validator('native_package').is_valid({**fixture['manifest'], **change}))
        self.create()
        with tarfile.open(self.output / 'bundle.tar', 'r:') as archive:
            validator('native_package').validate(json.load(archive.extractfile('manifest.json')))


if __name__ == '__main__':
    unittest.main()

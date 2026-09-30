import hashlib
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import package_handoff as handoff
from scripts import update_manifest


class PackageHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'source'
        self.root.mkdir()
        for directory in handoff.SOURCE_DIRECTORIES:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
            (self.root / directory / 'README.md').write_text('synthetic directory source\n')
        for name in (*handoff.ROOT_FILES, *handoff.EXPLICIT_FILES, 'scripts/aos-v1',
                     'src/aos/example.py', 'services/laya/worker.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('explicitly synthetic source\n')
        (self.root / 'scripts/aos-v1').chmod(0o751)
        self.output = self.root.parent / 'handoff.tar'
        self.manifest()

    def manifest(self):
        with patch.object(update_manifest, 'ROOT', self.root):
            update_manifest.main()

    def test_roundtrip_deterministic_source_only_and_safe_modes(self):
        for name in ('data/private.sqlite', 'models/model.safetensors', 'runs/token.txt',
                     '.codex/config.toml', '.env'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private excluded synthetic sentinel')
        result = handoff.package_handoff(self.root, self.output)
        second = self.root.parent / 'second.tar'
        self.assertEqual(result, handoff.package_handoff(self.root, second))
        self.assertEqual(self.output.read_bytes(), second.read_bytes())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'private excluded synthetic sentinel', self.output.read_bytes())
        extracted = self.root.parent / 'extracted'
        extracted.mkdir()
        with tarfile.open(self.output) as archive:
            names = archive.getnames()
            self.assertIn('MANIFEST.sha256', names)
            self.assertIn('CLAUDE.md', names)
            self.assertIn('services/laya/worker.py', names)
            self.assertEqual(archive.getmember('scripts/aos-v1').mode, 0o755)
            self.assertEqual(archive.getmember('README.md').mode, 0o644)
            self.assertTrue(all(member.isfile() and member.uid == member.gid == member.mtime == 0
                                for member in archive.getmembers()))
            archive.extractall(extracted, filter='data')
        self.assertEqual([member.name for member in handoff.validate_source_manifest(extracted)], names)
        self.assertTrue(result['source_only'])
        self.assertFalse(result['runtime_bundled'])
        self.assertFalse(result['complete_secret_audit'])

    def test_optional_license_and_notice_included_only_when_present(self):
        self.assertNotIn('LICENSE', handoff.collect_source_paths(self.root))
        (self.root / 'LICENSE').write_text('synthetic test license placeholder, not project license')
        (self.root / 'NOTICE.md').write_text('synthetic test notice')
        self.manifest()
        names = [member.name for member in handoff.validate_source_manifest(self.root)]
        self.assertIn('LICENSE', names)
        self.assertIn('NOTICE.md', names)

    def test_existing_destination_and_symlink_output_are_not_overwritten(self):
        self.output.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            handoff.package_handoff(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'existing')
        self.output.unlink()
        self.output.symlink_to(self.root / 'README.md')
        with self.assertRaises(FileExistsError):
            handoff.package_handoff(self.root, self.output)
        self.assertEqual((self.root / 'README.md').read_text(), 'explicitly synthetic source\n')

    def test_manifest_traversal_absolute_duplicate_and_unknown_members_rejected(self):
        filename = self.root / 'MANIFEST.sha256'
        original = filename.read_bytes()
        for name in ('../README.md', '/etc/passwd', 'docs/../README.md', 'docs//guide.md',
                     'models/model.json', 'docs/token.pem', 'README.md'):
            with self.subTest(name=name):
                filename.write_bytes(original + ('0' * 64 + '  ' + name + '\n').encode())
                with self.assertRaises(ValueError):
                    handoff.validate_source_manifest(self.root)
        filename.write_bytes(original)

    def test_stale_hash_missing_and_unlisted_source_rejected_before_output(self):
        source = self.root / 'src/aos/example.py'
        source.write_text('changed')
        with self.assertRaises(ValueError):
            handoff.package_handoff(self.root, self.output)
        self.assertFalse(self.output.exists())
        self.manifest()
        source.unlink()
        with self.assertRaises(ValueError):
            handoff.validate_source_manifest(self.root)
        source.write_text('changed')
        (self.root / 'src/aos/unlisted.py').write_text('new source')
        with self.assertRaises(ValueError):
            handoff.validate_source_manifest(self.root)

    def test_symlink_file_parent_and_hardlink_rejected(self):
        filename = self.root / 'src/aos/example.py'
        filename.unlink()
        filename.symlink_to(self.root / 'README.md')
        with self.assertRaises((ValueError, OSError)):
            handoff.validate_source_manifest(self.root)
        filename.unlink()
        os.link(self.root / 'README.md', filename)
        with self.assertRaises(ValueError):
            handoff.validate_source_manifest(self.root)
        filename.unlink()
        filename.write_text('explicitly synthetic source\n')
        directory = self.root / 'src/aos'
        moved = self.root.parent / 'moved'
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            handoff.validate_source_manifest(self.root)

    def test_fifo_and_unsafe_source_artifacts_rejected(self):
        filename = self.root / 'src/aos/example.py'
        filename.unlink()
        os.mkfifo(filename)
        with self.assertRaises(ValueError):
            handoff.validate_source_manifest(self.root)
        filename.unlink()
        filename.write_text('explicitly synthetic source\n')
        for name in ('docs/private.sqlite', 'tests/model.safetensors', 'config/.env',
                     'examples/token.json', 'examples/real-data.jsonl', 'docs/screenshot.png'):
            with self.subTest(name=name):
                artifact = self.root / name
                artifact.write_text('synthetic unsafe artifact')
                with self.assertRaises(ValueError):
                    handoff.collect_source_paths(self.root)
                artifact.unlink()

    def test_private_key_and_token_patterns_fail_before_export(self):
        filename = self.root / 'src/aos/example.py'
        for payload in ('-----BEGIN ' + 'PRIVATE KEY-----', 'hf_' + 'A' * 30):
            filename.write_text(payload)
            with self.assertRaises(ValueError):
                self.manifest()

    def test_file_replacement_during_read_rejected(self):
        filename = self.root / 'src/aos/example.py'
        original_read = os.read
        replaced = False

        def replace(descriptor, size):
            nonlocal replaced
            data = original_read(descriptor, size)
            if not replaced:
                replaced = True
                filename.unlink()
                filename.write_bytes(data)
            return data

        with patch.object(handoff.os, 'read', side_effect=replace):
            with self.assertRaises(ValueError):
                handoff.read_source_member(self.root, 'src/aos/example.py')

    def test_file_total_and_count_bounds(self):
        with patch.object(handoff, 'MAX_TOTAL_BYTES', 1):
            with self.assertRaises(ValueError):
                handoff.validate_source_manifest(self.root)
        with patch.object(handoff, 'MAX_FILES', 1):
            with self.assertRaises(ValueError):
                handoff.validate_source_manifest(self.root)
        with self.assertRaises(ValueError):
            handoff.read_source_member(self.root, 'README.md', 1)

    def test_updater_uses_exact_shared_inventory_and_excludes_caches(self):
        cache = self.root / 'scripts/__pycache__'
        cache.mkdir()
        (cache / 'private.pyc').write_bytes(b'cache')
        self.manifest()
        members = handoff.validate_source_manifest(self.root)
        self.assertFalse(any('__pycache__' in member.name for member in members))
        lines = (self.root / 'MANIFEST.sha256').read_text().splitlines()
        self.assertEqual(len(lines), len(members) - 1)
        for member in members:
            if member.name != 'MANIFEST.sha256':
                self.assertIn(hashlib.sha256(member.data).hexdigest() + '  ' + member.name, lines)

    def test_manifest_symlink_hardlink_and_unsafe_output_parent(self):
        manifest = self.root / 'MANIFEST.sha256'
        target = self.root.parent / 'unrelated.txt'
        target.write_text('unchanged')
        manifest.unlink()
        manifest.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.manifest()
        self.assertEqual(target.read_text(), 'unchanged')
        manifest.unlink()
        os.link(target, manifest)
        with self.assertRaises(ValueError):
            self.manifest()
        self.assertEqual(target.read_text(), 'unchanged')
        manifest.unlink()
        self.manifest()
        link = self.root.parent / 'output-link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            handoff.package_handoff(self.root, link / 'source.tar')
        self.assertFalse((self.root / 'source.tar').exists())

    def test_archive_failure_removes_only_new_incomplete_output(self):
        with patch.object(handoff.tarfile.TarFile, 'addfile', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                handoff.package_handoff(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_updater_failure_before_replace_preserves_previous_manifest(self):
        filename = self.root / 'MANIFEST.sha256'
        previous = filename.read_bytes()
        (self.root / 'src/aos/example.py').write_text('changed source')
        with patch.object(update_manifest.os, 'replace', side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(OSError):
                self.manifest()
        self.assertEqual(filename.read_bytes(), previous)
        self.assertEqual(list(self.root.glob('.manifest-*.tmp')), [])

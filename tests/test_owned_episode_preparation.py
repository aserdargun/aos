import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from aos.contracts import digest
from aos.owned_episode_preparation import OwnedEpisodePreparation
import test_owned_episode_learning as store_helpers


class OwnedEpisodePreparationTests(unittest.TestCase):
    setUp = store_helpers.OwnedEpisodeStoreTests.setUp
    accept = store_helpers.OwnedEpisodeStoreTests.accept

    def prepared(self):
        exported = self.store.export(self.episode_id, self.candidates, self.execution, self.accept())
        self.inspection = {'reviewable': True, 'candidates': self.candidates,
                           'execution_source_sha256': self.execution['execution_source_sha256']}
        self.learning = SimpleNamespace(store=self.store, inspect=Mock(return_value=self.inspection),
                                        scheduler=SimpleNamespace())
        self.preparation = OwnedEpisodePreparation(self.learning)
        return exported['export_sha256']

    def inventory(self):
        return {path.name: path.read_bytes() for path in (self.store.root / self.episode_id).iterdir()}

    def test_preview_is_read_only_and_explicit_publish_is_deterministic(self):
        checksum = self.prepared()
        before = self.inventory()
        preview = self.preparation.preview(self.episode_id, checksum)
        self.assertEqual(before, self.inventory())
        self.assertFalse(preview['persisted'])
        self.assertFalse(preview['training_ready'])
        self.assertFalse(preview['system1']['tokenizer_start_available'])
        with self.assertRaises(ValueError):
            self.preparation.publish(self.episode_id, checksum, '0' * 64)
        result = self.preparation.publish(self.episode_id, checksum, preview['conversion_sha256'])
        self.assertTrue(result['persisted'])
        published = self.inventory()
        reopened = OwnedEpisodePreparation(self.learning)
        self.assertEqual(reopened.inspect(self.episode_id, preview['conversion_sha256']), result)
        self.assertEqual(reopened.publish(self.episode_id, checksum, preview['conversion_sha256']), result)
        self.assertEqual(published, self.inventory())

    def test_missing_or_changed_export_is_not_recreated_by_preview(self):
        checksum = self.prepared()
        directory = self.store.root / self.episode_id
        path = directory / (checksum + '-system1.jsonl')
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.preparation.preview(self.episode_id, checksum)
        self.assertFalse(path.exists())
        with self.store.directory(self.episode_id) as descriptor:
            self.store._put_bytes(descriptor, path.name, original + b'\n')
        with self.assertRaisesRegex(ValueError, 'export_bytes_changed'):
            self.preparation.preview(self.episode_id, checksum)

    def test_source_drift_revocation_and_converted_byte_changes_fail_closed(self):
        checksum = self.prepared()
        preview = self.preparation.preview(self.episode_id, checksum)
        self.preparation.publish(self.episode_id, checksum, preview['conversion_sha256'])
        self.inspection['execution_source_sha256'] = 'c' * 64
        with self.assertRaises(ValueError):
            self.preparation.inspect(self.episode_id, preview['conversion_sha256'])
        self.inspection['execution_source_sha256'] = self.execution['execution_source_sha256']
        path = self.store.root / self.episode_id / (preview['conversion_sha256'] + '-system2.converted.jsonl')
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'conversion_source_changed'):
            self.preparation.inspect(self.episode_id, preview['conversion_sha256'])
        receipt = self.store.reviews(self.episode_id)['system2']['receipt_sha256']
        self.store.revoke(self.episode_id, 'system2', receipt)
        with self.assertRaises(ValueError):
            self.preparation.preview(self.episode_id, checksum)

    def test_export_and_conversion_have_exact_role_memberships_without_fake_splits(self):
        checksum = self.prepared()
        manifest, files, converted = self.preparation.material(self.episode_id, checksum)
        self.assertEqual(manifest['split'], 'development_only')
        self.assertEqual(manifest['review_receipts'], {role: value['receipt_sha256']
                         for role, value in self.store.reviews(self.episode_id).items()})
        for role in ('system1', 'system2'):
            rows = [json.loads(line) for line in files[role + '.converted.jsonl'].splitlines()]
            self.assertEqual(rows, converted[role])
            self.assertEqual(manifest['memberships'][role], [{
                'source_record_sha256': row['source_record_sha256'],
                'converted_record_sha256': digest(row)} for row in rows])
            self.assertTrue(all(row['split_group'] == manifest['source_group_sha256'] for row in rows))

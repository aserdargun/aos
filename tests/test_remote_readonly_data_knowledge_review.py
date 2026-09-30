import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.remote_readonly_data_knowledge_review import (
    RemoteReadonlyDataKnowledgeCandidatePreview,
    LiveRemoteReadonlyDataKnowledgeEvent,
    LiveRemoteReadonlyDataKnowledgePin,
    RemoteReadonlyDataKnowledgeReview,
    RemoteReadonlyDataKnowledgeReviewStore,
    register_remote_readonly_data_knowledge_review,
    recheck_remote_readonly_data_knowledge)
from aos.storage import TrajectoryStore


class RemoteReadonlyDataKnowledgeReviewTests(unittest.TestCase):
    def test_synthetic_live_pin_and_events_keep_authority_false(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_knowledge_live.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for key, schema_name, model in (
                ('pin', 'remote_readonly_data_knowledge_live_pin',
                 LiveRemoteReadonlyDataKnowledgePin),
                ('matched_event', 'remote_readonly_data_knowledge_live_event',
                 LiveRemoteReadonlyDataKnowledgeEvent),
                ('stale_event', 'remote_readonly_data_knowledge_live_event',
                 LiveRemoteReadonlyDataKnowledgeEvent)):
            schema = json.loads((REPO_ROOT / 'schemas' / (schema_name + '.schema.json')).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.Draft202012Validator(schema).validate(fixture[key])
            self.assertFalse(model.model_validate(fixture[key]).task_retrieval_authorized)
            for field in ('task_retrieval_authorized', 'execution_authorized',
                          'collection_authorized', 'training_ready'):
                with self.subTest(key=key, field=field), self.assertRaises(jsonschema.ValidationError):
                    jsonschema.Draft202012Validator(schema).validate({
                        **fixture[key], field: True})
        with self.assertRaises(ValueError):
            LiveRemoteReadonlyDataKnowledgeEvent.model_validate({
                **fixture['stale_event'], 'page_key': 'summary'})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({
                **fixture['stale_event'], 'page_key': 'summary'})
        with self.assertRaises(ValueError):
            LiveRemoteReadonlyDataKnowledgeEvent.model_validate({
                **fixture['matched_event'], 'page_key': None})
        with self.assertRaises(ValueError):
            LiveRemoteReadonlyDataKnowledgeEvent.model_validate({
                **fixture['matched_event'],
                'current_fingerprint_sha256': '0' * 64})
        with self.assertRaises(ValueError):
            LiveRemoteReadonlyDataKnowledgeEvent.model_validate({
                **fixture['stale_event'],
                'current_fingerprint_sha256': fixture['stale_event']['expected_fingerprint_sha256']})

    def test_synthetic_ui_candidate_requires_exact_hash_and_false_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_knowledge_ui_preview.json').read_text())
        self.assertTrue(fixture['synthetic'])
        preview = fixture['preview']
        schema = json.loads((REPO_ROOT / 'schemas/remote_readonly_data_knowledge_ui_preview.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(preview)
        self.assertEqual(digest(preview['candidate']), preview['candidate_sha256'])
        self.assertEqual(RemoteReadonlyDataKnowledgeCandidatePreview.model_validate(
            preview).candidate['page_key'], 'summary')
        with self.assertRaises(ValueError):
            RemoteReadonlyDataKnowledgeCandidatePreview.model_validate({
                **preview, 'candidate_sha256': '0' * 64})
        for field in ('task_retrieval_authorized', 'execution_authorized',
                      'collection_authorized', 'training_ready'):
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(schema).validate({
                    **preview, 'candidate': {**preview['candidate'], field: True}})

    def test_synthetic_review_receipt_and_recheck_do_not_grant_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_knowledge_review.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name, key in (
                ('remote_readonly_data_knowledge_review', 'record'),
                ('remote_readonly_data_knowledge_review_receipt', 'receipt'),
                ('remote_readonly_data_knowledge_recheck', 'recheck')):
            schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            validate = jsonschema.Draft202012Validator(schema).validate
            validate(fixture[key])
            for field in ('task_retrieval_authorized', 'execution_authorized',
                          'collection_authorized', 'training_ready'):
                with self.subTest(name=name, field=field), self.assertRaises(jsonschema.ValidationError):
                    validate({**fixture[key], field: True})
            with self.assertRaises(jsonschema.ValidationError):
                validate({**fixture[key], 'raw_json': {'secret': True}})
        self.assertEqual(digest(fixture['record']), fixture['receipt']['review_sha256'])
        self.assertEqual(fixture['receipt']['review_sha256'],
                         fixture['recheck']['review_sha256'])

    def test_private_review_store_rejects_unsafe_files(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_knowledge_review.json').read_text())
        record = RemoteReadonlyDataKnowledgeReview.model_validate(fixture['record'])
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            store = RemoteReadonlyDataKnowledgeReviewStore(root / 'reviews')
            checksum = store.register(record)
            self.assertEqual(checksum, fixture['receipt']['review_sha256'])
            self.assertEqual(store.register(record), checksum)
            self.assertEqual(store.get(checksum), record)
            stored = store.root / (checksum + '.json')
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
            stored.chmod(0o644)
            with self.assertRaises(ValueError):
                store.get(checksum)
            stored.chmod(0o600)
            alias = store.root / 'alias.json'
            os.link(stored, alias)
            with self.assertRaises(ValueError):
                store.get(checksum)
            alias.unlink()
            self.assertEqual(store.get(checksum), record)
            with self.assertRaises(ValueError):
                RemoteReadonlyDataKnowledgeReviewStore(Path('/tmp/aos-outside-reviews'))

    def test_missing_source_or_acknowledgement_never_writes_review(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            database = root / 'trajectory.sqlite'
            store = TrajectoryStore(database)
            store.close()
            selection = {'profiles': root / 'profiles', 'site_store': root / 'pages',
                         'review_store': root / 'reviews', 'knowledge_sha256': 'a' * 64,
                         'selected_profile_sha256': 'b' * 64,
                         'selected_plan_sha256': 'c' * 64,
                         'confirm_candidate_sha256': 'd' * 64}
            with self.assertRaises(ValueError):
                register_remote_readonly_data_knowledge_review(
                    database, 'before', 'after',
                    **selection, acknowledge_metadata_only=False)
            self.assertFalse((root / 'reviews').exists())
            with self.assertRaises((ValueError, FileNotFoundError)):
                register_remote_readonly_data_knowledge_review(
                    database, 'before', 'after',
                    **selection, acknowledge_metadata_only=True)
            self.assertFalse((root / 'reviews').exists())
            with self.assertRaises(FileNotFoundError):
                recheck_remote_readonly_data_knowledge(
                    database, 'current', profiles=root / 'profiles',
                    site_store=root / 'pages', review_store=root / 'reviews',
                    review_sha256='e' * 64,
                    selected_profile_sha256='b' * 64,
                    selected_plan_sha256='c' * 64)
            command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_knowledge_review',
                'register', '--database', str(database),
                '--profiles', str(root / 'profiles'),
                '--site-store', str(root / 'pages'),
                '--review-store', str(root / 'reviews'),
                '--selected-profile-sha256', 'b' * 64,
                '--selected-plan-sha256', 'c' * 64,
                '--before-run-id', 'before', '--after-run-id', 'after',
                '--knowledge-sha256', 'a' * 64,
                '--confirm-candidate-sha256', 'd' * 64,
                '--acknowledge-metadata-only'],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(command.returncode, 1)
            self.assertEqual(command.stdout, '')
            self.assertFalse((root / 'reviews').exists())


if __name__ == '__main__':
    unittest.main()

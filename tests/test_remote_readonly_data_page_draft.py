import json
from pathlib import Path
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT
from aos.remote_readonly_data_page_draft import (
    RemoteReadonlyDataPageDraftRegistration, RemoteReadonlyDataPageDraftSeed,
    register_remote_readonly_data_page_draft, seed_remote_readonly_data_page_draft)
from aos.storage import TrajectoryStore


class RemoteReadonlyDataPageDraftTests(unittest.TestCase):
    def test_synthetic_seed_and_registration_never_grant_authority(self):
        for name, model in (
                ('remote_readonly_data_page_draft_seed', RemoteReadonlyDataPageDraftSeed),
                ('remote_readonly_data_page_draft_registration',
                 RemoteReadonlyDataPageDraftRegistration)):
            fixture = json.loads((REPO_ROOT / 'examples' / (name + '.json')).read_text())
            schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            self.assertTrue(fixture['synthetic'])
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.Draft202012Validator(schema).validate(fixture['report'])
            self.assertEqual(model.model_validate(fixture['report']).page_key, 'summary')
            for field in ('reviewed', 'semantic_page_key_verified',
                          'task_retrieval_authorized', 'execution_authorized',
                          'collection_authorized', 'training_ready'):
                with self.subTest(name=name, field=field):
                    with self.assertRaises(jsonschema.ValidationError):
                        jsonschema.Draft202012Validator(schema).validate({
                            **fixture['report'], field: True})
        seed = json.loads((REPO_ROOT / 'examples/remote_readonly_data_page_draft_seed.json').read_text())['report']
        registration = json.loads((REPO_ROOT / 'examples/remote_readonly_data_page_draft_registration.json').read_text())['report']
        self.assertEqual(seed['draft_sha256'], registration['knowledge_sha256'])

    def test_missing_source_never_writes_page_or_seed(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            database = root / 'trajectory.sqlite'
            store = TrajectoryStore(database)
            store.close()
            options = {'profiles': root / 'profiles', 'store': root / 'pages',
                       'selected_profile_sha256': 'a' * 64,
                       'selected_plan_sha256': 'b' * 64, 'page_key': 'summary'}
            with self.assertRaises((OSError, ValueError)):
                seed_remote_readonly_data_page_draft(
                    database, 'before', 'after', **options)
            with self.assertRaises((OSError, ValueError)):
                register_remote_readonly_data_page_draft(
                    database, 'before', 'after', **options,
                    seed_root=root, confirm_sha256='c' * 64)
            self.assertFalse((root / 'pages').exists())
            self.assertFalse((root / ('a' * 64)).exists())


if __name__ == '__main__':
    unittest.main()

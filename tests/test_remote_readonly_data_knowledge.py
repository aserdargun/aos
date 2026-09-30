import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT
from aos.remote_readonly_data_knowledge import preview_remote_readonly_data_knowledge
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


class RemoteReadonlyDataKnowledgeTests(unittest.TestCase):
    def test_synthetic_candidate_cannot_grant_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_knowledge.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_readonly_data_knowledge.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator.check_schema(schema)
        validate = jsonschema.Draft202012Validator(schema).validate
        validate(fixture['report'])
        for field in ('origin_verified', 'account_verified', 'site_outcome_verified',
                      'reviewed', 'task_retrieval_authorized', 'execution_authorized',
                      'collection_authorized', 'training_ready'):
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                validate({**fixture['report'], field: True})
        with self.assertRaises(jsonschema.ValidationError):
            validate({**fixture['report'], 'raw_json': {'private': True}})

    def test_missing_or_unsafe_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / 'trajectory.sqlite'
            store = TrajectoryStore(database)
            store.close()
            profile = WebApplicationProfile.model_validate(json.loads(
                (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
            profiles = WebApplicationProfiles(root / 'profiles')
            checksum = profile_report(profile).profile_sha256
            profiles.register(profile, confirm_sha256=checksum)
            selection = {'profiles': profiles.root, 'store': root / 'pages',
                         'knowledge_sha256': 'b' * 64,
                         'selected_profile_sha256': checksum,
                         'selected_plan_sha256': 'a' * 64}
            with self.assertRaises(ValueError):
                preview_remote_readonly_data_knowledge(
                    database, 'missing-before', 'missing-after', **selection)
            alias = root / 'alias.sqlite'
            alias.symlink_to(database)
            with self.assertRaises(ValueError):
                preview_remote_readonly_data_knowledge(
                    alias, 'missing-before', 'missing-after', **selection)
            command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_knowledge',
                '--database', str(database), '--before-run-id', 'missing-before',
                '--after-run-id', 'missing-after', '--profiles', str(profiles.root),
                '--store', str(root / 'pages'), '--knowledge-sha256', 'b' * 64,
                '--selected-profile-sha256', checksum,
                '--selected-plan-sha256', 'a' * 64],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(command.returncode, 1)
            self.assertEqual(command.stdout, '')
            self.assertNotIn(profile.entry_url, command.stderr)


if __name__ == '__main__':
    unittest.main()

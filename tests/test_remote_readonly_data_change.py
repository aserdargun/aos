import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.remote_readonly_data_change import (FINGERPRINT_VERSION,
                                             compare_remote_readonly_data_change)
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


class RemoteReadonlyDataChangeTests(unittest.TestCase):
    def test_synthetic_change_has_no_authority_or_raw_content(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_change.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_readonly_data_change.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator.check_schema(schema)
        validate = jsonschema.Draft202012Validator(schema).validate
        report = fixture['report']
        validate(report)
        self.assertEqual(report['data_changed_indices'], [0])
        self.assertFalse(report['page_identity_changed'])
        for marker, expected in (('e', report['before_fingerprint_sha256']),
                                 ('f', report['after_fingerprint_sha256'])):
            transport = {'title_sha256': 'a' * 64, 'heading_sha256': 'b' * 64,
                         'responses': {'entry_response_sha256': 'c' * 64,
                                       'asset_response_sha256': ['d' * 64],
                                       'data_response_sha256': [marker * 64]}}
            self.assertEqual(digest({'version': FINGERPRINT_VERSION, **transport}), expected)
        for field in ('site_outcome_verified', 'account_verified', 'rights_reviewed',
                      'reviewed', 'task_retrieval_authorized', 'execution_authorized',
                      'collection_authorized', 'training_ready'):
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                validate({**report, field: True})
        with self.assertRaises(jsonschema.ValidationError):
            validate({**report, 'raw_json': {'title': 'private'}})

    def test_missing_unsafe_or_same_run_is_rejected(self):
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
            selection = {'profiles': profiles.root, 'selected_profile_sha256': checksum,
                         'selected_plan_sha256': 'a' * 64}
            with self.assertRaises(ValueError):
                compare_remote_readonly_data_change(database, 'missing-run', 'missing-run',
                                                    **selection)
            with self.assertRaises(ValueError):
                compare_remote_readonly_data_change(database, 'missing-before', 'missing-after',
                                                    **selection)
            alias = root / 'alias.sqlite'
            alias.symlink_to(database)
            with self.assertRaises(ValueError):
                compare_remote_readonly_data_change(alias, 'missing-before', 'missing-after',
                                                    **selection)
            command = subprocess.run([
                sys.executable, '-m', 'aos.remote_readonly_data_change',
                '--database', str(database), '--before-run-id', 'missing-before',
                '--after-run-id', 'missing-after', '--profiles', str(profiles.root),
                '--selected-profile-sha256', checksum,
                '--selected-plan-sha256', 'a' * 64],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(command.returncode, 1)
            self.assertEqual(command.stdout, '')
            self.assertNotIn(profile.entry_url, command.stderr)


if __name__ == '__main__':
    unittest.main()

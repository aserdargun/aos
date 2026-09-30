import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT
from aos.remote_static_learning_source import inspect_remote_readonly_data_source
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


class RemoteReadonlyDataSourceTests(unittest.TestCase):
    def test_synthetic_report_never_grants_authority(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_readonly_data_source.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_readonly_data_source.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        for field in ('site_outcome_verified', 'account_verified', 'rights_reviewed',
                      'redaction_reviewed', 'collection_authorized', 'training_ready'):
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(schema).validate({**fixture['report'], field: True})

    def test_missing_or_unsafe_run_never_becomes_a_source(self):
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
            arguments = {'profiles': profiles.root, 'selected_profile_sha256': checksum,
                         'selected_plan_sha256': 'a' * 64}
            with self.assertRaises(ValueError):
                inspect_remote_readonly_data_source(database, 'missing-run', **arguments)
            with self.assertRaises((ValueError, FileNotFoundError)):
                inspect_remote_readonly_data_source(database, 'missing-run', **{
                    **arguments, 'selected_profile_sha256': '0' * 64})
            alias = root / 'alias.sqlite'
            alias.symlink_to(database)
            with self.assertRaises(ValueError):
                inspect_remote_readonly_data_source(alias, 'missing-run', **arguments)
            command = subprocess.run([sys.executable, '-m', 'aos.remote_readonly_data_source',
                                      '--database', str(database), '--run-id', 'missing-run',
                                      '--profiles', str(profiles.root),
                                      '--selected-profile-sha256', checksum,
                                      '--selected-plan-sha256', 'a' * 64],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(command.returncode, 1)
            self.assertEqual(command.stdout, '')
            self.assertNotIn(profile.entry_url, command.stderr)


if __name__ == '__main__':
    unittest.main()

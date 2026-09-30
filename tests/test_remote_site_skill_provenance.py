import json
from pathlib import Path
import subprocess
import sys
import unittest

from aos.contracts import REPO_ROOT
from aos.dataset import validator
from aos.remote_site_skill_provenance import inspect_remote_site_skill_sources


class RemoteSiteSkillProvenanceTests(unittest.TestCase):
    def test_synthetic_fixture_keeps_authority_and_outcome_closed(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_site_skill_provenance.json').read_text())
        self.assertTrue(fixture['synthetic'])
        report = fixture['report']
        validator('remote_site_skill_provenance').validate(report)
        for field in ('site_outcome_verified', 'account_verified', 'parameter_values_bound',
                      'reviewed', 'execution_authorized', 'collection_authorized',
                      'activation_authorized', 'training_ready'):
            with self.subTest(field=field):
                self.assertFalse(validator('remote_site_skill_provenance').is_valid(
                    {**report, field: True}))

    def test_invalid_route_selection_fails_before_source_read(self):
        with self.assertRaisesRegex(ValueError, 'invalid_route_index'):
            inspect_remote_site_skill_sources(
                Path('/missing.sqlite'), 'run-missing', profiles=Path('/missing'),
                pages=Path('/missing'), skills=Path('/missing'),
                skill_sha256='a' * 64, selected_plan_sha256='b' * 64,
                route_index=True)

    def test_cli_does_not_echo_invalid_private_argument(self):
        result = subprocess.run(
            [sys.executable, '-m', 'aos.remote_site_skill_provenance',
             '--route-index', 'private-token-do-not-export'],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('private-token-do-not-export', result.stderr)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()

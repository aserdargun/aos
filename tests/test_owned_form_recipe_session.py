import json
from pathlib import Path
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT
from aos.local_app import LocalAppState, managed_backend_command
from aos.owned_form_invocation_session import (
    provision_owned_synthetic_form_invocation,
    verify_owned_form_invocation_manifest)
from aos.lifecycle import ProcessIdentity


class OwnedFormRecipeSessionTests(unittest.TestCase):
    def test_recipe_manifest_is_v2_bound_and_rejects_changed_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = provision_owned_synthetic_form_invocation(
                root / 'owned-form', 19432, mode='owned_synthetic_form_recipe')
            manifest = bundle['manifest']
            schema = json.loads((REPO_ROOT / 'schemas/owned_form_recipe_session.schema.json').read_text())
            fixture = json.loads((REPO_ROOT / 'examples/owned_form_recipe_session.json').read_text())
            jsonschema.Draft202012Validator(schema).validate(fixture['manifest'])
            jsonschema.Draft202012Validator(schema).validate(manifest)
            self.assertEqual(manifest['schema_version'], '2.0')
            self.assertEqual(manifest['mode'], 'owned_synthetic_form_recipe')
            self.assertEqual(manifest['recipe_sha256'],
                             manifest['sources']['remote-form-skill-recipe.json'])
            self.assertEqual(tuple(step['operation'] for step in bundle['invocation']['steps']),
                             ('open_entry', 'fill_form', 'read_state_before', 'submit_form',
                              'read_receipt', 'read_state_after'))
            self.assertEqual(verify_owned_form_invocation_manifest(
                root / 'owned-form', bundle['manifest_sha256']), manifest)
            recipe_file = root / 'owned-form/remote-form-skill-recipe.json'
            recipe_file.write_bytes(recipe_file.read_bytes() + b' ')
            with self.assertRaises(ValueError):
                verify_owned_form_invocation_manifest(root / 'owned-form', bundle['manifest_sha256'])

    def test_v1_bundle_stays_v1_and_recipe_mode_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = provision_owned_synthetic_form_invocation(root / 'owned-form', 19433)
            self.assertEqual(bundle['manifest']['schema_version'], '1.0')
            self.assertEqual(bundle['manifest']['mode'], 'owned_synthetic_form_invocation')
            self.assertIsNone(bundle['recipe_sha256'])
            self.assertNotIn('remote-form-skill-recipe.json', bundle['manifest']['sources'])
            self.assertEqual(verify_owned_form_invocation_manifest(
                root / 'owned-form', bundle['manifest_sha256']), bundle['manifest'])

    def test_recipe_state_pin_and_backend_forwarding_are_exclusive(self):
        identity = ProcessIdentity(boot_id='00000000-0000-0000-0000-000000000000',
                                   pid=100, start_ticks=1, pid_namespace=1, uid=1000)
        state = LocalAppState(
            session='app-' + 'a' * 32, mode='real',
            owned_synthetic_form_recipe=True,
            owned_form_manifest_sha256='b' * 64,
            owned_form_invocation_sha256='c' * 64,
            owned_form_recipe_sha256='d' * 64,
            phase='starting', supervisor=identity, started_at='2026-09-27T00:00:00Z')
        self.assertEqual(state.owned_form_recipe_sha256, 'd' * 64)
        with self.assertRaises(ValueError):
            LocalAppState.model_validate({**state.model_dump(), 'owned_form_recipe_sha256': None})
        with tempfile.TemporaryDirectory() as temporary:
            command = managed_backend_command(
                Path(temporary) / 'app-test', 'real', 41,
                owned_form_listener_fd=42, owned_form_manifest_sha256='e' * 64,
                owned_form_recipe_sha256='d' * 64)
            self.assertIn('--owned-synthetic-form-recipe', command)
            self.assertIn('--owned-form-listener-fd', command)
            with self.assertRaises(ValueError):
                managed_backend_command(Path(temporary) / 'app-test', 'real', 41,
                                        owned_form_recipe_sha256='d' * 64)


if __name__ == '__main__':
    unittest.main()

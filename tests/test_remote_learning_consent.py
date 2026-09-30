import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
from pydantic import ValidationError

from aos.contracts import REPO_ROOT, digest
from aos.remote_learning_consent import RemoteLearningConsent, RemoteLearningConsents


class RemoteLearningConsentTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_learning_consent.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.example = fixture['consent']
        self.schema = json.loads((REPO_ROOT / 'schemas/remote_learning_consent.schema.json').read_text())

    def test_canonical_scope_rejects_authority_expansion(self):
        jsonschema.Draft202012Validator.check_schema(self.schema)
        jsonschema.Draft202012Validator(self.schema).validate(self.example)
        RemoteLearningConsent.model_validate(self.example)
        state_consent = {**self.example, 'scope': 'remote_form_state_model_metadata_only',
                         'state_plan_sha256': 'a' * 64}
        jsonschema.Draft202012Validator(self.schema).validate(state_consent)
        RemoteLearningConsent.model_validate(state_consent)
        for invalid in ({**self.example, 'state_plan_sha256': 'a' * 64},
                        {key: value for key, value in state_consent.items()
                         if key != 'state_plan_sha256'}):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(self.schema).validate(invalid)
            with self.assertRaises(ValidationError):
                RemoteLearningConsent.model_validate(invalid)
        for field in ('external_rights_verified', 'raw_content_allowed',
                      'training_authorized', 'promotion_authorized'):
            with self.subTest(field=field):
                expanded = {**self.example, field: True}
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.Draft202012Validator(self.schema).validate(expanded)
                with self.assertRaises(ValidationError):
                    RemoteLearningConsent.model_validate(expanded)
        for roles in ([], ['system2', 'system1'], ['system1', 'system1'], ['system3']):
            with self.subTest(roles=roles), self.assertRaises(ValidationError):
                RemoteLearningConsent.model_validate({**self.example, 'roles': roles})

    def test_private_store_rejects_alias_and_changed_file(self):
        live = {**self.example, 'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}
        consent = RemoteLearningConsent.model_validate(live)
        checksum = digest(consent.model_dump())
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary) / 'consents'
            store = RemoteLearningConsents(root)
            source = {'database': Path(temporary) / 'unused.sqlite',
                      'profiles': Path(temporary) / 'profiles'}
            with patch('aos.remote_learning_consent.preview_remote_learning_consent', return_value=consent):
                with self.assertRaises(ValueError):
                    store.register(consent, confirm_sha256='0' * 64, **source)
                self.assertEqual(store.register(consent, confirm_sha256=checksum, **source), checksum)
            self.assertEqual(store.get(checksum), consent)
            path = root / (checksum + '.json')
            alias = root / 'alias.json'
            alias.hardlink_to(path)
            with self.assertRaises(ValueError):
                store.get(checksum)
            alias.unlink()
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                store.get(checksum)


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest

import jsonschema
from pydantic import ValidationError

from aos.contracts import REPO_ROOT
from aos.remote_form_learning_stream import (poll_remote_form_learning_stream,
                                             watch_remote_form_learning_stream)
from aos.remote_learning_consent import RemoteLearningConsent


class RemoteFormLearningStreamTests(unittest.TestCase):
    def test_synthetic_contract_keeps_authority_closed(self):
        for mode in ('remote_form_learning_stream', 'remote_form_state_learning_stream'):
            with self.subTest(mode=mode):
                fixture = json.loads((REPO_ROOT / f'examples/{mode}.json').read_text())
                self.assertTrue(fixture['synthetic'])
                for name in ('entry', 'report'):
                    schema = json.loads((REPO_ROOT / f'schemas/{mode}_{name}.schema.json').read_text())
                    jsonschema.Draft202012Validator.check_schema(schema)
                    jsonschema.Draft202012Validator(schema).validate(fixture[name])
                    with self.assertRaises(jsonschema.ValidationError):
                        jsonschema.Draft202012Validator(schema).validate(
                            {**fixture[name], 'training_ready': True})
                    with self.assertRaises(jsonschema.ValidationError):
                        jsonschema.Draft202012Validator(schema).validate(
                            {key: value for key, value in fixture[name].items()
                             if key != 'state_plan_sha256'}
                            if 'state' in mode else
                            {**fixture[name], 'state_plan_sha256': 'a' * 64})
        consent = json.loads((REPO_ROOT / 'examples/remote_learning_consent.json').read_text())['consent']
        RemoteLearningConsent.model_validate({**consent, 'scope': 'remote_form_model_metadata_only'})
        RemoteLearningConsent.model_validate({**consent,
            'scope': 'remote_form_state_model_metadata_only', 'state_plan_sha256': 'a' * 64})
        with self.assertRaises(ValidationError):
            RemoteLearningConsent.model_validate({**consent, 'scope': 'remote_anything_metadata_only'})

    def test_missing_consent_and_invalid_watch_never_create_outbox(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            selection = {'profiles': root / 'profiles', 'consents': root / 'consents',
                         'consent_sha256': 'a' * 64, 'outbox_dir': root / 'outbox'}
            with self.assertRaises((FileNotFoundError, ValueError)):
                poll_remote_form_learning_stream(root / 'missing.sqlite', **selection)
            with self.assertRaises(ValueError):
                watch_remote_form_learning_stream(root / 'missing.sqlite', **selection,
                                                  watch_seconds=301)
            self.assertFalse((root / 'outbox').exists())


if __name__ == '__main__':
    unittest.main()

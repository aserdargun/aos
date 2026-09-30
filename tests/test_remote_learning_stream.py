import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

import jsonschema

from aos.contracts import REPO_ROOT
from aos.remote_learning_stream import (_open_store, _store_for_consent, poll_remote_learning_stream,
                                        watch_remote_learning_stream)


class RemoteLearningStreamTests(unittest.TestCase):
    def test_synthetic_contract_keeps_review_and_training_closed(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_learning_stream.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for name in ('entry', 'report'):
            schema_name = 'remote_learning_stream_' + name
            schema = json.loads((REPO_ROOT / f'schemas/{schema_name}.schema.json').read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.Draft202012Validator(schema).validate(fixture[name])
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(schema).validate(
                    {**fixture[name], 'training_ready': True})
        schema = json.loads((REPO_ROOT / 'schemas/remote_learning_stream_entry.schema.json').read_text())
        for field in ('transport_readback_verified', 'site_outcome_verified',
                      'external_rights_verified', 'redaction_reviewed'):
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(schema).validate(
                    {**fixture['entry'], field: True})
        supervisor = {**fixture['entry'], 'role': 'system2',
                      'model_kind': 'bonsai_native_supervisor', 'route_index': None}
        jsonschema.Draft202012Validator(schema).validate(supervisor)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(
                {**supervisor, 'route_index': 0})

    def test_store_is_private_append_only_and_schema_pinned(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary) / 'outbox'
            connection, directory = _open_store(root)
            try:
                self.assertEqual((root / 'remote-learning-stream.sqlite').stat().st_mode & 0o777, 0o600)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute('INSERT INTO source_binding VALUES(1,?,?,?,?,?,?,?,?)',
                                       ('a' * 64, 'b' * 64, 'synthetic-run', 'c' * 64,
                                        'd' * 64, 'e' * 64, 'f' * 64, '1' * 64))
                    connection.execute('UPDATE source_binding SET source_ref=? WHERE singleton=1',
                                       ('2' * 64,))
                connection.rollback()
            finally:
                connection.close()
                os.close(directory)
            connection, directory = _open_store(root)
            connection.close()
            os.close(directory)
            alias = Path(temporary) / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                _open_store(alias)

    def test_missing_consent_and_invalid_watch_never_create_outbox(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            arguments = {'profiles': root / 'profiles', 'consents': root / 'consents',
                         'consent_sha256': 'a' * 64, 'outbox_dir': root / 'outbox'}
            with self.assertRaises((FileNotFoundError, ValueError)):
                poll_remote_learning_stream(root / 'missing.sqlite', **arguments)
            with self.assertRaises(ValueError):
                watch_remote_learning_stream(root / 'missing.sqlite', **arguments,
                                             watch_seconds=301)
            self.assertFalse((root / 'outbox').exists())

    def test_each_consent_uses_private_store_and_existing_legacy_binding_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary) / 'outbox'
            first = _store_for_consent(root, 'a' * 64)
            self.assertEqual(first, root / ('a' * 64))
            connection, directory = _open_store(first)
            connection.close()
            os.close(directory)
            self.assertEqual(_store_for_consent(root, 'b' * 64), root / ('b' * 64))

            legacy, directory = _open_store(root)
            try:
                legacy.execute('INSERT INTO source_binding VALUES(1,?,?,?,?,?,?,?,?)',
                               ('1' * 64, '2' * 64, 'legacy-run', '3' * 64,
                                '4' * 64, '5' * 64, 'c' * 64, '6' * 64))
                legacy.commit()
            finally:
                legacy.close()
                os.close(directory)
            self.assertEqual(_store_for_consent(root, 'c' * 64), root)
            self.assertEqual(_store_for_consent(root, 'd' * 64), root / ('d' * 64))
            (root / 'remote-learning-stream.sqlite').chmod(0o644)
            with self.assertRaises(ValueError):
                _store_for_consent(root, 'd' * 64)


if __name__ == '__main__':
    unittest.main()

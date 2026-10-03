import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from aos.contracts import REPO_ROOT


class DevelopmentJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((REPO_ROOT / 'docs/release_acceptance.json').read_text())
        cls.schema = json.loads((REPO_ROOT / 'schemas/release_acceptance_snapshot.schema.json').read_text())
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def test_current_manual_checkpoint_preserves_six_acceptance_gates(self):
        self.validator.validate(self.snapshot)
        self.assertEqual(self.snapshot['schema_version'], '1.1')
        self.assertFalse(self.snapshot['runtime_authority'])
        self.assertFalse(self.snapshot['product_complete'])
        self.assertEqual(self.snapshot['observed_at'], '2026-10-03T05:50:46Z')
        self.assertEqual([(stage['id'], stage['source'], stage['verification'], stage['delivery'])
                         for stage in self.snapshot['stages']], [
            ('candidate', 'partial', 'cpu_verified', 'not_delivered'),
            ('scientist', 'partial', 'cpu_verified', 'not_delivered'),
            ('native_workflow', 'partial', 'historical_native', 'historical'),
            ('user_delivery', 'partial', 'cpu_verified', 'historical'),
            ('learning', 'partial', 'historical_native', 'not_delivered'),
            ('swapp', 'pending', 'pending', 'pending')])
        checkpoint = self.snapshot['checkpoint']
        self.assertEqual(checkpoint['scope'], 'manual_development_checkpoint')
        self.assertEqual([update['state'] for update in checkpoint['updates']],
                         ['verified_cpu', 'in_progress', 'in_progress', 'in_progress', 'next'])
        for update in checkpoint['updates']:
            for evidence in update['evidence']:
                if not evidence.startswith('data/'):
                    self.assertTrue((REPO_ROOT / evidence).is_file(), evidence)

    @unittest.skipUnless(os.environ.get('AOS_PRIVATE_EVIDENCE_TESTS') == '1',
                         'Private development evidence is not bundled with source')
    def test_local_private_checkpoint_evidence_exists(self):
        evidence_paths = [evidence for update in self.snapshot['checkpoint']['updates']
                          for evidence in update['evidence'] if evidence.startswith('data/')]
        self.assertTrue(evidence_paths)
        for evidence in evidence_paths:
            self.assertTrue((REPO_ROOT / evidence).is_file(), evidence)

    def test_v1_compatibility_and_version_specific_checkpoint(self):
        historical = copy.deepcopy(self.snapshot)
        historical['schema_version'] = '1.0'
        historical.pop('checkpoint')
        self.validator.validate(historical)
        for value in [dict(historical, checkpoint=self.snapshot['checkpoint']),
                      dict(historical, schema_version='1.1')]:
            with self.assertRaises(ValidationError):
                self.validator.validate(value)

    def invalid_checkpoint(self, field, value):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['checkpoint'][field] = value
        return snapshot

    def test_rejects_authority_live_status_dates_and_extra_fields(self):
        for field, value in [('runtime_authority', True), ('scope', 'live_agent_status'),
                             ('recorded_at', '2026-02-30T07:39:51Z'), ('execution_authorized', True)]:
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    self.validator.validate(self.invalid_checkpoint(field, value))

    def test_rejects_invalid_localized_copy_state_and_evidence(self):
        for field, value in [('state', 'native_verified'), ('state', True), ('title', {'en': 'Missing Turkish'}),
                             ('title', {'en': 'x' * 121, 'tr': 'Başlık'}),
                             ('detail', {'en': ' ', 'tr': 'Detay'}),
                             ('recorded_at', '2026-02-30T07:39:51Z'),
                             ('evidence', ['docs/../README.md']), ('evidence', ['https://example.invalid']),
                             ('evidence', ['docs/STATUS.md\n']), ('id', 'entry\n')]:
            with self.subTest(field=field, value=value):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['checkpoint']['updates'][0][field] = value
                with self.assertRaises(ValidationError):
                    self.validator.validate(snapshot)

    def test_rejects_unbounded_and_duplicate_update_records(self):
        update = self.snapshot['checkpoint']['updates'][0]
        for updates in [[], [update] * 13, [update, update]]:
            with self.assertRaises(ValidationError):
                self.validator.validate(self.invalid_checkpoint('updates', updates))

    def test_typescript_checkpoint_validator_rejects_malformed_contracts(self):
        source = (REPO_ROOT / 'ui/src/DevelopmentJournal.tsx').read_text()
        validator_source = source[source.index('type LocalizedCopy'):source.index('export function DevelopmentJournal')]
        valid = self.snapshot['checkpoint']
        values = [valid, None, {}, dict(valid, scope='live_agent_status'),
                  dict(valid, recorded_at='2026-02-30T07:39:51Z'), dict(valid, runtime_authority=True),
                  dict(valid, updates=[]), dict(valid, focus={'en': 'Only English'})]
        update = valid['updates'][0]
        for changes in [{'state': True}, {'state': 'native_verified'}, {'id': update['id'] + '\n'},
                        {'title': {'en': 'x' * 121, 'tr': 'Başlık'}},
                        {'evidence': ['docs/../README.md']}, {'evidence': ['docs/STATUS.md\n']},
                        {'evidence': ['docs/STATUS.md', 'docs/STATUS.md']}]:
            values.append(dict(valid, updates=[dict(update, **changes)]))
        values.append(dict(valid, updates=[update, dict(update, detail={'en': 'Different', 'tr': 'Farklı'})]))
        with tempfile.TemporaryDirectory(prefix='aos-development-journal-validator-') as directory:
            module = Path(directory) / 'checkpoint.ts'
            module.write_text(validator_source)
            script = """import {pathToFileURL} from 'node:url';
import {readFileSync} from 'node:fs';
const {validDevelopmentCheckpoint} = await import(pathToFileURL(process.argv[1]).href);
const values = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(values.map(validDevelopmentCheckpoint)));"""
            result = subprocess.run(['node', '--experimental-strip-types', '--input-type=module', '-e', script, str(module)],
                input=json.dumps(values), capture_output=True, text=True, timeout=20, cwd=REPO_ROOT / 'ui')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True] + [False] * (len(values) - 1))


if __name__ == '__main__':
    unittest.main()

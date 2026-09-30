from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import jsonschema

from aos.benchmark import BenchmarkReport, bounded_read, identities, inspect_sample, run_benchmark, verify_benchmark
from aos.benchmark_worker import execute
from aos.contracts import HELLO_CONTENT, REPO_ROOT, canonical


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = REPO_ROOT / 'runs' / ('benchmark-tests-' + uuid4().hex)
        try:
            cls.report = run_benchmark(cls.baseline, repeats=2, include_synthetic=True)
            cls.report_bytes = (cls.baseline / 'report.json').read_bytes()
            cls.checksum = hashlib.sha256(cls.report_bytes).hexdigest()
        except BaseException:
            shutil.rmtree(cls.baseline, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.baseline)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def copied(self):
        output = self.root / 'copy'
        shutil.copytree(self.baseline, output)
        return output

    def test_real_fixture_runs_are_independent_and_verified(self):
        self.assertEqual(len(self.report.samples), 4)
        self.assertEqual(len({sample.run_id for sample in self.report.samples}), 4)
        for sample in self.report.samples:
            self.assertEqual((self.baseline / sample.directory / 'workspace/hello.txt').read_bytes(), HELLO_CONTENT.encode())
            self.assertGreater(sample.duration_seconds, 0)
            self.assertEqual(sample.system2_model_calls, 0)
            self.assertEqual(sample.fixture_supervisor_calls, int(sample.case == 'recovery-path'))
        self.assertFalse(self.report.real_model)
        self.assertFalse(self.report.promotion_authorized)
        self.assertEqual(self.report.independent_held_out_tasks, 0)

    def test_verifier_is_read_only_and_detects_current_identity(self):
        before = {str(path): path.read_bytes() for path in self.baseline.rglob('*') if path.is_file()}
        self.assertEqual(verify_benchmark(self.baseline, self.checksum), self.report)
        after = {str(path): path.read_bytes() for path in self.baseline.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        changed = {**identities(), 'source_sha256': '0' * 64}
        with patch('aos.benchmark.identities', return_value=changed), self.assertRaisesRegex(ValueError, 'pins_differ'):
            verify_benchmark(self.baseline, self.checksum)

    def test_external_hash_and_report_tampering_are_rejected(self):
        for checksum in ('', 'x', '0' * 64):
            with self.assertRaises(ValueError):
                verify_benchmark(self.baseline, checksum)
        output = self.copied()
        (output / 'report.json').write_bytes(self.report_bytes + b' ')
        with self.assertRaisesRegex(ValueError, 'checksum_mismatch'):
            verify_benchmark(output, self.checksum)

    def test_independent_read_rejects_false_artifact(self):
        output = self.copied()
        (output / 'sample-01/workspace/hello.txt').write_text('not the verified content')
        with self.assertRaisesRegex(ValueError, 'artifact_mismatch'):
            verify_benchmark(output, self.checksum)

    def test_database_tampering_and_action_scope_are_rejected(self):
        output = self.copied()
        database = output / 'sample-01/trace.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute("UPDATE actions SET arguments_json=? WHERE tool='filesystem.write'",
                                   (canonical({'path': '/etc/unauthorized', 'content': HELLO_CONTENT}),))
        with self.assertRaisesRegex(ValueError, 'action_scope'):
            verify_benchmark(output, self.checksum)

    def test_missing_verification_fails_even_when_file_is_correct(self):
        output = self.copied()
        database = output / 'sample-01/trace.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute('DELETE FROM verifications')
        with self.assertRaisesRegex(ValueError, 'verification_missing'):
            verify_benchmark(output, self.checksum)

    def test_initial_inconsistent_outcome_and_terminal_state_are_rejected(self):
        sample = self.report.samples[0]
        mutations = [
            ("UPDATE runs SET outcome='failed'", ()),
            ("UPDATE runs SET outcome='unknown'", ()),
            ("UPDATE runtime_states SET state_json=json_set(state_json,'$.phase','PAUSED')", ()),
            ("UPDATE runtime_states SET state_version=state_version+1", ()),
            ("UPDATE steps SET state='FAILED'", ()),
            ("UPDATE state_snapshots SET content_sha256=?", ('0' * 64,)),
        ]
        for index, (statement, parameters) in enumerate(mutations):
            with self.subTest(statement=statement):
                directory = self.root / f'inconsistent-{index}'
                shutil.copytree(self.baseline / sample.directory, directory)
                with closing(sqlite3.connect(directory / 'trace.sqlite')) as connection:
                    with connection:
                        connection.execute(statement, parameters)
                with self.assertRaises(ValueError):
                    inspect_sample(directory, sample.case, sample.run_id)

    def test_alias_extra_files_and_open_journal_are_rejected(self):
        output = self.copied()
        extra = output / 'sample-01/trace.sqlite-wal'
        extra.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'sample_file_set'):
            verify_benchmark(output, self.checksum)
        extra.unlink()
        artifact = output / 'sample-01/workspace/hello.txt'
        artifact.unlink()
        artifact.symlink_to(self.baseline / 'sample-01/workspace/hello.txt')
        with self.assertRaises((ValueError, OSError)):
            verify_benchmark(output, self.checksum)
        artifact.unlink()
        outside = self.root / 'outside'
        outside.write_bytes(HELLO_CONTENT.encode())
        os.link(outside, artifact)
        try:
            with self.assertRaisesRegex(ValueError, 'unsafe_file'):
                verify_benchmark(output, self.checksum)
        finally:
            artifact.unlink()

    def test_scope_optin_repeat_and_existing_output_constraints(self):
        for repeats in (0, 6, True, 1.5):
            with self.assertRaises(ValueError):
                run_benchmark(self.root / 'outside', repeats=repeats, include_synthetic=True)
        with self.assertRaises(ValueError):
            run_benchmark(self.root / 'outside', include_synthetic=True)
        with self.assertRaises(ValueError):
            run_benchmark(self.baseline)
        with self.assertRaises(FileExistsError):
            run_benchmark(self.baseline, include_synthetic=True)

    def test_partial_child_failure_cannot_publish_success(self):
        output = REPO_ROOT / 'runs' / ('benchmark-failure-' + uuid4().hex)
        self.addCleanup(lambda: shutil.rmtree(output, ignore_errors=True))
        with patch('aos.benchmark.run_bounded', return_value=subprocess.CompletedProcess([], 1, b'', b'')):
            with self.assertRaisesRegex(ValueError, 'incomplete_suite'):
                run_benchmark(output, include_synthetic=True)
        self.assertFalse((output / 'report.json').exists())

    def test_identity_race_prevents_report_publication(self):
        output = REPO_ROOT / 'runs' / ('benchmark-race-' + uuid4().hex)
        self.addCleanup(lambda: shutil.rmtree(output, ignore_errors=True))
        pinned = identities()
        changed = {**pinned, 'catalog_sha256': '0' * 64}
        with patch('aos.benchmark.identities', side_effect=[pinned, changed]):
            with self.assertRaisesRegex(ValueError, 'pins_changed'):
                run_benchmark(output, include_synthetic=True)
        self.assertFalse((output / 'report.json').exists())

    def test_schema_fixture_and_report_semantic_boundaries(self):
        schema = json.loads((REPO_ROOT / 'schemas/benchmark.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **BenchmarkReport.model_json_schema()})
        jsonschema.Draft202012Validator(schema).validate(self.report.model_dump())
        example = json.loads((REPO_ROOT / 'examples/benchmark.json').read_text())
        self.assertTrue(example['synthetic'])
        BenchmarkReport.model_validate(example['report'])
        jsonschema.Draft202012Validator(schema).validate(example['report'])
        for changes in ({'real_model': True}, {'promotion_authorized': True}, {'independent_held_out_tasks': 30},
                        {'samples': self.report.model_dump()['samples'][:-1]}, {'coverage_gaps': []}):
            with self.assertRaises(ValueError):
                BenchmarkReport.model_validate({**self.report.model_dump(), **changes})

    def test_worker_refuses_nonfresh_or_unsupported_scope(self):
        with self.assertRaises(ValueError):
            execute(self.root, 'arbitrary-shell')
        (self.root / 'preserve').write_text('existing')
        with self.assertRaisesRegex(ValueError, 'fresh_directory'):
            execute(self.root, 'file-roundtrip')
        self.assertEqual((self.root / 'preserve').read_text(), 'existing')

    def test_reader_rejects_fifo_large_file_and_parent_symlink(self):
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, 'unsafe_file'):
            bounded_read(fifo)
        path = self.root / 'large'
        path.write_bytes(b'12345')
        with self.assertRaises(ValueError):
            bounded_read(path, 4)
        alias = self.root / 'alias'
        alias.symlink_to(self.baseline, target_is_directory=True)
        with self.assertRaises(ValueError):
            verify_benchmark(alias, self.checksum)


if __name__ == '__main__':
    unittest.main()

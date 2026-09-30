import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos import capability_evidence as evidence


class CapabilityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'data').mkdir()
        self.addCleanup(patch.stopall)
        patch.object(evidence, 'REPO_ROOT', self.root).start()
        self.index = 0

    def entry(self, case='real_tasks', status='passed', **counts):
        values = dict.fromkeys(evidence.COUNT_KEYS, 0)
        values['passed'] = 1
        values.update(counts)
        return {'case': case, 'status': status, 'counts': values, 'seconds': 1.25,
                'exit_code': 0 if status in ('passed', 'partial') else 1,
                'runner_errors': 0, 'runner_failures': 0,
                'tests': [{'test': 'PRIVATE_DO_NOT_EXPOSE', 'status': status}],
                'scope': 'PRIVATE_SCOPE'}

    def report(self, entries=None, *, stamp='2026-09-27T07:00:00+00:00', **extra):
        return {'started_at': stamp, 'cases': entries if entries is not None else [self.entry()],
                'real_site_acceptance': False, 'approval_driver': 'test harness', **extra}

    def save(self, report):
        self.index += 1
        directory = self.root / 'data' / f'capability-check-{self.index:08d}'
        directory.mkdir(mode=0o700)
        filename = directory / 'report.json'
        filename.write_text(json.dumps(report))
        filename.chmod(0o600)
        return filename

    def rows(self):
        report = evidence.read_capability_evidence()
        return report, {row['case']: row for row in report['cases']}

    def test_empty_and_legacy_private_report(self):
        report, rows = self.rows()
        self.assertTrue(report['available'])
        self.assertEqual(set(rows), set(evidence.CAPABILITY_CASES))
        self.assertTrue(all(row['status'] == 'not_run' for row in rows.values()))
        self.save(self.report())
        report, rows = self.rows()
        self.assertEqual(rows['real_tasks']['status'], 'passed')
        self.assertEqual(rows['real_tasks']['seconds'], 1.25)
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertNotIn('tests', rows['real_tasks'])
        self.assertTrue(report['historical_only'])
        self.assertFalse(report['real_site_acceptance'])

    def test_separate_batches_latest_per_case_and_unrun(self):
        self.save(self.report([self.entry('ui'), self.entry('real_tasks')]))
        self.save(self.report([self.entry('transport')], stamp='2026-09-27T08:00:00+00:00'))
        self.save(self.report([self.entry('real_tasks', 'failed', passed=0, failed=1)],
                              stamp='2026-09-27T09:00:00+00:00',
                              requested_cases=['real_tasks', 'real_learning'],
                              unrun_cases=['real_learning']))
        report, rows = self.rows()
        self.assertTrue(report['available'])
        self.assertEqual(rows['ui']['status'], 'passed')
        self.assertEqual(rows['transport']['status'], 'passed')
        self.assertEqual(rows['real_tasks']['status'], 'failed')
        self.assertEqual(rows['real_learning']['status'], 'not_run')
        self.assertEqual(rows['real_learning']['started_at'], '2026-09-27T09:00:00+00:00')

    def test_unrun_replaces_old_green_and_missing_stays_distinct(self):
        self.save(self.report([self.entry('real_learning')]))
        self.save(self.report([], stamp='2026-09-27T08:00:00+00:00',
                              requested_cases=['real_learning'], unrun_cases=['real_learning']))
        _report, rows = self.rows()
        self.assertEqual(rows['real_learning']['status'], 'not_run')
        self.assertIsNotNone(rows['real_learning']['started_at'])
        self.assertIsNone(rows['ui']['started_at'])

    def test_partial_skips_expected_failure_and_infrastructure_error(self):
        self.save(self.report([
            self.entry('contracts', 'partial', skipped=2),
            self.entry('ui', 'not_verified', passed=0, skipped=1),
            self.entry('transport', 'partial', expected_failure=1),
            {'case': 'real_tasks', 'status': 'infrastructure_error', 'exit_code': 1}]))
        report, rows = self.rows()
        self.assertTrue(report['available'])
        self.assertEqual(rows['contracts']['status'], 'partial')
        self.assertEqual(rows['ui']['status'], 'not_verified')
        self.assertEqual(rows['transport']['status'], 'partial')
        self.assertEqual(rows['real_tasks']['status'], 'infrastructure_error')
        self.assertIsNone(rows['real_tasks']['counts'])

    def test_malformed_newest_does_not_fall_back(self):
        self.save(self.report())
        filename = self.save(self.report(stamp='2026-09-27T08:00:00+00:00'))
        filename.write_text('{truncated')
        report, rows = self.rows()
        self.assertFalse(report['available'])
        self.assertEqual(rows['real_tasks']['status'], 'unavailable')

    def test_directory_and_file_symlinks_hardlinks_and_permissions_rejected(self):
        for mutation in ('directory_link', 'file_link', 'hardlink', 'directory_mode', 'file_mode'):
            with self.subTest(mutation=mutation):
                filename = self.save(self.report())
                if mutation == 'directory_link':
                    moved = self.root / f'outside-{self.index}'
                    filename.parent.rename(moved)
                    filename.parent.symlink_to(moved, target_is_directory=True)
                elif mutation == 'file_link':
                    target = filename.parent / 'other.json'
                    filename.rename(target)
                    filename.symlink_to(target)
                elif mutation == 'hardlink':
                    os.link(filename, filename.parent / 'other.json')
                elif mutation == 'directory_mode':
                    filename.parent.chmod(0o755)
                else:
                    filename.chmod(0o644)
                report, rows = self.rows()
                self.assertFalse(report['available'])
                self.assertTrue(all(row['status'] == 'unavailable' for row in rows.values()))
                if mutation == 'directory_link':
                    filename.parent.unlink()
                else:
                    for child in filename.parent.iterdir():
                        child.unlink()
                    filename.parent.rmdir()

    def test_invalid_counts_boolean_numbers_and_false_success_rejected(self):
        for mutate in ('boolean', 'negative', 'nan', 'false_pass', 'duplicate', 'unrun_conflict'):
            with self.subTest(mutate=mutate):
                report = self.report()
                if mutate == 'boolean':
                    report['cases'][0]['counts']['passed'] = True
                elif mutate == 'negative':
                    report['cases'][0]['seconds'] = -1
                elif mutate == 'nan':
                    report['cases'][0]['seconds'] = float('nan')
                elif mutate == 'false_pass':
                    report['cases'][0]['counts']['failed'] = 1
                elif mutate == 'duplicate':
                    report['cases'].append(dict(report['cases'][0]))
                else:
                    report.update(requested_cases=['real_tasks'], unrun_cases=['real_tasks'])
                with self.assertRaises(ValueError):
                    evidence._project(report)

    def test_bounded_scan_read_and_missing_data(self):
        filename = self.save(self.report())
        with patch.object(evidence, 'MAX_REPORT_BYTES', 8):
            self.assertFalse(self.rows()[0]['available'])
        with patch.object(evidence, 'MAX_DIRECTORIES', 0):
            self.assertFalse(self.rows()[0]['available'])
        with patch.object(evidence, 'MAX_ENTRIES', 0):
            self.assertFalse(self.rows()[0]['available'])
        with patch.object(evidence, 'MAX_TOTAL_BYTES', 8):
            self.assertFalse(self.rows()[0]['available'])
        filename.unlink()
        filename.parent.rmdir()
        (self.root / 'data').rmdir()
        self.assertFalse(self.rows()[0]['available'])

    def test_report_replacement_during_read_fails_closed(self):
        filename = self.save(self.report())
        original_read = os.read
        changed = False

        def replace(descriptor, length):
            nonlocal changed
            content = original_read(descriptor, length)
            if not changed:
                changed = True
                filename.unlink()
                filename.write_text(json.dumps(self.report()))
                filename.chmod(0o600)
            return content

        with patch.object(evidence.os, 'read', side_effect=replace):
            self.assertFalse(self.rows()[0]['available'])

    def test_conflicting_same_timestamp_never_selects_arbitrary_green(self):
        self.save(self.report())
        self.save(self.report([self.entry('real_tasks', 'failed', passed=0, failed=1)]))
        _report, rows = self.rows()
        self.assertEqual(rows['real_tasks']['status'], 'unavailable')

    def test_owner_check_and_duplicate_json_keys(self):
        filename = self.save(self.report())
        with patch.object(evidence.os, 'getuid', return_value=os.getuid() + 1):
            self.assertFalse(self.rows()[0]['available'])
        filename.write_text('{"started_at": 1, "started_at": 2}')
        self.assertFalse(self.rows()[0]['available'])

    def test_newer_valid_case_can_follow_older_unreadable_run(self):
        filename = self.save(self.report())
        filename.write_text('broken')
        os.utime(filename, (1, 1))
        os.utime(filename.parent, (1, 1))
        self.save(self.report([self.entry('ui')]))
        report, rows = self.rows()
        self.assertFalse(report['available'])
        self.assertEqual(rows['ui']['status'], 'passed')
        self.assertEqual(rows['real_tasks']['status'], 'unavailable')

    def test_directory_mode_change_during_read_fails_closed(self):
        filename = self.save(self.report())
        original_read = os.read

        def change_mode(descriptor, length):
            content = original_read(descriptor, length)
            filename.parent.chmod(0o755)
            return content

        with patch.object(evidence.os, 'read', side_effect=change_mode):
            self.assertFalse(self.rows()[0]['available'])

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT


specification = importlib.util.spec_from_file_location('capability_check', REPO_ROOT / 'scripts/check_capabilities.py')
capabilities = importlib.util.module_from_spec(specification)
specification.loader.exec_module(capabilities)


class CapabilityCheckTests(unittest.TestCase):
    def test_atomic_report_replacement_preserves_previous_open_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'report.json'
            capabilities.publish_report(path, {'status': 'pending'})
            with path.open() as previous:
                capabilities.publish_report(path, {'status': 'passed'}, replace=True)
                self.assertEqual(json.load(previous), {'status': 'pending'})
            self.assertEqual(json.loads(path.read_text()), {'status': 'passed'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(temporary).iterdir()), [path])
            with self.assertRaises(FileExistsError):
                capabilities.publish_report(path, {'status': 'overwritten'})
            self.assertEqual(json.loads(path.read_text()), {'status': 'passed'})

    def test_failed_report_publication_never_truncates_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'report.json'
            capabilities.publish_report(path, {'status': 'pending'})
            for operation in ('replace', 'fsync'):
                with self.subTest(operation=operation), patch.object(
                        capabilities.os, operation, side_effect=OSError('synthetic publication failure')):
                    with self.assertRaises(OSError):
                        capabilities.publish_report(path, {'status': 'passed'}, replace=True)
                self.assertEqual(json.loads(path.read_text()), {'status': 'pending'})
                self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_real_pool_lock_rejects_overlap_and_preserves_cpu_parallelism(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(capabilities, 'ROOT', Path(temporary)):
            (Path(temporary) / 'data').mkdir()
            with capabilities.real_pool_lock(['real_tasks']) as descriptor:
                self.assertIsInstance(descriptor, int)
                with self.assertRaises(BlockingIOError), capabilities.real_pool_lock(['real_mcp']):
                    self.fail('Overlapping real-model pool was admitted')
                with capabilities.real_pool_lock(['contracts', 'ui', 'transport']) as cpu_lock:
                    self.assertIsNone(cpu_lock)
            with capabilities.real_pool_lock(['real_mcp']) as fresh:
                capabilities.validate_real_lock(fresh)

    def test_worker_keeps_lock_after_parent_descriptor_is_closed(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(capabilities, 'ROOT', Path(temporary)):
            (Path(temporary) / 'data').mkdir()
            child = None
            try:
                with capabilities.real_pool_lock(['real_tasks']) as descriptor:
                    child = subprocess.Popen([sys.executable, '-c',
                        'import os,sys; os.fstat(int(sys.argv[1])); print("ready", flush=True); sys.stdin.read(1)',
                        str(descriptor)], pass_fds=(descriptor,), stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.assertEqual(child.stdout.readline().strip(), 'ready')
                with self.assertRaises(BlockingIOError), capabilities.real_pool_lock(['real_tasks']):
                    self.fail('Live child lost its inherited lock')
                child.communicate('\n', timeout=5)
                self.assertEqual(child.returncode, 0)
                with capabilities.real_pool_lock(['real_tasks']) as fresh:
                    capabilities.validate_real_lock(fresh)
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)

    def test_private_lock_rejects_links_and_permissive_files(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(capabilities, 'ROOT', Path(temporary)):
            root = Path(temporary) / 'data'
            root.mkdir()
            path = root / '.capability-real.lock'
            target = root / 'target'
            target.touch(mode=0o600)
            for kind in ('symlink', 'hardlink', 'public'):
                if kind == 'symlink':
                    path.symlink_to(target)
                elif kind == 'hardlink':
                    os.link(target, path)
                else:
                    path.touch()
                    path.chmod(0o644)
                with self.subTest(kind=kind), self.assertRaises((OSError, ValueError)):
                    with capabilities.real_pool_lock(['real_tasks']):
                        self.fail('Invalid lock admitted')
                path.unlink()
            self.assertEqual(target.read_bytes(), b'')

    def test_busy_pool_creates_no_report_or_worker_and_list_remains_read_only(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(capabilities, 'ROOT', Path(temporary)):
            root = Path(temporary) / 'data'
            root.mkdir()
            with capabilities.real_pool_lock(['real_tasks']), patch.object(capabilities, 'run_pool') as run:
                with patch.object(sys, 'argv', ['check_capabilities', '--profile', 'real']), patch(
                        'sys.stderr', io.StringIO()):
                    self.assertEqual(capabilities.main(), 2)
                with patch.object(sys, 'argv', ['check_capabilities', '--profile', 'real', '--list']), patch(
                        'sys.stdout', io.StringIO()):
                    self.assertEqual(capabilities.main(), 0)
                run.assert_not_called()
            self.assertEqual([path.name for path in root.iterdir()], ['.capability-real.lock'])

    def test_direct_real_worker_requires_lock_before_loading_models(self):
        with patch.object(sys, 'argv', ['check_capabilities', '--case', 'real_tasks',
                                       '--worker-output', '/unused']), patch.object(
                capabilities, 'run_worker') as worker, patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                capabilities.main()
            self.assertEqual(raised.exception.code, 2)
            worker.assert_not_called()

    def test_pool_passes_lock_to_worker_and_publishes_complete_report(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(capabilities, 'ROOT', Path(temporary)):
            root = Path(temporary) / 'data'
            root.mkdir()
            with capabilities.real_pool_lock(['real_tasks']) as descriptor:
                def synthetic_worker(command, **options):
                    self.assertEqual(options['pass_fds'], (descriptor,))
                    self.assertEqual(command[-2:], ['--real-lock-fd', str(descriptor)])
                    capabilities.validate_real_lock(descriptor)
                    directory = Path(command[command.index('--worker-output') + 1])
                    evidence = capabilities.summarize([{'test': 'synthetic', 'status': 'passed',
                                                       'seconds': 0}], successful=True)
                    capabilities.publish_report(directory / 'real_tasks.json',
                                                {**evidence, 'case': 'real_tasks', 'seconds': 0})
                    return subprocess.CompletedProcess(command, 0)

                with patch.object(capabilities.subprocess, 'run', side_effect=synthetic_worker), patch(
                        'sys.stdout', io.StringIO()):
                    self.assertEqual(capabilities.run_pool(['real_tasks'], descriptor), 0)
            report_path = next(root.glob('capability-check-*/report.json'))
            report = json.loads(report_path.read_text())
            self.assertEqual(report['unrun_cases'], [])
            self.assertEqual(report['cases'][0]['status'], 'passed')
            self.assertEqual(report['cases'][0]['exit_code'], 0)

    def test_private_reports_do_not_change_test_process_umask(self):
        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'report.json'
                with capabilities.private_output(path) as output:
                    output.write('{}')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                public_fixture = Path(temporary) / 'public-fixture'
                public_fixture.mkdir()
                self.assertEqual(public_fixture.stat().st_mode & 0o777, 0o755)
        finally:
            os.umask(previous)

    def test_environment_cannot_accidentally_enable_real_models(self):
        environment = capabilities.case_environment('contracts', {
            'AOS_OWNED_ADAPTER_PAIR_TESTS': '1', 'AOS_DESKTOP_TESTS': '1', 'PATH': '/bin'})
        self.assertFalse(any(key.endswith('_TESTS') for key in environment))
        self.assertEqual(environment['PATH'], '/bin')
        real = capabilities.case_environment('real_mcp', environment)
        self.assertEqual({key for key in real if key.endswith('_TESTS')},
                         set(capabilities.CASES['real_mcp']['flags']))

    def test_skips_and_expected_failures_are_not_proof(self):
        self.assertEqual(capabilities.summarize([], successful=True)['status'], 'not_verified')
        for status in ('skipped', 'expected_failure'):
            report = capabilities.summarize([{'status': status}], successful=True)
            self.assertEqual(report['status'], 'not_verified')
            self.assertEqual(report['counts']['passed'], 0)
        self.assertEqual(capabilities.summarize([{'status': 'passed'}, {'status': 'skipped'}],
                                               successful=True)['status'], 'partial')

    def test_subtest_failures_remain_failures(self):
        class SyntheticFailure(unittest.TestCase):
            def runTest(self):
                with self.subTest('synthetic'):
                    self.fail('intentional result recorder check')

        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=capabilities.EvidenceResult).run(SyntheticFailure())
        report = capabilities.summarize(result.records, successful=result.wasSuccessful())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['counts']['failed'], 1)
        self.assertEqual(report['counts']['passed'], 0)

    def test_profile_mapping_separates_fixture_and_real_evidence(self):
        self.assertEqual(capabilities.PROFILES['core'], ['contracts'])
        self.assertEqual(capabilities.PROFILES['all'], list(capabilities.CASES))
        self.assertTrue(all(name.startswith('real_') for name in capabilities.PROFILES['real']))
        self.assertNotIn('AOS_OWNED_ADAPTER_PAIR_TESTS', capabilities.CASES['ui']['flags'])

    def test_visible_transport_never_enables_real_inference(self):
        transport = capabilities.CASES['transport']
        self.assertIn('test_desktop_mcp.MCPBrowserIntegrationTests', transport['tests'])
        self.assertIn('test_desktop_vision.DesktopVisionIntegrationTests', transport['tests'])
        self.assertFalse(any('REAL' in flag or 'ADAPTER' in flag for flag in transport['flags']))

    def test_task_context_ui_pool_includes_start_and_audit_without_real_flags(self):
        case = capabilities.CASES['ui']
        self.assertIn('test_task_knowledge_ui', case['tests'])
        self.assertIn('test_task_knowledge_report_ui', case['tests'])
        self.assertFalse(any('REAL' in flag or 'ADAPTER' in flag for flag in case['flags']))

    def test_task_context_real_flag_is_explicit_and_never_leaks_to_fixture_pools(self):
        selector = 'test_task_knowledge_real.TaskKnowledgeRealTests'
        flag = 'AOS_TASK_KNOWLEDGE_REAL_TESTS'
        for name, case in capabilities.CASES.items():
            with self.subTest(case=name):
                environment = capabilities.case_environment(name, {flag: '1', 'PATH': '/usr/bin:/bin'})
                self.assertEqual(selector in case['tests'], name == 'real_tasks')
                self.assertEqual(flag in environment, name == 'real_tasks')
                self.assertEqual(environment['PATH'], '/usr/bin:/bin')

    def test_real_pool_includes_visible_tasks_and_gpu_lifecycle(self):
        tasks = capabilities.CASES['real_tasks']
        self.assertIn('AOS_DESKTOP_TESTS', tasks['flags'])
        self.assertIn('test_visible_scheduler.VisibleSchedulerTests.test_real_bonsai_decider_visible_capture_and_independent_verification', tasks['tests'])
        self.assertIn('test_visible_scheduler.VisibleSchedulerTests.test_reusable_real_decider_two_fresh_decisions_and_job_release', tasks['tests'])
        self.assertIn('test_failure_followup_real.FailureFollowupRealTests', tasks['tests'])
        self.assertIn('AOS_FAILURE_FOLLOWUP_REAL_TESTS', tasks['flags'])
        self.assertIn('test_failure_guidance_real.FailureGuidanceRealTests', tasks['tests'])
        self.assertIn('AOS_FAILURE_GUIDANCE_REAL_TESTS', tasks['flags'])
        self.assertEqual(capabilities.CASES['real_takeover']['tests'], ['test_desktop_task_ui.RealDesktopTaskTests'])
        self.assertEqual(len(capabilities.CASES['real_mcp']['tests']), 3)

    def test_real_guidance_opt_in_is_not_inherited_by_fixture_cases(self):
        selector = 'test_failure_guidance_real.FailureGuidanceRealTests'
        flag = 'AOS_FAILURE_GUIDANCE_REAL_TESTS'
        environment = {flag: '1', 'PATH': '/usr/bin:/bin'}
        for name, case in capabilities.CASES.items():
            with self.subTest(case=name):
                selected = capabilities.case_environment(name, environment)
                self.assertEqual(selector in case['tests'], name == 'real_tasks')
                self.assertEqual(flag in selected, name == 'real_tasks')
                self.assertEqual(selected['PATH'], environment['PATH'])
                if name == 'real_tasks':
                    self.assertEqual(selected[flag], '1')
                    self.assertEqual(selected['AOS_DESKTOP_TESTS'], '1')

    def test_managed_s2_context_runs_only_in_real_reuse(self):
        selector = 'test_owned_skill_knowledge_managed.OwnedSkillKnowledgeManagedTests'
        flag = 'AOS_OWNED_SKILL_KNOWLEDGE_MANAGED_TESTS'
        for name, case in capabilities.CASES.items():
            with self.subTest(case=name):
                environment = capabilities.case_environment(name, {flag: '1'})
                self.assertEqual(selector in case['tests'], name == 'real_reuse')
                self.assertEqual(flag in environment, name == 'real_reuse')

    def test_native_s2_context_and_ui_opt_ins_remain_separate(self):
        selector = 'test_owned_skill_knowledge_real.OwnedSkillKnowledgeRealTests'
        flag = 'AOS_OWNED_SKILL_KNOWLEDGE_REAL_TESTS'
        for name, case in capabilities.CASES.items():
            with self.subTest(case=name):
                environment = capabilities.case_environment(name, {flag: '1'})
                self.assertEqual(selector in case['tests'], name == 'real_tasks')
                self.assertEqual(flag in environment, name == 'real_tasks')
                self.assertEqual('test_owned_skill_knowledge_ui' in case['tests'], name == 'ui')

    def test_class_and_subtest_skips_cannot_be_reported_as_complete(self):
        class SkippedClass(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest('synthetic setup prerequisite unavailable')

            def runTest(self):
                self.fail('must not run')

        class PartialSubtest(unittest.TestCase):
            def runTest(self):
                with self.subTest('available'):
                    self.assertTrue(True)
                with self.subTest('unavailable'):
                    self.skipTest('synthetic subtest unavailable')

        class CompleteTest(unittest.TestCase):
            def runTest(self):
                self.assertTrue(True)

        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=capabilities.EvidenceResult).run(
            unittest.TestSuite([SkippedClass(), PartialSubtest(), CompleteTest()]))
        report = capabilities.summarize(result.records, successful=result.wasSuccessful())
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['counts']['skipped'], 2)
        self.assertEqual(len(result.skipped), 2)

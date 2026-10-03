import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import setup_local


class SetupLocalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ('pyproject.toml', 'uv.lock', 'requirements-validation.txt'):
            (self.root / name).write_text('synthetic setup source\n')
        (self.root / 'ui').mkdir()
        (self.root / 'ui/package.json').write_text('{"packageManager":"pnpm@11.26.0"}')

    def tool_version(self, arguments, **kwargs):
        return {'uv': 'uv 0.10.0', 'node': 'v24.0.0', 'pnpm': '11.26.0'}[Path(arguments[0]).name]

    def test_existing_external_traversal_and_symlink_paths_are_refused(self):
        (self.root / '.venv').mkdir()
        for path in ('.venv', '/tmp/environment', '../environment',
                     'data/setup/../environment', 'src/environment', 'data//setup/environment'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                setup_local.fresh_destination(self.root, path)
        (self.root / 'data').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            setup_local.fresh_destination(self.root, 'data/setup/environment')

    def test_plan_does_not_create_destination_or_run_commands(self):
        with patch.object(setup_local, 'source_snapshot', return_value=('a' * 64, [])), \
                patch.object(setup_local.shutil, 'which', return_value='/synthetic/uv'), \
                patch.object(setup_local, 'capture', side_effect=self.tool_version), \
                patch.object(setup_local, 'installed_versions', return_value={'python': '3.13.0'}), \
                patch.object(setup_local, 'command') as command:
            report = setup_local.setup(self.root, environment_path='data/setup/environment',
                                       python='/synthetic/python', offline=True)
        command.assert_not_called()
        self.assertFalse((self.root / 'data').exists())
        self.assertFalse(report['installed'])
        self.assertFalse(report['deployed'])
        self.assertIn('--locked', report['commands'][0])
        self.assertIn('--offline', report['commands'][0])

    def test_prerequisite_report_is_presence_only_and_does_not_prepare_setup(self):
        model = self.root / 'models/decider'
        code = self.root / 'models/decider-code'
        model.mkdir(parents=True)
        code.mkdir()
        (self.root / 'models/decider-manifest.json').write_text(json.dumps(
            {'model_path': str(model), 'code_path': str(code)}))
        missing = self.root / 'models/bonsai-missing'
        (self.root / 'models/bonsai-manifest.json').write_text(json.dumps(
            {'model_path': str(missing), 'runtime_path': str(missing), 'server_path': str(missing / 'server'),
             'weights_file': 'weights.gguf', 'projector_file': 'projector.gguf'}))
        with patch.object(setup_local, 'source_snapshot', side_effect=AssertionError('hash walk not permitted')), \
                patch.object(setup_local, 'capture', side_effect=AssertionError('process probe not permitted')), \
                patch.object(setup_local, 'command', side_effect=AssertionError('commands not permitted')):
            report = setup_local.setup(self.root, environment_path='data/setup/environment',
                                       python='/missing/python', prerequisites_only=True)
        self.assertTrue(report['read_only'])
        self.assertFalse(report['presence_complete'])
        self.assertFalse(report['runtime_verified'])
        self.assertFalse(report['gpu_readiness_verified'])
        self.assertIn(str(missing), report['missing']['artifacts'])
        self.assertTrue(all(report['next_steps']))
        self.assertFalse((self.root / 'data').exists())

    def test_prerequisite_manifest_failures_are_bounded_and_reported(self):
        models = self.root / 'models'
        models.mkdir()
        (models / 'decider-manifest.json').write_text('[]')
        (models / 'bonsai-manifest.json').write_text(json.dumps({'model_path': 42}))
        (models / 'browser-manifest.json').write_bytes(b'{' + b' ' * (1024 * 1024))
        target = self.root / 'private.json'
        target.write_text('{}')
        (models / 'desktop-manifest.json').symlink_to(target)
        with patch.object(setup_local.shutil, 'which', return_value=None):
            report = setup_local.prerequisites(self.root, python='/missing/python')
        self.assertFalse(report['presence_complete'])
        self.assertEqual(len(report['runtime_artifacts']), 4)
        for entry in report['runtime_artifacts']:
            self.assertFalse(entry['present'])
            self.assertTrue(entry['issue'])
        self.assertTrue(all(report['next_steps']))

    def test_overlap_is_rejected_before_command(self):
        with patch.object(setup_local, 'source_snapshot', return_value=('a' * 64, [])), \
                patch.object(setup_local, 'command') as command, self.assertRaises(ValueError):
            setup_local.setup(self.root, environment_path='data/setup/shared', python='/synthetic/python',
                              install=True, build_ui=True, ui_path='data/setup/shared/console')
        command.assert_not_called()

    def test_success_runs_cli_help_not_a_task_or_runtime_and_does_not_update_existing_directory(self):
        with patch.object(setup_local, 'source_snapshot', return_value=('a' * 64, [])), \
                patch.object(setup_local.shutil, 'which', return_value='/synthetic/uv'), \
                patch.object(setup_local, 'capture', side_effect=self.tool_version), \
                patch.object(setup_local, 'installed_versions', return_value={'python': '3.13.0'}), \
                patch.object(setup_local, 'command') as command:
            report = setup_local.setup(self.root, environment_path='data/setup/environment',
                                       python='/synthetic/python', install=True)
        self.assertTrue(report['installed'])
        self.assertEqual(command.call_args_list[-1].args[0][-3:], ['-m', 'aos.cli', '--help'])
        self.assertFalse(report['runtime_started'])
        self.assertFalse(report['models_downloaded'])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['installed_versions']['python'], '3.13.0')
        self.assertEqual(report['source_inputs']['uv.lock'],
                         hashlib.sha256((self.root / 'uv.lock').read_bytes()).hexdigest())
        with self.assertRaises(ValueError):
            setup_local.fresh_destination(self.root, 'data/setup/environment')

    def test_failure_preserves_partial_output_without_success_claim(self):
        with patch.object(setup_local, 'source_snapshot', return_value=('a' * 64, [])), \
                patch.object(setup_local.shutil, 'which', return_value='/synthetic/uv'), \
                patch.object(setup_local, 'capture', side_effect=self.tool_version), \
                patch.object(setup_local, 'command', side_effect=[None, subprocess.CalledProcessError(3, ['synthetic'])]), \
                self.assertRaises(setup_local.SetupFailure) as caught:
            setup_local.setup(self.root, environment_path='data/setup/environment',
                              python='/synthetic/python', install=True, offline=True)
        report = caught.exception.report
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['failure']['stage'], 'validation_dependencies')
        self.assertEqual(report['completed_steps'], ['locked_dependencies'])
        self.assertFalse(report['installed'])
        self.assertFalse(report['deployed'])
        self.assertTrue((self.root / 'data/setup/environment').is_dir())
        self.assertIn('fresh paths', report['failure']['next_step'])

    def test_source_change_invalidates_otherwise_successful_cpu_setup(self):
        with patch.object(setup_local, 'source_snapshot', side_effect=[('a' * 64, []), ('b' * 64, [])]), \
                patch.object(setup_local.shutil, 'which', return_value='/synthetic/uv'), \
                patch.object(setup_local, 'capture', side_effect=self.tool_version), \
                patch.object(setup_local, 'installed_versions', return_value={'python': '3.13.0'}), \
                patch.object(setup_local, 'command'), self.assertRaises(setup_local.SetupFailure) as caught:
            setup_local.setup(self.root, environment_path='data/setup/environment',
                              python='/synthetic/python', install=True)
        self.assertFalse(caught.exception.report['installed'])
        self.assertEqual(caught.exception.report['failure']['stage'], 'final_source_verification')

    def test_staged_console_receipt_hashes_actual_build_and_failure_does_not_claim_install(self):
        def simulated_build(arguments, *, root, environment):
            if arguments[-2:] == ['run', 'build']:
                (root / 'dist').mkdir()
                (root / 'dist/index.html').write_text('<html>synthetic console</html>')

        for build_succeeds in (True, False):
            suffix = 'success' if build_succeeds else 'failure'
            with self.subTest(build_succeeds=build_succeeds), \
                    patch.object(setup_local, 'source_snapshot', return_value=('a' * 64, ['ui/package.json'])), \
                    patch.object(setup_local.shutil, 'which', side_effect=lambda name: '/synthetic/' + name), \
                    patch.object(setup_local, 'capture', side_effect=self.tool_version), \
                    patch.object(setup_local, 'installed_versions', return_value={'python': '3.13.0'}), \
                    patch.object(setup_local, 'command', side_effect=simulated_build if build_succeeds else None):
                if build_succeeds:
                    report = setup_local.setup(self.root, environment_path=f'data/{suffix}/environment',
                        python='/synthetic/python', install=True, build_ui=True, ui_path=f'data/{suffix}/console')
                    self.assertTrue(report['ui_staged'])
                    self.assertEqual(report['build_artifacts']['index.html'],
                        hashlib.sha256(b'<html>synthetic console</html>').hexdigest())
                else:
                    with self.assertRaises(setup_local.SetupFailure) as caught:
                        setup_local.setup(self.root, environment_path=f'data/{suffix}/environment',
                            python='/synthetic/python', install=True, build_ui=True, ui_path=f'data/{suffix}/console')
                    self.assertFalse(caught.exception.report['installed'])
                    self.assertFalse(caught.exception.report['ui_staged'])
                    self.assertEqual(caught.exception.report['failure']['stage'], 'console_artifact_verification')
        self.assertFalse((self.root / 'ui/dist').exists())
        self.assertFalse((self.root / 'ui/node_modules').exists())

    def test_wrong_pnpm_and_missing_prerequisites_fail_before_environment_creation(self):
        with patch.object(setup_local, 'source_snapshot', return_value=('a' * 64, [])), \
                patch.object(setup_local.shutil, 'which', return_value=None), self.assertRaisesRegex(ValueError, 'uv, node, pnpm'):
            setup_local.setup(self.root, environment_path='data/setup/environment', python='/synthetic/python', build_ui=True)
        with patch.object(setup_local, 'source_snapshot', return_value=('a' * 64, [])), \
                patch.object(setup_local.shutil, 'which', side_effect=lambda name: '/synthetic/' + name), \
                patch.object(setup_local, 'capture', return_value='wrong-version'), \
                patch.object(setup_local, 'command') as command, self.assertRaises(setup_local.SetupFailure) as caught:
            setup_local.setup(self.root, environment_path='data/setup/environment', python='/synthetic/python',
                              install=True, build_ui=True)
        self.assertEqual(caught.exception.report['failure']['stage'], 'prerequisites')
        command.assert_not_called()
        self.assertFalse((self.root / 'data').exists())

    def test_installed_version_probe_binds_prefix_and_checkout_source(self):
        target = self.root / 'data/setup/environment'
        evidence = {'python': '3.13.0', 'prefix': str(target),
                    'aos_source': str(self.root / 'src/aos/__init__.py'),
                    'distributions': {'aos-local': '0.1.0'}}
        with patch.object(setup_local, 'capture', return_value=json.dumps(evidence)) as capture:
            self.assertEqual(setup_local.installed_versions(target, root=self.root, environment={}), evidence)
            self.assertIn('-I', capture.call_args.args[0])
        for field, value in [('prefix', '/foreign/environment'), ('aos_source', '/foreign/source/__init__.py')]:
            with self.subTest(field=field), patch.object(setup_local, 'capture', return_value=json.dumps({**evidence, field: value})), \
                    self.assertRaises(ValueError):
                setup_local.installed_versions(target, root=self.root, environment={})

    def test_build_receipt_rejects_symlinks_and_missing_index(self):
        staged_ui = self.root / 'staged'
        (staged_ui / 'dist').mkdir(parents=True)
        with self.assertRaises(ValueError):
            setup_local.build_artifacts(staged_ui)
        (staged_ui / 'dist/index.html').symlink_to(self.root / 'uv.lock')
        with self.assertRaises(ValueError):
            setup_local.build_artifacts(staged_ui)

    def test_cli_failure_prints_machine_readable_failed_receipt_and_nonzero_exit(self):
        report = {'installed': True, 'ui_staged': False, 'completed_steps': ['locked_dependencies']}
        failure = setup_local.SetupFailure(report, 'validation_dependencies', subprocess.TimeoutExpired(['synthetic'], 600))
        output = io.StringIO()
        with patch.object(setup_local.sys, 'argv', ['setup_local', '--install']), \
                patch.object(setup_local, 'setup', side_effect=failure), \
                patch('sys.stdout', output), patch('sys.stderr', io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            setup_local.main()
        self.assertEqual(caught.exception.code, 2)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt['status'], 'failed')
        self.assertFalse(receipt['installed'])
        self.assertEqual(receipt['failure']['stage'], 'validation_dependencies')
        self.assertIn('time limit', receipt['failure']['reason'])

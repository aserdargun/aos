from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aos import local_preflight
from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop_mcp_bundle import PACKAGES, prepare_bundle


class LocalPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch('aos.local_preflight.REPO_ROOT', self.root).start()

    def file(self, name, contents='synthetic'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def manifest(self, name, payload):
        return self.file('models/' + name + '-manifest.json', canonical(payload))

    def test_aggregate_success_is_not_runtime_or_training_claim(self):
        with ExitStack() as stack:
            for name in ('application', 'frontend', 'desktop', 'browser', 'mcp', 'decider', 'bonsai'):
                stack.enter_context(patch('aos.local_preflight._' + name))
            report = local_preflight.check_local()
        self.assertTrue(report['ready'])
        self.assertTrue(report['read_only'])
        self.assertFalse(report['inference_verified'])
        self.assertFalse(report['resource_admission_verified'])
        self.assertEqual(len(report['checks']), 7)
        self.assertTrue(all(check['ok'] and not check['action'] for check in report['checks']))

    def test_each_failure_blocks_and_does_not_expose_exception_payload(self):
        for failed in ('application', 'frontend', 'desktop', 'browser', 'mcp', 'decider', 'bonsai'):
            with self.subTest(failed=failed), ExitStack() as stack:
                for name in ('application', 'frontend', 'desktop', 'browser', 'mcp', 'decider', 'bonsai'):
                    stack.enter_context(patch('aos.local_preflight._' + name,
                                              side_effect=ValueError('private-token-example') if name == failed else None))
                report = local_preflight.check_local()
            self.assertFalse(report['ready'])
            rejected = [check for check in report['checks'] if not check['ok']]
            self.assertEqual([check['name'] for check in rejected], [failed])
            self.assertTrue(rejected[0]['action'])
            self.assertNotIn('private-token-example', canonical(report))

    def test_fixture_never_calls_real_model_checks(self):
        with ExitStack() as stack:
            for name in ('application', 'frontend', 'desktop', 'browser', 'mcp'):
                stack.enter_context(patch('aos.local_preflight._' + name))
            decider = stack.enter_context(patch('aos.local_preflight._decider'))
            bonsai = stack.enter_context(patch('aos.local_preflight._bonsai'))
            report = local_preflight.check_local('fixture')
        self.assertTrue(report['ready'])
        self.assertEqual(len(report['checks']), 5)
        self.assertIn('Fixture', report['notice'])
        decider.assert_not_called()
        bonsai.assert_not_called()
        with self.assertRaises(ValueError):
            local_preflight.check_local('auto')

    def test_process_is_bounded_offline_and_fails_closed(self):
        with patch('aos.local_preflight.run_bounded', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')) as process:
            self.assertEqual(local_preflight._process(['/usr/bin/bwrap', '--version']), b'{}')
        self.assertEqual(process.call_args.kwargs['input'], b'')
        self.assertEqual(process.call_args.kwargs['max_output'], 1048576)
        self.assertEqual(process.call_args.kwargs['env']['CUDA_VISIBLE_DEVICES'], '')
        self.assertEqual(process.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
        with patch('aos.local_preflight.run_bounded', return_value=subprocess.CompletedProcess([], 1, b'', b'private')):
            with self.assertRaises(ValueError):
                local_preflight._process(['/usr/bin/bwrap', '--version'])

    def test_python_preserves_virtualenv_path_without_resolving_symlink(self):
        target = self.file('interpreter')
        target.chmod(0o700)
        executable = self.root / '.venv/bin/python'
        executable.parent.mkdir(parents=True)
        executable.symlink_to(target)
        with patch('aos.local_preflight._process', return_value=b'{"synthetic":"1"}') as process:
            self.assertEqual(local_preflight._dependencies(executable), {'synthetic': '1'})
        self.assertEqual(process.call_args.args[0][:4], [str(executable), '-I', '-B', '-c'])
        self.assertNotIn('import torch', process.call_args.args[0][-1])

    def test_application_requires_all_exact_optional_dependencies(self):
        self.file('pyproject.toml', '[project]\ndependencies=["base==1"]\n'
                  '[project.optional-dependencies]\nbrowser=["browser==2"]\n'
                  'desktop=["desktop==3"]\ndataset=["dataset==4"]\n')
        versions = {'base': '1', 'browser': '2', 'desktop': '3', 'dataset': '4'}
        package = self.file('src/aos/__init__.py', '')
        with patch('aos.local_preflight._dependencies', return_value=versions), patch(
                'aos.local_preflight._process', return_value=json.dumps(str(package)).encode()):
            local_preflight._application()
        for changed in ({**versions, 'desktop': 'different'}, {'base': '1'}):
            with patch('aos.local_preflight._dependencies', return_value=changed), self.assertRaises(ValueError):
                local_preflight._application()
        other = self.file('different_package/__init__.py', '')
        with patch('aos.local_preflight._dependencies', return_value=versions), patch(
                'aos.local_preflight._process', return_value=json.dumps(str(other)).encode()), self.assertRaises(ValueError):
            local_preflight._application()

    def frontend(self):
        self.file('ui/package.json', '{}')
        self.file('ui/pnpm-lock.yaml', 'synthetic')
        self.file('ui/index.html', 'synthetic')
        self.file('ui/src/main.tsx', 'synthetic')
        self.file('ui/dist/assets/index.js', 'synthetic')
        self.file('ui/dist/assets/index.css', 'synthetic')
        return self.file('ui/dist/index.html', '<script src="/ui/assets/index.js"></script>'
                         '<link rel="stylesheet" href="/ui/assets/index.css">')

    def test_frontend_missing_stale_and_escaping_assets_fail(self):
        index = self.frontend()
        local_preflight._frontend()
        asset = self.root / 'ui/dist/assets/index.js'
        asset.unlink()
        with self.assertRaises(ValueError):
            local_preflight._frontend()

        outside = self.file('outside.js')
        asset.symlink_to(outside)
        with self.assertRaises(ValueError):
            local_preflight._frontend()
        asset.unlink()
        asset.write_text('synthetic')
        self.file('ui/src/main.tsx', 'changed')
        os.utime(self.root / 'ui/src/main.tsx', ns=(index.stat().st_mtime_ns + 1000000,) * 2)
        with self.assertRaises(ValueError):
            local_preflight._frontend()

    def test_frontend_checks_missing_lazy_chunk_and_source_inputs(self):
        self.frontend()
        self.file('ui/dist/assets/index.js', 'import(`./rfb-test.js`)')
        with self.assertRaises(ValueError):
            local_preflight._frontend()
        self.file('ui/dist/assets/rfb-test.js', 'synthetic')
        local_preflight._frontend()
        (self.root / 'ui/package.json').unlink()
        with self.assertRaises(ValueError):
            local_preflight._frontend()

    def test_project_frontend_is_checked_without_refreshing_shared_dist(self):
        shared = self.frontend()
        original = shared.read_bytes()
        self.file('ui/src/main.tsx', 'changed source')
        os.utime(self.root / 'ui/src/main.tsx', ns=(shared.stat().st_mtime_ns + 1000000,) * 2)
        project = self.root / 'data/local-app-project-learning-demo/ui'
        self.file(str((project / 'assets/index.js').relative_to(self.root)), 'synthetic')
        self.file(str((project / 'assets/index.css').relative_to(self.root)), 'synthetic')
        index = self.file(str((project / 'index.html').relative_to(self.root)), original.decode())
        os.utime(index, ns=(shared.stat().st_mtime_ns + 2000000,) * 2)
        local_preflight._frontend(project)
        self.assertEqual(shared.read_bytes(), original)
        with self.assertRaises(ValueError):
            local_preflight._frontend()
        (project / 'assets/index.js').unlink()
        with self.assertRaises(ValueError):
            local_preflight._frontend(project)

    def test_desktop_only_inspects_immutable_image_and_source(self):
        source = self.file('computer/synthetic.txt')
        sources = {source.name: hashlib.sha256(source.read_bytes()).hexdigest()}
        image_id = 'sha256:' + 'a' * 64
        self.manifest('desktop', {'image_id': image_id, 'source_files': sources, 'source_sha256': digest(sources)})
        image = {'Id': image_id, 'Config': {'Labels': {'com.aos.source-sha256': digest(sources)}}}
        with patch('aos.local_preflight.os.getuid', return_value=1000), patch(
                'aos.local_preflight._process', return_value=canonical([image]).encode()) as process:
            local_preflight._desktop()
            self.assertEqual(process.call_args.args[0], [*local_preflight.DOCKER, 'image', 'inspect', image_id])
            image['Id'] = 'sha256:' + 'b' * 64
            process.return_value = canonical([image]).encode()
            with self.assertRaises(ValueError):
                local_preflight._desktop()
            source.write_text('tampered')
            process.reset_mock()
            with self.assertRaises(ValueError):
                local_preflight._desktop()
            process.assert_not_called()

    def test_browser_hashes_are_checked_without_starting_browser(self):
        executable = self.file('models/browser/chrome-headless-shell')
        executable.chmod(0o700)
        self.manifest('browser', {'playwright_version': '1.63.0', 'revision': '1243',
                                 'browser_version': '153.0.8010.12', 'browser_root': str(executable.parent),
                                 'files': {executable.name: hashlib.sha256(executable.read_bytes()).hexdigest()}})
        with patch('aos.local_preflight._process', return_value=b'bubblewrap synthetic') as process:
            local_preflight._browser()
            self.assertEqual(process.call_args.args[0], ['/usr/bin/bwrap', '--version'])
            executable.write_text('tampered')
            with self.assertRaises(ValueError):
                local_preflight._browser()

    def test_mcp_bundle_must_be_pinned_and_not_symlinked(self):
        modules = self.root / 'node_modules'
        for package, version in PACKAGES.items():
            package_root = modules / package
            package_root.mkdir(parents=True)
            (package_root / 'package.json').write_text(canonical({
                'name': package, 'version': version,
                **({'dependencies': {name: PACKAGES[name] for name in ('playwright', 'playwright-core')}}
                   if package == '@playwright/mcp' else {})}))
        prepare_bundle(modules, self.root / 'models/desktop-mcp-v001')
        local_preflight._mcp()
        archive = self.root / 'models/desktop-mcp-v001/packages.zip'
        payload = archive.read_bytes()
        archive.write_bytes(payload + b'changed')
        with self.assertRaises(ValueError):
            local_preflight._mcp()
        archive.write_bytes(payload)
        linked = archive.with_name('linked.zip')
        linked.symlink_to(archive)
        archive.unlink()
        archive.symlink_to(linked)
        with self.assertRaises(ValueError):
            local_preflight._mcp()

    def test_decider_existing_verifier_and_metadata_only(self):
        self.file('services/decider/worker.py', (REPO_ROOT / 'services/decider/worker.py').read_text())
        pins = {key: 'a' * 40 for key in ('checkpoint_revision', 'tokenizer_revision', 'code_revision')}
        for prefix in ('model', 'code'):
            artifact = self.file('models/' + prefix + '/synthetic.bin')
            pins.update({prefix + '_path': str(artifact.parent),
                         prefix + '_files': {artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()}})
        pins['dependencies'] = {'synthetic': '1'}
        self.manifest('decider', pins)
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with patch('aos.local_preflight._dependencies', return_value=pins['dependencies']):
            local_preflight._decider()
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()})
        with patch('aos.local_preflight._dependencies', return_value={'synthetic': '2'}), self.assertRaises(ValueError):
            local_preflight._decider()

    def test_bonsai_reuses_read_only_verifier_without_plan_or_server(self):
        pins = {key: 'a' * 40 for key in ('checkpoint_revision', 'tokenizer_revision', 'projector_revision', 'code_revision')}
        for prefix in ('model', 'runtime'):
            artifact = self.file('models/' + prefix + '/synthetic.bin')
            artifact.chmod(0o700)
            pins.update({prefix + '_path': str(artifact.parent),
                         prefix + '_files': {artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()}})
        pins.update(weights_file='synthetic.bin', projector_file='synthetic.bin',
                    server_path=str(artifact), native_libraries={str(artifact): hashlib.sha256(artifact.read_bytes()).hexdigest()})
        self.manifest('bonsai', pins)
        with patch('aos.supervisor.asyncio.create_subprocess_exec') as spawning:
            local_preflight._bonsai()
        spawning.assert_not_called()
        artifact.write_text('tampered')
        with self.assertRaises(Exception):
            local_preflight._bonsai()

    def test_manifest_rejects_symlink_oversized_or_nonobject(self):
        target = self.file('synthetic.json', '{}')
        link = self.root / 'link.json'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            local_preflight._json_file(link)
        for payload in ('[]', ' ' * 1048577):
            target.write_text(payload)
            with self.assertRaises(ValueError):
                local_preflight._json_file(target)

    def test_cli_returns_readiness_exit_code_and_explicit_fixture(self):
        for ready, code in ((False, 1), (True, 0)):
            with patch('aos.local_preflight.sys.argv', ['doctor', '--fixture']), patch(
                    'aos.local_preflight.check_local', return_value={'ready': ready}) as doctor, patch('builtins.print') as output:
                self.assertEqual(local_preflight.main(), code)
            doctor.assert_called_once_with('fixture')
            self.assertEqual(json.loads(output.call_args.args[0]), {'ready': ready})


if __name__ == '__main__':
    unittest.main()

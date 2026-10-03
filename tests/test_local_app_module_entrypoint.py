import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aos.contracts import REPO_ROOT, canonical, digest
from aos.shared_desktop_plan import SharedDesktopLimits, SharedDesktopTemplate


BOOTSTRAP = '''
from pathlib import Path
import runpy
import sys
import aos.local_app as canonical_app
root = Path(sys.argv[1])
canonical_app.REPO_ROOT = root
canonical_app.BASE = root / 'data/local-app-v1'
sys.argv = ['aos.local_app', *sys.argv[2:]]
runpy.run_module('aos.local_app', run_name='__main__')
'''


class LocalAppModuleEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-module-entrypoint-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        (self.root / 'data').mkdir(mode=0o700)
        self.sources = {}
        for name in ('synthetic-python', 'synthetic-launcher.py'):
            path = self.file(name, b'SYNTHETIC pinned input; never executed or imported')
            self.sources[str(path)] = self.sha(path.read_bytes())
        config = self.file('synthetic-config.json', b'{"synthetic":true}')
        self.configs = {str(config): self.sha(config.read_bytes())}

    @staticmethod
    def sha(content):
        return hashlib.sha256(content).hexdigest()

    def file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        path.chmod(0o600)
        return path

    def cli(self, arguments, *, successful=True):
        environment = {**os.environ, 'PYTHONPATH': str(REPO_ROOT / 'src')}
        result = subprocess.run([sys.executable, '-c', BOOTSTRAP, str(self.root), *arguments],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0 if successful else 1, result.stderr)
        return json.loads(result.stdout) if successful else result

    def template(self, project):
        base = self.root / 'data' / ('local-app-v1' if project is None else 'local-app-project-' + project)
        port = 8765 if project is None else 48765
        template = SharedDesktopTemplate(origin='http://127.0.0.1:' + str(port), manager_base=str(base),
            session_root=str(base), project=project, python_path=str(self.root / 'synthetic-python'),
            python_sha256=self.sources[str(self.root / 'synthetic-python')],
            launcher_path=str(self.root / 'synthetic-launcher.py'),
            launcher_sha256=self.sources[str(self.root / 'synthetic-launcher.py')],
            source_files=self.sources, source_sha256=digest(self.sources), config_files=self.configs,
            config_sha256=digest(self.configs), broker_socket=str(self.root / 'synthetic.sock'),
            broker_identity_sha256='a' * 64,
            limits=SharedDesktopLimits(cpu_quota_percent=100, memory_max_bytes=536870912,
                                       tasks_max=32, stop_timeout_seconds=5))
        path = self.file('template-' + (project or 'default') + '.json',
                         canonical(template.model_dump(mode='json')).encode())
        return template, path

    def test_subprocess_main_boundary_prepares_and_provisions_named_and_default_scopes(self):
        for project in (None, 'synthetic-module'):
            with self.subTest(project=project):
                template, template_path = self.template(project)
                selected = [] if project is None else ['--project', project, '--project-port', '48765']
                plan_path = self.root / ('plan-' + (project or 'default') + '.json')
                prepared = self.cli(['prepare-shared', *selected, '--shared-template', str(template_path),
                    '--shared-template-sha256', self.sha(template_path.read_bytes()),
                    '--shared-output', str(plan_path), '--expected-session', 'none'])
                self.assertEqual(prepared['phase'], 'prepared')
                self.assertFalse(prepared['runtime_started'])
                self.assertFalse(Path(prepared['workspace']).exists())
                self.assertEqual(prepared['url'], template.url)
                plan = json.loads(plan_path.read_bytes())
                self.assertEqual(plan['template']['manager_base'], template.manager_base)
                self.assertEqual(plan['template']['project'], project)
                base = Path(template.manager_base)
                base.mkdir(mode=0o700)
                provisioned = self.cli(['provision-shared', *selected, '--shared-plan', str(plan_path),
                    '--shared-plan-sha256', prepared['plan_sha256'], '--expected-session', 'none'])
                self.assertEqual(provisioned['phase'], 'provisioned')
                self.assertFalse(provisioned['runtime_started'])
                self.assertFalse(provisioned['execution_authorized'])
                session = base / prepared['session']
                self.assertEqual(set(session.iterdir()), {session / 'workspace', session / 'shared-provision.json'})
                self.assertEqual(list((session / 'workspace').iterdir()), [])
                self.assertFalse((base / 'current.json').exists())
                self.assertFalse((session / 'trajectory.sqlite3').exists())
                self.assertFalse((session / 'shared-launch-intent.json').exists())
                self.assertFalse((self.root / 'runs').exists())

    def test_subprocess_main_boundary_still_rejects_wrong_named_scope_without_output(self):
        _template, template_path = self.template('synthetic-module')
        output = self.root / 'wrong-plan.json'
        result = self.cli(['prepare-shared', '--project', 'other-synthetic', '--project-port', '48766',
            '--shared-template', str(template_path), '--shared-template-sha256', self.sha(template_path.read_bytes()),
            '--shared-output', str(output), '--expected-session', 'none'], successful=False)
        self.assertIn('selected manager base', result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(list((self.root / 'data').iterdir()), [])


if __name__ == '__main__':
    unittest.main()

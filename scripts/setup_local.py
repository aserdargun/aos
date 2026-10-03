"""Prepare a fresh isolated CPU environment and staged console, never deploy."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from scripts.package_handoff import collect_source_paths, read_source_member


ROOT = Path(__file__).resolve().parents[1]
UI_FILES = {'ui/package.json', 'ui/pnpm-lock.yaml', 'ui/pnpm-workspace.yaml',
            'ui/tsconfig.json', 'ui/vite.config.ts', 'ui/index.html', 'ui/app-icon.svg'}


class SetupFailure(ValueError):
    def __init__(self, report, stage, error):
        report.update(status='failed', installed=False, ui_staged=False,
                      observed_at=datetime.now(timezone.utc).isoformat(),
                      failure={'stage': stage, 'reason': failure_reason(error),
                               'next_step': 'Inspect command output and partial destinations; retry only in fresh paths.'})
        self.report = report
        super().__init__(report['failure']['reason'])


def failure_reason(error):
    if isinstance(error, subprocess.TimeoutExpired):
        return 'Command exceeded the setup time limit'
    if isinstance(error, subprocess.CalledProcessError):
        return f'Command exited with status {error.returncode}; inspect its output for dependency/cache or build errors'
    if isinstance(error, OSError):
        return f'Filesystem or prerequisite command failed ({type(error).__name__})'
    return str(error)


def capture(arguments, *, root, environment):
    return subprocess.check_output(arguments, cwd=root, env=environment, text=True,
                                   timeout=60).strip()


def installed_versions(target, *, root, environment):
    probe = ('import importlib.metadata,json,pathlib,sys,aos; '
             'print(json.dumps({"python":sys.version.split()[0],"prefix":sys.prefix,'
             '"aos_source":str(pathlib.Path(aos.__file__).resolve()),'
             '"distributions":{name:importlib.metadata.version(name) for name in '
             '["aos-local","pydantic","jsonschema","rfc3339-validator","PyYAML",'
             '"fastapi","uvicorn","websockets","httpx","playwright"]}}))')
    result = json.loads(capture([str(target / 'bin/python'), '-I', '-c', probe],
                               root=root, environment=environment))
    if (result['prefix'] != str(target)
            or result['aos_source'] != str(root / 'src/aos/__init__.py')
            or not isinstance(result['distributions'], dict)
            or not all(type(value) is str and value for value in result['distributions'].values())):
        raise ValueError('Installed interpreter/source binding does not match this fresh environment and checkout')
    return result


def build_artifacts(staged_ui):
    output = staged_ui / 'dist'
    if output.is_symlink() or not output.is_dir():
        raise ValueError('Console build did not produce a regular dist directory')
    artifacts = {}
    for path in sorted(output.rglob('*')):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > 32 * 1024 * 1024 or len(artifacts) >= 4096):
            raise ValueError('Console artifact is not a bounded single-link regular file')
        artifacts[path.relative_to(output).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if 'index.html' not in artifacts:
        raise ValueError('Console build has no index.html')
    return artifacts


def source_snapshot(root):
    names = collect_source_paths(root)
    expected = ''.join(f'{hashlib.sha256(read_source_member(root, name).data).hexdigest()}  {name}\n'
                       for name in names).encode()
    actual = read_source_member(root, 'MANIFEST.sha256').data
    if actual != expected:
        raise ValueError('Source manifest is stale; review source changes before setup')
    return hashlib.sha256(actual).hexdigest(), names


def _read_prerequisite_manifest(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError('manifest must be a regular file no larger than 1 MiB')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            payload = stream.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError('manifest exceeds 1 MiB')
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError('manifest must contain a JSON object')
        return result
    finally:
        os.close(descriptor)


def prerequisites(root, *, python, build_ui=False):
    root = Path(root).resolve(strict=True)
    tools = {'python': shutil.which(python) or str(Path(python).expanduser()), 'uv': shutil.which('uv'),
             'docker': shutil.which('docker'), 'nvidia-smi': shutil.which('nvidia-smi')}
    if build_ui:
        tools.update(node=shutil.which('node'), pnpm=shutil.which('pnpm'))
    tool_checks = [{'name': name, 'present': bool(path and Path(path).is_file() and os.access(path, os.X_OK)),
                    'path': path} for name, path in tools.items()]
    artifacts = []
    for filename, keys in (('decider-manifest.json', ('model_path', 'code_path')),
                           ('bonsai-manifest.json', ('model_path', 'runtime_path', 'server_path',
                                                     'weights_file', 'projector_file')),
                           ('browser-manifest.json', ('browser_root',)),
                           ('desktop-manifest.json', ('image_id',))):
        manifest_path = root / 'models' / filename
        try:
            manifest = _read_prerequisite_manifest(manifest_path)
            absent_keys = [key for key in keys if key not in manifest]
            if absent_keys:
                raise ValueError('missing required keys: ' + ', '.join(absent_keys))
            if filename == 'desktop-manifest.json' and (type(manifest['image_id']) is not str
                                                        or not manifest['image_id']):
                raise ValueError('image_id must be a non-empty string')
        except (OSError, ValueError, json.JSONDecodeError) as error:
            reason = str(error) if isinstance(error, ValueError) else 'manifest is missing or unreadable'
            artifacts.append({'manifest': str(manifest_path), 'present': False,
                              'paths': [], 'issue': reason,
                              'next_step': 'Prepare the local runtime manifest and artifacts using docs/DEVELOPMENT.md and docs/BONSAI_RUNTIME.md.'})
            continue
        paths = []
        for key in keys:
            if key in ('weights_file', 'projector_file', 'image_id'):
                continue
            value = manifest[key]
            if type(value) is not str or not value:
                paths.append({'name': key, 'path': None, 'present': False,
                              'issue': 'manifest path/identity must be a non-empty string'})
                continue
            path = Path(value).expanduser()
            paths.append({'name': key, 'path': str(path), 'present': path.exists()})
        if filename == 'bonsai-manifest.json' and all(type(manifest[key]) is str and manifest[key]
                                                       for key in ('model_path', 'weights_file', 'projector_file')):
            for key in ('weights_file', 'projector_file'):
                path = Path(manifest['model_path']).expanduser() / manifest[key]
                paths.append({'name': key, 'path': str(path), 'present': path.is_file()})
        elif filename == 'bonsai-manifest.json':
            for key in ('weights_file', 'projector_file'):
                paths.append({'name': key, 'path': None, 'present': False,
                              'issue': 'manifest path/filename must be a non-empty string'})
        artifacts.append({'manifest': str(manifest_path), 'present': True, 'paths': paths,
                          'unprobed': ([{'name': 'desktop_image', 'identity': manifest['image_id'],
                                         'reason': 'Docker image presence is not queried by this read-only report'}]
                                       if filename == 'desktop-manifest.json' else []),
                          'next_step': 'Inspect missing local artifacts against the pinned manifest; setup does not download them.'})
    missing_tools = [item['name'] for item in tool_checks if not item['present']]
    missing_artifacts = [entry['manifest'] for entry in artifacts if not entry['present']]
    missing_artifacts.extend(path['path'] or f"{entry['manifest']}:{path['name']}"
                             for entry in artifacts for path in entry['paths'] if not path['present'])
    next_steps = []
    if missing_tools:
        next_steps.append('Install missing host tools using the host owner’s normal process.')
    if missing_artifacts:
        next_steps.append('Inspect missing local runtime manifests/artifacts and prepare them explicitly; setup does not download them.')
    next_steps.extend(['Run the existing aos-v1 doctor for full pin/hash prerequisite checks.',
                       'This report is presence-only; it does not verify versions, hashes, GPU admission, inference, runtime readiness, or task success.'])
    return {'schema': 'aos.local-setup-prerequisites.v1',
            'observed_at': datetime.now(timezone.utc).isoformat(), 'status': 'observed',
            'presence_complete': not missing_tools and not missing_artifacts, 'read_only': True,
            'tools': tool_checks, 'runtime_artifacts': artifacts,
            'missing': {'tools': missing_tools, 'artifacts': missing_artifacts},
            'next_steps': next_steps,
            'runtime_verified': False, 'gpu_readiness_verified': False, 'inference_verified': False}


def fresh_destination(root, relative):
    if type(relative) is not str or not relative or Path(relative).is_absolute():
        raise ValueError('Setup destinations must be relative to this checkout')
    components = Path(relative).parts
    if any(part in {'', '.', '..'} for part in relative.split('/')):
        raise ValueError('Parent traversal is not allowed')
    if relative != '.venv' and (len(components) < 3 or components[0] != 'data'):
        raise ValueError('Use .venv or a fresh directory under data/<setup-name>/')
    target = root.joinpath(*components)
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('Symlink setup destination is not allowed')
    if target.exists():
        raise ValueError('Destination already exists; existing environments/builds are never updated or deleted')
    return target


def command(arguments, *, root, environment):
    subprocess.run(arguments, cwd=root, env=environment, check=True, timeout=600)


def setup(root, *, environment_path, python, install=False, offline=False,
          build_ui=False, ui_path='data/setup-local/ui', prerequisites_only=False):
    root = Path(root).resolve(strict=True)
    if prerequisites_only:
        return prerequisites(root, python=python, build_ui=build_ui)
    manifest, names = source_snapshot(root)
    target = fresh_destination(root, environment_path)
    staged_ui = fresh_destination(root, ui_path) if build_ui else None
    if staged_ui is not None and (target == staged_ui or target in staged_ui.parents or staged_ui in target.parents):
        raise ValueError('Environment and UI staging directories must not overlap')
    uv = shutil.which('uv')
    node = shutil.which('node') if build_ui else None
    pnpm = shutil.which('pnpm') if build_ui else None
    missing = [name for name, binary in [('uv', uv), *([('node', node), ('pnpm', pnpm)] if build_ui else [])]
               if binary is None]
    if missing:
        raise ValueError('Missing prerequisites: ' + ', '.join(missing)
                         + '; install them separately and retry. Setup never changes system packages')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('UV_', 'VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME'))}
    environment.update(UV_PROJECT_ENVIRONMENT=str(target), UV_PYTHON_DOWNLOADS='never',
                       UV_NO_PROGRESS='1', PYTHONDONTWRITEBYTECODE='1')
    uv_arguments = [uv, 'sync', '--locked', '--python', python,
                    '--extra', 'dataset', '--extra', 'desktop', '--extra', 'browser']
    if offline:
        uv_arguments.append('--offline')
    validation = [uv, 'pip', 'install', '--python', str(target / 'bin/python'),
                  '-r', str(root / 'requirements-validation.txt')]
    if offline:
        validation.append('--offline')
    report = {'schema': 'aos.local-setup.v1', 'source_manifest_sha256': manifest,
              'status': 'planned', 'observed_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'cpu_dependencies_and_optional_staged_console',
              'source_binding': 'editable_checkout_at_observation_time',
              'source_inputs': {}, 'tool_versions': {}, 'installed_versions': None,
              'build_artifacts': {}, 'completed_steps': [], 'failure': None,
              'installed': False, 'ui_staged': False, 'environment': str(target),
              'ui_directory': None if staged_ui is None else str(staged_ui),
              'models_downloaded': False, 'runtime_started': False, 'deployed': False,
              'scientist_admitted': False, 'gpu_release_verified': False,
              'commands': [uv_arguments, validation],
              'remaining': ['Pinned local model/browser/desktop preparation',
                            'Explicit UI promotion in a fresh or reviewed idle checkout',
                            'Authorized runtime startup and real task acceptance']}
    if not install:
        return report
    stage = 'prerequisites'
    try:
        report['tool_versions']['uv'] = capture([uv, '--version'], root=root, environment=environment)
        if build_ui:
            report['tool_versions']['node'] = capture([node, '--version'], root=root, environment=environment)
            report['tool_versions']['pnpm'] = capture([pnpm, '--version'], root=root, environment=environment)
            package = json.loads(read_source_member(root, 'ui/package.json').data)
            if package['packageManager'] != 'pnpm@' + report['tool_versions']['pnpm']:
                raise ValueError('Use the pnpm version pinned by ui/package.json before staging the console')
        report['source_inputs'] = {name: hashlib.sha256(read_source_member(root, name).data).hexdigest()
                                   for name in ('pyproject.toml', 'uv.lock', 'requirements-validation.txt')}
        stage = 'environment_creation'
        fresh_destination(root, environment_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(mode=0o700)
        cli_help = [str(target / 'bin/python'), '-I', '-m', 'aos.cli', '--help']
        report['commands'].append(cli_help)
        for stage, arguments in [('locked_dependencies', uv_arguments), ('validation_dependencies', validation),
                                 ('cli_import', cli_help)]:
            command(arguments, root=root, environment=environment)
            report['completed_steps'].append(stage)
        stage = 'installed_version_verification'
        report['installed_versions'] = installed_versions(target, root=root, environment=environment)
        report['completed_steps'].append(stage)
        if staged_ui is not None:
            stage = 'console_staging'
            fresh_destination(root, ui_path)
            staged_ui.parent.mkdir(parents=True, exist_ok=True)
            staged_ui.mkdir(mode=0o700)
            for name in names:
                if name in UI_FILES or name.startswith('ui/src/'):
                    payload = read_source_member(root, name).data
                    report['source_inputs'][name] = hashlib.sha256(payload).hexdigest()
                    destination = staged_ui / name.removeprefix('ui/')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(payload)
            arguments = [pnpm, 'install', '--frozen-lockfile', '--ignore-scripts']
            if offline:
                arguments.append('--offline')
            for stage, arguments in [('console_dependencies', arguments), ('console_build', [pnpm, 'run', 'build'])]:
                report['commands'].append(arguments)
                command(arguments, root=staged_ui, environment=environment)
                report['completed_steps'].append(stage)
            stage = 'console_artifact_verification'
            report['build_artifacts'] = build_artifacts(staged_ui)
            report['completed_steps'].append(stage)
        stage = 'final_source_verification'
        if source_snapshot(root)[0] != manifest:
            raise ValueError('Source changed during setup; review changes and retry in fresh paths before promotion')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        raise SetupFailure(report, stage, error) from error
    report.update(status='completed', installed=True, ui_staged=staged_ui is not None,
                  observed_at=datetime.now(timezone.utc).isoformat())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', default='.venv')
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--install', action='store_true', help='Explicitly install in a new environment; otherwise show the plan')
    parser.add_argument('--offline', action='store_true', help='Use local dependency caches only')
    parser.add_argument('--build-ui', action='store_true', help='Build in a fresh staging directory, not ui/dist')
    parser.add_argument('--ui-directory', default='data/setup-local/ui')
    parser.add_argument('--prerequisites', action='store_true',
                        help='Read-only presence report; does not hash models or start runtime processes')
    arguments = parser.parse_args()
    try:
        report = setup(ROOT, environment_path=arguments.environment, python=arguments.python,
                       install=arguments.install, offline=arguments.offline,
                       build_ui=arguments.build_ui, ui_path=arguments.ui_directory,
                       prerequisites_only=arguments.prerequisites)
    except SetupFailure as error:
        print(json.dumps(error.report, indent=2, sort_keys=True))
        parser.exit(2, f'Setup stopped at {error.report["failure"]["stage"]}: {error}\n')
    except ValueError as error:
        parser.exit(2, f'Setup refused: {error}\n')
    except (OSError, subprocess.SubprocessError) as error:
        parser.exit(2, f'Setup stopped ({type(error).__name__}); existing runtime was not restarted.\n')
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()

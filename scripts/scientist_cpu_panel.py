"""Check, or explicitly start, a fresh reviewed CPU-only Scientist console."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys

from aos.contracts import REPO_ROOT, digest
from aos.scientist_cpu_capability import ScientistCpuReviewedGrant
from aos.scientist_cpu_session import prepare_scientist_cpu_startup
from aos.scientist_lab_service import ScientistLabStartup
from aos.scientist_protocol import _reject_constant, _unique_object
from aos.scientist_transport import ScientistAdmissionError
from scripts.setup_local import source_snapshot


SOURCE_ROOT = Path(__file__).resolve().parents[1]
STORE_OPTIONS = (
    'console-assets-root', 'web-profiles-root', 'knowledge-root', 'site-knowledge-root',
    'site-skills-root', 'page-seed-root', 'route-review-root', 'json-review-root',
    'json-page-seed-root', 'web-task-root', 'web-form-plan-root', 'web-form-value-root',
    'web-form-state-root', 'web-route-root', 'web-static-root', 'web-readonly-data-root',
)


def checked_hash(value):
    if re.fullmatch('[a-f0-9]{64}', value) is None:
        raise argparse.ArgumentTypeError('Expected a lowercase SHA256')
    return value


def private_directory(path):
    metadata = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError('Expected an owned, non-symlink private directory')


def bounded_bytes(path, *, private=False, limit=131072):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Input path cannot contain links or traversal')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_size > limit or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022
                or (private and stat.S_IMODE(metadata.st_mode) != 0o600)):
            raise ValueError('Input must be a bounded owned regular file with safe permissions')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValueError('Input exceeds its bound')
        return payload
    finally:
        os.close(descriptor)


def reviewed_document(path, expected):
    payload = bounded_bytes(path, private=True)
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError('Reviewed document changed')
    json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return payload


def ui_artifacts_sha256(root):
    private_directory(root)
    artifacts = {}
    total = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('UI artifacts cannot contain links')
        if path.is_dir():
            continue
        payload = bounded_bytes(path, limit=32 * 1024 * 1024)
        total += len(payload)
        if len(artifacts) >= 4096 or total > 64 * 1024 * 1024:
            raise ValueError('UI artifacts exceed their bounds')
        artifacts[path.relative_to(root).as_posix()] = hashlib.sha256(payload).hexdigest()
    if 'index.html' not in artifacts:
        raise ValueError('Prepared UI has no index.html')
    return digest(artifacts)


def check(options):
    if (SOURCE_ROOT != REPO_ROOT
            or re.fullmatch(r'scientist-[a-z0-9][a-z0-9-]{0,37}', options.project) is None
            or re.fullmatch(r'app-[a-f0-9]{32}', options.session) is None
            or not 1024 <= options.port <= 65535 or options.port == 8765):
        raise ValueError('Expected a dedicated Scientist project, explicit session and non-default port')
    root = REPO_ROOT / 'data' / ('local-app-project-' + options.project)
    private_directory(root)
    private_directory(root / 'review')
    if any(path.name.startswith('app-') for path in root.iterdir()):
        raise ValueError('Existing session requires inspection; automatic reuse is forbidden')
    startup = ScientistLabStartup.model_validate_json(reviewed_document(
        root / 'review/startup.json', options.startup_sha256), strict=True)
    grant = ScientistCpuReviewedGrant.model_validate_json(reviewed_document(
        root / 'review/grant.json', options.grant_sha256), strict=True)
    if (startup.token_file != root / 'review/scientist.token'
            or grant.capability.max_experiments != 1 or grant.capability.max_wall_seconds > 600
            or grant.aos_source_manifest_sha256 != options.source_manifest_sha256):
        raise ValueError('CPU panel requires one bounded experiment and exact source/credential scope')
    bounded_bytes(startup.token_file, private=True, limit=4096)
    if source_snapshot(REPO_ROOT)[0] != options.source_manifest_sha256:
        raise ValueError('Reviewed AOS source differs')
    desktop = bounded_bytes(REPO_ROOT / 'models/desktop-manifest.json', limit=1048576)
    if hashlib.sha256(desktop).hexdigest() != options.desktop_manifest_sha256:
        raise ValueError('Reviewed desktop manifest differs')
    if ui_artifacts_sha256(root / 'ui') != options.ui_artifacts_sha256:
        raise ValueError('Reviewed private UI differs')
    startup, _client, grant = prepare_scientist_cpu_startup(startup, grant)
    return root, startup, grant


def start(options, root, startup, grant):
    session = root / options.session
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', options.port))
        listener.listen(128)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if any(name.startswith('app-') for name in os.listdir(descriptor)):
                raise ValueError('Existing session requires inspection')
            os.mkdir(options.session, mode=0o700, dir_fd=descriptor)
            serve_panel(options, session, listener, startup, grant)
        finally:
            os.close(descriptor)


def serve_panel(options, session, listener, startup, grant):
    arguments = ['scientist_cpu_panel', '--engine', 'fixture', '--vision-engine', 'disabled',
        '--port', str(options.port), '--listen-fd', str(listener.fileno()),
        '--workspace', str(session / 'workspace'), '--database', str(session / 'session.sqlite'),
        '--trajectory-database', str(session / 'trajectory.sqlite')]
    for option in STORE_OPTIONS:
        arguments.extend(['--' + option, str(session / option.removesuffix('-root'))])
    if options.local_ui_auto_login:
        arguments.append('--local-ui-auto-login')
    previous_arguments = sys.argv
    previous_cuda = os.environ.get('CUDA_VISIBLE_DEVICES')
    previous_umask = os.umask(0o077)
    try:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
        sys.argv = arguments
        from scripts.serve_desktop import main as serve

        serve(scientist_lab_config=startup, scientist_cpu_grant=grant)
    finally:
        sys.argv = previous_arguments
        os.umask(previous_umask)
        if previous_cuda is None:
            os.environ.pop('CUDA_VISIBLE_DEVICES', None)
        else:
            os.environ['CUDA_VISIBLE_DEVICES'] = previous_cuda


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='Fresh scientist-NAME project; never the active pilot')
    parser.add_argument('--session', required=True, help='Reviewed fresh app-<32 lowercase hex> identity')
    parser.add_argument('--port', required=True, type=int)
    for name in ('startup', 'grant', 'source-manifest', 'desktop-manifest', 'ui-artifacts'):
        parser.add_argument('--' + name + '-sha256', required=True, type=checked_hash)
    parser.add_argument('--start', action='store_true', help='Explicitly start the reviewed separate CPU desktop')
    parser.add_argument('--local-ui-auto-login', action='store_true')
    options = parser.parse_args(argv)
    try:
        root, startup, grant = check(options)
    except (OSError, ValueError, ScientistAdmissionError):
        parser.error('CPU panel review is missing, changed, unsafe or incompatible; nothing started')
    if not options.start:
        print(json.dumps({'configuration_verified': True, 'runtime_started': False,
            'remote_requested': False, 'gpu_authorized': False,
            'source_manifest_sha256': options.source_manifest_sha256}))
        return
    try:
        start(options, root, startup, grant)
    except (OSError, ValueError, ScientistAdmissionError):
        parser.error('CPU panel could not complete startup; inspect retained session before retrying')


if __name__ == '__main__':
    main()

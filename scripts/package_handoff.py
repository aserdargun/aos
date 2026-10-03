"""Create a deterministic source-only tar from a verified source manifest."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORIES = ('docs', 'database', 'schemas', 'examples', 'config', 'benchmarks',
                      'training/recipes', 'src/aos', 'services/decider', 'services/laya', 'services/bonsai', 'tests', 'scripts',
                      'computer', 'ui/src', 'ui/src-tauri/src')
ROOT_FILES = ('README.md', 'CODEX_KICKOFF.md', 'AGENTS.md', 'CLAUDE.md', 'CONTRIBUTING.md',
              'SECURITY.md', '.gitignore', 'requirements-validation.txt', 'pyproject.toml', 'uv.lock')
EXPLICIT_FILES = (
    'services/broker_runtime.py', 'services/bonsai_projection.py',
    'ui/package.json', 'ui/pnpm-lock.yaml', 'ui/pnpm-workspace.yaml', 'ui/tsconfig.json',
    'ui/vite.config.ts', 'ui/index.html', 'ui/src-tauri/Cargo.toml', 'ui/src-tauri/Cargo.lock',
    'ui/src-tauri/build.rs', 'ui/src-tauri/tauri.conf.json', 'ui/app-icon.svg',
    'ui/src-tauri/icons/128x128.png', 'data/README.md', 'datasets/README.md',
    'models/README.md', 'adapters/README.md', 'runs/README.md', 'training/README.md')
OPTIONAL_FILES = ('LICENSE', 'LICENSE.md', 'LICENSE.txt', 'NOTICE', 'NOTICE.md', 'NOTICE.txt')
SOURCE_SUFFIXES = {'.py', '.json', '.md', '.tsx', '.sql', '.ts', '.css', '.yaml', '.yml',
                   '.html', '.desktop', '.txt', '.toml', '.lock', '.rs', '.js', '.svg', '.sh'}
MAX_FILES = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SourceMember:
    name: str
    data: bytes
    mode: int


def source_name_allowed(name):
    if (not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9_./-]+', name) is None
            or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/'))):
        return False
    if name in (*ROOT_FILES, *EXPLICIT_FILES, *OPTIONAL_FILES):
        return True
    parts = PurePosixPath(name).parts
    if any(part.startswith('.') or part in ('__pycache__', 'node_modules', 'target', 'dist')
           for part in parts):
        return False
    if not any(name.startswith(directory + '/') for directory in SOURCE_DIRECTORIES):
        return False
    if parts[-1].lower() in ('secrets.json', 'credentials.json', 'token.json', 'tokens.json'):
        return False
    if name in ('scripts/aos-v1', 'scripts/aos-parameter-project', 'scripts/aos-parameter-skill', 'computer/Dockerfile',
                'examples/system1_choice.jsonl', 'examples/system2_supervisor.jsonl'):
        return True
    return PurePosixPath(name).suffix in SOURCE_SUFFIXES


@contextmanager
def _directory(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('parent traversal refused')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def read_source_member(root, name, limit=MAX_FILE_BYTES):
    if name != 'MANIFEST.sha256' and not source_name_allowed(name):
        raise ValueError('source path is not allowlisted')
    path = Path(root) / name
    with _directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > limit):
                raise ValueError('source must be a bounded single-link regular file')
            chunks = []
            remaining = before.st_size + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b''.join(chunks)
            if (len(data) != before.st_size or _identity(before) != _identity(os.fstat(descriptor))
                    or _identity(before) != _identity(os.stat(path.name, dir_fd=parent,
                                                            follow_symlinks=False))):
                raise ValueError('source changed during read')
            if name != 'ui/src-tauri/icons/128x128.png':
                text = data.decode('utf-8')
                private_key = '-----BEGIN ' + r'(?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
                access_token = r'(?:hf_|ghp_|sk-proj-)' + r'[A-Za-z0-9]{20,}'
                if re.search(private_key, text) or re.search(access_token, text):
                    raise ValueError('possible private credential in source')
            return SourceMember(name, data, 0o755 if before.st_mode & 0o111 else 0o644)
        finally:
            os.close(descriptor)


def collect_source_paths(root):
    root = Path(root)
    names = set(ROOT_FILES + EXPLICIT_FILES)
    names.update(name for name in OPTIONAL_FILES if os.path.lexists(root / name))
    for directory in SOURCE_DIRECTORIES:
        with _directory(root / directory):
            pass
        for parent, directories, files in os.walk(root / directory, followlinks=False):
            directories[:] = sorted(name for name in directories if name != '__pycache__')
            for name in directories:
                if (Path(parent) / name).is_symlink():
                    raise ValueError('source directory symlink refused')
            for name in files:
                if name.endswith('.pyc'):
                    continue
                relative = (Path(parent) / name).relative_to(root).as_posix()
                if not source_name_allowed(relative):
                    raise ValueError('unexpected file in source directory: ' + relative)
                names.add(relative)
                if len(names) > MAX_FILES:
                    raise ValueError('source file count exceeds bound')
    return sorted(names)


def validate_source_manifest(root):
    root = Path(root)
    manifest = read_source_member(root, 'MANIFEST.sha256', MAX_MANIFEST_BYTES)
    entries = {}
    for line in manifest.data.decode('ascii').splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([A-Za-z0-9_./-]+)', line)
        if match is None:
            raise ValueError('malformed source manifest')
        checksum, name = match.groups()
        if name in entries or not source_name_allowed(name):
            raise ValueError('duplicate or unauthorized manifest path')
        entries[name] = checksum
        if len(entries) > MAX_FILES:
            raise ValueError('manifest file count exceeds bound')
    if set(entries) != set(collect_source_paths(root)):
        raise ValueError('source manifest inventory differs; review and update it first')
    members = [manifest]
    total = len(manifest.data)
    for name in sorted(entries):
        member = read_source_member(root, name, min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
        if hashlib.sha256(member.data).hexdigest() != entries[name]:
            raise ValueError('source manifest hash mismatch: ' + name)
        members.append(member)
        total += len(member.data)
    return sorted(members, key=lambda member: member.name)


def package_handoff(root, output):
    members = validate_source_manifest(root)
    output = Path(output).absolute()
    if output.suffix != '.tar':
        raise ValueError('output must be a .tar source archive')
    with _directory(output.parent) as parent:
        descriptor = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as archive:
                    for member in members:
                        info = tarfile.TarInfo(member.name)
                        info.size = len(member.data)
                        info.mode = member.mode
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ''
                        archive.addfile(info, io.BytesIO(member.data))
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(output.name, dir_fd=parent)
            raise
    return {'source_only': True, 'files': len(members),
            'manifest_sha256': hashlib.sha256(next(member.data for member in members
                                                  if member.name == 'MANIFEST.sha256')).hexdigest(),
            'runtime_bundled': False, 'installer': False, 'complete_secret_audit': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args()
    import json
    print(json.dumps(package_handoff(ROOT, arguments.output), sort_keys=True))


if __name__ == '__main__':
    main()

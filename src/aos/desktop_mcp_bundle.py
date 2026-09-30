import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile


MCP_VERSION = '0.0.82'
PLAYWRIGHT_VERSION = '1.64.0-alpha-1789764292000'
PACKAGES = {'@playwright/mcp': MCP_VERSION, 'playwright': PLAYWRIGHT_VERSION,
            'playwright-core': PLAYWRIGHT_VERSION}
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILES = 2048


def member_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
            or str(path) != name or not any(name.startswith(package + '/') for package in PACKAGES)):
        raise ValueError('Invalid MCP package path')
    return path


def inspect_bundle(payload):
    try:
        _inspect_bundle(payload)
    except (zipfile.BadZipFile, KeyError, TypeError, UnicodeError) as error:
        raise ValueError('Malformed MCP package archive') from error


def _inspect_bundle(payload):
    if not payload or len(payload) > MAX_ARCHIVE:
        raise ValueError('MCP archive size exceeded')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if (not 0 < len(members) <= MAX_FILES or len(set(names)) != len(names)
                or sum(member.file_size for member in members) > MAX_EXPANDED):
            raise ValueError('MCP archive contents exceeded bounds')
        for member in members:
            member_name(member.filename)
            if not stat.S_ISREG(member.external_attr >> 16) or member.flag_bits & 1:
                raise ValueError('Only unencrypted regular MCP package files are allowed')
        for package, version in PACKAGES.items():
            metadata = json.loads(archive.read(package + '/package.json'))
            if metadata.get('name') != package or metadata.get('version') != version:
                raise ValueError('MCP dependency version differs')
        dependencies = json.loads(archive.read('@playwright/mcp/package.json'))['dependencies']
        if dependencies != {package: PACKAGES[package] for package in ('playwright', 'playwright-core')}:
            raise ValueError('MCP dependency set differs')
        if archive.testzip() is not None:
            raise ValueError('MCP archive CRC differs')


def prepare_bundle(node_modules: Path, output: Path):
    if not node_modules.is_dir() or node_modules.is_symlink():
        raise ValueError('Explicit existing npm package directory required')
    buffer = io.BytesIO()
    total = 0
    count = 0
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for package in PACKAGES:
            root = node_modules / package
            if not root.is_dir() or root.is_symlink() or root.resolve() != node_modules.resolve() / package:
                raise ValueError('MCP package root must not be a symlink')
            for path in sorted(root.rglob('*')):
                if path.is_symlink():
                    raise ValueError('MCP package contains a symlink')
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ValueError('MCP package contains a non-regular file')
                count += 1
                total += path.stat().st_size
                if count > MAX_FILES or total > MAX_EXPANDED:
                    raise ValueError('MCP package size exceeded')
                name = str(path.relative_to(node_modules))
                member_name(name)
                member = zipfile.ZipInfo(name)
                member.external_attr = (stat.S_IFREG | 0o600) << 16
                member.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(member, path.read_bytes())
    payload = buffer.getvalue()
    inspect_bundle(payload)
    checksum = hashlib.sha256(payload).hexdigest()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    archive_path = output / 'packages.zip'
    archive_path.write_bytes(payload)
    archive_path.chmod(0o600)
    manifest = {'version': 1, 'bundle_sha256': checksum, 'mcp_version': MCP_VERSION,
                'playwright_version': PLAYWRIGHT_VERSION}
    manifest_path = output / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    manifest_path.chmod(0o600)
    return manifest_path


def read_bundle(manifest_path: Path):
    pins = json.loads(manifest_path.read_text())
    if (not isinstance(pins, dict) or set(pins) != {
            'version', 'bundle_sha256', 'mcp_version', 'playwright_version'}
            or type(pins['version']) is not int or pins['version'] != 1
            or pins['mcp_version'] != MCP_VERSION or pins['playwright_version'] != PLAYWRIGHT_VERSION):
        raise ValueError('Invalid pinned MCP manifest')
    with (manifest_path.parent / 'packages.zip').open('rb') as stream:
        payload = stream.read(MAX_ARCHIVE + 1)
    if hashlib.sha256(payload).hexdigest() != pins['bundle_sha256']:
        raise ValueError('MCP artifact hash differs; prepare explicitly')
    inspect_bundle(payload)
    return pins, payload


def unpack_bundle(payload, root):
    inspect_bundle(payload)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            target = root.joinpath(*member_name(member.filename).parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(archive.read(member))
            target.chmod(0o600)

import argparse
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import tarfile
from typing import Annotated, Literal

from pydantic import Field, model_validator

from aos.contracts import REPO_ROOT, TypedModel, canonical


MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_BINARY_BYTES + 1024 * 1024
SOURCE_FILES = ('ui/src-tauri/Cargo.toml', 'ui/src-tauri/build.rs',
                'ui/src-tauri/src/main.rs', 'ui/src-tauri/tauri.conf.json')
LOCK_FILES = ('ui/src-tauri/Cargo.lock', 'ui/pnpm-lock.yaml', 'uv.lock')
HashPin = Annotated[str, Field(pattern='^[a-f0-9]{64}$')]
SourceName = Literal['ui/src-tauri/Cargo.toml', 'ui/src-tauri/build.rs',
                     'ui/src-tauri/src/main.rs', 'ui/src-tauri/tauri.conf.json']
LockName = Literal['ui/src-tauri/Cargo.lock', 'ui/pnpm-lock.yaml', 'uv.lock']


class NativePackageManifest(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    format: Literal['aos_native_shell_tar_v1'] = 'aos_native_shell_tar_v1'
    synthetic: bool
    profile: Literal['debug', 'release']
    platform: Literal['linux-x86_64'] = 'linux-x86_64'
    artifact: Literal['aos-console'] = 'aos-console'
    artifact_bytes: int = Field(ge=64, le=MAX_BINARY_BYTES)
    artifact_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    source_sha256: dict[SourceName, HashPin] = Field(min_length=4, max_length=4)
    dependency_lock_sha256: dict[LockName, HashPin] = Field(min_length=3, max_length=3)
    source_scope: Literal['tauri_shell_configuration_only'] = 'tauri_shell_configuration_only'
    backend_origin: Literal['http://127.0.0.1:8765/ui/'] = 'http://127.0.0.1:8765/ui/'
    runtime_profile: Literal['external_backend_and_system_webkitgtk_x11'] = 'external_backend_and_system_webkitgtk_x11'
    build_provenance_verified: Literal[False] = False
    runtime_dependencies_bundled: Literal[False] = False
    standalone_inference: Literal[False] = False
    installation_authorized: Literal[False] = False
    promotion_authorized: Literal[False] = False

    @model_validator(mode='after')
    def validate_pins(self):
        for values, expected in ((self.source_sha256, SOURCE_FILES), (self.dependency_lock_sha256, LOCK_FILES)):
            if set(values) != set(expected) or any(re.fullmatch('[a-f0-9]{64}', value) is None for value in values.values()):
                raise ValueError('package_pin_set_invalid')
        return self


@contextmanager
def directory_descriptor(path: Path):
    path = path.absolute()
    if '..' in path.parts:
        raise ValueError('parent_traversal_refused')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def read_file(path: Path, limit: int, private: bool = False) -> bytes:
    with directory_descriptor(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > limit:
                raise ValueError('owned_bounded_regular_file_required')
            if private and (stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
                raise ValueError('private_single_link_required')
            payload = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if len(payload) > limit or len(payload) != before.st_size or identity(before) != identity(after) or identity(after) != identity(linked):
                raise ValueError('package_input_changed')
            return payload


def validate_binary(payload: bytes):
    if (len(payload) < 64 or payload[:7] != b'\x7fELF\x02\x01\x01'
            or int.from_bytes(payload[16:18], 'little') not in (2, 3)
            or int.from_bytes(payload[18:20], 'little') != 62
            or int.from_bytes(payload[52:54], 'little') != 64):
        raise ValueError('linux_x86_64_elf_header_required')


def archive_bytes(manifest: NativePackageManifest, binary: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, payload, mode in (('manifest.json', (canonical(manifest.model_dump()) + '\n').encode(), 0o600),
                                    ('aos-console', binary, 0o700)):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = mode
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def package_summary(manifest: NativePackageManifest, payload: bytes) -> dict:
    return {'schema_version': '1.0', 'format': manifest.format, 'synthetic': manifest.synthetic,
            'profile': manifest.profile, 'archive_sha256': hashlib.sha256(payload).hexdigest(),
            'archive_bytes': len(payload), 'artifact_sha256': manifest.artifact_sha256,
            'build_provenance_verified': False, 'standalone_inference': False,
            'installation_authorized': False, 'promotion_authorized': False}


def create_package(root: Path, output: Path, profile: str, synthetic: bool = False) -> dict:
    root, output = root.absolute(), output.absolute()
    if profile not in ('debug', 'release'):
        raise ValueError('explicit_build_profile_required')
    if output.parent != root / 'runs' or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', output.name):
        raise ValueError('new_direct_runs_output_required')
    binary_path = root / 'ui/src-tauri/target' / profile / 'aos-console'
    binary = read_file(binary_path, MAX_BINARY_BYTES)
    validate_binary(binary)
    pin = lambda name: hashlib.sha256(read_file(root / name, 4 * 1024 * 1024)).hexdigest()
    manifest = NativePackageManifest(synthetic=synthetic, profile=profile, artifact_bytes=len(binary),
                                     artifact_sha256=hashlib.sha256(binary).hexdigest(),
                                     source_sha256={name: pin(name) for name in SOURCE_FILES},
                                     dependency_lock_sha256={name: pin(name) for name in LOCK_FILES})
    payload = archive_bytes(manifest, binary)
    with directory_descriptor(output.parent) as parent:
        owner = os.fstat(parent)
        if owner.st_uid != os.getuid() or stat.S_IMODE(owner.st_mode) & 0o022:
            raise ValueError('owned_nonwritable_runs_parent_required')
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
        descriptor = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            target = os.open('bundle.tar', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            with os.fdopen(target, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    result = verify_package(output, hashlib.sha256(payload).hexdigest())
    return {**result, 'status': 'native_shell_package_created'}


def verify_package(directory: Path, expected_sha256: str) -> dict:
    if re.fullmatch('[a-f0-9]{64}', expected_sha256) is None:
        raise ValueError('external_archive_digest_required')
    with directory_descriptor(directory) as descriptor:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('private_package_directory_required')
        if set(os.listdir(descriptor)) != {'bundle.tar'}:
            raise ValueError('package_file_set_invalid')
    payload = read_file(directory / 'bundle.tar', MAX_ARCHIVE_BYTES, private=True)
    with directory_descriptor(directory) as descriptor:
        current = os.fstat(descriptor)
        if ((current.st_dev, current.st_ino, current.st_mode, current.st_uid) !=
                (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid)
                or set(os.listdir(descriptor)) != {'bundle.tar'}):
            raise ValueError('package_directory_changed')
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError('external_archive_digest_mismatch')
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as archive:
        members = []
        for member in archive:
            members.append(member)
            if len(members) > 2 or not member.isfile() or member.pax_headers:
                raise ValueError('archive_members_invalid')
        if [member.name for member in members] != ['manifest.json', 'aos-console']:
            raise ValueError('archive_members_invalid')
        if members[0].size > 65536 or members[1].size > MAX_BINARY_BYTES:
            raise ValueError('archive_member_oversize')
        with archive.extractfile(members[0]) as stream:
            manifest = NativePackageManifest.model_validate_json(stream.read())
        with archive.extractfile(members[1]) as stream:
            binary = stream.read()
    validate_binary(binary)
    if len(binary) != manifest.artifact_bytes or hashlib.sha256(binary).hexdigest() != manifest.artifact_sha256:
        raise ValueError('artifact_content_mismatch')
    if payload != archive_bytes(manifest, binary):
        raise ValueError('archive_not_canonical')
    return {**package_summary(manifest, payload), 'status': 'native_shell_package_integrity_verified'}


def main():
    parser = argparse.ArgumentParser(description='Private native shell arşivi; kurulum, inference veya promotion yapmaz')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--profile', choices=('debug', 'release'), required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--package', type=Path, required=True)
    verify.add_argument('--sha256', required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == 'create':
            result = create_package(REPO_ROOT, arguments.output, arguments.profile)
        else:
            result = verify_package(arguments.package, arguments.sha256)
        print(canonical(result))
    except (ValueError, OSError, tarfile.TarError):
        parser.exit(1, 'Native paket işlemi başarısız; private yol, build profili ve bütünlüğü kontrol edin.\n')


if __name__ == '__main__':
    main()

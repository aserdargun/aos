"""Offline, exact-review CLI for private synthetic parameter projects."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

from .contracts import REPO_ROOT, canonical, digest
from .workspace_identity import open_existing_workspace


MAX_REQUEST_BYTES = 8192


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('owned_parameter_project_duplicate_key')
        result[name] = value
    return result


def _read_request(path):
    path = Path(path).absolute()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise ValueError('owned_parameter_project_request_inside_checkout')
    parent_fd = open_existing_workspace(path.parent)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or not 1 <= before.st_size <= MAX_REQUEST_BYTES):
            raise ValueError('owned_parameter_project_request_file_invalid')
        content = b''
        while len(content) <= MAX_REQUEST_BYTES:
            chunk = os.read(descriptor, MAX_REQUEST_BYTES + 1 - len(content))
            if not chunk:
                break
            content += chunk
        after = os.fstat(descriptor)
        linked = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size',
                  'st_mtime_ns', 'st_ctime_ns')
        if (len(content) > MAX_REQUEST_BYTES or len(content) != before.st_size
                or any(getattr(before, field) != getattr(after, field)
                       or getattr(after, field) != getattr(linked, field) for field in fields)):
            raise ValueError('owned_parameter_project_request_changed')
        request = json.loads(content.decode('utf-8'), object_pairs_hook=_unique_object)
        if (type(request) is not dict
                or set(request) != {'application_key', 'port', 'parameters'}
                or content not in (canonical(request).encode(), canonical(request).encode() + b'\n')):
            raise ValueError('owned_parameter_project_request_invalid')
        return request
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _directory(path):
    directory = Path(path)
    if not directory.is_absolute() or '..' in directory.parts:
        raise ValueError('owned_parameter_project_directory_invalid')
    if directory == REPO_ROOT or REPO_ROOT in directory.parents:
        raise ValueError('owned_parameter_project_directory_inside_checkout')
    parent_fd = open_existing_workspace(directory.parent)
    try:
        metadata = os.fstat(parent_fd)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('owned_parameter_project_parent_invalid')
        resolved_parent = directory.parent.resolve(strict=True)
        if resolved_parent == REPO_ROOT or REPO_ROOT in resolved_parent.parents:
            raise ValueError('owned_parameter_project_directory_inside_checkout')
    finally:
        os.close(parent_fd)
    return directory


def _review(request, directory):
    from .owned_parameter_project import (owned_parameter_project_review,
                                         owned_parameter_project_review_sha256)

    if (type(request['port']) is not int or not 1 <= request['port'] <= 65535
            or request['port'] == 443):
        raise ValueError('owned_parameter_project_port_invalid')
    if directory.exists() or directory.is_symlink():
        raise ValueError('owned_parameter_project_directory_exists')
    review = owned_parameter_project_review(request['application_key'], request['parameters'])
    parameters_sha256 = owned_parameter_project_review_sha256(
        request['application_key'], request['parameters'])
    review_sha256 = digest({'schema_version': 'owned-parameter-project-request-v1',
                            'directory': str(directory), 'request': request})
    return {'application_key': request['application_key'],
            'scope': review['scope'], 'synthetic': True,
            'parameter_keys': sorted(request['parameters']),
            'parameters_sha256': parameters_sha256,
            'request_sha256': review_sha256,
            'directory_sha256': digest({'directory': str(directory)}),
            'port': request['port'], 'runtime_started': False,
            'execution_admitted': False, 'training_ready': False}


def _sha256(value):
    if re.fullmatch('[a-f0-9]{64}', value) is None:
        raise argparse.ArgumentTypeError('expected lowercase SHA-256')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('plan', 'provision'):
        subparser = commands.add_parser(command, allow_abbrev=False)
        subparser.add_argument('--request', required=True, type=Path)
        subparser.add_argument('--directory', required=True, type=Path)
        if command == 'provision':
            subparser.add_argument('--confirm-request-sha256', required=True, type=_sha256)
            subparser.add_argument('--human-confirmation', required=True, choices=['PROVISION'])
    verifier = commands.add_parser('verify', allow_abbrev=False)
    verifier.add_argument('--directory', required=True, type=Path)
    verifier.add_argument('--manifest-sha256', required=True, type=_sha256)
    arguments = parser.parse_args(argv)
    try:
        directory = _directory(arguments.directory)
        if arguments.command == 'verify':
            from .owned_parameter_project import verify_owned_parameter_project_manifest

            manifest = verify_owned_parameter_project_manifest(directory, arguments.manifest_sha256)
            report = {'status': 'verified', 'manifest_sha256': arguments.manifest_sha256,
                      'application_key': manifest['application_key'], 'synthetic': True,
                      'directory_sha256': digest({'directory': str(directory)}),
                      'runtime_started': False, 'execution_admitted': False,
                      'training_ready': False}
        else:
            request = _read_request(arguments.request)
            report = _review(request, directory)
            report['status'] = 'planned'
            if arguments.command == 'provision':
                if arguments.confirm_request_sha256 != report['request_sha256']:
                    raise ValueError('owned_parameter_project_request_confirmation_mismatch')
                from .owned_parameter_project import provision_owned_parameter_project

                bundle = provision_owned_parameter_project(
                    directory, request['port'], application_key=request['application_key'],
                    parameters=request['parameters'],
                    confirm_parameters_sha256=report['parameters_sha256'],
                    human_confirmation=True)
                report['status'] = 'provisioned'
                report['manifest_sha256'] = bundle['manifest_sha256']
        print(canonical(report))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print(canonical({'status': 'rejected',
                         'error': 'owned_parameter_project_command_rejected'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE = 'aos-usage-record.service'
TIMER = 'aos-usage-record.timer'


def unit_argument(value, *, environment_expansion=False):
    value = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('Control characters in paths are unsupported')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if environment_expansion:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def build_plan(repo=REPO_ROOT, home=None):
    if sys.platform != 'linux':
        raise ValueError('Linux user systemd is required')
    repo = Path(repo).absolute()
    home = Path.home() if home is None else Path(home).absolute()
    interpreter = repo / '.venv/bin/python'
    collector = repo / 'scripts/record_usage.py'
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK) or not collector.is_file():
        raise ValueError('Repository collector and executable virtualenv interpreter required')
    arguments = [interpreter, collector, '--session-root', home / '.codex/sessions']
    command = ' '.join(unit_argument(argument, environment_expansion=position > 0)
                       for position, argument in enumerate(arguments))
    service = ('[Unit]\nDescription=AOS private usage accounting\n\n[Service]\nType=oneshot\n'
               'UMask=0077\nExecStart=' + command + '\n'
               'StandardOutput=null\nStandardError=journal\nNoNewPrivileges=true\n')
    timer = ('[Unit]\nDescription=AOS hourly private usage accounting\n\n[Timer]\n'
             'OnCalendar=*-*-* *:00:00 UTC\nAccuracySec=1min\nPersistent=false\n'
             'Unit=' + SERVICE + '\n\n[Install]\nWantedBy=timers.target\n')
    return {'directory': home / '.config/systemd/user', 'units': {SERVICE: service, TIMER: timer}}


def check_existing(plan, *, require_all=False):
    directory = plan['directory']
    for parent in (directory, *directory.parents):
        if parent.is_symlink():
            raise ValueError('Symlinked unit directories are unsupported')
    if directory.exists():
        metadata = directory.stat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise ValueError('User unit directory must be owned and not writable by other users')
    for name, content in plan['units'].items():
        path = directory / name
        if path.is_symlink():
            raise ValueError('Existing symlinked unit refused')
        if not path.exists():
            if require_all:
                raise ValueError('Install matching units before enabling')
            continue
        metadata = path.stat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or path.read_text() != content):
            raise ValueError('Unknown or conflicting existing unit refused')


def check_effective_conflicts(plan):
    result = subprocess.run(['systemd-analyze', '--user', 'unit-paths'], check=True,
                            capture_output=True, text=True)
    if not isinstance(result.stdout, str) or not result.stdout.strip():
        raise ValueError('Effective user unit search paths unavailable')
    directories = [Path(value) for value in result.stdout.splitlines()]
    if any(not directory.is_absolute() for directory in directories):
        raise ValueError('Unsupported user unit search path')
    if plan['directory'] not in directories:
        raise ValueError('Planned unit directory is not in effective user search paths')
    for name in plan['units']:
        stem, kind = name.rsplit('.', 1)
        dropins = [name + '.d', kind + '.d']
        dropins.extend(stem[:position + 1] + '.' + kind + '.d'
                       for position, character in enumerate(stem) if character == '-')
        for directory in directories:
            candidate = directory / name
            if (candidate.exists() or candidate.is_symlink()) and candidate != plan['directory'] / name:
                raise ValueError('Other effective same-name unit refused')
            for dropin in dropins:
                path = directory / dropin
                if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
                    raise ValueError('Effective unit drop-ins require separate review')


def install(plan):
    check_existing(plan)
    check_effective_conflicts(plan)
    directory = plan['directory']
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir(mode=0o700)
    check_existing(plan)
    for name, content in plan['units'].items():
        path = directory / name
        if path.exists():
            continue
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)


def enable(plan):
    check_existing(plan, require_all=True)
    check_effective_conflicts(plan)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', TIMER], check=True)


def main():
    parser = argparse.ArgumentParser(description='Plan private usage recording; installation and enablement are explicit')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--enable', action='store_true')
    arguments = parser.parse_args()
    try:
        plan = build_plan()
        check_existing(plan)
        if arguments.install:
            install(plan)
        if arguments.enable:
            enable(plan)
        print(json.dumps({'installed': arguments.install, 'enabled': arguments.enable,
                          'directory': str(plan['directory']), 'units': plan['units']}))
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(1, 'Usage timer operation refused or incomplete; existing conflicts are not overwritten.\n')


if __name__ == '__main__':
    main()

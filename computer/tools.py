import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time


HELLO = 'Hello from the local agent.\n'


def command(arguments, timeout=10):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout, check=True)
    if len(result.stdout) > 65536:
        raise ValueError('Output exceeds bound')
    return result.stdout.decode()


def read():
    descriptor = os.open('/workspace/hello.txt', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 4096:
            raise ValueError('Unsafe file')
        return os.read(descriptor, 4097).decode()
    finally:
        os.close(descriptor)


def main():
    request = json.loads(sys.stdin.buffer.readline(4097))
    if set(request) != {'tool', 'arguments'}:
        raise ValueError('Invalid request')
    tool, arguments = request['tool'], request['arguments']
    if tool in {'read', 'write', 'checksum'}:
        expected = {'path', 'content'} if tool == 'write' else {'path'}
        if set(arguments) != expected or arguments['path'] != '/workspace/hello.txt':
            raise ValueError('Scope exceeds authorization')
        if tool == 'read':
            return {'content': read()}
        if tool == 'checksum':
            checksum = hashlib.sha256(read().encode()).hexdigest()
            return {'stdout': checksum + '  /workspace/hello.txt\n', 'exit_code': 0}
        if arguments['content'] != HELLO:
            raise ValueError('Content exceeds authorization')
        descriptor = os.open('/workspace/hello.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(HELLO.encode())
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'bytes_written': len(HELLO.encode())}
    if arguments:
        raise ValueError('No arguments permitted')
    if tool == 'probe':
        with socket.create_connection(('127.0.0.1', 5901), timeout=2) as connection:
            banner = connection.recv(12).decode()
        return {'display': '1280x800' in command(['xdpyinfo']),
                'xfce': bool(command(['pgrep', '-x', 'xfwm4']).strip()), 'vnc': banner,
                'uid': os.getuid(), 'network_interfaces': sorted(os.listdir('/sys/class/net')),
                'docker_socket': Path('/var/run/docker.sock').exists(),
                'host_home': Path('/home/cachyos').exists(), 'note': Path('/tmp/aos-note.json').exists()}
    if tool == 'read_note':
        descriptor = os.open('/tmp/aos-note.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096 or metadata.st_nlink != 1:
                raise ValueError('Unsafe note')
            return json.loads(stream.read(4097))
    if tool == 'type_note':
        window = command(['xdotool', 'search', '--onlyvisible', '--name', '^AOS Synthetic Input$']).splitlines()[0]
        command(['xdotool', 'windowactivate', '--sync', window])
        command(['xdotool', 'key', '--clearmodifiers', '--window', window, 'Home', 'shift+End', 'BackSpace'])
        command(['xdotool', 'type', '--clearmodifiers', '--window', window, '--', 'AOS desktop input'])
        return {'applied': True}
    if tool == 'office_pdf':
        directory = Path('/tmp/aos-pdf')
        directory.mkdir(exist_ok=True)
        if (directory / 'synthetic.pdf').exists():
            raise ValueError('No overwrite')
        command(['libreoffice', '-env:UserInstallation=file:///tmp/aos-office', '--headless', '--convert-to', 'pdf',
                 '--outdir', str(directory), '/opt/aos/synthetic.txt'], timeout=45)
        actual = command(['pdftotext', str(directory / 'synthetic.pdf'), '-']).strip()
        return {'actual': actual, 'sha256': hashlib.sha256((directory / 'synthetic.pdf').read_bytes()).hexdigest()}
    if tool == 'versions':
        return {name: command(argv).strip() for name, argv in {
            'chromium': ['chromium', '--version'], 'codium': ['codium', '--no-sandbox', '--version'],
            'libreoffice': ['libreoffice', '--version'], 'python': ['python3', '--version'],
            'node': ['node', '--version'], 'pnpm': ['pnpm', '--version'], 'git': ['git', '--version']}.items()}
    raise ValueError('Unknown tool')


if __name__ == '__main__':
    try:
        print(json.dumps(main()))
    except FileNotFoundError:
        print(json.dumps({'error': 'ELEMENT_MISSING'}))
    except ValueError:
        print(json.dumps({'error': 'UNSAFE_ACTION'}))
    except Exception:
        print(json.dumps({'error': 'TOOL_FAILURE'}))

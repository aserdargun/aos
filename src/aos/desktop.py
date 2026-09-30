import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .computer import WorkspaceRuntime
from .contracts import AOSFault, ErrorCode, REPO_ROOT, canonical, digest, identifier
from .workspace_identity import open_existing_workspace, workspace_identity
from .lifecycle import LifecycleJournal


DOCKER = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock']


class DesktopRuntime(WorkspaceRuntime):
    def __init__(self, root: Path, manifest: Path):
        super().__init__(root)
        self.manifest = manifest
        self.runtime_id = identifier('desktop')
        self.container_id = None
        self.pins = {}
        self.lifecycle = None

    def docker(self, arguments: list[str], payload: bytes | None = None, timeout: float = 30) -> bytes:
        try:
            result = subprocess.run([*DOCKER, *arguments], input=payload, capture_output=True, timeout=timeout,
                                    env={'PATH': '/usr/bin:/bin'}, check=False)
        except subprocess.TimeoutExpired:
            raise AOSFault(ErrorCode.TIMEOUT, 'Desktop Docker operation timed out; no host fallback') from None
        if result.returncode or len(result.stdout) > 1048576:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Owned desktop Docker operation failed')
        return result.stdout

    def start(self) -> None:
        if self.container_id is not None or self.descriptor is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Desktop already started')
        self.pins = json.loads(self.manifest.read_text())
        if not re.fullmatch('sha256:[a-f0-9]{64}', self.pins.get('image_id', '')):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Immutable desktop image ID required')
        inspected = json.loads(self.docker(['image', 'inspect', self.pins['image_id']]))[0]
        sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (REPO_ROOT / 'computer').iterdir() if path.is_file()}
        if (digest(sources) != self.pins['source_sha256'] or sources != self.pins['source_files']
                or inspected['Config']['Labels'].get('com.aos.source-sha256') != self.pins['source_sha256']):
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Desktop image/source pin differs; rebuild explicitly')
        if os.getuid() == 0 or ',' in str(self.root) or self.root.is_symlink():
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Non-root explicit workspace required')
        allowed = (REPO_ROOT / 'data').resolve()
        if (not self.root.is_relative_to(REPO_ROOT)
                or not self.root.resolve().is_relative_to(allowed) or self.root.resolve() == allowed):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Desktop mount must be a dedicated directory under repository data/')
        super().start()
        name = 'aos-desktop-' + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:20]
        try:
            for directory in (self.root, *self.root.parents):
                descriptor = open_existing_workspace(directory)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if directory == REPO_ROOT:
                    break
            self.lifecycle = LifecycleJournal(self.root, self.descriptor, self.runtime_id, self.pins['image_id'], self.pins['source_sha256'])
            uid, gid = os.getuid(), os.getgid()
            arguments = ['create', '--name', name, '--label', 'com.aos.runtime=' + self.runtime_id,
                         '--label', 'com.aos.lifecycle=' + digest(self.lifecycle.birth.model_dump()),
                         '--network', 'none', '--init', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                         '--user', f'{uid}:{gid}', '--pids-limit', '512', '--memory', '3g', '--cpus', '2',
                         '--shm-size', '256m', '--stop-timeout', '10',
                         '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m,mode=1777',
                         '--tmpfs', f'/run:rw,nosuid,nodev,size=32m,mode=755,uid={uid},gid={gid}',
                         '--tmpfs', f'/home/agent:rw,nosuid,nodev,size=512m,mode=700,uid={uid},gid={gid}',
                         '--mount', f'type=bind,source={self.root.resolve()},target=/workspace', self.pins['image_id']]
            self.container_id = self.docker(arguments).decode().strip()
            if not re.fullmatch('[a-f0-9]{64}', self.container_id):
                raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Invalid owned container ID')
            self.lifecycle.record('created', self.container_id)
            self.docker(['start', self.container_id])
            self.lifecycle.record('started', self.container_id)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                try:
                    evidence = self.perform('probe', {})
                    if evidence['display'] and evidence['xfce'] and evidence['note'] and evidence['vnc'].startswith('RFB '):
                        return
                except AOSFault:
                    pass
                time.sleep(0.5)
            raise AOSFault(ErrorCode.TIMEOUT, 'Isolated desktop did not become ready')
        except BaseException:
            try:
                self.stop()
            finally:
                if self.lifecycle is not None:
                    self.lifecycle.close()
                    self.lifecycle = None
            raise

    def perform(self, tool: str, arguments: dict) -> dict:
        if self.container_id is None:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Desktop is stopped')
        if tool not in {'read', 'write', 'checksum', 'probe', 'type_note', 'read_note', 'office_pdf', 'versions'}:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Unsupported desktop tool')
        payload = (canonical({'tool': tool, 'arguments': arguments}) + '\n').encode()
        if len(payload) > 4096:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Desktop tool input exceeds bound')
        result = json.loads(self.docker(['exec', '-i', self.container_id, '/usr/bin/python3', '/opt/aos/tools.py'], payload, timeout=55))
        if 'error' in result:
            code = ErrorCode(result['error'])
            if code == ErrorCode.ELEMENT_MISSING:
                raise FileNotFoundError('Authorized desktop file is absent')
            raise AOSFault(code, 'Isolated desktop tool failed')
        return result

    def read(self, path: str) -> str:
        self._name(path)
        return self.perform('read', {'path': path})['content']

    def write(self, path: str, content: str) -> dict:
        self._name(path)
        return self.perform('write', {'path': path, 'content': content})

    def checksum(self, path: str) -> dict:
        self._name(path)
        return self.perform('checksum', {'path': path})

    def status(self) -> dict:
        running = False
        if self.container_id:
            inspected = json.loads(self.docker(['inspect', self.container_id]))[0]
            running = bool(inspected['State']['Running'])
        return {'kind': 'docker_xfce', 'runtime_id': self.runtime_id, 'container_id': self.container_id,
                'image_id': self.pins.get('image_id'), 'running': running,
                'workspace_identity': workspace_identity(self.root, self.descriptor).model_dump() if self.descriptor is not None else None,
                'lifecycle_ref': digest(self.lifecycle.birth.model_dump()) if self.lifecycle is not None else None,
                'real_execution': True, 'desktop': True, 'network': False, 'mount': '/workspace'}

    def pointer_position(self) -> dict:
        container_id = self.container_id
        if container_id is None or not re.fullmatch('[a-f0-9]{64}', container_id):
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Desktop pointer is unavailable')
        inspected = json.loads(self.docker(['inspect', container_id], timeout=3))[0]
        labels = inspected['Config']['Labels']
        if (inspected['Id'] != container_id or inspected['Image'] != self.pins.get('image_id')
                or labels.get('com.aos.runtime') != self.runtime_id
                or labels.get('com.aos.source-sha256') != self.pins.get('source_sha256')):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Desktop runtime identity changed')
        if not inspected['State']['Running']:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Desktop pointer is unavailable')
        payload = self.docker(['exec', container_id, '/usr/bin/xdotool', 'getmouselocation', '--shell',
                               'getdisplaygeometry', '--shell'], timeout=3)
        if len(payload) > 128:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Desktop pointer output is invalid')
        try:
            lines = payload.decode('ascii').splitlines()
            values = {}
            for line in lines:
                match = re.fullmatch(r'(X|Y|SCREEN|WINDOW|WIDTH|HEIGHT)=([0-9]{1,20})', line)
                if match is None or match[1] in values:
                    raise ValueError
                values[match[1]] = int(match[2])
            if set(values) != {'X', 'Y', 'SCREEN', 'WINDOW', 'WIDTH', 'HEIGHT'}:
                raise ValueError
            width, height = values['WIDTH'], values['HEIGHT']
            if not (0 < width <= 16384 and 0 < height <= 16384
                    and 0 <= values['X'] < width and 0 <= values['Y'] < height):
                raise ValueError
        except (UnicodeError, ValueError):
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Desktop pointer output is invalid') from None
        return {'x': values['X'], 'y': values['Y'], 'width': width, 'height': height}

    def stop(self) -> None:
        removed = None
        if self.container_id is not None:
            container_id = self.container_id
            inspected = json.loads(self.docker(['inspect', container_id]))[0]
            if inspected['Config']['Labels'].get('com.aos.runtime') != self.runtime_id:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Refusing to stop a container owned by another runtime')
            self.docker(['stop', '--time', '10', container_id], timeout=20)
            self.docker(['rm', container_id])
            self.container_id = None
            removed = container_id
        try:
            if removed and self.lifecycle is not None and not self.lifecycle.failed:
                self.lifecycle.record('removed', removed)
        finally:
            if self.lifecycle is not None:
                self.lifecycle.close()
                self.lifecycle = None
            super().stop()

    def restart(self) -> None:
        self.stop()
        self.runtime_id = identifier('desktop')
        self.start()

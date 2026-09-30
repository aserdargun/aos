import hashlib
import json
import os
import subprocess

from .browser import BrowserRuntime
from .contracts import AOSFault, ErrorCode, REPO_ROOT, digest
from .desktop import DOCKER, DesktopRuntime


class DesktopBrowserRuntime(BrowserRuntime):
    worker_file = 'desktop_browser_worker.py'
    allowed_tools = frozenset({'browser.observe', 'browser.verify', 'browser.fill', 'browser.submit'})

    def __init__(self, desktop: DesktopRuntime):
        super().__init__(desktop.manifest)
        self.desktop = desktop
        self.parent_id = desktop.runtime_id
        self.container_id = desktop.container_id

    def owned_desktop(self):
        if (self.desktop.container_id is None or self.desktop.container_id != self.container_id
                or self.desktop.runtime_id != self.parent_id or self.desktop.descriptor is None):
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Visible browser requires its original running desktop')
        inspected = json.loads(self.desktop.docker(['inspect', self.container_id]))[0]
        if (not inspected['State']['Running'] or inspected['Image'] != self.desktop.pins['image_id']
                or inspected['Config']['Labels'].get('com.aos.runtime') != self.parent_id
                or inspected['HostConfig']['NetworkMode'] != 'none'):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Visible browser desktop ownership or isolation differs')

    def start(self):
        if self.process is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Visible browser is already started')
        self.owned_desktop()
        self.pins = dict(self.desktop.pins)
        worker = self.worker_source()
        fixture = (REPO_ROOT / 'examples' / self.fixture_file).read_text()
        try:
            self.process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', self.container_id, '/usr/bin/python3', '-u', '-c', worker, fixture],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
            self.evidence = self.receive(25)
            if (self.evidence.get('ready') is not True or self.evidence.get('headed') is not True
                    or self.evidence.get('display') != ':99'
                    or self.evidence.get('chromium_sha256') != self.pins['chromium_sha256']
                    or self.evidence.get('network_namespace') == os.readlink('/proc/self/ns/net')
                    or self.evidence.get('home_visible') is not False
                    or self.evidence.get('docker_socket_visible') is not False):
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Visible browser isolation handshake differs')
        except BaseException:
            self.stop()
            raise

    def perform(self, tool, arguments):
        if tool not in self.allowed_tools:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only the fixed visible task is authorized')
        self.owned_desktop()
        return super().perform(tool, arguments)

    def worker_source(self):
        return (REPO_ROOT / 'src/aos' / self.worker_file).read_text()

    def status(self):
        return {'kind': 'docker_chromium', 'runtime_id': self.runtime_id,
                'parent_runtime_id': self.parent_id, 'container_id': self.container_id,
                'browser_display': 'desktop', 'image_id': self.pins.get('image_id'),
                'running': (self.process is not None and self.process.poll() is None
                            and self.desktop.runtime_id == self.parent_id
                            and self.desktop.container_id == self.container_id
                            and self.desktop.descriptor is not None),
                'real_execution': True, 'desktop': True, 'network': False, 'vision': self.vision,
                'scope': self.scope, 'runtime_digest': digest(self.pins),
                'fixture_sha256': hashlib.sha256((REPO_ROOT / 'examples' / self.fixture_file).read_bytes()).hexdigest(),
                'worker_sha256': hashlib.sha256(self.worker_source().encode()).hexdigest(),
                'isolation': self.evidence}

    def stop(self):
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                pass
        super().stop()

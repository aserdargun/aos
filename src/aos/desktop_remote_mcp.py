import base64
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

from pydantic import Field

from .contracts import AOSFault, ErrorCode, TypedModel, digest
from .desktop import DOCKER
from .desktop_mcp import DesktopMCPBrowserRuntime
from .desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle
from .web_application_binding import WebRuntimePin
from .web_https_relay import ExactEntryRelay


class RemoteEntryObservation(TypedModel):
    url: str = Field(min_length=1, max_length=2048)
    title_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    heading_sha256: str = Field(pattern='^[a-f0-9]{64}$')


class DesktopRemoteEntryMCPRuntime(DesktopMCPBrowserRuntime):
    worker_file = 'desktop_remote_mcp_worker.py'
    shared_worker_files = (('aos_mcp_worker_shared', 'desktop_mcp_worker.py'),)
    allowed_tools = frozenset({'browser.remote.open', 'browser.remote.observe'})

    def __init__(self, desktop, mcp_manifest: Path):
        super().__init__(desktop, mcp_manifest)
        self.relay = None
        self.socket_path = desktop.root / 'relay.sock'
        self.stop_event = threading.Event()
        self.relay_thread = None
        self.relay_report = None
        self.relay_error = None
        self.remote_opened = False

    def runtime_pin(self) -> WebRuntimePin:
        self.owned_desktop()
        bundle_pins, _payload = read_bundle(self.mcp_manifest)
        return WebRuntimePin(runtime_id=self.runtime_id, parent_runtime_id=self.parent_id,
                             container_id=self.container_id, image_id=self.desktop.pins['image_id'],
                             mcp_bundle_sha256=bundle_pins['bundle_sha256'],
                             worker_sha256=hashlib.sha256(self.worker_source().encode()).hexdigest(),
                             runtime_digest=digest({**self.desktop.pins, 'mcp': bundle_pins}))

    def attach_relay(self, relay: ExactEntryRelay):
        if (not isinstance(relay, ExactEntryRelay) or self.relay is not None
                or self.process is not None or self.relay_thread is not None
                or relay.draft.runtime != self.runtime_pin()):
            raise ValueError('Exact live remote MCP runtime pin required')
        self.relay = relay

    def _serve_relay(self):
        try:
            self.relay_report = self.relay.serve(self.socket_path, stop_event=self.stop_event)
        except Exception as error:
            self.relay_error = error

    def start(self):
        if self.process is not None or self.relay_thread is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote MCP entry runtime is already started')
        self.owned_desktop()
        if self.relay is None or self.relay.draft.runtime != self.runtime_pin():
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Exact remote MCP runtime pin is not attached')
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote relay socket already exists')
        try:
            mcp_pins, payload = read_bundle(self.mcp_manifest)
        except (ValueError, OSError, KeyError) as error:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Pinned MCP preparation is invalid') from error
        self.pins = {**self.desktop.pins, 'mcp': mcp_pins}
        self.relay_thread = threading.Thread(target=self._serve_relay, daemon=True)
        self.relay_thread.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.relay_error is not None:
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Remote relay could not bind')
                try:
                    metadata = self.socket_path.lstat()
                    if (stat.S_ISSOCK(metadata.st_mode)
                            and stat.S_IMODE(metadata.st_mode) == 0o600
                            and metadata.st_uid == os.getuid()):
                        break
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Remote relay socket identity differs')
                except FileNotFoundError:
                    time.sleep(0.01)
            else:
                raise AOSFault(ErrorCode.TIMEOUT, 'Remote relay socket startup timed out')
            self.process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', self.container_id, '/usr/bin/python3', '-u', '-c',
                 self.worker_source(), self.relay.entry_url, self.relay.draft.binding_sha256,
                 mcp_pins['bundle_sha256']],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
            self.process.stdin.write(base64.b64encode(payload) + b'\n')
            self.process.stdin.write(self.relay.client_token.encode() + b'\n')
            self.process.stdin.flush()
            self.evidence = self.receive(25)
            if (self.evidence.get('ready') is not True or self.evidence.get('headed') is not True
                    or self.evidence.get('display') != ':99'
                    or self.evidence.get('chromium_sha256') != self.pins['chromium_sha256']
                    or self.evidence.get('network_namespace') == os.readlink('/proc/self/ns/net')
                    or self.evidence.get('home_visible') is not False
                    or self.evidence.get('docker_socket_visible') is not False
                    or self.evidence.get('transport') != 'playwright_mcp'
                    or self.evidence.get('bundle_sha256') != mcp_pins['bundle_sha256']
                    or self.evidence.get('server_version') != PLAYWRIGHT_VERSION
                    or self.evidence.get('remote_entry_gate') is not True
                    or self.evidence.get('binding_sha256') != self.relay.draft.binding_sha256):
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Remote MCP isolation or identity differs')
        except BaseException:
            self.stop()
            raise

    def perform(self, tool, arguments):
        if (self.relay is None or tool not in self.allowed_tools
                or tool == 'browser.remote.open'
                and arguments != {'profile_sha256': self.relay.profile_sha256,
                                 'binding_sha256': self.relay.draft.binding_sha256,
                                 'entry_url': self.relay.entry_url}
                or tool == 'browser.remote.observe' and arguments != {}
                or tool == 'browser.remote.open' and self.remote_opened
                or tool == 'browser.remote.observe' and not self.remote_opened):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only the one-shot remote entry is available')
        if tool == 'browser.remote.open':
            self.remote_opened = True
        result = super().perform(tool, {})
        if tool == 'browser.remote.open':
            self.relay_thread.join(timeout=2)
            if (self.relay_thread.is_alive() or self.relay_error is not None
                    or self.relay_report is None
                    or self.relay_report.binding_sha256 != self.relay.draft.binding_sha256):
                self.stop()
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Remote entry relay did not attest the fetch')
        return result

    def validate_result(self, tool, result):
        if tool not in self.allowed_tools:
            raise ValueError('Invalid remote entry observation')
        observed = RemoteEntryObservation.model_validate(result)
        if observed.url != self.relay.entry_url:
            raise ValueError('Invalid remote entry URL')
        return observed.model_dump()

    def read(self, path):
        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'No general remote site read scope is authorized')

    def status(self):
        return {'kind': 'docker_chromium_remote_entry_mcp', 'runtime_id': self.runtime_id,
                'parent_runtime_id': self.parent_id, 'container_id': self.container_id,
                'running': (self.process is not None and self.process.poll() is None
                            and self.desktop.runtime_id == self.parent_id
                            and self.desktop.container_id == self.container_id
                            and self.desktop.descriptor is not None),
                'real_execution': True, 'desktop': True, 'network': False,
                'browser_transport': 'playwright_mcp',
                'entry_url': self.relay.entry_url if self.relay else None,
                'binding_sha256': self.relay.draft.binding_sha256 if self.relay else None,
                'runtime_digest': digest(self.pins),
                'worker_sha256': hashlib.sha256(self.worker_source().encode()).hexdigest(),
                'isolation': self.evidence,
                'relay_report': self.relay_report.model_dump() if self.relay_report else None}

    def stop(self):
        self.stop_event.set()
        super().stop()
        if self.relay_thread is not None:
            self.relay_thread.join(timeout=2)

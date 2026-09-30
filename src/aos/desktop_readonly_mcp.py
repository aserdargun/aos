import base64
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

from pydantic import Field

from .contracts import AOSFault, ErrorCode, canonical, digest
from .desktop import DOCKER
from .desktop_mcp import DesktopMCPBrowserRuntime
from .desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle
from .desktop_remote_mcp import DesktopRemoteEntryMCPRuntime, RemoteEntryObservation
from .web_application_binding import WebTaskAdmissionDraft, verify_web_readonly_routes, verify_web_task_binding
from .web_https_relay import ExactReadOnlyRouteRelay


class RemoteRouteObservation(RemoteEntryObservation):
    planned_link_indices: list[int] = Field(max_length=8)
    unregistered_link_count: int = Field(ge=0, le=64)
    links_truncated: bool


class DesktopReadOnlyRoutesMCPRuntime(DesktopRemoteEntryMCPRuntime):
    worker_file = 'desktop_readonly_mcp_worker.py'
    allowed_tools = frozenset({'browser.remote.route', 'browser.remote.observe'})

    def __init__(self, desktop, mcp_manifest: Path):
        super().__init__(desktop, mcp_manifest)
        self.draft = None
        self.last_route_index = -1
        self.last_readback_index = -1
        self.expected_url = None

    def attach_relay(self, relay: ExactReadOnlyRouteRelay, draft: WebTaskAdmissionDraft):
        if (not isinstance(relay, ExactReadOnlyRouteRelay)
                or not isinstance(draft, WebTaskAdmissionDraft)
                or self.relay is not None or self.process is not None
                or self.relay_thread is not None):
            raise ValueError('Exact read-only relay and live runtime pin required')
        checked = verify_web_task_binding(relay.profiles, draft)
        verify_web_readonly_routes(relay.profiles, relay.task, relay.plan)
        if (checked.task != relay.task or checked.runtime != self.runtime_pin()
                or checked.profile_sha256 != relay.plan.profile_sha256):
            raise ValueError('Exact read-only relay and live runtime pin required')
        self.relay = relay
        self.draft = checked

    def _serve_relay(self):
        try:
            self.relay_report = self.relay.serve(
                self.socket_path, wait_seconds=self.relay.task.max_seconds,
                stop_event=self.stop_event)
        except Exception as error:
            self.relay_error = error

    def start(self):
        if self.process is not None or self.relay_thread is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only MCP runtime is already started')
        self.owned_desktop()
        if self.relay is None or self.draft is None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Exact read-only relay is not attached')
        try:
            verify_web_task_binding(self.relay.profiles, self.draft)
            verify_web_readonly_routes(self.relay.profiles, self.relay.task, self.relay.plan)
            if self.draft.runtime != self.runtime_pin():
                raise ValueError('Read-only runtime pin changed')
        except (ValueError, OSError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only task or runtime pin changed') from error
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only relay socket already exists')
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
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Read-only relay could not bind')
                try:
                    metadata = self.socket_path.lstat()
                    if (stat.S_ISSOCK(metadata.st_mode)
                            and stat.S_IMODE(metadata.st_mode) == 0o600
                            and metadata.st_uid == os.getuid()):
                        break
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Read-only relay socket identity differs')
                except FileNotFoundError:
                    time.sleep(0.01)
            else:
                raise AOSFault(ErrorCode.TIMEOUT, 'Read-only relay socket startup timed out')
            self.process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', self.container_id, '/usr/bin/python3', '-u', '-c',
                 self.worker_source(), self.relay.plan_sha256, mcp_pins['bundle_sha256']],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
            self.process.stdin.write(base64.b64encode(payload) + b'\n')
            self.process.stdin.write(self.relay.client_token.encode() + b'\n')
            self.process.stdin.write(canonical(self.relay.plan.routes).encode() + b'\n')
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
                    or self.evidence.get('readonly_route_gate') is not True
                    or self.evidence.get('plan_sha256') != self.relay.plan_sha256
                    or self.evidence.get('route_count') != len(self.relay.plan.routes)
                    or self.evidence.get('routes_sha256') != digest(self.relay.plan.routes)):
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Read-only MCP isolation or identity differs')
        except BaseException:
            self.stop()
            raise

    def _verify_scope(self):
        self.owned_desktop()
        try:
            verify_web_task_binding(self.relay.profiles, self.draft)
            verify_web_readonly_routes(self.relay.profiles, self.relay.task, self.relay.plan)
            if self.draft.runtime != self.runtime_pin():
                raise ValueError('Read-only runtime pin changed')
        except (ValueError, OSError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only task or runtime pin changed') from error

    def perform(self, tool, arguments):
        if self.relay is None or self.draft is None or tool not in self.allowed_tools:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only exact read-only routes are available')
        if tool == 'browser.remote.route':
            route_index = len(self.relay.reports)
            expected = {'profile_sha256': self.relay.plan.profile_sha256,
                        'binding_sha256': self.draft.binding_sha256,
                        'plan_sha256': self.relay.plan_sha256,
                        'route_index': route_index,
                        'url': self.relay.plan.routes[route_index]
                        if route_index < len(self.relay.plan.routes) else None}
            if (arguments != expected or route_index >= len(self.relay.plan.routes)
                    or route_index > 0 and self.last_readback_index != route_index - 1):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only route scope or sequence differs')
            self.expected_url = expected['url']
        else:
            route_index = self.last_route_index
            if arguments != {'route_index': route_index} or route_index < 0:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only observation scope differs')
            self.expected_url = self.relay.plan.routes[route_index]
        try:
            self._verify_scope()
            if tool == 'browser.remote.route':
                self.relay.arm(route_index)
            result = DesktopMCPBrowserRuntime.perform(
                self, tool, {'route_index': route_index})
            if tool == 'browser.remote.route':
                report = self.relay.wait_for_route(route_index)
                if (report.plan_sha256 != self.relay.plan_sha256
                        or report.request_sha256 != digest({'method': 'GET', 'url': self.expected_url})):
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Read-only relay report differs')
                self.last_route_index = route_index
            else:
                self.last_readback_index = route_index
            return result
        except BaseException:
            self.stop()
            raise

    def validate_result(self, tool, result):
        if tool not in self.allowed_tools or self.expected_url is None:
            raise ValueError('Invalid read-only route observation')
        observed = RemoteRouteObservation.model_validate(result)
        if (observed.url != self.expected_url
                or observed.planned_link_indices != sorted(set(observed.planned_link_indices))
                or any(index < 0 or index >= len(self.relay.plan.routes)
                       for index in observed.planned_link_indices)):
            raise ValueError('Read-only route URL differs')
        return observed.model_dump()

    def status(self):
        return {'kind': 'docker_chromium_readonly_routes_mcp',
                'runtime_id': self.runtime_id, 'parent_runtime_id': self.parent_id,
                'container_id': self.container_id, 'image_id': self.desktop.pins.get('image_id'),
                'running': (self.process is not None and self.process.poll() is None
                            and self.desktop.runtime_id == self.parent_id
                            and self.desktop.container_id == self.container_id
                            and self.desktop.descriptor is not None),
                'real_execution': True, 'desktop': True, 'network': False,
                'browser_transport': 'playwright_mcp',
                'plan_sha256': self.relay.plan_sha256 if self.relay else None,
                'route_count': len(self.relay.plan.routes) if self.relay else None,
                'completed_routes': len(self.relay.reports) if self.relay else 0,
                'runtime_digest': digest(self.pins),
                'worker_sha256': hashlib.sha256(self.worker_source().encode()).hexdigest(),
                'isolation': self.evidence}

"""Pinned visible MCP runtime for one exact static HTTPS bundle."""

import base64
import hashlib
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

from .contracts import AOSFault, ErrorCode, canonical, digest
from .desktop import DOCKER
from .desktop_mcp import DesktopMCPBrowserRuntime
from .desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle
from .desktop_remote_mcp import DesktopRemoteEntryMCPRuntime, RemoteEntryObservation
from .web_application_binding import WebTaskAdmissionDraft, verify_web_task_binding
from .web_static_asset_relay import ExactStaticBundleRelay


class DesktopStaticBundleMCPRuntime(DesktopRemoteEntryMCPRuntime):
    worker_file = 'desktop_static_bundle_mcp_worker.py'
    allowed_tools = frozenset({'browser.static.open', 'browser.static.observe'})

    def __init__(self, desktop, mcp_manifest: Path):
        super().__init__(desktop, mcp_manifest)
        self.draft = None
        self._awaiting_open = False

    def attach_relay(self, relay: ExactStaticBundleRelay, draft: WebTaskAdmissionDraft):
        if (not isinstance(relay, ExactStaticBundleRelay)
                or not isinstance(draft, WebTaskAdmissionDraft)
                or self.relay is not None or self.process is not None
                or self.relay_thread is not None):
            raise ValueError('Exact static bundle relay and live runtime pin required')
        checked = verify_web_task_binding(relay.profiles, draft)
        relay._verify_plan(relay.profiles, relay.task, relay.plan)
        if (checked.task != relay.task or checked.runtime != self.runtime_pin()
                or checked.profile_sha256 != relay.plan.profile_sha256):
            raise ValueError('Exact static bundle relay and live runtime pin required')
        self.relay = relay
        self.draft = checked

    def _verify_scope(self):
        self.owned_desktop()
        try:
            if (verify_web_task_binding(self.relay.profiles, self.draft) != self.draft
                    or self.relay._verify_plan(self.relay.profiles, self.relay.task,
                                               self.relay.plan) != self.relay.plan
                    or self.draft.runtime != self.runtime_pin()
                    or self.draft.task != self.relay.task):
                raise ValueError('static_bundle_binding_changed')
        except (ValueError, OSError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Static bundle task or runtime pin changed') from error

    def receive(self, timeout):
        return super().receive(max(timeout, 35) if self._awaiting_open else timeout)

    def start(self):
        if self.process is not None or self.relay_thread is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Static bundle MCP runtime is already started')
        if self.relay is None or self.draft is None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Exact static bundle relay is not attached')
        self._verify_scope()
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Static bundle socket already exists')
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
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Static bundle relay could not bind')
                try:
                    metadata = self.socket_path.lstat()
                    if (stat.S_ISSOCK(metadata.st_mode)
                            and stat.S_IMODE(metadata.st_mode) == 0o600
                            and metadata.st_uid == os.getuid()):
                        break
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Static bundle socket identity differs')
                except FileNotFoundError:
                    time.sleep(0.01)
            else:
                raise AOSFault(ErrorCode.TIMEOUT, 'Static bundle socket startup timed out')
            self.process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', self.container_id, '/usr/bin/python3', '-u', '-c',
                 self.worker_source(), self.relay.entry_url, self.relay.plan_sha256,
                 self.draft.binding_sha256, mcp_pins['bundle_sha256']],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
            self.process.stdin.write(base64.b64encode(payload) + b'\n')
            self.process.stdin.write(self.relay.client_token.encode() + b'\n')
            self.process.stdin.write(canonical(self.relay.plan.model_dump()).encode() + b'\n')
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
                    or self.evidence.get('static_bundle_gate') is not True
                    or self.evidence.get('plan_sha256') != self.relay.plan_sha256
                    or self.evidence.get('binding_sha256') != self.draft.binding_sha256
                    or self.evidence.get('asset_count') != len(self.relay.plan.assets)
                    or self.evidence.get('data_count') != len(
                        getattr(self.relay.plan, 'data_resources', []))):
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Static bundle MCP isolation or identity differs')
        except BaseException:
            self.stop()
            raise

    def perform(self, tool, arguments):
        if (self.relay is None or self.draft is None or tool not in self.allowed_tools
                or tool == 'browser.static.open'
                and arguments != {'profile_sha256': self.relay.plan.profile_sha256,
                                 'binding_sha256': self.draft.binding_sha256,
                                 'plan_sha256': self.relay.plan_sha256,
                                 'entry_url': self.relay.entry_url}
                or tool == 'browser.static.observe' and arguments != {}
                or tool == 'browser.static.open' and self.remote_opened
                or tool == 'browser.static.observe' and not self.remote_opened):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only the exact static bundle is available')
        self._verify_scope()
        try:
            if tool == 'browser.static.open':
                self.relay.arm()
                self.remote_opened = True
                self._awaiting_open = True
            result = DesktopMCPBrowserRuntime.perform(self, tool, {})
            if tool == 'browser.static.open':
                self.relay_thread.join(timeout=2)
                if (self.relay_thread.is_alive() or self.relay_error is not None
                        or self.relay_report is None
                        or self.relay_report.plan_sha256 != self.relay.plan_sha256
                        or len(self.relay_report.asset_response_sha256)
                        != len(self.relay.plan.assets)
                        or len(getattr(self.relay_report, 'data_response_sha256', []))
                        != len(getattr(self.relay.plan, 'data_resources', []))):
                    raise AOSFault(ErrorCode.RUNTIME_CRASH,
                                   'Static bundle relay did not attest the fetch')
            return result
        except BaseException:
            self.stop()
            raise
        finally:
            self._awaiting_open = False

    def validate_result(self, tool, result):
        if tool not in self.allowed_tools:
            raise ValueError('Invalid static bundle observation')
        observed = RemoteEntryObservation.model_validate(result)
        if observed.url != self.relay.entry_url:
            raise ValueError('Invalid static bundle entry URL')
        return observed.model_dump()

    def status(self):
        return {'kind': ('docker_chromium_readonly_data_bundle_mcp'
                         if self.relay and hasattr(self.relay.plan, 'data_resources')
                         else 'docker_chromium_static_bundle_mcp'),
                'runtime_id': self.runtime_id, 'parent_runtime_id': self.parent_id,
                'container_id': self.container_id, 'image_id': self.desktop.pins.get('image_id'),
                'running': (self.process is not None and self.process.poll() is None
                            and self.desktop.runtime_id == self.parent_id
                            and self.desktop.container_id == self.container_id
                            and self.desktop.descriptor is not None),
                'real_execution': True, 'desktop': True, 'network': False,
                'browser_transport': 'playwright_mcp',
                'plan_sha256': self.relay.plan_sha256 if self.relay else None,
                'asset_count': len(self.relay.plan.assets) if self.relay else None,
                'data_count': len(getattr(self.relay.plan, 'data_resources', []))
                if self.relay else None,
                'binding_sha256': self.draft.binding_sha256 if self.draft else None,
                'runtime_digest': digest(self.pins),
                'worker_sha256': hashlib.sha256(self.worker_source().encode()).hexdigest(),
                'isolation': self.evidence,
                'relay_report': self.relay_report.model_dump() if self.relay_report else None}

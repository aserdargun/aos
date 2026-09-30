import base64
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

from .contracts import AOSFault, ErrorCode, canonical, digest
from .desktop import DOCKER
from .desktop_mcp import DesktopMCPBrowserRuntime
from .desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle
from .desktop_remote_mcp import DesktopRemoteEntryMCPRuntime, RemoteEntryObservation
from .web_application_binding import WebTaskAdmissionDraft, verify_web_task_binding
from .web_https_form_transport import (ExactHTTPSFormRelay, form_stage_arguments,
                                       exact_form_fields, form_body,
                                       verify_web_https_form_plan,
                                       verify_web_https_form_target_grant)
from .web_https_form_state_probe import (ExactHTTPSFormStateProbe,
                                         form_state_action_arguments)


class DesktopHTTPSFormMCPRuntime(DesktopRemoteEntryMCPRuntime):
    worker_file = 'desktop_form_mcp_worker.py'
    allowed_tools = frozenset({'browser.form.open', 'browser.form.fill',
                               'browser.form.submit', 'browser.form.receipt',
                               'browser.form.observe'})
    stages = ('browser.form.open', 'browser.form.fill', 'browser.form.submit',
              'browser.form.receipt', 'browser.form.observe')

    def __init__(self, desktop, mcp_manifest: Path):
        super().__init__(desktop, mcp_manifest)
        self.draft = None
        self.field_name = None
        self.value = None
        self.form_fields = None
        self.stage = 0
        self.state_probe = None
        self.recipe_operations = None
        self.recipe_position = 0

    def attach_state_probe(self, probe: ExactHTTPSFormStateProbe):
        if (not isinstance(probe, ExactHTTPSFormStateProbe)
                or self.relay is None or probe.transport is not self.relay.transport
                or self.state_probe is not None or self.process is not None):
            raise ValueError('Exact form state probe requires the attached transport')
        if probe.plan.submitted_field_name is not None:
            probe.bind_submitted_fields(self.form_fields)
        self.state_probe = probe

    def attach_relay(self, relay: ExactHTTPSFormRelay, draft: WebTaskAdmissionDraft, *,
                     field_name: str | None = None, value: str | None = None,
                     fields: list[dict] | None = None,
                     recipe_operations: tuple[str, ...] | None = None):
        if (not isinstance(relay, ExactHTTPSFormRelay)
                or not isinstance(draft, WebTaskAdmissionDraft)
                or self.relay is not None or self.process is not None
                or self.relay_thread is not None):
            raise ValueError('Exact form relay and live runtime pin required')
        normalized = exact_form_fields(field_name, value, fields)
        transport = relay.transport
        checked = verify_web_task_binding(transport.profiles, draft)
        verify_web_https_form_plan(transport.profiles, transport.task, transport.plan)
        body = form_body(normalized)
        if (checked.task != transport.task or checked.runtime != self.runtime_pin()
                or checked.profile_sha256 != transport.plan.profile_sha256
                or hashlib.sha256(body).hexdigest() != transport.plan.body_sha256
                or len(body) != transport.plan.body_bytes):
            raise ValueError('Exact form relay and live runtime pin required')
        if recipe_operations is not None:
            from .site_skill_form_recipe import SUPPORTED_RECIPE_ORDERS

            if (not isinstance(recipe_operations, tuple)
                    or recipe_operations not in SUPPORTED_RECIPE_ORDERS):
                raise ValueError('Unsupported exact form recipe order')
            self.recipe_operations = recipe_operations
        self.relay = relay
        self.draft = checked
        self.field_name = field_name
        self.value = value
        self.form_fields = normalized

    def _serve_relay(self):
        try:
            self.relay_report = self.relay.serve(
                self.socket_path, wait_seconds=min(self.relay.transport.task.max_seconds, 30),
                stop_event=self.stop_event)
        except Exception as error:
            self.relay_error = error

    def _verify_scope(self):
        self.owned_desktop()
        try:
            transport = self.relay.transport
            checked = verify_web_task_binding(transport.profiles, self.draft)
            verify_web_https_form_plan(transport.profiles, transport.task, transport.plan)
            verify_web_https_form_target_grant(transport.plan, transport.public_plan_sha256)
            body = form_body(self.form_fields)
            if (checked.task != transport.task or checked.runtime != self.runtime_pin()
                    or checked.profile_sha256 != transport.plan.profile_sha256
                    or hashlib.sha256(body).hexdigest() != transport.plan.body_sha256
                    or len(body) != transport.plan.body_bytes):
                raise ValueError('Form task, input, or runtime pin changed')
        except (ValueError, OSError, TypeError) as error:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form task or runtime pin changed') from error

    def start(self):
        if self.process is not None or self.relay_thread is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form MCP runtime is already started')
        if self.relay is None or self.draft is None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Exact form relay is not attached')
        self._verify_scope()
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form relay socket already exists')
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
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Form relay could not bind')
                try:
                    metadata = self.socket_path.lstat()
                    if (stat.S_ISSOCK(metadata.st_mode)
                            and stat.S_IMODE(metadata.st_mode) == 0o600
                            and metadata.st_uid == os.getuid()):
                        break
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Form relay socket identity differs')
                except FileNotFoundError:
                    time.sleep(0.01)
            else:
                raise AOSFault(ErrorCode.TIMEOUT, 'Form relay socket startup timed out')
            plan = self.relay.transport.plan
            self.process = subprocess.Popen(
                [*DOCKER, 'exec', '-i', self.container_id, '/usr/bin/python3', '-u', '-c',
                 self.worker_source(), self.relay.transport.plan_sha256,
                 mcp_pins['bundle_sha256']],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
            self.process.stdin.write(base64.b64encode(payload) + b'\n')
            self.process.stdin.write(self.relay.client_token.encode() + b'\n')
            form_input = {
                'entry_url': plan.entry_url, 'submit_url': plan.submit_url,
                'receipt_url': plan.receipt_url, 'body_sha256': plan.body_sha256,
                'body_bytes': plan.body_bytes,
                'confirm_public_plan_sha256': self.relay.transport.public_plan_sha256}
            if self.field_name is None:
                form_input['fields'] = [
                    {'name': name, 'value': value} for name, value in self.form_fields]
            else:
                form_input.update({'field_name': self.field_name, 'value': self.value})
            self.process.stdin.write((canonical(form_input) + '\n').encode())
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
                    or self.evidence.get('https_form_gate') is not True
                    or self.evidence.get('plan_sha256') != self.relay.transport.plan_sha256
                    or self.evidence.get('body_sha256') != plan.body_sha256
                    or self.evidence.get('public_plan_sha256')
                    != self.relay.transport.public_plan_sha256):
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Form MCP isolation or identity differs')
        except BaseException:
            self.stop()
            raise

    def perform(self, tool, arguments):
        if tool in {'browser.form.state_before', 'browser.form.state_after'}:
            phase = 'before' if tool.endswith('before') else 'after'
            expected_stage = (2 if phase == 'before'
                              and self.recipe_operations is not None
                              and self.recipe_operations[1] == 'fill_form'
                              else 1 if phase == 'before' else 5)
            recipe_operation = 'read_state_' + phase
            if (self.state_probe is None or self.stage != expected_stage
                    or self.recipe_operations is not None
                    and (self.recipe_position >= len(self.recipe_operations)
                         or self.recipe_operations[self.recipe_position] != recipe_operation)
                    or self.process is None or self.process.poll() is not None
                    or arguments != form_state_action_arguments(
                        self.state_probe.plan, self.draft.binding_sha256, phase,
                        self.state_probe.transport.cookie_sha256)):
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'Only the next exact form state phase is available')
            try:
                self._verify_scope()
                if phase == 'before':
                    self.state_probe.observe_before(
                        confirm_request_sha256=arguments['request_sha256'])
                    if self.recipe_operations is not None:
                        self.recipe_position += 1
                    return {'status': 'before_response_matched',
                            'state_plan_sha256': self.state_probe.plan_sha256,
                            'response_sha256': self.state_probe._before_response.response_sha256}
                report = self.state_probe.observe_after(
                    confirm_request_sha256=arguments['request_sha256'])
                if self.recipe_operations is not None:
                    self.recipe_position += 1
                return report.model_dump()
            except BaseException:
                self.stop()
                raise
        recipe_operation = {
            'browser.form.open': 'open_entry', 'browser.form.fill': 'fill_form',
            'browser.form.submit': 'submit_form', 'browser.form.receipt': 'read_receipt',
        }.get(tool)
        if (self.recipe_operations is not None
                and recipe_operation is not None
                and (self.recipe_position >= len(self.recipe_operations)
                     or self.recipe_operations[self.recipe_position] != recipe_operation)):
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Only the next recipe-bound form operation is available')
        if (self.relay is None or self.draft is None or self.stage >= len(self.stages)
                or tool != self.stages[self.stage]
                or arguments != form_stage_arguments(
                    self.relay.transport.plan, self.draft.binding_sha256,
                    self.field_name if self.field_name is not None else tuple(
                        name for name, _value in self.form_fields), self.stage,
                    self.relay.transport.cookie_sha256)):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Only the next exact form stage is available')
        try:
            self._verify_scope()
            if (tool == 'browser.form.fill' and self.state_probe is not None
                    and self.state_probe._before_response is None
                    and not (self.recipe_operations is not None
                             and self.recipe_operations.index('read_state_before')
                             > self.recipe_operations.index('fill_form'))):
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'Form state baseline must be read before fill')
            if (tool == 'browser.form.submit' and self.state_probe is not None
                    and self.state_probe._before_response is None):
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'Form state baseline must be read before submit')
            self.stage += 1
            result = DesktopMCPBrowserRuntime.perform(self, tool, {})
            if self.recipe_operations is not None and recipe_operation is not None:
                self.recipe_position += 1
            if tool == 'browser.form.submit' and not self.relay.transport._submitted:
                raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Form submit was not attested')
            if tool == 'browser.form.receipt':
                self.relay_thread.join(timeout=2)
                if (self.relay_thread.is_alive() or self.relay_error is not None
                        or self.relay_report is None
                        or self.relay_report.plan_sha256 != self.relay.transport.plan_sha256):
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Form receipt was not attested')
            return result
        except BaseException:
            self.stop()
            raise

    def validate_result(self, tool, result):
        if tool in {'browser.form.fill', 'browser.form.submit'}:
            if result != {'stage': 'filled' if tool == 'browser.form.fill' else 'submitted',
                          'body_sha256': self.relay.transport.plan.body_sha256}:
                raise ValueError('Form stage attestation differs')
            return result
        expected_url = (self.relay.transport.plan.receipt_url
                        if tool in {'browser.form.receipt', 'browser.form.observe'}
                        else self.relay.transport.plan.entry_url)
        observed = RemoteEntryObservation.model_validate(result)
        if observed.url != expected_url:
            raise ValueError('Form page URL differs')
        return observed.model_dump()

    def status(self):
        return {'kind': 'docker_chromium_https_form_mcp', 'runtime_id': self.runtime_id,
                'parent_runtime_id': self.parent_id, 'container_id': self.container_id,
                'image_id': self.desktop.pins.get('image_id'),
                'running': (self.process is not None and self.process.poll() is None
                            and self.desktop.runtime_id == self.parent_id
                            and self.desktop.container_id == self.container_id
                            and self.desktop.descriptor is not None),
                'real_execution': True, 'desktop': True, 'network': False,
                'browser_transport': 'playwright_mcp',
                'plan_sha256': self.relay.transport.plan_sha256 if self.relay else None,
                'completed_stages': self.stage, 'runtime_digest': digest(self.pins),
                'worker_sha256': hashlib.sha256(self.worker_source().encode()).hexdigest(),
                'isolation': self.evidence}

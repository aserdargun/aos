import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .browser import BROWSER_SCOPE, BROWSER_VALUE, BrowserRuntime
from .contracts import AOSFault, Action, ErrorCode, HELLO_CONTENT, HELLO_PATH, LOCAL_NAVIGATION_SCOPE, REMOTE_ENTRY_SCOPE, REMOTE_ROUTES_SCOPE, REMOTE_STATIC_ASSETS_SCOPE, REMOTE_FORM_SCOPE, STAGING_WORKFLOW_MESSAGE, STAGING_WORKFLOW_SCOPE, Phase, State, canonical, digest, identifier, now
from .storage import TrajectoryStore
from .vision import VISION_SCOPE, VisionRuntime
from .web_application import WebApplicationProfile
from .workspace_identity import open_existing_workspace, workspace_identity
from .write_receipts import FileWitness, WriteReceipt, witness_file


class ComputerRuntime(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def status(self) -> dict: ...
    def read(self, path: str) -> str: ...
    def write(self, path: str, content: str) -> dict: ...
    def checksum(self, path: str) -> dict: ...


class WorkspaceRuntime:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self.descriptor: int | None = None
        self.runtime_id = "workspace-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:24]

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.descriptor)
            self.descriptor = None
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Workspace already has an active writer") from None

    def stop(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def start_existing(self) -> None:
        if self.descriptor is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Workspace already started")
        descriptor = open_existing_workspace(self.root)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor

    def status(self) -> dict:
        return {"kind": "descriptor_workspace", "runtime_id": self.runtime_id,
                "running": self.descriptor is not None, "real_execution": True,
                "desktop": False, "network": False, "mount": "/workspace",
                "workspace_identity": workspace_identity(self.root, self.descriptor).model_dump()
                if self.descriptor is not None else None}

    def _name(self, path: str) -> str:
        if self.descriptor is None or path != HELLO_PATH:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Path is outside the authorized hello scope")
        return "hello.txt"

    def read(self, path: str) -> str:
        name = self._name(path)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.descriptor)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 4096:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Only a bounded regular file with one link is allowed")
            return os.read(descriptor, 4097).decode("utf-8")
        finally:
            os.close(descriptor)

    def write(self, path: str, content: str) -> dict:
        name = self._name(path)
        if content != HELLO_CONTENT:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Content is outside the authorized hello scope")
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.descriptor)
        try:
            payload = content.encode()
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count == 0:
                    raise OSError("Incomplete write")
                written += count
            os.fsync(descriptor)
            os.fsync(self.descriptor)
            result = {"bytes_written": written}
            if type(self) is WorkspaceRuntime:
                try:
                    result['_write_witness'] = witness_file(self.descriptor, descriptor).model_dump()
                except ValueError:
                    raise AOSFault(ErrorCode.RUNTIME_CRASH, 'Created file changed before write witness') from None
            return result
        finally:
            os.close(descriptor)

    def checksum(self, path: str) -> dict:
        self._name(path)
        self.read(path)
        command = ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                   "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr",
                   "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib", "/lib64",
                   "--ro-bind", f"/proc/self/fd/{self.descriptor}", "/workspace",
                   "--chdir", "/workspace", "--", "/usr/bin/sha256sum", HELLO_PATH]
        try:
            result = subprocess.run(command, capture_output=True, timeout=5, check=False,
                                    pass_fds=(self.descriptor,), env={}, start_new_session=True)
        except subprocess.TimeoutExpired:
            raise AOSFault(ErrorCode.TIMEOUT, "Sandboxed checksum timed out") from None
        except OSError:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Bubblewrap is unavailable") from None
        if result.returncode or len(result.stdout) > 4096 or len(result.stderr) > 4096:
            raise AOSFault(ErrorCode.RUNTIME_CRASH, "Sandboxed checksum failed; no host fallback")
        return {"stdout": result.stdout.decode(), "exit_code": result.returncode}


class ToolRegistry:
    @staticmethod
    def validate(action: Action) -> None:
        if action.tool.startswith("vision."):
            if action.tool == "vision.verify":
                if action.arguments:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, "Visual verification accepts no arguments")
                return
            patterns = {"capture_id": "[a-f0-9]{32}", "element_id": "visual-[a-f0-9]{24}", "scene_sha256": "[a-f0-9]{64}"}
            if set(action.arguments) != set(patterns) or any(
                    not isinstance(action.arguments[key], str)
                    or not re.fullmatch(pattern, action.arguments[key])
                    for key, pattern in patterns.items()):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Visual action must use bounded symbolic identifiers")
            return
        if action.tool.startswith("browser."):
            if action.tool.startswith('browser.static.'):
                if action.tool == 'browser.static.observe' and action.arguments == {}:
                    return
                if action.tool != 'browser.static.open':
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Unsupported static bundle tool')
                arguments = action.arguments
                if (set(arguments) != {'profile_sha256', 'binding_sha256',
                                       'plan_sha256', 'entry_url'}
                        or any(not isinstance(arguments[key], str)
                               or re.fullmatch('[a-f0-9]{64}', arguments[key]) is None
                               for key in ('profile_sha256', 'binding_sha256', 'plan_sha256'))):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Static bundle requires exact hashes')
                try:
                    if not isinstance(arguments['entry_url'], str):
                        raise ValueError('non_string_static_entry')
                    WebApplicationProfile.entry_is_canonical(arguments['entry_url'])
                except ValueError as error:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION,
                                   'Static bundle requires a canonical entry URL') from error
                return
            if action.tool.startswith('browser.form.'):
                if action.tool in {'browser.form.state_before', 'browser.form.state_after'}:
                    phase = 'before' if action.tool.endswith('before') else 'after'
                    expected = {'profile_sha256', 'binding_sha256', 'form_plan_sha256',
                                'state_plan_sha256', 'phase', 'url', 'request_sha256'}
                    arguments = action.arguments
                    if 'cookie_sha256' in arguments:
                        expected.add('cookie_sha256')
                    if 'marker_id_sha256' in arguments or 'expected_marker_sha256' in arguments:
                        expected.update(('marker_id_sha256', 'expected_marker_sha256'))
                    if 'submitted_field_name_sha256' in arguments:
                        expected.add('submitted_field_name_sha256')
                    if (set(arguments) != expected or arguments['phase'] != phase
                            or 'submitted_field_name_sha256' in arguments
                            and 'marker_id_sha256' not in arguments
                            or any(not isinstance(arguments[key], str)
                                   or re.fullmatch('[a-f0-9]{64}', arguments[key]) is None
                                   for key in ('profile_sha256', 'binding_sha256',
                                               'form_plan_sha256', 'state_plan_sha256',
                                               'request_sha256')
                                   + (('marker_id_sha256', 'expected_marker_sha256')
                                      if 'marker_id_sha256' in arguments else ())
                                   + (('submitted_field_name_sha256',)
                                      if 'submitted_field_name_sha256' in arguments else ())
                                   + (('cookie_sha256',) if 'cookie_sha256' in arguments else ()))):
                        raise AOSFault(ErrorCode.UNSAFE_ACTION,
                                       'Form state action requires exact phase and hashes')
                    try:
                        if not isinstance(arguments['url'], str) or not arguments['url'].startswith('https://'):
                            raise ValueError('non_https_form_state_url')
                        WebApplicationProfile.entry_is_canonical(arguments['url'])
                    except (TypeError, ValueError) as error:
                        raise AOSFault(ErrorCode.UNSAFE_ACTION,
                                       'Form state action requires canonical HTTPS URL') from error
                    return
                expected_stage = {'browser.form.open': 0, 'browser.form.fill': 1,
                                  'browser.form.submit': 2, 'browser.form.receipt': 3,
                                  'browser.form.observe': 4}.get(action.tool)
                expected = {'profile_sha256', 'binding_sha256', 'plan_sha256',
                            'stage', 'url'}
                if 'cookie_sha256' in action.arguments:
                    expected.add('cookie_sha256')
                if expected_stage in (1, 2):
                    expected.add('field_names' if 'field_names' in action.arguments else 'field_name')
                if expected_stage == 2:
                    expected.add('body_sha256')
                if (expected_stage is None or set(action.arguments) != expected
                        or any(not isinstance(action.arguments[key], str)
                               or re.fullmatch('[a-f0-9]{64}', action.arguments[key]) is None
                               for key in ('profile_sha256', 'binding_sha256', 'plan_sha256')
                               + (('cookie_sha256',) if 'cookie_sha256' in action.arguments else ())
                               + (('body_sha256',) if expected_stage == 2 else ()))
                        or type(action.arguments['stage']) is not int
                        or action.arguments['stage'] != expected_stage
                        or expected_stage in (1, 2)
                        and (('field_name' in action.arguments
                              and (not isinstance(action.arguments['field_name'], str)
                                   or not action.arguments['field_name'].isidentifier()))
                             or 'field_names' in action.arguments
                             and (not isinstance(action.arguments['field_names'], str)
                                  or not 2 <= len(action.arguments['field_names'].split(',')) <= 8
                                  or any(not isinstance(name, str)
                                         or re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}', name) is None
                                         for name in action.arguments['field_names'].split(','))
                                  or len(set(action.arguments['field_names'].split(',')))
                                  != len(action.arguments['field_names'].split(','))))):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form action requires exact stage and binding')
                try:
                    if (not isinstance(action.arguments['url'], str)
                            or not action.arguments['url'].startswith('https://')):
                        raise ValueError('non_https_form_action_url')
                    WebApplicationProfile.entry_is_canonical(action.arguments['url'])
                except ValueError as error:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION,
                                   'Form action requires a canonical HTTPS target') from error
                return
            if action.tool.startswith('browser.remote.'):
                if action.tool == 'browser.remote.open':
                    expected = {'profile_sha256', 'binding_sha256', 'entry_url'}
                elif action.tool == 'browser.remote.route':
                    expected = {'profile_sha256', 'binding_sha256', 'plan_sha256', 'route_index', 'url'}
                elif action.tool == 'browser.remote.observe':
                    expected = set(action.arguments)
                    if expected not in (set(), {'route_index'}):
                        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote observation scope differs')
                else:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Unsupported remote browser tool')
                if (set(action.arguments) != expected
                        or action.tool in {'browser.remote.open', 'browser.remote.route'}
                        and (any(not isinstance(action.arguments[key], str)
                                 or re.fullmatch('[a-f0-9]{64}', action.arguments[key]) is None
                                 for key in ('profile_sha256', 'binding_sha256'))
                             or action.tool == 'browser.remote.route'
                             and (not isinstance(action.arguments['plan_sha256'], str)
                                  or re.fullmatch('[a-f0-9]{64}', action.arguments['plan_sha256']) is None))
                        or action.tool == 'browser.remote.open'
                        and (not isinstance(action.arguments['entry_url'], str)
                             or not action.arguments['entry_url'].startswith('https://')
                             or len(action.arguments['entry_url']) > 2048)
                        or action.tool == 'browser.remote.route'
                        and (type(action.arguments['route_index']) is not int
                             or not 0 <= action.arguments['route_index'] < 8
                             or not isinstance(action.arguments['url'], str)
                             or not action.arguments['url'].startswith('https://')
                             or len(action.arguments['url']) > 2048)
                        or action.tool == 'browser.remote.observe' and expected
                        and (type(action.arguments['route_index']) is not int
                             or not 0 <= action.arguments['route_index'] < 8)):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote entry requires its exact binding hash')
                return
            if action.tool.startswith("browser.staging."):
                if action.tool in {"browser.staging.open", "browser.staging.snapshot"}:
                    expected = set()
                elif action.tool in {"browser.staging.follow", "browser.staging.submit"}:
                    expected = {"snapshot_id", "element_id"}
                elif action.tool == "browser.staging.fill":
                    expected = {"snapshot_id", "element_id", "value"}
                else:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, "Unsupported synthetic staging tool")
                if (set(action.arguments) != expected
                        or any(not isinstance(action.arguments[key], str)
                               or re.fullmatch('[a-f0-9]{32}', action.arguments[key]) is None
                               for key in ('snapshot_id', 'element_id') if key in expected)
                        or action.tool == 'browser.staging.fill'
                        and action.arguments['value'] != STAGING_WORKFLOW_MESSAGE):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, "Synthetic staging requires exact references and value")
                return
            if action.tool.startswith("browser.fixture."):
                if action.tool in {"browser.fixture.open", "browser.fixture.snapshot"}:
                    expected = set()
                else:
                    expected = {"snapshot_id", "element_id"}
                if (set(action.arguments) != expected or any(
                        not isinstance(value, str) or re.fullmatch('[a-f0-9]{32}', value) is None
                        for value in action.arguments.values())):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, "Local fixture navigation requires exact symbolic references")
                return
            expected = set() if action.tool == "browser.verify" else {"snapshot_id", "element_id"}
            if action.tool == "browser.fill":
                expected.add("value")
            if (set(action.arguments) != expected
                    or any(not isinstance(value, str) or not value or len(value) > 100
                           for value in action.arguments.values())
                    or (action.tool == "browser.fill" and action.arguments["value"] != BROWSER_VALUE)):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Browser arguments exceed fixture authorization")
            return
        expected = {"path", "content"} if action.tool == "filesystem.write" else {"path"}
        if set(action.arguments) != expected or action.arguments["path"] != HELLO_PATH:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Tool arguments exceed the authorized scope")
        if action.tool == "filesystem.write" and action.arguments["content"] != HELLO_CONTENT:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Write content differs from authorization")


class SafetyPolicy:
    @staticmethod
    def check(action: Action, state: State, runtime_id: str) -> None:
        ToolRegistry.validate(action)
        if (state.owner != "AGENT" or state.phase != Phase.EXECUTE
                or action.owner_lease_id != state.owner_lease_id
                or action.state_version != state.state_version
                or action.runtime_id != state.runtime_id or action.runtime_id != runtime_id
                or action.run_id != state.run_id or action.task_id != state.task_id
                or action.step_id != state.step_id or time.time() > action.deadline):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Action state, lease, deadline or authority is invalid")
        if state.task_kind == "vision_canvas":
            if (not action.tool.startswith("vision.") or state.authorized_path != VISION_SCOPE
                    or state.authorized_content != "SAVE"
                    or (action.tool == "vision.click" and (
                        action.arguments["capture_id"] != state.capture_id
                        or action.arguments["scene_sha256"] != state.scene_sha256
                        or action.selected_option != action.arguments["element_id"]))):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Visual action exceeds capture-bound task authority")
        elif state.task_kind == "browser_form":
            if (action.tool not in {"browser.fill", "browser.submit", "browser.verify"} or state.authorized_path != BROWSER_SCOPE
                    or state.authorized_content != BROWSER_VALUE
                    or (action.tool == "browser.fill" and action.selected_option != "fill_message")
                    or (action.tool == "browser.submit" and action.selected_option != "submit_form")):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Action exceeds browser task authority")
        elif state.task_kind == "browser_local_navigation":
            if (state.authorized_path != LOCAL_NAVIGATION_SCOPE or state.authorized_content != "DETAILS"
                    or (action.tool, action.selected_option) not in {
                        ("browser.fixture.open", "open_start"),
                        ("browser.fixture.follow", "follow_details"),
                        ("browser.fixture.snapshot", "open_start"),
                        ("browser.fixture.snapshot", "follow_details")}):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Action exceeds fixed local navigation authority")
        elif state.task_kind == "browser_staging_workflow":
            if (state.authorized_path != STAGING_WORKFLOW_SCOPE
                    or state.authorized_content != STAGING_WORKFLOW_MESSAGE
                    or (action.tool, action.selected_option) not in {
                        ('browser.staging.open', 'open_app'),
                        ('browser.staging.follow', 'follow_draft'),
                        ('browser.staging.fill', 'fill_message'),
                        ('browser.staging.submit', 'submit_draft'),
                        ('browser.staging.snapshot', 'open_app'),
                        ('browser.staging.snapshot', 'follow_draft'),
                        ('browser.staging.snapshot', 'fill_message'),
                        ('browser.staging.snapshot', 'submit_draft')}):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Action exceeds fixed synthetic staging authority")
        elif state.task_kind == 'browser_remote_entry':
            if (state.authorized_path != REMOTE_ENTRY_SCOPE
                    or (action.tool, action.selected_option) not in {
                        ('browser.remote.open', 'open_entry'),
                        ('browser.remote.observe', 'open_entry')}
                    or action.tool == 'browser.remote.open'
                    and action.arguments['binding_sha256'] != state.authorized_content
                    or action.tool == 'browser.remote.observe' and action.arguments):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Action exceeds exact remote entry authority')
        elif state.task_kind == 'browser_remote_routes':
            if (state.authorized_path != REMOTE_ROUTES_SCOPE
                    or (action.tool, action.selected_option) not in {
                        ('browser.remote.route', 'open_entry'),
                        ('browser.remote.observe', 'open_entry')}
                    or action.tool == 'browser.remote.route'
                    and action.arguments['plan_sha256'] != state.authorized_content
                    or action.tool == 'browser.remote.observe'
                    and set(action.arguments) != {'route_index'}):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Action exceeds exact read-only routes authority')
        elif state.task_kind == 'browser_remote_static_assets':
            if (state.authorized_path != REMOTE_STATIC_ASSETS_SCOPE
                    or (action.tool, action.selected_option) not in {
                        ('browser.static.open', 'open_entry'),
                        ('browser.static.observe', 'open_entry')}
                    or action.tool == 'browser.static.open'
                    and action.arguments['plan_sha256'] != state.authorized_content
                    or action.tool == 'browser.static.observe' and action.arguments):
                raise AOSFault(ErrorCode.UNSAFE_ACTION,
                               'Action exceeds exact static bundle authority')
        elif state.task_kind == 'browser_remote_form':
            if action.tool in {'browser.form.state_before', 'browser.form.state_after'}:
                if (state.authorized_path != REMOTE_FORM_SCOPE
                        or action.arguments['form_plan_sha256'] != state.authorized_content
                        or (action.tool, action.selected_option) not in {
                            ('browser.form.state_before', 'read_state_before'),
                            ('browser.form.state_after', 'read_state_after')}):
                    raise AOSFault(ErrorCode.UNSAFE_ACTION,
                                   'Action exceeds exact HTTPS form state authority')
                return
            if (state.authorized_path != REMOTE_FORM_SCOPE
                    or (action.tool, action.selected_option) not in {
                        ('browser.form.open', 'open_entry'),
                        ('browser.form.fill', 'fill_form'),
                        ('browser.form.submit', 'submit_form'),
                        ('browser.form.receipt', 'read_receipt'),
                        ('browser.form.observe', 'read_receipt')}
                    or action.arguments['plan_sha256'] != state.authorized_content):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Action exceeds exact HTTPS form authority')
        elif (action.tool.startswith(("browser.", "vision.")) or action.arguments["path"] != state.authorized_path
              or (action.tool == "filesystem.write" and action.arguments["content"] != state.authorized_content)):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Action exceeds hello task authority")


class ComputerGateway:
    def __init__(self, store: TrajectoryStore, runtime: WorkspaceRuntime | BrowserRuntime):
        self.store = store
        self.runtime = runtime

    def record_write_receipt(self, action: Action, evidence: dict) -> None:
        receipt = WriteReceipt(run_ref=digest({'run_id': action.run_id}), action_ref=digest({'action_id': action.action_id}),
                               envelope_sha256=digest(action.model_dump(mode='json')),
                               workspace_sha256=digest(workspace_identity(self.runtime.root, self.runtime.descriptor).model_dump()),
                               file=FileWitness.model_validate(evidence))
        with self.store.connection:
            self.store.insert('observations', observation_id=identifier('observation'), run_id=action.run_id,
                              step_id=action.step_id, action_id=action.action_id, kind='filesystem.write_receipt',
                              payload_json=canonical(receipt.model_dump()), created_at=now())

    def observe(self, state: State) -> str:
        current = self.store.state(state.run_id)
        if (current != state or current.phase != Phase.OBSERVE or current.owner != "AGENT"
                or current.runtime_id != self.runtime.runtime_id):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Observation requires current state and agent ownership")
        return self.runtime.read(current.authorized_path)

    def execute(self, action: Action, decision_id: str) -> dict:
        connection = self.store.connection
        serialized = action.model_dump(mode="json")
        payload_hash = digest(serialized)
        with connection:
            previous = connection.execute(
                "SELECT actions.*,action_envelopes.payload_sha256 FROM actions JOIN action_envelopes USING(action_id) WHERE run_id=? AND idempotency_key=?",
                (action.run_id, action.idempotency_key),
            ).fetchone()
            if previous:
                if previous["payload_sha256"] != payload_hash:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, "Idempotency key reused with a different envelope")
                if previous["status"] == "ok":
                    return json.loads(previous["result_json"])
                raise AOSFault(ErrorCode.RUNTIME_CRASH, "Prior action requires reconciliation; replay is forbidden")
            decision = connection.execute(
                "SELECT selected_option,policy_result FROM decisions WHERE decision_id=? AND run_id=? AND step_id=?",
                (decision_id, action.run_id, action.step_id),
            ).fetchone()
            if (not decision or decision["policy_result"] != "allow"
                    or decision["selected_option"] != action.selected_option):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Action has no allowed decision")
            self.store.insert("actions", action_id=action.action_id, run_id=action.run_id, step_id=action.step_id,
                              decision_id=decision_id, idempotency_key=action.idempotency_key, tool=action.tool,
                              arguments_json=canonical(action.arguments), status="intent", created_at=now())
            self.store.insert("action_envelopes", action_id=action.action_id,
                              envelope_json=canonical(serialized), payload_sha256=payload_hash)
        try:
            SafetyPolicy.check(action, self.store.state(action.run_id), self.runtime.runtime_id)
            visual_tool = action.tool.startswith("vision.")
            browser_tool = action.tool.startswith("browser.")
            if (visual_tool != isinstance(self.runtime, VisionRuntime)
                    or browser_tool != (isinstance(self.runtime, BrowserRuntime) and not isinstance(self.runtime, VisionRuntime))):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, "Tool does not match runtime type")
        except AOSFault:
            with connection:
                connection.execute("UPDATE actions SET status='denied',error_code='UNSAFE_ACTION',completed_at=? WHERE action_id=?",
                                   (now(), action.action_id))
            raise
        with connection:
            connection.execute("UPDATE actions SET status='running',actual_option=? WHERE action_id=?",
                               (action.selected_option, action.action_id))
        try:
            if action.tool.startswith(("browser.", "vision.")):
                result = self.runtime.perform(action.tool, action.arguments)
            elif action.tool == "filesystem.write":
                result = self.runtime.write(**action.arguments)
                if type(self.runtime) is WorkspaceRuntime:
                    evidence = result.pop('_write_witness', None)
                    if evidence is not None:
                        self.record_write_receipt(action, evidence)
            elif action.tool == "filesystem.read":
                result = {"content": self.runtime.read(**action.arguments)}
            else:
                result = self.runtime.checksum(**action.arguments)
        except (OSError, UnicodeError, AOSFault) as error:
            fault = error if isinstance(error, AOSFault) else AOSFault(
                ErrorCode.ELEMENT_MISSING if isinstance(error, FileNotFoundError) else ErrorCode.TOOL_FAILURE,
                "Authorized file is absent" if isinstance(error, FileNotFoundError) else "Workspace operation failed",
                retryable=isinstance(error, FileNotFoundError),
            )
            known_browser_rejection = action.tool.startswith(("browser.", "vision.")) and fault.code in {
                ErrorCode.UI_CHANGED, ErrorCode.ELEMENT_MISSING, ErrorCode.UNSAFE_ACTION}
            status = "error" if action.tool in {"filesystem.read", "browser.verify", "browser.fixture.snapshot", "browser.remote.observe", "browser.static.observe", "vision.verify"} or known_browser_rejection else "uncertain"
            with connection:
                connection.execute("UPDATE actions SET status=?,error_code=?,result_json=?,completed_at=? WHERE action_id=?",
                                   (status, fault.code.value, canonical(fault.payload()), now(), action.action_id))
            raise fault from None
        with connection:
            connection.execute("UPDATE actions SET status='ok',result_json=?,completed_at=? WHERE action_id=?",
                               (canonical(result), now(), action.action_id))
        return result

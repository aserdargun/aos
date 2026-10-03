import argparse
from contextlib import contextmanager
from contextlib import ExitStack
from contextvars import ContextVar
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import httpx
from pydantic import Field, model_serializer, model_validator

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, TypedModel, canonical, digest, now
from .lifecycle import ProcessIdentity, observe_process, private_directory, process_identity, read_journal
from .workspace_identity import open_existing_workspace, workspace_identity

if TYPE_CHECKING:
    from .shared_desktop_host import SharedDesktopState


BASE = REPO_ROOT / 'data/local-app-v1'
URL = 'http://127.0.0.1:8765/ui/'
ORIGIN = 'http://127.0.0.1:8765'
MCP_MANIFEST = REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'
WEB_PROFILES = REPO_ROOT / 'data/web-applications'


class LocalAppInstance(TypedModel):
    base: Path
    port: int = Field(ge=1024, le=65535)
    project: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9-]{0,47}$')

    @model_validator(mode='after')
    def exact_project_scope(self):
        if (not self.base.is_absolute() or '..' in self.base.parts
                or self.project is None and self.base.name.startswith('local-app-project-')
                or self.project is not None and (
                    self.base != REPO_ROOT / 'data' / ('local-app-project-' + self.project)
                    or self.port == 8765)):
            raise ValueError('Named project requires its exact private base and separate loopback port')
        return self

    @classmethod
    def for_project(cls, name, port):
        if type(name) is not str or type(port) is not int:
            raise ValueError('Project name and integer port must be supplied together')
        return cls(base=REPO_ROOT / 'data' / ('local-app-project-' + name), project=name, port=port)

    @property
    def origin(self):
        return 'http://127.0.0.1:' + str(self.port)

    @property
    def url(self):
        return self.origin + '/ui/'

    def arguments(self):
        return [] if self.project is None else ['--project', self.project, '--project-port', str(self.port)]

    def check_state(self, state):
        if (state.project != self.project
                or self.project is not None and state.url != self.url):
            raise ValueError('Recorded manager project or loopback port differs from the selected instance')


_INSTANCE = ContextVar('local_app_instance', default=None)


def current_instance():
    return _INSTANCE.get() or LocalAppInstance(base=BASE, port=int(ORIGIN.rsplit(':', 1)[1]))


@contextmanager
def instance_scope(instance):
    if not isinstance(instance, LocalAppInstance):
        raise ValueError('Typed local application instance required')
    token = _INSTANCE.set(instance)
    try:
        yield instance
    finally:
        _INSTANCE.reset(token)


def _check_expected_session(state, expected_session):
    if expected_session is None and current_instance().project is not None:
        raise ValueError('Named lifecycle effects require an explicit --expected-session')
    if expected_session is not None and (
            type(expected_session) is not str or re.fullmatch(r'app-[a-f0-9]{32}', expected_session) is None
            or state is None or state.session != expected_session):
        raise ValueError('Local session changed; no lifecycle effect admitted')
    if state is not None:
        current_instance().check_state(state)


def _assert_named_state(state):
    if current_instance().project is not None:
        current_instance().check_state(state)
        if read_state() != state:
            raise ValueError('Local session changed during named lifecycle operation')


def _check_named_http_scope(tasks, state):
    if current_instance().project is not None and (
            tasks.get('manager_scope') != {'project': current_instance().project, 'port': current_instance().port}
            or tasks.get('manager_session') != state.session):
        raise ValueError('Authenticated desktop belongs to another manager scope or session')


class LocalAppState(TypedModel):
    version: Literal['1'] = '1'
    session: str = Field(pattern='^app-[a-f0-9]{32}$')
    mode: Literal['real', 'fixture']
    synthetic_staging: bool = False
    synthetic_learning: bool = False
    remote_entry_profile_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_entry_task_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_routes_plan_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_static_assets_plan_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_route_review_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_route_review_source_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_json_review_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_json_review_source_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_form_plan_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_form_field_name: str | None = Field(default=None, pattern='^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    remote_form_fields_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_form_state_plan_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    remote_form_cookie_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    owned_synthetic_form_invocation: bool = False
    owned_synthetic_form_recipe: bool = False
    owned_synthetic_parameter_project: bool = False
    owned_form_manifest_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    owned_form_invocation_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    owned_form_recipe_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    owned_parameter_project_directory: str | None = None
    owned_parameter_project_manifest_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    owned_parameter_project_runtime: Literal['fixture_cpu'] | None = None
    owned_skill_reuse_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')
    owned_skill_source_session: str | None = Field(default=None, pattern='^app-[a-f0-9]{32}$')
    phase: Literal['starting', 'running', 'stopped', 'failed']
    supervisor: ProcessIdentity
    backend: ProcessIdentity | None = None
    token_name: str | None = Field(default=None, pattern=r'^desktop-console-[a-f0-9]{16}\.token$')
    started_at: str
    project: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9-]{0,47}$')
    url: str = Field(default=URL, pattern=r'^http://127\.0\.0\.1:[0-9]{4,5}/ui/$')

    @model_serializer(mode='wrap')
    def retain_legacy_serialization(self, handler):
        value = handler(self)
        if self.project is None:
            value.pop('project', None)
        return value

    @model_validator(mode='after')
    def remote_entry_requires_real_paired_pins(self):
        if self.project is None:
            if self.url != 'http://127.0.0.1:8765/ui/':
                raise ValueError('Default manager retains its fixed loopback URL')
        elif self.url != LocalAppInstance.for_project(self.project, int(self.url.split(':')[-1].split('/')[0])).url:
            raise ValueError('Named project URL is not canonical')
        if ((self.owned_parameter_project_directory is None)
                != (self.owned_parameter_project_manifest_sha256 is None)
                or self.owned_synthetic_parameter_project != (self.owned_parameter_project_directory is not None)
                or self.owned_synthetic_parameter_project != (self.owned_parameter_project_runtime == 'fixture_cpu')
                or self.owned_parameter_project_directory is not None
                and (self.owned_synthetic_form_recipe or self.owned_synthetic_form_invocation
                     or self.owned_skill_reuse_sha256 is not None
                     or self.owned_form_manifest_sha256 != self.owned_parameter_project_manifest_sha256
                     or not Path(self.owned_parameter_project_directory).is_absolute()
                     or '..' in Path(self.owned_parameter_project_directory).parts
                     or Path(self.owned_parameter_project_directory).is_relative_to(REPO_ROOT))):
            raise ValueError('Owned parameter project requires its exact external recipe source')
        if ((self.owned_skill_reuse_sha256 is None)
                != (self.owned_skill_source_session is None)
                or self.owned_skill_reuse_sha256 is not None
                and (not self.owned_synthetic_form_invocation
                     or self.owned_synthetic_form_recipe
                     or self.owned_skill_source_session == self.session)):
            raise ValueError('Owned skill reuse requires a distinct retained v1 source')
        if ((self.remote_entry_profile_sha256 is None) != (self.remote_entry_task_sha256 is None)
                or self.remote_entry_profile_sha256 is not None and self.mode != 'real'
                or self.remote_routes_plan_sha256 is not None
                and self.remote_entry_task_sha256 is None
                or self.remote_static_assets_plan_sha256 is not None
                and self.remote_entry_task_sha256 is None
                or ((self.remote_route_review_sha256 is None)
                    != (self.remote_route_review_source_sha256 is None))
                or self.remote_route_review_sha256 is not None
                and self.remote_routes_plan_sha256 is None
                or ((self.remote_json_review_sha256 is None)
                    != (self.remote_json_review_source_sha256 is None))
                or self.remote_json_review_sha256 is not None
                and self.remote_static_assets_plan_sha256 is None
                or self.remote_form_plan_sha256 is None
                and (self.remote_form_field_name is not None
                     or self.remote_form_fields_sha256 is not None)
                or self.remote_form_plan_sha256 is not None
                and (self.remote_form_field_name is None)
                == (self.remote_form_fields_sha256 is None)
                or self.remote_form_plan_sha256 is not None
                and self.remote_entry_task_sha256 is None
                or self.remote_form_state_plan_sha256 is not None
                and self.remote_form_plan_sha256 is None
                or self.remote_form_cookie_sha256 is not None
                and self.remote_form_plan_sha256 is None
                or type(self.owned_synthetic_form_invocation) is not bool
                or type(self.owned_synthetic_form_recipe) is not bool
                or self.owned_synthetic_form_invocation and self.owned_synthetic_form_recipe
                or (self.owned_synthetic_form_recipe or self.owned_synthetic_parameter_project)
                and (self.mode != 'real' or self.synthetic_staging or self.synthetic_learning
                     or self.owned_form_manifest_sha256 is None
                     or self.owned_form_invocation_sha256 is None
                     or self.owned_form_recipe_sha256 is None
                     or any(value is not None for value in (
                         self.remote_entry_profile_sha256, self.remote_entry_task_sha256,
                         self.remote_routes_plan_sha256, self.remote_static_assets_plan_sha256,
                         self.remote_form_plan_sha256, self.remote_form_state_plan_sha256,
                         self.remote_form_cookie_sha256)))
                or self.owned_synthetic_form_invocation
                and (self.mode != 'real' or self.synthetic_staging or self.synthetic_learning
                     or self.owned_form_manifest_sha256 is None
                     or self.owned_form_invocation_sha256 is None
                     or any(value is not None for value in (
                         self.remote_entry_profile_sha256, self.remote_entry_task_sha256,
                         self.remote_routes_plan_sha256, self.remote_static_assets_plan_sha256,
                         self.remote_form_plan_sha256, self.remote_form_state_plan_sha256,
                         self.remote_form_cookie_sha256)))
                or not (self.owned_synthetic_form_invocation
                        or self.owned_synthetic_form_recipe or self.owned_synthetic_parameter_project)
                and (self.owned_form_manifest_sha256 is not None
                     or self.owned_form_invocation_sha256 is not None)
                or not (self.owned_synthetic_form_recipe or self.owned_synthetic_parameter_project)
                and self.owned_form_recipe_sha256 is not None):
            raise ValueError('Remote entry requires real mode and paired exact pins')
        return self


def private_read(path: Path, limit: int = 65536, *, append_log: bool = False) -> bytes:
    parent = open_existing_workspace(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1 or metadata.st_size > limit):
                raise ValueError('Private regular owner-only file required')
            content = os.read(descriptor, limit + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_nlink') if append_log else ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
            size_valid = (metadata.st_size <= len(content) <= limit and after.st_size >= len(content)
                          and linked.st_size >= len(content)) if append_log else len(content) == metadata.st_size
            if not size_valid or any(getattr(metadata, field) != getattr(current, field)
                                                     for current in (after, linked) for field in fields):
                raise ValueError('Private file changed')
            return content
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def prepare_base() -> None:
    parent = open_existing_workspace(current_instance().base.parent)
    try:
        try:
            os.mkdir(current_instance().base.name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    os.close(private_directory(current_instance().base))


def read_state() -> 'LocalAppState | SharedDesktopState | None':
    if not current_instance().base.exists():
        return None
    os.close(private_directory(current_instance().base))
    for attempt in range(3):
        try:
            raw = private_read(current_instance().base / 'current.json')
            header = json.loads(raw)
            if isinstance(header, dict) and header.get('version') == '2':
                from .shared_desktop_host import SharedDesktopState

                state = SharedDesktopState.model_validate_json(raw)
            else:
                state = LocalAppState.model_validate_json(raw)
            current_instance().check_state(state)
            return state
        except FileNotFoundError:
            return None
        except ValueError as error:
            if str(error) != 'Private file changed' or attempt == 2:
                raise
            time.sleep(.01)


def write_state(state: 'LocalAppState | SharedDesktopState') -> None:
    current_instance().check_state(state)
    parent = private_directory(current_instance().base)
    temporary = '.state-' + uuid4().hex
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(canonical(state.model_dump()))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, 'current.json', src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


@contextmanager
def instance_lock():
    parent = private_directory(current_instance().base)
    try:
        descriptor = os.open('manager.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError('Unsafe manager lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptor
    finally:
        os.close(descriptor)


def signal_owned(expected: ProcessIdentity, wait: float = 0) -> bool:
    descriptor = os.pidfd_open(expected.pid, 0)
    try:
        if select.select([descriptor], [], [], 0)[0] or process_identity(expected.pid) != expected:
            raise ValueError('Process identity changed; no signal sent')
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        return bool(select.select([descriptor], [], [], wait)[0]) if wait else True
    finally:
        os.close(descriptor)


def current_status() -> dict:
    state = read_state()
    if state is None:
        return {'phase': 'not_started', 'url': current_instance().url} | (
            {'manager_scope': {'project': current_instance().project, 'port': current_instance().port}}
            if current_instance().project is not None else {})
    if state.version == '2':
        from .shared_desktop_manager import status

        return status(state)
    supervisor = observe_process(state.supervisor)
    backend = observe_process(state.backend) if state.backend else 'not_recorded'
    phase = state.phase
    if phase in {'starting', 'running'} and supervisor != 'same_process':
        phase = 'needs_inspection'
    result = {'phase': phase, 'mode': state.mode, 'synthetic_staging': state.synthetic_staging,
              'synthetic_learning': state.synthetic_learning,
              'remote_entry_profile_sha256': state.remote_entry_profile_sha256,
              'remote_entry_task_sha256': state.remote_entry_task_sha256,
              'remote_routes_plan_sha256': state.remote_routes_plan_sha256,
              'remote_static_assets_plan_sha256': state.remote_static_assets_plan_sha256,
              'remote_route_review_sha256': state.remote_route_review_sha256,
              'remote_route_review_source_sha256': state.remote_route_review_source_sha256,
              'remote_json_review_sha256': state.remote_json_review_sha256,
              'remote_json_review_source_sha256': state.remote_json_review_source_sha256,
              'remote_form_plan_sha256': state.remote_form_plan_sha256,
              'remote_form_field_name': state.remote_form_field_name,
              'remote_form_fields_sha256': state.remote_form_fields_sha256,
              'remote_form_state_plan_sha256': state.remote_form_state_plan_sha256,
              'remote_form_cookie_sha256': state.remote_form_cookie_sha256,
              'owned_synthetic_form_invocation': state.owned_synthetic_form_invocation,
              'owned_synthetic_form_recipe': state.owned_synthetic_form_recipe,
              'owned_synthetic_parameter_project': state.owned_synthetic_parameter_project,
              'owned_form_manifest_sha256': state.owned_form_manifest_sha256,
              'owned_form_invocation_sha256': state.owned_form_invocation_sha256,
              'owned_form_recipe_sha256': state.owned_form_recipe_sha256,
              'owned_parameter_project_directory': state.owned_parameter_project_directory,
              'owned_parameter_project_manifest_sha256': state.owned_parameter_project_manifest_sha256,
              'owned_parameter_project_runtime': state.owned_parameter_project_runtime,
              'owned_parameter_project_real_model': False if state.owned_synthetic_parameter_project else None,
              'owned_skill_reuse_sha256': state.owned_skill_reuse_sha256,
              'owned_skill_source_session': state.owned_skill_source_session,
              'url': current_instance().url, 'session': state.session,
              'supervisor': supervisor, 'backend': backend, 'directory': str(current_instance().base / state.session)}
    if phase == 'running' and backend != 'same_process':
        result['phase'] = 'needs_inspection'
    if result['phase'] == 'running' and state.token_name:
        result['token_file'] = str(REPO_ROOT / 'runs' / state.token_name)
    if current_instance().project is not None:
        result['manager_scope'] = {'project': current_instance().project, 'port': current_instance().port}
    return result


def token_value(state: 'LocalAppState | SharedDesktopState') -> str:
    current_instance().check_state(state)
    if state.version == '2':
        from .shared_desktop_manager import token_value as shared_token_value

        return shared_token_value(state)
    if state.phase != 'running' or not state.token_name or observe_process(state.supervisor) != 'same_process':
        raise ValueError('No verified running local application')
    if state.backend is None or observe_process(state.backend) != 'same_process':
        raise ValueError('Backend no longer running')
    value = private_read(REPO_ROOT / 'runs' / state.token_name, 128).decode()
    if not re.fullmatch('[A-Za-z0-9_-]{43}', value):
        raise ValueError('Invalid local token')
    return value


def _session_readiness(state: LocalAppState, *, auto_login: bool) -> dict:
    instance = current_instance()
    instance.check_state(state)
    result = {'authenticated': False}
    if instance.project is not None:
        result['manager_scope'] = {'project': instance.project, 'port': instance.port}
        result['manager_session'] = state.session
    if auto_login:
        result['local_auto_login'] = True
    return result


def clean_shutdown(state: 'LocalAppState | SharedDesktopState') -> bool:
    if state.version == '2':
        from .shared_desktop_manager import clean_shutdown as shared_clean_shutdown

        return shared_clean_shutdown(state)
    if state.backend is None or state.token_name is None or (REPO_ROOT / 'runs' / state.token_name).exists():
        return False
    journals = current_instance().base / state.session / '.aos-lifecycle'
    try:
        os.close(private_directory(journals))
        paths = list(journals.iterdir())
        if not 1 <= len(paths) <= 10000:
            return False
        for path in paths:
            events, _digest = read_journal(path)
            if events[-1].stage != 'removed' or events[0].birth.process != state.backend:
                return False
    except (OSError, ValueError):
        return False
    return True


def prepare_remote_entry(mode: str, profile_sha256: str | None,
                         task_file: Path | None) -> tuple[bytes, str] | None:
    if (profile_sha256 is None) != (task_file is None):
        raise ValueError('Remote entry requires an exact profile hash and private task file together')
    if profile_sha256 is None:
        return None
    if mode != 'real' or re.fullmatch('[a-f0-9]{64}', profile_sha256) is None:
        raise ValueError('Managed remote entry requires real mode and an exact profile hash')
    from .web_application import WebApplicationProfiles, canonical_origin
    from .web_application_binding import WebTaskContract

    source = Path(task_file).absolute()
    if not source.is_relative_to(REPO_ROOT / 'data'):
        raise ValueError('Remote entry task must be private under ignored data')
    task = WebTaskContract.model_validate_json(private_read(source))
    profile = WebApplicationProfiles(WEB_PROFILES).get(profile_sha256)
    if (profile.environment not in {'staging', 'production'}
            or canonical_origin(profile.entry_url)[1]
            or task.profile_sha256 != profile_sha256
            or task.entry_url != profile.entry_url
            or task.task_key not in profile.task_keys
            or not set(task.allowed_origins).issubset(profile.allowed_origins)
            or canonical_origin(task.entry_url)[0] not in task.allowed_origins
            or 'browser.navigate' not in task.tools):
        raise ValueError('Remote entry task differs from its stored profile')
    content = canonical(task.model_dump(mode='json')).encode()
    return content, hashlib.sha256(content).hexdigest()


def prepare_remote_routes(mode: str, profile_sha256: str | None,
                          task_file: Path | None, plan_file: Path | None) -> tuple[bytes, str] | None:
    if plan_file is None:
        return None
    remote_entry = prepare_remote_entry(mode, profile_sha256, task_file)
    if remote_entry is None:
        raise ValueError('Read-only routes require an exact private task and profile')
    source = Path(plan_file).absolute()
    if not source.is_relative_to(REPO_ROOT / 'data'):
        raise ValueError('Read-only route plan must be private under ignored data')
    from .web_application import WebApplicationProfiles
    from .web_application_binding import (WebReadOnlyRoutePlan, WebTaskContract,
                                          verify_web_readonly_routes)

    plan = WebReadOnlyRoutePlan.model_validate_json(private_read(source, 20000))
    task = WebTaskContract.model_validate_json(remote_entry[0])
    verify_web_readonly_routes(WebApplicationProfiles(WEB_PROFILES), task, plan)
    content = canonical(plan.model_dump(mode='json')).encode()
    return content, hashlib.sha256(content).hexdigest()


def prepare_managed_remote_route_review(
        mode: str, profile_sha256: str | None, plan_sha256: str | None,
        source_database: Path | None, site_store: Path | None,
        review_store: Path | None, review_sha256: str | None):
    options = (source_database, site_store, review_store, review_sha256)
    if all(option is None for option in options):
        return None
    if (mode != 'real' or profile_sha256 is None or plan_sha256 is None
            or not all(option is not None for option in options)):
        raise ValueError('Managed route review requires real mode, route plan and four exact options')
    from .remote_route_knowledge_review import prepare_live_remote_route_knowledge

    sources = [Path(source).absolute() for source in options[:3]]
    if not all(source.is_relative_to(REPO_ROOT / 'data') for source in sources):
        raise ValueError('Managed route review sources must stay under private data')
    try:
        metadata = sources[0].lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
            raise ValueError('Managed route review database must be owner-only')
        pin = prepare_live_remote_route_knowledge(
            sources[0], profiles=WEB_PROFILES, site_store=sources[1],
            review_store=sources[2], review_sha256=review_sha256,
            selected_profile_sha256=profile_sha256, selected_plan_sha256=plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
        raise ValueError('Managed route review source is unavailable or stale') from error
    source_sha256 = digest({
        'database': str(sources[0]), 'site_store': str(sources[1]),
        'review_store': str(sources[2]), 'review_sha256': review_sha256,
        'source_snapshot_sha256': pin.source_snapshot_sha256})
    return (sources[0], sources[1], sources[2], review_sha256,
            pin.source_snapshot_sha256, source_sha256)


def prepare_managed_remote_json_review(
        mode: str, profile_sha256: str | None, plan_sha256: str | None,
        source_database: Path | None, site_store: Path | None,
        review_store: Path | None, review_sha256: str | None):
    options = (source_database, site_store, review_store, review_sha256)
    if all(option is None for option in options):
        return None
    if (mode != 'real' or profile_sha256 is None or plan_sha256 is None
            or not all(option is not None for option in options)):
        raise ValueError('Managed JSON review requires real mode, v2 plan and four exact options')
    from .remote_readonly_data_knowledge_review import (
        prepare_live_remote_readonly_data_knowledge)

    sources = [Path(source).absolute() for source in options[:3]]
    if not all(source.is_relative_to(REPO_ROOT / 'data') for source in sources):
        raise ValueError('Managed JSON review sources must stay under private data')
    try:
        metadata = sources[0].lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
            raise ValueError('Managed JSON review database must be owner-only')
        pin = prepare_live_remote_readonly_data_knowledge(
            sources[0], profiles=WEB_PROFILES, site_store=sources[1],
            review_store=sources[2], review_sha256=review_sha256,
            selected_profile_sha256=profile_sha256,
            selected_plan_sha256=plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
        raise ValueError('Managed JSON review source is unavailable or stale') from error
    source_sha256 = digest({
        'database': str(sources[0]), 'site_store': str(sources[1]),
        'review_store': str(sources[2]), 'review_sha256': review_sha256,
        'source_snapshot_sha256': pin.source_snapshot_sha256})
    return (sources[0], sources[1], sources[2], review_sha256,
            pin.source_snapshot_sha256, source_sha256)


def write_new_private_plan(destination: Path, content: bytes) -> None:
    parent = private_directory(destination.parent)
    try:
        descriptor = os.open(destination.name,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def plan_remote_routes(profile_sha256: str | None, task_file: Path | None,
                       plan_file: Path | None, routes_file: Path | None) -> dict:
    remote_entry = prepare_remote_entry('real', profile_sha256, task_file)
    if remote_entry is None or plan_file is None or routes_file is None:
        raise ValueError('Read-only route planning requires exact profile, task, source and destination')
    destination = Path(plan_file).absolute()
    source = Path(routes_file).absolute()
    if (not destination.is_relative_to(REPO_ROOT / 'data')
            or not source.is_relative_to(REPO_ROOT / 'data')):
        raise ValueError('Read-only route files must remain under ignored data')
    from .contracts import digest
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract, plan_web_readonly_routes

    content = private_read(source, 16384)
    if not content.endswith(b'\n') or b'\r' in content:
        raise ValueError('Read-only route source must contain one canonical URL per line')
    routes = [line.decode('utf-8') for line in content[:-1].split(b'\n')]
    task = WebTaskContract.model_validate_json(remote_entry[0])
    plan = plan_web_readonly_routes(WebApplicationProfiles(WEB_PROFILES), task, routes)
    write_new_private_plan(destination, canonical(plan.model_dump(mode='json')).encode())
    return {'profile_sha256': profile_sha256, 'plan_sha256': digest(plan.model_dump()),
            'routes': plan.routes, 'status': 'draft',
            'execution_authorized': False, 'collection_authorized': False}


def prepare_remote_static_assets(mode: str, profile_sha256: str | None,
                                 task_file: Path | None,
                                 plan_file: Path | None) -> tuple[bytes, str] | None:
    if plan_file is None:
        return None
    remote_entry = prepare_remote_entry(mode, profile_sha256, task_file)
    if remote_entry is None:
        raise ValueError('Static assets require an exact private task and profile')
    source = Path(plan_file).absolute()
    if not source.is_relative_to(REPO_ROOT / 'data'):
        raise ValueError('Static asset plan must be private under ignored data')
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract
    from .web_static_assets import WebStaticAssetPlan
    from .web_readonly_data import WebReadOnlyDataBundlePlan, verify_web_bundle_plan

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Web bundle plan has a duplicate JSON field')
            value[key] = item
        return value

    document = json.loads(private_read(source, 32000), object_pairs_hook=unique_pairs)
    if not isinstance(document, dict):
        raise ValueError('Web bundle plan must be an object')
    plan_type = {'1.0': WebStaticAssetPlan,
                 '2.0': WebReadOnlyDataBundlePlan}.get(document.get('schema_version'))
    if plan_type is None:
        raise ValueError('Unsupported web bundle plan version')
    plan = plan_type.model_validate(document)
    task = WebTaskContract.model_validate_json(remote_entry[0])
    verify_web_bundle_plan(WebApplicationProfiles(WEB_PROFILES), task, plan)
    content = canonical(plan.model_dump(mode='json')).encode()
    return content, hashlib.sha256(content).hexdigest()


def plan_remote_static_assets(profile_sha256: str | None, task_file: Path | None,
                              plan_file: Path | None, assets_file: Path | None) -> dict:
    remote_entry = prepare_remote_entry('real', profile_sha256, task_file)
    if remote_entry is None or plan_file is None or assets_file is None:
        raise ValueError('Static asset planning requires exact profile, task, source and destination')
    destination = Path(plan_file).absolute()
    source = Path(assets_file).absolute()
    if (not destination.is_relative_to(REPO_ROOT / 'data')
            or not source.is_relative_to(REPO_ROOT / 'data')):
        raise ValueError('Static asset files must remain under ignored data')
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract
    from .web_static_assets import plan_web_static_assets

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Static asset source has a duplicate JSON field')
            value[key] = item
        return value

    assets = json.loads(private_read(source, 20000), object_pairs_hook=unique_pairs)
    if not isinstance(assets, list):
        raise ValueError('Static asset source must be a JSON list')
    task = WebTaskContract.model_validate_json(remote_entry[0])
    plan = plan_web_static_assets(WebApplicationProfiles(WEB_PROFILES), task, assets)
    write_new_private_plan(destination, canonical(plan.model_dump(mode='json')).encode())
    return {'profile_sha256': profile_sha256, 'plan_sha256': digest(plan.model_dump()),
            'asset_count': len(plan.assets), 'status': 'draft',
            'execution_authorized': False, 'collection_authorized': False}


def plan_remote_readonly_data(profile_sha256: str | None, task_file: Path | None,
                              plan_file: Path | None, source_file: Path | None) -> dict:
    remote_entry = prepare_remote_entry('real', profile_sha256, task_file)
    if remote_entry is None or plan_file is None or source_file is None:
        raise ValueError('Read-only data planning requires exact profile, task, source and destination')
    destination = Path(plan_file).absolute()
    source = Path(source_file).absolute()
    if (not destination.is_relative_to(REPO_ROOT / 'data')
            or not source.is_relative_to(REPO_ROOT / 'data')
            or destination == source):
        raise ValueError('Read-only data files must remain under ignored data')
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract
    from .web_readonly_data import plan_web_readonly_data_bundle

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Read-only data source has a duplicate JSON field')
            value[key] = item
        return value

    document = json.loads(private_read(source, 32000), object_pairs_hook=unique_pairs)
    if not isinstance(document, dict) or set(document) != {'assets', 'data_resources'}:
        raise ValueError('Read-only data source requires only assets and data_resources')
    task = WebTaskContract.model_validate_json(remote_entry[0])
    plan = plan_web_readonly_data_bundle(WebApplicationProfiles(WEB_PROFILES), task,
                                         document['assets'], document['data_resources'])
    write_new_private_plan(destination, canonical(plan.model_dump(mode='json')).encode())
    return {'profile_sha256': profile_sha256, 'plan_sha256': digest(plan.model_dump()),
            'asset_count': len(plan.assets), 'data_count': len(plan.data_resources),
            'status': 'draft', 'execution_authorized': False,
            'collection_authorized': False}


def prepare_remote_form(mode: str, profile_sha256: str | None,
                        task_file: Path | None, plan_file: Path | None,
                        field_name: str | None, value_file: Path | None,
                        public_plan_sha256: str | None,
                        *, fields_file: Path | None = None) -> tuple[bytes, str, bytes] | None:
    options = (plan_file, field_name, value_file, public_plan_sha256, fields_file)
    if all(option is None for option in options):
        return None
    if (plan_file is None or public_plan_sha256 is None
            or (fields_file is None and (field_name is None or value_file is None))
            or (fields_file is not None and (field_name is not None or value_file is not None))
            or field_name is not None
            and re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}', field_name) is None):
        raise ValueError('Public HTTPS form requires one private input mode and exact grant')
    remote_entry = prepare_remote_entry(mode, profile_sha256, task_file)
    if remote_entry is None:
        raise ValueError('Public HTTPS form requires a registered task and profile')
    plan_source = Path(plan_file).absolute()
    value_source = Path(value_file if fields_file is None else fields_file).absolute()
    if (not plan_source.is_relative_to(REPO_ROOT / 'data')
            or not value_source.is_relative_to(REPO_ROOT / 'data')):
        raise ValueError('Public HTTPS form files must be private under ignored data')
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract
    from .web_https_form_transport import (WebHTTPSFormPlan, exact_form_fields,
                                           form_body, parse_form_fields_document,
                                           verify_web_https_form_plan,
                                           verify_web_https_form_target_grant)

    plan = WebHTTPSFormPlan.model_validate_json(private_read(plan_source, 20000))
    task = WebTaskContract.model_validate_json(remote_entry[0])
    profiles = WebApplicationProfiles(WEB_PROFILES)
    verify_web_https_form_plan(profiles, task, plan)
    if verify_web_https_form_target_grant(plan, public_plan_sha256) is not True:
        raise ValueError('Managed HTTPS form requires a public target')
    value_bytes = private_read(value_source, 8192 if fields_file is not None else 4096)
    if fields_file is None:
        value = value_bytes.decode('utf-8')
        if not value.isprintable():
            raise ValueError('Private form value is not printable')
        fields = exact_form_fields(field_name, value)
    else:
        fields = parse_form_fields_document(value_bytes)
    body = form_body(fields)
    if (len(body) != plan.body_bytes
            or hashlib.sha256(body).hexdigest() != plan.body_sha256):
        raise ValueError('Private form value differs from exact public plan')
    content = canonical(plan.model_dump(mode='json')).encode()
    return content, hashlib.sha256(content).hexdigest(), value_bytes


def plan_remote_form_fields(source_file: Path | None,
                            fields_file: Path | None) -> dict:
    if source_file is None or fields_file is None:
        raise ValueError('Ordered form fields require private source and destination')
    source = Path(source_file).absolute()
    destination = Path(fields_file).absolute()
    if (not source.is_relative_to(REPO_ROOT / 'data')
            or not destination.is_relative_to(REPO_ROOT / 'data')
            or source == destination):
        raise ValueError('Ordered form fields must remain under ignored private data')
    from .web_https_form_transport import exact_form_fields, form_body

    def unique_object(pairs):
        if len({key for key, _value in pairs}) != len(pairs):
            raise ValueError('Ordered form source has duplicate JSON keys')
        return dict(pairs)

    fields = exact_form_fields(None, None, json.loads(
        private_read(source, 8192).decode('utf-8'), object_pairs_hook=unique_object))
    if any(not value.isprintable() for _name, value in fields):
        raise ValueError('Ordered form values must be printable single-line text')
    body = form_body(fields)
    document = {'schema_version': '1.0',
                'fields': [{'name': name, 'value': value} for name, value in fields],
                'body_sha256': hashlib.sha256(body).hexdigest(),
                'body_bytes': len(body), 'execution_authorized': False,
                'collection_authorized': False}
    content = canonical(document).encode()
    write_new_private_plan(destination, content)
    return {'field_names': [name for name, _value in fields],
            'fields_sha256': hashlib.sha256(content).hexdigest(),
            'body_sha256': document['body_sha256'], 'body_bytes': len(body),
            'status': 'draft', 'execution_authorized': False,
            'collection_authorized': False}


def plan_remote_form(profile_sha256: str | None, task_file: Path | None,
                     plan_file: Path | None, field_name: str | None,
                     value_file: Path | None, submit_url: str | None,
                     receipt_url: str | None, *, fields_file: Path | None = None) -> dict:
    remote_entry = prepare_remote_entry('real', profile_sha256, task_file)
    if (remote_entry is None or plan_file is None
            or submit_url is None or receipt_url is None
            or (fields_file is None and (field_name is None or value_file is None))
            or (fields_file is not None and (field_name is not None or value_file is not None))
            or field_name is not None
            and re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}', field_name) is None):
        raise ValueError('Public form planning requires exact profile, task, URLs and private input')
    destination = Path(plan_file).absolute()
    source = Path(value_file if fields_file is None else fields_file).absolute()
    if (not destination.is_relative_to(REPO_ROOT / 'data')
            or not source.is_relative_to(REPO_ROOT / 'data')):
        raise ValueError('Public form plan and value must remain under ignored data')
    from .contracts import digest
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract
    from .web_https_form_transport import (exact_form_fields, form_body,
                                           parse_form_fields_document, plan_web_https_form,
                                           verify_web_https_form_target_grant)

    if fields_file is None:
        value = private_read(source, 4096).decode('utf-8')
        if not value.isprintable():
            raise ValueError('Private form value is not printable')
        fields = exact_form_fields(field_name, value)
    else:
        fields = parse_form_fields_document(private_read(source, 8192))
    body = form_body(fields)
    task = WebTaskContract.model_validate_json(remote_entry[0])
    plan = plan_web_https_form(WebApplicationProfiles(WEB_PROFILES), task,
                               submit_url=submit_url, receipt_url=receipt_url,
                               body_sha256=hashlib.sha256(body).hexdigest(),
                               body_bytes=len(body))
    plan_sha256 = digest(plan.model_dump())
    if verify_web_https_form_target_grant(plan, plan_sha256) is not True:
        raise ValueError('Managed HTTPS form planning requires a public target')
    content = canonical(plan.model_dump(mode='json')).encode()
    write_new_private_plan(destination, content)
    return {'profile_sha256': profile_sha256, 'plan_sha256': plan_sha256,
            'entry_url': plan.entry_url, 'submit_url': plan.submit_url,
            'receipt_url': plan.receipt_url,
            **({'field_name': field_name} if fields_file is None else
               {'field_names': [name for name, _value in fields]}),
            'body_sha256': plan.body_sha256, 'status': 'draft',
            'execution_authorized': False, 'collection_authorized': False}


def prepare_remote_form_state(mode: str, profile_sha256: str | None,
                              task_file: Path | None, form_plan_file: Path | None,
                              field_name: str | None, value_file: Path | None,
                              form_grant_sha256: str | None,
                              state_plan_file: Path | None,
                              state_grant_sha256: str | None,
                              *, fields_file: Path | None = None) -> tuple[bytes, str] | None:
    if state_plan_file is None and state_grant_sha256 is None:
        return None
    if state_plan_file is None or state_grant_sha256 is None:
        raise ValueError('HTTPS form state requires a private plan and separate exact grant')
    prepared_form = prepare_remote_form(mode, profile_sha256, task_file, form_plan_file,
                                        field_name, value_file, form_grant_sha256,
                                        fields_file=fields_file)
    if prepared_form is None:
        raise ValueError('HTTPS form state requires an exact form plan')
    source = Path(state_plan_file).absolute()
    if not source.is_relative_to(REPO_ROOT / 'data'):
        raise ValueError('HTTPS form state plan must remain under ignored data')
    import ssl
    from .contracts import digest
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract
    from .web_https_form_transport import WebHTTPSFormPlan
    from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                             verify_submitted_field_binding,
                                             verify_web_https_form_state_plan)

    content = private_read(source, 20000)
    plan = WebHTTPSFormStatePlan.model_validate_json(content)
    canonical_content = canonical(plan.model_dump(mode='json')).encode()
    if content != canonical_content:
        raise ValueError('HTTPS form state plan is not canonical')
    form_plan = WebHTTPSFormPlan.model_validate_json(prepared_form[0])
    task = WebTaskContract.model_validate_json(prepare_remote_entry(
        mode, profile_sha256, task_file)[0])
    verify_web_https_form_state_plan(
        WebApplicationProfiles(WEB_PROFILES), task, form_plan, plan,
        ssl.create_default_context(),
        confirm_public_form_plan_sha256=form_grant_sha256,
        confirm_public_state_plan_sha256=state_grant_sha256)
    if plan.submitted_field_name is not None:
        from .web_https_form_transport import exact_form_fields, parse_form_fields_document

        fields = (exact_form_fields(field_name, prepared_form[2].decode('utf-8'))
                  if fields_file is None else parse_form_fields_document(prepared_form[2]))
        verify_submitted_field_binding(plan, form_plan, fields)
    return content, digest(plan.model_dump())


def plan_remote_form_state(profile_sha256: str | None, task_file: Path | None,
                           form_plan_file: Path | None, field_name: str | None,
                           value_file: Path | None, form_grant_sha256: str | None,
                           state_plan_file: Path | None, state_url: str | None,
                           before_sha256: str | None, after_sha256: str | None,
                           marker_id: str | None = None,
                           before_marker_sha256: str | None = None,
                           after_marker_sha256: str | None = None,
                           submitted_field_name: str | None = None,
                           *, fields_file: Path | None = None) -> dict:
    if (state_plan_file is None or state_url is None or before_sha256 is None
            or after_sha256 is None):
        raise ValueError('HTTPS form state planning requires destination, URL and two response hashes')
    prepared_form = prepare_remote_form('real', profile_sha256, task_file, form_plan_file,
                                        field_name, value_file, form_grant_sha256,
                                        fields_file=fields_file)
    if prepared_form is None:
        raise ValueError('HTTPS form state planning requires an exact public form')
    destination = Path(state_plan_file).absolute()
    if not destination.is_relative_to(REPO_ROOT / 'data'):
        raise ValueError('HTTPS form state plan must remain under ignored data')
    import ssl
    from .contracts import digest
    from .web_application import WebApplicationProfiles
    from .web_application_binding import WebTaskContract
    from .web_https_form_transport import ExactHTTPSFormTransport, WebHTTPSFormPlan
    from .web_https_form_state_probe import (plan_web_https_form_state,
                                             verify_submitted_field_binding)

    task = WebTaskContract.model_validate_json(prepare_remote_entry(
        'real', profile_sha256, task_file)[0])
    form_plan = WebHTTPSFormPlan.model_validate_json(prepared_form[0])
    transport = ExactHTTPSFormTransport(
        WebApplicationProfiles(WEB_PROFILES), task, form_plan, prepared_form[1],
        consume_approval=lambda _request_sha256: False,
        tls_context=ssl.create_default_context(),
        confirm_public_plan_sha256=form_grant_sha256)
    plan = plan_web_https_form_state(
        transport, state_url=state_url,
        expected_before_sha256=before_sha256,
        expected_after_sha256=after_sha256,
        marker_id=marker_id,
        expected_before_marker_sha256=before_marker_sha256,
        expected_after_marker_sha256=after_marker_sha256,
        submitted_field_name=submitted_field_name)
    if submitted_field_name is not None:
        from .web_https_form_transport import exact_form_fields, parse_form_fields_document

        fields = (exact_form_fields(field_name, prepared_form[2].decode('utf-8'))
                  if fields_file is None else parse_form_fields_document(prepared_form[2]))
        verify_submitted_field_binding(plan, form_plan, fields)
    write_new_private_plan(destination, canonical(plan.model_dump(mode='json')).encode())
    return {'profile_sha256': profile_sha256, 'form_plan_sha256': prepared_form[1],
            'state_plan_sha256': digest(plan.model_dump()), 'state_url': plan.state_url,
            'status': 'draft', 'execution_authorized': False,
            'collection_authorized': False}


def prepare_remote_form_cookie(mode: str, profile_sha256: str | None,
                               task_file: Path | None, form_plan_file: Path | None,
                               field_name: str | None, value_file: Path | None,
                               form_grant_sha256: str | None,
                               cookie_file: Path | None, cookie_sha256: str | None,
                               *, require_confirmation: bool = True,
                               fields_file: Path | None = None
                               ) -> tuple[bytes, str] | None:
    if cookie_file is None and cookie_sha256 is None:
        return None
    if (cookie_file is None or require_confirmation and cookie_sha256 is None
            or not require_confirmation and cookie_sha256 is not None):
        raise ValueError('HTTPS form cookie requires a private file and exact hash')
    if prepare_remote_form(mode, profile_sha256, task_file, form_plan_file,
                           field_name, value_file, form_grant_sha256,
                           fields_file=fields_file) is None:
        raise ValueError('HTTPS form cookie requires an exact public form')
    source = Path(cookie_file).absolute()
    if not source.is_relative_to(REPO_ROOT / 'data'):
        raise ValueError('HTTPS form cookie must remain under ignored data')
    from .web_https_preflight import validate_https_cookie_header

    content = private_read(source, 2048)
    validate_https_cookie_header(content.decode('ascii'))
    checksum = hashlib.sha256(content).hexdigest()
    if cookie_sha256 is not None and checksum != cookie_sha256:
        raise ValueError('HTTPS form cookie differs from exact hash')
    return content, checksum


def write_private_remote_file(directory: Path, name: str, content: bytes) -> None:
    if name not in {'remote-entry-task.json', 'remote-routes-plan.json',
                    'remote-static-assets-plan.json',
                    'remote-form-plan.json', 'remote-form-value.txt',
                    'remote-form-fields.json',
                    'remote-form-state-plan.json', 'remote-form-cookie.txt'}:
        raise ValueError('Unexpected remote file name')
    descriptor = os.open(directory / name,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def retire_remote_form_value(directory: Path) -> None:
    descriptor = private_directory(directory)
    try:
        for name in ('remote-form-value.txt', 'remote-form-fields.json'):
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def retire_remote_form_cookie(directory: Path) -> None:
    descriptor = private_directory(directory)
    try:
        try:
            os.unlink('remote-form-cookie.txt', dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_remote_entry_task(directory: Path, content: bytes) -> None:
    write_private_remote_file(directory, 'remote-entry-task.json', content)


def require_local_preflight(mode: str) -> None:
    from .local_preflight import check_local

    report = (check_local(mode, frontend_root=current_instance().base / 'ui')
              if current_instance().project is not None else check_local(mode))
    if not report['ready']:
        failures = '; '.join(item['detail'] + ' ' + item['action']
                             for item in report['checks'] if not item['ok'])
        raise ValueError('Ön kontrol başarısız: ' + failures)


def _frontend_source_snapshot(root=None):
    root = REPO_ROOT / 'ui' if root is None else Path(root)
    pending = [root]
    files = {}
    consumed = 0
    while pending:
        directory = pending.pop()
        if directory.is_symlink():
            raise ValueError('Frontend source directories cannot be symlinks')
        for path in directory.iterdir():
            if path.is_symlink() and (directory != root or path.name != 'node_modules'):
                raise ValueError('Frontend source links are not accepted')
            if path.is_dir():
                if directory != root or path.name in {'src', 'public'}:
                    pending.append(path)
                continue
            if len(files) >= 1024:
                raise ValueError('Frontend source inventory exceeds its bound')
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size > 2097152:
                    raise ValueError('Frontend source file exceeds its regular-file bound')
                content = bytearray()
                while len(content) <= before.st_size:
                    part = os.read(descriptor, min(65536, before.st_size + 1 - len(content)))
                    if not part:
                        break
                    content.extend(part)
                after = os.fstat(descriptor)
                linked = path.stat(follow_symlinks=False)
                fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
                if (len(content) != before.st_size or any(getattr(before, name) != getattr(current, name)
                        for current in (after, linked) for name in fields)):
                    raise ValueError('Frontend source changed during bounded readback')
                consumed += len(content)
                if consumed > 16777216:
                    raise ValueError('Frontend source bytes exceed their total bound')
                files[str(path.relative_to(root))] = hashlib.sha256(content).hexdigest()
            finally:
                os.close(descriptor)
    if not {'package.json', 'pnpm-lock.yaml', 'index.html', 'src/main.tsx'} <= files.keys():
        raise ValueError('Frontend source inventory is incomplete')
    return files


def _ui_prepare_idle(state):
    if read_state() != state:
        raise ValueError('Project state changed during UI preparation')
    if state is None:
        return
    if (state.phase != 'stopped' or state.backend is None
            or observe_process(state.supervisor) != 'not_observed'
            or observe_process(state.backend) != 'not_observed'):
        raise ValueError('UI preparation requires a stopped project with both processes absent')
    log = private_read(current_instance().base / state.session / 'backend.log', append_log=True).decode('utf-8')
    tokens = re.findall(r'^Local token file \(0600\): ([^\n]+)$', log, re.MULTILINE)
    if len(tokens) != 1:
        raise ValueError('Stopped UI preparation requires one exact retired token record')
    token = Path(tokens[0])
    if (token.parent != REPO_ROOT / 'runs'
            or re.fullmatch(r'desktop-console-[a-f0-9]{16}\.token', token.name) is None
            or state.token_name is not None and state.token_name != token.name
            or not clean_shutdown(state.model_copy(update={'token_name': token.name}))):
        raise ValueError('Project cleanup is not proven; UI preparation is unavailable')


def _publish_project_ui(directory_fd, staging_name):
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, 'renameat2', None)
    if rename is None:
        raise ValueError('Atomic no-replace UI publication is unavailable on this host')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(directory_fd, os.fsencode(staging_name), directory_fd, b'ui', 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))
    os.fsync(directory_fd)


def prepare_ui() -> dict:
    instance = current_instance()
    if instance.project is None:
        raise ValueError('prepare-ui requires an explicit named project and loopback port')
    initial = read_state()
    _ui_prepare_idle(initial)
    prepare_base()
    with instance_lock():
        _ui_prepare_idle(initial)
        directory_fd = private_directory(instance.base)
        try:
            if os.path.lexists(instance.base / 'ui'):
                raise ValueError('Project UI already exists; prepare-ui never overwrites or updates it')
            sources = _frontend_source_snapshot()
            executable = REPO_ROOT / 'ui/node_modules/vite/bin/vite.js'
            if (not Path('/usr/bin/node').is_file() or not executable.is_file()
                    or not executable.resolve().is_relative_to(REPO_ROOT / 'ui/node_modules')):
                raise ValueError('Installed local Node/Vite is required; no dependency installation is attempted')
            staging_name = '.prepare-ui-' + uuid4().hex
            os.mkdir(staging_name, 0o700, dir_fd=directory_fd)
            staging = instance.base / staging_name
            staging_fd = private_directory(staging)
            try:
                before = os.fstat(staging_fd)
                environment = {'PATH': '/usr/bin:/bin', 'HOME': str(instance.base), 'NODE_ENV': 'production',
                    'npm_config_offline': 'true', 'COREPACK_ENABLE_NETWORK': '0', 'CUDA_VISIBLE_DEVICES': '',
                    'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
                result = run_bounded(['/usr/bin/node', str(executable), 'build', str(REPO_ROOT / 'ui'),
                    '--outDir', str(staging)], input=b'', env=environment, timeout=30, max_output=262144)
                log_descriptor = os.open(staging_name + '.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory_fd)
                with os.fdopen(log_descriptor, 'wb') as stream:
                    stream.write(result.stdout + b'\nSTDERR\n' + result.stderr)
                    stream.flush()
                    os.fsync(stream.fileno())
                if result.returncode != 0:
                    raise ValueError('UI builder failed; inspect private staging directory ' + str(staging))
                from .local_preflight import _frontend

                _frontend(staging)
                _ui_prepare_idle(initial)
                if _frontend_source_snapshot() != sources:
                    raise ValueError('Frontend sources changed during UI preparation; staged output is not published')
                current = staging.stat(follow_symlinks=False)
                if (not stat.S_ISDIR(current.st_mode) or stat.S_IMODE(current.st_mode) != 0o700
                        or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
                    raise ValueError('Private UI staging identity changed')
                current_base = private_directory(instance.base)
                try:
                    if (os.fstat(current_base).st_dev, os.fstat(current_base).st_ino) != (
                            os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino):
                        raise ValueError('Project root changed during UI preparation')
                finally:
                    os.close(current_base)
                _ui_prepare_idle(initial)
                _publish_project_ui(directory_fd, staging_name)
                return {'status': 'prepared', 'manager_scope': {'project': instance.project, 'port': instance.port},
                        'ui_root': str(instance.base / 'ui'), 'source_sha256': digest(sources),
                        'runtime_started': False, 'execution_authorized': False}
            finally:
                os.close(staging_fd)
        finally:
            os.close(directory_fd)


def prepare_owned_skill_reuse(previous, release_sha256, selection_sha256, *, base=None):
    from .owned_skill_reuse import preview_owned_skill_reuse

    base = current_instance().base if base is None else Path(base).absolute()
    if (previous is None or base.parent != REPO_ROOT / 'data'
            or re.fullmatch(r'local-app-(?:v1|test-[a-f0-9]{32}|project-[a-z0-9][a-z0-9-]{0,47})', base.name) is None):
        raise ValueError('Owned skill reuse requires a stopped managed source')
    if previous.project is not None or current_instance().project is not None:
        current_instance().check_state(previous)
        if base != current_instance().base:
            raise ValueError('Owned skill reuse belongs to another project base')
    source_session = previous.owned_skill_source_session or previous.session
    source_directory = base / source_session
    result = preview_owned_skill_reuse(
        previous_session_directory=base / previous.session,
        previous_state=previous, owned_root=source_directory / 'owned-form',
        database=source_directory / 'store.sqlite',
        manifest_sha256=previous.owned_form_manifest_sha256,
        release_sha256=release_sha256, selection_sha256=selection_sha256)
    return result


def load_owned_skill_reuse_startup(directory: Path, preview_sha256: str):
    directory = Path(directory).absolute()
    if (re.fullmatch(r'app-[a-f0-9]{32}', directory.name) is None
            or not isinstance(preview_sha256, str)
            or re.fullmatch(r'[a-f0-9]{64}', preview_sha256) is None):
        raise ValueError('Owned skill reuse preview pin invalid')
    os.close(private_directory(directory))
    previous_bytes = private_read(directory / 'owned-skill-reuse-previous-state.json')
    previous = LocalAppState.model_validate_json(previous_bytes)
    if canonical(previous.model_dump()).encode() != previous_bytes:
        raise ValueError('Owned skill reuse previous state is not canonical')
    preview_bytes = private_read(directory / 'owned-skill-reuse-preview.json')
    preview = json.loads(preview_bytes)
    if (canonical(preview).encode() != preview_bytes
            or hashlib.sha256(preview_bytes).hexdigest() != preview_sha256):
        raise ValueError('Owned skill reuse preview changed')
    result = prepare_owned_skill_reuse(previous, preview['release_sha256'],
                                      preview['selection_sha256'], base=directory.parent)
    if result['preview_sha256'] != preview_sha256 or result['preview'] != preview:
        raise ValueError('Owned skill reuse source changed')
    source_session = previous.owned_skill_source_session or previous.session
    return result, previous, directory.parent / source_session


def prepare_owned_parameter_project(directory: Path | None, manifest_sha256: str | None):
    if directory is None and manifest_sha256 is None:
        return None
    if (directory is None or not isinstance(manifest_sha256, str)
            or re.fullmatch('[a-f0-9]{64}', manifest_sha256) is None):
        raise ValueError('Owned parameter project requires an absolute directory and exact manifest pin')
    root = Path(directory)
    if not root.is_absolute() or '..' in root.parts or root.is_relative_to(REPO_ROOT):
        raise ValueError('Owned parameter project must be private and outside the checkout')
    from .owned_parameter_project import read_owned_parameter_project

    bundle = read_owned_parameter_project(root, manifest_sha256)
    if bundle['manifest']['port'] in {8765, current_instance().port}:
        raise ValueError('Owned parameter project port conflicts with the local application')
    return bundle


def start(mode: str = 'real', *, check: bool = True, expected_previous_session: str | None = None,
          expected_previous_state: LocalAppState | None = None,
          synthetic_staging: bool = False,
          synthetic_learning: bool = False,
          owned_synthetic_form_invocation: bool = False,
          owned_synthetic_form_recipe: bool = False,
          owned_parameter_project_directory: Path | None = None,
          owned_parameter_project_manifest_sha256: str | None = None,
          owned_parameter_project_engine: str | None = None,
          owned_skill_release_sha256: str | None = None,
          owned_skill_selection_sha256: str | None = None,
          owned_skill_reuse_confirm_sha256: str | None = None,
          remote_entry_profile_sha256: str | None = None,
          remote_entry_task_file: Path | None = None,
          remote_routes_plan_file: Path | None = None,
          remote_static_assets_plan_file: Path | None = None,
          remote_readonly_data_plan_file: Path | None = None,
          remote_route_review_source_database: Path | None = None,
          remote_route_review_site_store: Path | None = None,
          remote_route_review_store: Path | None = None,
          remote_route_review_sha256: str | None = None,
          remote_json_review_source_database: Path | None = None,
          remote_json_review_site_store: Path | None = None,
          remote_json_review_store: Path | None = None,
          remote_json_review_sha256: str | None = None,
          remote_form_plan_file: Path | None = None,
          remote_form_field_name: str | None = None,
          remote_form_value_file: Path | None = None,
          remote_form_fields_file: Path | None = None,
          remote_form_public_plan_sha256: str | None = None,
          remote_form_state_plan_file: Path | None = None,
          remote_form_public_state_plan_sha256: str | None = None,
          remote_form_cookie_file: Path | None = None,
          remote_form_cookie_sha256: str | None = None) -> dict:
    if current_instance().project is not None:
        read_state()
    reuse_pins = (owned_skill_release_sha256, owned_skill_selection_sha256,
                  owned_skill_reuse_confirm_sha256)
    reuse_requested = any(value is not None for value in reuse_pins)
    project_requested = (owned_parameter_project_directory is not None
                         or owned_parameter_project_manifest_sha256 is not None)
    if (project_requested and owned_parameter_project_engine != 'fixture'
            or not project_requested and owned_parameter_project_engine is not None):
        raise ValueError('Owned parameter project requires explicit --owned-parameter-project-engine fixture')
    if (current_instance().project is not None and not project_requested
            and not owned_synthetic_form_invocation and owned_skill_reuse_confirm_sha256 is None
            and mode != 'fixture'):
        raise ValueError('Named project start requires explicit owned learning or a CPU fixture session')
    if project_requested:
        owned_parameter_project_directory = (Path(owned_parameter_project_directory)
                                             if owned_parameter_project_directory is not None else None)
        if (mode != 'real' or owned_synthetic_form_invocation or owned_synthetic_form_recipe
                or reuse_requested):
            raise ValueError('Owned parameter project requires an exclusive real session')
        owned_synthetic_form_recipe = True
    if reuse_requested:
        if (mode != 'real' or owned_synthetic_form_invocation or owned_synthetic_form_recipe
                or any(not isinstance(value, str)
                       or re.fullmatch(r'[a-f0-9]{64}', value) is None for value in reuse_pins)):
            raise ValueError('Owned skill reuse requires three exact pins and an exclusive real session')
        owned_synthetic_form_invocation = True
    if (mode not in {'real', 'fixture'} or type(synthetic_staging) is not bool
            or type(synthetic_learning) is not bool
            or type(owned_synthetic_form_invocation) is not bool
            or type(owned_synthetic_form_recipe) is not bool
            or owned_synthetic_form_invocation and owned_synthetic_form_recipe):
        raise ValueError('Explicit real or fixture mode required')
    if (owned_synthetic_form_invocation or owned_synthetic_form_recipe) and (
            mode != 'real' or synthetic_staging or synthetic_learning
            or any(option is not None for option in (
                remote_entry_profile_sha256, remote_entry_task_file,
                remote_routes_plan_file, remote_static_assets_plan_file,
                remote_readonly_data_plan_file, remote_route_review_source_database,
                remote_route_review_site_store, remote_route_review_store,
                remote_route_review_sha256, remote_json_review_source_database,
                remote_json_review_site_store, remote_json_review_store,
                remote_json_review_sha256, remote_form_plan_file,
                remote_form_field_name, remote_form_value_file,
                remote_form_fields_file, remote_form_public_plan_sha256,
                remote_form_state_plan_file, remote_form_public_state_plan_sha256,
                remote_form_cookie_file, remote_form_cookie_sha256))):
        raise ValueError('Owned synthetic form invocation is an exclusive opt-in mode')
    remote_entry = prepare_remote_entry(mode, remote_entry_profile_sha256, remote_entry_task_file)
    remote_task_sha256 = remote_entry[1] if remote_entry is not None else None
    remote_routes = prepare_remote_routes(mode, remote_entry_profile_sha256,
                                          remote_entry_task_file, remote_routes_plan_file)
    remote_routes_sha256 = remote_routes[1] if remote_routes is not None else None
    if remote_static_assets_plan_file is not None and remote_readonly_data_plan_file is not None:
        raise ValueError('Only one web bundle plan may be pinned')
    remote_static_assets = prepare_remote_static_assets(
        mode, remote_entry_profile_sha256, remote_entry_task_file,
        remote_readonly_data_plan_file or remote_static_assets_plan_file)
    if (remote_readonly_data_plan_file is not None and remote_static_assets is not None
            and json.loads(remote_static_assets[0])['schema_version'] != '2.0'):
        raise ValueError('Read-only data start requires a v2 plan')
    if (remote_static_assets_plan_file is not None and remote_static_assets is not None
            and json.loads(remote_static_assets[0])['schema_version'] != '1.0'):
        raise ValueError('Static asset start requires a v1 plan')
    remote_static_assets_sha256 = (remote_static_assets[1]
                                   if remote_static_assets is not None else None)
    route_review = prepare_managed_remote_route_review(
        mode, remote_entry_profile_sha256, remote_routes_sha256,
        remote_route_review_source_database, remote_route_review_site_store,
        remote_route_review_store, remote_route_review_sha256)
    route_review_source_sha256 = route_review[5] if route_review is not None else None
    if (any(option is not None for option in (
            remote_json_review_source_database, remote_json_review_site_store,
            remote_json_review_store, remote_json_review_sha256))
            and (remote_static_assets is None
                 or json.loads(remote_static_assets[0])['schema_version'] != '2.0')):
        raise ValueError('Managed JSON review requires a private v2 plan')
    json_review = prepare_managed_remote_json_review(
        mode, remote_entry_profile_sha256, remote_static_assets_sha256,
        remote_json_review_source_database, remote_json_review_site_store,
        remote_json_review_store, remote_json_review_sha256)
    json_review_source_sha256 = json_review[5] if json_review is not None else None
    remote_form = prepare_remote_form(mode, remote_entry_profile_sha256,
                                      remote_entry_task_file, remote_form_plan_file,
                                      remote_form_field_name, remote_form_value_file,
                                      remote_form_public_plan_sha256,
                                      fields_file=remote_form_fields_file)
    remote_form_sha256 = remote_form[1] if remote_form is not None else None
    remote_form_fields_sha256 = (hashlib.sha256(remote_form[2]).hexdigest()
                                 if remote_form is not None and remote_form_fields_file is not None else None)
    remote_form_state = prepare_remote_form_state(
        mode, remote_entry_profile_sha256, remote_entry_task_file,
        remote_form_plan_file, remote_form_field_name, remote_form_value_file,
        remote_form_public_plan_sha256, remote_form_state_plan_file,
        remote_form_public_state_plan_sha256, fields_file=remote_form_fields_file)
    remote_form_state_sha256 = remote_form_state[1] if remote_form_state is not None else None
    remote_form_cookie = prepare_remote_form_cookie(
        mode, remote_entry_profile_sha256, remote_entry_task_file,
        remote_form_plan_file, remote_form_field_name, remote_form_value_file,
        remote_form_public_plan_sha256, remote_form_cookie_file,
        remote_form_cookie_sha256, fields_file=remote_form_fields_file)
    remote_form_cookie_pin = remote_form_cookie[1] if remote_form_cookie is not None else None
    owned_bundle = prepare_owned_parameter_project(
        owned_parameter_project_directory, owned_parameter_project_manifest_sha256)
    prepare_base()
    owned_form_recipe_sha256 = None
    try:
        with instance_lock() as lock:
            previous = read_state()
            if previous is not None and previous.version == '2':
                raise ValueError('Shared mode requires a new reviewed shared plan and activation; no native fallback')
            if expected_previous_session is not None:
                _check_expected_session(previous, expected_previous_session)
            if expected_previous_state is not None and previous != expected_previous_state:
                raise ValueError('Restart predecessor identity changed before instance-lock admission')
            if previous is not None and (previous.phase != 'stopped' or observe_process(previous.supervisor) == 'same_process'
                                         or previous.backend and observe_process(previous.backend) == 'same_process'):
                raise ValueError('Previous session requires inspection; no automatic orphan cleanup or restart')
            if check:
                require_local_preflight('fixture' if project_requested else mode)
            if expected_previous_session is not None:
                _assert_named_state(previous)
            with ExitStack() as socket_stack:
                reuse_material = None
                owned_workspace_lock = None
                if project_requested:
                    from .owned_learning_workspace import OwnedLearningWorkspace

                    owned_workspace_lock = socket_stack.enter_context(
                        OwnedLearningWorkspace.acquire(owned_parameter_project_directory, create=True))
                    owned_bundle = prepare_owned_parameter_project(
                        owned_parameter_project_directory, owned_parameter_project_manifest_sha256)
                    if (owned_parameter_project_directory / '.aos-parameter-activation.json').exists():
                        raise ValueError('Owned parameter project was already activated; prepare a fresh project')
                    owned_form_recipe_sha256 = owned_bundle['recipe_sha256']
                if reuse_requested:
                    from .owned_learning_workspace import OwnedLearningWorkspace

                    reuse_material = prepare_owned_skill_reuse(
                        previous, owned_skill_release_sha256, owned_skill_selection_sha256)
                    source_session = previous.owned_skill_source_session or previous.session
                    owned_workspace_lock = socket_stack.enter_context(
                        OwnedLearningWorkspace.acquire(current_instance().base / source_session / 'owned-form', create=True))
                    reuse_material = prepare_owned_skill_reuse(
                        previous, owned_skill_release_sha256, owned_skill_selection_sha256)
                    if reuse_material['preview_sha256'] != owned_skill_reuse_confirm_sha256:
                        raise ValueError('Owned skill reuse confirmation changed')
                listener = socket_stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(('127.0.0.1', current_instance().port))
                listener.listen(128)
                owned_listener = None
                if owned_synthetic_form_invocation or owned_synthetic_form_recipe:
                    owned_listener = socket_stack.enter_context(
                        socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                    owned_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    owned_port = (owned_bundle['manifest']['port'] if project_requested else
                                  int(reuse_material['preview']['origin'].rsplit(':', 1)[1])
                                  if reuse_material is not None else 0)
                    owned_listener.bind(('127.0.0.1', owned_port))
                    owned_listener.listen(8)
                session = 'app-' + uuid4().hex
                directory = current_instance().base / session
                directory.mkdir(mode=0o700)
                process = None
                launch_failed = False
                try:
                    if project_requested:
                        write_new_private_plan(
                            owned_parameter_project_directory / '.aos-parameter-activation.json',
                            canonical({'session': session,
                                       'manifest_sha256': owned_parameter_project_manifest_sha256}).encode())
                    elif reuse_material is not None:
                        write_new_private_plan(directory / 'owned-skill-reuse-previous-state.json',
                                               canonical(previous.model_dump()).encode())
                        write_new_private_plan(directory / 'owned-skill-reuse-preview.json',
                                               canonical(reuse_material['preview']).encode())
                        owned_bundle = {
                            'manifest_sha256': previous.owned_form_manifest_sha256,
                            'invocation_sha256': previous.owned_form_invocation_sha256}
                    elif owned_listener is not None:
                        from .owned_form_invocation_session import (
                            provision_owned_synthetic_form_invocation)

                        owned_bundle = (
                            provision_owned_synthetic_form_invocation(
                                directory / 'owned-form', owned_listener.getsockname()[1],
                                mode='owned_synthetic_form_recipe')
                            if owned_synthetic_form_recipe else
                            provision_owned_synthetic_form_invocation(
                                directory / 'owned-form', owned_listener.getsockname()[1]))
                        owned_form_recipe_sha256 = owned_bundle.get('recipe_sha256')
                        from .owned_learning_workspace import OwnedLearningWorkspace

                        owned_workspace_lock = socket_stack.enter_context(
                            OwnedLearningWorkspace.acquire(directory / 'owned-form', create=True))
                    if remote_entry is not None:
                        write_remote_entry_task(directory, remote_entry[0])
                    if remote_routes is not None:
                        write_private_remote_file(directory, 'remote-routes-plan.json', remote_routes[0])
                    if remote_static_assets is not None:
                        write_private_remote_file(directory, 'remote-static-assets-plan.json',
                                                  remote_static_assets[0])
                    if remote_form is not None:
                        write_private_remote_file(directory, 'remote-form-plan.json', remote_form[0])
                        write_private_remote_file(directory, 'remote-form-fields.json' if remote_form_fields_file is not None
                                                  else 'remote-form-value.txt', remote_form[2])
                    if remote_form_state is not None:
                        write_private_remote_file(directory, 'remote-form-state-plan.json', remote_form_state[0])
                    if remote_form_cookie is not None:
                        write_private_remote_file(directory, 'remote-form-cookie.txt', remote_form_cookie[0])
                    with (directory / 'manager.log').open('xb') as log:
                        os.chmod(directory / 'manager.log', 0o600)
                        if expected_previous_session is not None:
                            _assert_named_state(previous)
                        process = subprocess.Popen([sys.executable, '-m', 'aos.local_app', '_supervise', '--session', session,
                                                    '--base', str(current_instance().base),
                                                    *current_instance().arguments(),
                                                    '--mode', mode, '--lock-fd', str(lock), '--listen-fd', str(listener.fileno()),
                                                    *(['--owned-learning-lock-fd', str(owned_workspace_lock.lock_fd)]
                                                      if owned_workspace_lock is not None else []),
                                                    *(['--owned-skill-reuse-sha256', reuse_material['preview_sha256']]
                                                      if reuse_material is not None else []),
                                                    *(['--owned-parameter-project-directory', str(owned_parameter_project_directory),
                                                       '--owned-parameter-project-manifest-sha256', owned_parameter_project_manifest_sha256,
                                                       '--owned-parameter-project-engine', 'fixture']
                                                      if project_requested else []),
                                                    *(['--owned-synthetic-form-invocation'
                                                       if owned_synthetic_form_invocation else
                                                       '--owned-synthetic-form-recipe']
                                                      if owned_listener is not None and not project_requested else []),
                                                    *(['--owned-form-listener-fd', str(owned_listener.fileno()),
                                                       '--owned-form-manifest-sha256', owned_bundle['manifest_sha256']]
                                                      if owned_listener is not None else []),
                                                    *(['--synthetic-staging'] if synthetic_staging else []),
                                                    *(['--synthetic-learning'] if synthetic_learning else []),
                                                    *(['--remote-entry-profile-sha256', remote_entry_profile_sha256,
                                                       '--remote-entry-task-sha256', remote_task_sha256]
                                                      if remote_entry is not None else []),
                                                    *(['--remote-routes-plan-sha256', remote_routes_sha256]
                                                      if remote_routes is not None else []),
                                                    *(['--remote-static-assets-plan-sha256',
                                                       remote_static_assets_sha256]
                                                      if remote_static_assets is not None else []),
                                                    *(['--remote-route-review-source-database', str(route_review[0]),
                                                       '--remote-route-review-site-store', str(route_review[1]),
                                                       '--remote-route-review-store', str(route_review[2]),
                                                       '--remote-route-review-sha256', route_review[3],
                                                       '--remote-route-review-source-sha256', route_review[5]]
                                                      if route_review is not None else []),
                                                    *(['--remote-json-review-source-database', str(json_review[0]),
                                                       '--remote-json-review-site-store', str(json_review[1]),
                                                       '--remote-json-review-store', str(json_review[2]),
                                                       '--remote-json-review-sha256', json_review[3],
                                                       '--remote-json-review-source-sha256', json_review[5]]
                                                      if json_review is not None else []),
                                                    *(['--remote-form-plan-sha256', remote_form_sha256,
                                                       *(['--remote-form-fields-sha256', remote_form_fields_sha256]
                                                         if remote_form_fields_sha256 is not None else
                                                         ['--remote-form-field-name', remote_form_field_name])]
                                                      if remote_form is not None else []),
                                                    *(['--remote-form-state-plan-sha256', remote_form_state_sha256]
                                                      if remote_form_state is not None else []),
                                                    *(['--remote-form-cookie-sha256', remote_form_cookie_pin]
                                                      if remote_form_cookie is not None else [])],
                                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                                   pass_fds=(lock, listener.fileno(), *(
                                                       [owned_listener.fileno()] if owned_listener is not None else []),
                                                       *((owned_workspace_lock.lock_fd,)
                                                         if owned_workspace_lock is not None else ())),
                                                   cwd=REPO_ROOT,
                                                   env={**os.environ, 'PYTHONPATH': str(REPO_ROOT / 'src')})
                        launch_failed = process.wait(timeout=10) != 0
                        if launch_failed:
                            raise ValueError('Manager launch failed; inspect ' + str(directory))
                except Exception:
                    if process is None or launch_failed:
                        if remote_form is not None:
                            retire_remote_form_value(directory)
                        if remote_form_cookie is not None:
                            retire_remote_form_cookie(directory)
                    raise
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state = read_state()
                if state is not None and state.session == session and state.phase != 'starting':
                    if state.phase != 'running':
                        raise ValueError('Startup failed; inspect private logs in ' + str(directory))
                    return current_status()
                time.sleep(.2)
            if state is not None and state.session == session and observe_process(state.supervisor) == 'same_process':
                signal_owned(state.supervisor, wait=130)
            raise ValueError('Startup deadline exceeded; inspect ' + str(directory))
    except BlockingIOError:
        if expected_previous_session is not None:
            raise ValueError('Restart predecessor changed or another manager owns the instance; no replacement adopted') from None
        if project_requested:
            raise ValueError('Another local application manager owns the instance; project activation requires a fresh session') from None
        status = current_status()
        if (status['phase'] == 'running' and status.get('mode') == mode
                and status.get('synthetic_staging') is synthetic_staging
                and status.get('synthetic_learning') is synthetic_learning
                and status.get('remote_entry_profile_sha256') == remote_entry_profile_sha256
                and status.get('remote_entry_task_sha256') == remote_task_sha256
                and status.get('remote_routes_plan_sha256') == remote_routes_sha256
                and status.get('remote_static_assets_plan_sha256') == remote_static_assets_sha256
                and status.get('remote_route_review_sha256') == remote_route_review_sha256
                and status.get('remote_route_review_source_sha256') == route_review_source_sha256
                and status.get('remote_json_review_sha256') == remote_json_review_sha256
                and status.get('remote_json_review_source_sha256') == json_review_source_sha256
                and status.get('remote_form_plan_sha256') == remote_form_sha256
                and status.get('remote_form_field_name') == remote_form_field_name
                and status.get('remote_form_fields_sha256') == remote_form_fields_sha256
                and status.get('remote_form_state_plan_sha256') == remote_form_state_sha256
                and status.get('remote_form_cookie_sha256') == remote_form_cookie_pin
                and status.get('owned_synthetic_form_invocation')
                is owned_synthetic_form_invocation
                and status.get('owned_synthetic_form_recipe') is owned_synthetic_form_recipe
                and status.get('owned_form_manifest_sha256')
                == (owned_bundle['manifest_sha256'] if owned_bundle is not None else None)
                and status.get('owned_form_invocation_sha256')
                == (owned_bundle['invocation_sha256'] if owned_bundle is not None else None)
                and status.get('owned_form_recipe_sha256')
                == (owned_bundle.get('recipe_sha256') if owned_bundle is not None else None)):
            return status
        raise ValueError('Another local application manager owns the instance') from None


def managed_backend_command(directory: Path, mode: str, listen_fd: int,
                            *, synthetic_staging: bool = False, synthetic_learning: bool = False,
                            owned_form_listener_fd: int | None = None,
                            owned_learning_lock_fd: int | None = None,
                            owned_skill_reuse_sha256: str | None = None,
                            owned_form_manifest_sha256: str | None = None,
                            owned_form_recipe_sha256: str | None = None,
                            owned_parameter_project_directory: Path | None = None,
                            owned_parameter_project_manifest_sha256: str | None = None,
                            remote_entry_profile_sha256: str | None = None,
                            remote_entry_task_sha256: str | None = None,
                            remote_routes_plan_sha256: str | None = None,
                            remote_static_assets_plan_sha256: str | None = None,
                            remote_route_review_source_database: Path | None = None,
                            remote_route_review_site_store: Path | None = None,
                            remote_route_review_store: Path | None = None,
                            remote_route_review_sha256: str | None = None,
                            remote_route_review_source_snapshot_sha256: str | None = None,
                            remote_json_review_source_database: Path | None = None,
                            remote_json_review_site_store: Path | None = None,
                            remote_json_review_store: Path | None = None,
                            remote_json_review_sha256: str | None = None,
                            remote_json_review_source_snapshot_sha256: str | None = None,
                            remote_form_plan_sha256: str | None = None,
                            remote_form_field_name: str | None = None,
                            remote_form_fields_sha256: str | None = None,
                            remote_form_state_plan_sha256: str | None = None,
                            remote_form_cookie_sha256: str | None = None) -> list[str]:
    reuse_source_directory = None
    project = prepare_owned_parameter_project(
        owned_parameter_project_directory, owned_parameter_project_manifest_sha256)
    if project is not None and (
            owned_skill_reuse_sha256 is not None or owned_learning_lock_fd is None
            or owned_form_listener_fd is None
            or owned_form_manifest_sha256 != project['manifest_sha256']
            or owned_form_recipe_sha256 != project['recipe_sha256']
            or any(value is not None for value in (
                remote_entry_profile_sha256, remote_entry_task_sha256,
                remote_routes_plan_sha256, remote_static_assets_plan_sha256,
                remote_route_review_source_database, remote_route_review_site_store,
                remote_route_review_store, remote_route_review_sha256,
                remote_route_review_source_snapshot_sha256,
                remote_json_review_source_database, remote_json_review_site_store,
                remote_json_review_store, remote_json_review_sha256,
                remote_json_review_source_snapshot_sha256, remote_form_plan_sha256,
                remote_form_field_name, remote_form_fields_sha256,
                remote_form_state_plan_sha256, remote_form_cookie_sha256))):
        raise ValueError('Owned parameter project requires its retained source and listener locks')
    if owned_skill_reuse_sha256 is not None:
        if owned_form_listener_fd is None or owned_learning_lock_fd is None:
            raise ValueError('Owned skill reuse requires inherited source and listener locks')
        _material, _previous, reuse_source_directory = load_owned_skill_reuse_startup(
            directory, owned_skill_reuse_sha256)
    if (owned_learning_lock_fd is not None
            and (type(owned_learning_lock_fd) is not int or owned_learning_lock_fd < 0
                 or owned_form_listener_fd is None)):
        raise ValueError('Invalid owned learning lock descriptor')
    if (mode not in {'real', 'fixture'} or type(synthetic_staging) is not bool
            or type(synthetic_learning) is not bool
            or (owned_form_listener_fd is None) != (owned_form_manifest_sha256 is None)
            or owned_form_listener_fd is not None and (
                mode != 'real' or synthetic_staging or synthetic_learning
                or type(owned_form_listener_fd) is not int
                or not re.fullmatch('[a-f0-9]{64}', owned_form_manifest_sha256 or '')
                or any(value is not None for value in (remote_entry_profile_sha256,
                                                       remote_entry_task_sha256,
                                                       remote_form_plan_sha256,
                                                       remote_form_state_plan_sha256,
                                                       remote_form_cookie_sha256)))
            or owned_form_recipe_sha256 is not None and (
                owned_form_listener_fd is None
                or not re.fullmatch('[a-f0-9]{64}', owned_form_recipe_sha256))
            or (remote_entry_profile_sha256 is None) != (remote_entry_task_sha256 is None)
            or remote_entry_profile_sha256 is not None and (
                mode != 'real'
                or re.fullmatch('[a-f0-9]{64}', remote_entry_profile_sha256) is None
                or re.fullmatch('[a-f0-9]{64}', remote_entry_task_sha256) is None)
            or remote_routes_plan_sha256 is not None and (
                remote_entry_task_sha256 is None
                or re.fullmatch('[a-f0-9]{64}', remote_routes_plan_sha256) is None)
            or remote_static_assets_plan_sha256 is not None and (
                remote_entry_task_sha256 is None
                or re.fullmatch('[a-f0-9]{64}', remote_static_assets_plan_sha256) is None)
            or any(option is not None for option in (
                remote_route_review_source_database, remote_route_review_site_store,
                remote_route_review_store, remote_route_review_sha256,
                remote_route_review_source_snapshot_sha256)) and (
                mode != 'real' or remote_routes_plan_sha256 is None
                or not all(option is not None for option in (
                    remote_route_review_source_database, remote_route_review_site_store,
                    remote_route_review_store, remote_route_review_sha256,
                    remote_route_review_source_snapshot_sha256))
                or re.fullmatch('[a-f0-9]{64}', remote_route_review_sha256) is None
                or re.fullmatch('[a-f0-9]{64}', remote_route_review_source_snapshot_sha256) is None
                or not all(Path(source).absolute().is_relative_to(REPO_ROOT / 'data')
                           for source in (remote_route_review_source_database,
                                          remote_route_review_site_store,
                                          remote_route_review_store)))
            or any(option is not None for option in (
                remote_json_review_source_database, remote_json_review_site_store,
                remote_json_review_store, remote_json_review_sha256,
                remote_json_review_source_snapshot_sha256)) and (
                mode != 'real' or remote_static_assets_plan_sha256 is None
                or not all(option is not None for option in (
                    remote_json_review_source_database, remote_json_review_site_store,
                    remote_json_review_store, remote_json_review_sha256,
                    remote_json_review_source_snapshot_sha256))
                or re.fullmatch('[a-f0-9]{64}', remote_json_review_sha256) is None
                or re.fullmatch('[a-f0-9]{64}', remote_json_review_source_snapshot_sha256) is None
                or not all(Path(source).absolute().is_relative_to(REPO_ROOT / 'data')
                           for source in (remote_json_review_source_database,
                                          remote_json_review_site_store,
                                          remote_json_review_store)))
            or remote_form_plan_sha256 is None and (
                remote_form_field_name is not None or remote_form_fields_sha256 is not None)
            or remote_form_plan_sha256 is not None and (
                mode != 'real' or remote_entry_task_sha256 is None
                or re.fullmatch('[a-f0-9]{64}', remote_form_plan_sha256) is None
                or (remote_form_field_name is None) == (remote_form_fields_sha256 is None)
                or remote_form_field_name is not None and
                re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}', remote_form_field_name) is None
                or remote_form_fields_sha256 is not None and
                re.fullmatch('[a-f0-9]{64}', remote_form_fields_sha256) is None)
            or remote_form_state_plan_sha256 is not None and (
                remote_form_plan_sha256 is None
                or re.fullmatch('[a-f0-9]{64}', remote_form_state_plan_sha256) is None)
            or remote_form_cookie_sha256 is not None and (
                remote_form_plan_sha256 is None
                or re.fullmatch('[a-f0-9]{64}', remote_form_cookie_sha256) is None)):
        raise ValueError('Invalid managed backend mode')
    if current_instance().project is not None and directory.parent != current_instance().base:
        raise ValueError('Backend directory differs from its named manager instance')
    return [sys.executable, str(REPO_ROOT / 'scripts/serve_desktop.py'), '--listen-fd', str(listen_fd),
            *(['--port', str(current_instance().port),
               *[item for name in ('knowledge', 'web-profiles', 'web-task', 'web-form-plan', 'web-form-value',
                                  'web-form-state', 'web-route', 'web-static', 'web-readonly-data')
                 for item in ('--' + name + '-root', str(project['profiles_root']
                     if name == 'web-profiles' and project is not None else current_instance().base / name))]]
              if current_instance().project is not None else []),
            '--workspace', str(directory / 'workspace'), '--database',
            str((reuse_source_directory or directory) / 'store.sqlite'),
            *(['--local-ui-auto-login'] if current_instance().project is None and not synthetic_staging and not synthetic_learning
              and owned_form_listener_fd is None and remote_entry_profile_sha256 is None else []),
            *(['--managed-retention'] if current_instance().project is None and directory.parent == current_instance().base
              and re.fullmatch(r'app-[a-f0-9]{32}', directory.name)
              and owned_skill_reuse_sha256 is None else []),
            *(['--owned-learning-lock-fd', str(owned_learning_lock_fd)]
              if owned_learning_lock_fd is not None else []),
            *(['--owned-skill-reuse-sha256', owned_skill_reuse_sha256]
              if owned_skill_reuse_sha256 is not None else []),
            *(['--owned-parameter-project-directory', str(owned_parameter_project_directory),
               '--owned-parameter-project-manifest-sha256', owned_parameter_project_manifest_sha256,
               '--owned-form-listener-fd', str(owned_form_listener_fd)]
              if project is not None else []),
            '--engine', 'decider' if mode == 'real' and project is None else 'fixture', '--browser-tasks', '--desktop-browser',
            *(['--owned-form-listener-fd', str(owned_form_listener_fd),
               '--owned-form-manifest-sha256', owned_form_manifest_sha256,
               *(['--owned-synthetic-form-recipe']
                 if owned_form_recipe_sha256 is not None else [])]
              if owned_form_listener_fd is not None and project is None else []),
            *(['--desktop-navigation-mcp-manifest', str(MCP_MANIFEST)] if project is None else []),
            '--desktop-vision',
            *(['--desktop-staging-mcp-manifest', str(MCP_MANIFEST)] if synthetic_staging else []),
            *(['--synthetic-learning-stream-dir', str(directory / 'learning-stream')] if synthetic_learning else []),
            *(['--remote-entry-mcp-manifest', str(MCP_MANIFEST),
               '--remote-entry-profile-sha256', remote_entry_profile_sha256,
               '--remote-entry-task-file', str(directory / 'remote-entry-task.json'),
               '--remote-entry-task-sha256', remote_entry_task_sha256]
              if remote_entry_profile_sha256 is not None else []),
            *(['--remote-routes-plan-file', str(directory / 'remote-routes-plan.json'),
               '--remote-routes-plan-sha256', remote_routes_plan_sha256]
              if remote_routes_plan_sha256 is not None else []),
            *(['--remote-static-assets-plan-file',
               str(directory / 'remote-static-assets-plan.json'),
               '--remote-static-assets-plan-sha256', remote_static_assets_plan_sha256]
              if remote_static_assets_plan_sha256 is not None else []),
            *(['--remote-route-review-source-database', str(remote_route_review_source_database),
               '--remote-route-review-site-store', str(remote_route_review_site_store),
               '--remote-route-review-store', str(remote_route_review_store),
               '--remote-route-review-sha256', remote_route_review_sha256,
               '--remote-route-review-source-snapshot-sha256', remote_route_review_source_snapshot_sha256]
              if remote_route_review_sha256 is not None else []),
            *(['--remote-json-review-source-database', str(remote_json_review_source_database),
               '--remote-json-review-site-store', str(remote_json_review_site_store),
               '--remote-json-review-store', str(remote_json_review_store),
               '--remote-json-review-sha256', remote_json_review_sha256,
               '--remote-json-review-source-snapshot-sha256', remote_json_review_source_snapshot_sha256]
              if remote_json_review_sha256 is not None else []),
            *(['--remote-form-plan-file', str(directory / 'remote-form-plan.json'),
               '--remote-form-plan-sha256', remote_form_plan_sha256,
               *(['--remote-form-fields-file', str(directory / 'remote-form-fields.json'),
                  '--remote-form-fields-sha256', remote_form_fields_sha256]
                 if remote_form_fields_sha256 is not None else
                 ['--remote-form-field-name', remote_form_field_name,
                  '--remote-form-value-file', str(directory / 'remote-form-value.txt')]),
               '--remote-form-public-plan-sha256', remote_form_plan_sha256]
              if remote_form_plan_sha256 is not None else []),
            *(['--remote-form-state-plan-file', str(directory / 'remote-form-state-plan.json'),
               '--remote-form-state-plan-sha256', remote_form_state_plan_sha256,
               '--remote-form-public-state-plan-sha256', remote_form_state_plan_sha256]
              if remote_form_state_plan_sha256 is not None else []),
            *(['--remote-form-cookie-file', str(directory / 'remote-form-cookie.txt'),
               '--remote-form-cookie-sha256', remote_form_cookie_sha256]
              if remote_form_cookie_sha256 is not None else []),
            '--vision-engine', 'bonsai' if mode == 'real' and project is None else 'fixture',
            *(['--reuse-decider', '--prewarm-decider', '--prewarm-idle-seconds', '300',
               '--gpu-idle-seconds', '30']
              if mode == 'real' and project is None else [])]


def supervise(session: str, mode: str, lock_fd: int, listen_fd: int,
              *, synthetic_staging: bool = False, synthetic_learning: bool = False,
              owned_synthetic_form_recipe: bool | None = None,
              owned_form_listener_fd: int | None = None,
              owned_learning_lock_fd: int | None = None,
              owned_skill_reuse_sha256: str | None = None,
              owned_form_manifest_sha256: str | None = None,
              owned_form_recipe_sha256: str | None = None,
              owned_parameter_project_directory: Path | None = None,
              owned_parameter_project_manifest_sha256: str | None = None,
              remote_entry_profile_sha256: str | None = None,
              remote_entry_task_sha256: str | None = None,
              remote_routes_plan_sha256: str | None = None,
              remote_static_assets_plan_sha256: str | None = None,
              remote_route_review_source_database: Path | None = None,
              remote_route_review_site_store: Path | None = None,
              remote_route_review_store: Path | None = None,
              remote_route_review_sha256: str | None = None,
              remote_route_review_source_sha256: str | None = None,
              remote_json_review_source_database: Path | None = None,
              remote_json_review_site_store: Path | None = None,
              remote_json_review_store: Path | None = None,
              remote_json_review_sha256: str | None = None,
              remote_json_review_source_sha256: str | None = None,
              remote_form_plan_sha256: str | None = None,
              remote_form_field_name: str | None = None,
              remote_form_fields_sha256: str | None = None,
              remote_form_state_plan_sha256: str | None = None,
              remote_form_cookie_sha256: str | None = None) -> None:
    if current_instance().project is not None:
        read_state()
        with socket.socket(fileno=os.dup(listen_fd)) as listener:
            if (listener.family != socket.AF_INET or listener.type != socket.SOCK_STREAM
                    or listener.getsockname() != ('127.0.0.1', current_instance().port)
                    or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
                raise ValueError('Inherited UI listener differs from the pinned project loopback socket')
    if (owned_skill_reuse_sha256 is not None
            and (owned_form_listener_fd is None or owned_learning_lock_fd is None
                 or owned_synthetic_form_recipe is True)):
        raise ValueError('Owned skill reuse requires its retained locked v1 source')
    if (owned_learning_lock_fd is not None
            and (type(owned_learning_lock_fd) is not int or owned_learning_lock_fd < 0
                 or owned_form_listener_fd is None)):
        raise ValueError('Invalid owned learning lock descriptor')
    if (not re.fullmatch('app-[a-f0-9]{32}', session) or mode not in {'real', 'fixture'}
            or type(synthetic_staging) is not bool or type(synthetic_learning) is not bool
            or owned_synthetic_form_recipe is not None
            and type(owned_synthetic_form_recipe) is not bool
            or (owned_form_listener_fd is None) != (owned_form_manifest_sha256 is None)
            or owned_form_listener_fd is None and owned_form_recipe_sha256 is not None
            or owned_form_listener_fd is not None and (mode != 'real'
                or synthetic_staging or synthetic_learning
                or type(owned_form_listener_fd) is not int
                or not re.fullmatch('[a-f0-9]{64}', owned_form_manifest_sha256 or '')
                or owned_form_recipe_sha256 is not None
                and not re.fullmatch('[a-f0-9]{64}', owned_form_recipe_sha256))):
        raise ValueError('Invalid manager scope')
    os.close(private_directory(current_instance().base / session))
    lock_info = os.fstat(lock_fd)
    expected_lock = (current_instance().base / 'manager.lock').stat(follow_symlinks=False)
    if (lock_info.st_dev, lock_info.st_ino) != (expected_lock.st_dev, expected_lock.st_ino):
        raise ValueError('Inherited instance lock mismatch')
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_arguments: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_arguments: shutdown.set())
    directory = current_instance().base / session
    project = prepare_owned_parameter_project(
        owned_parameter_project_directory, owned_parameter_project_manifest_sha256)
    if project is not None:
        if (owned_skill_reuse_sha256 is not None or owned_learning_lock_fd is None
                or owned_form_listener_fd is None
                or owned_form_manifest_sha256 != project['manifest_sha256']):
            raise ValueError('Owned parameter project requires exact retained activation')
        activation = json.loads(private_read(
            owned_parameter_project_directory / '.aos-parameter-activation.json'))
        if activation != {'session': session, 'manifest_sha256': project['manifest_sha256']}:
            raise ValueError('Owned parameter project activation belongs to another manager session')
    owned_source_directory = directory
    if owned_skill_reuse_sha256 is not None:
        _material, _previous, owned_source_directory = load_owned_skill_reuse_startup(
            directory, owned_skill_reuse_sha256)
    owned_form_manifest = None
    if owned_form_listener_fd is not None:
        from .owned_form_invocation_session import verify_owned_form_invocation_manifest

        owned_form_manifest = (project['manifest'] if project is not None else
                               verify_owned_form_invocation_manifest(
                                   owned_source_directory / 'owned-form', owned_form_manifest_sha256))
        manifest_recipe_mode = (project is not None
                               or owned_form_manifest.get('mode') == 'owned_synthetic_form_recipe')
        if (owned_synthetic_form_recipe is not None
                and owned_synthetic_form_recipe is not manifest_recipe_mode):
            raise ValueError('Owned form manager mode differs from private manifest')
        owned_synthetic_form_recipe = manifest_recipe_mode
        manifest_recipe_sha256 = owned_form_manifest.get('recipe_sha256')
        if (owned_form_recipe_sha256 is not None
                and owned_form_recipe_sha256 != manifest_recipe_sha256):
            raise ValueError('Owned form recipe pin differs from private manifest')
        owned_form_recipe_sha256 = manifest_recipe_sha256
        with socket.socket(fileno=os.dup(owned_form_listener_fd)) as form_listener:
            if (form_listener.family != socket.AF_INET or form_listener.type != socket.SOCK_STREAM
                    or form_listener.getsockname()[0] != '127.0.0.1'
                    or form_listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
                    or owned_form_manifest['origin']
                    != f"https://w3-owned-form.aos.invalid:{form_listener.getsockname()[1]}"):
                raise ValueError('Owned HTTPS listener does not match its private manifest')
    state = LocalAppState(session=session, mode=mode, synthetic_staging=synthetic_staging,
                          project=current_instance().project,
                          url=current_instance().url if current_instance().project is not None else 'http://127.0.0.1:8765/ui/',
                          synthetic_learning=synthetic_learning,
                          owned_synthetic_form_invocation=(owned_form_listener_fd is not None
                                                           and not owned_synthetic_form_recipe),
                          owned_synthetic_form_recipe=bool(owned_synthetic_form_recipe) and project is None,
                          owned_synthetic_parameter_project=project is not None,
                          owned_parameter_project_directory=(str(owned_parameter_project_directory)
                                                             if project is not None else None),
                          owned_parameter_project_manifest_sha256=owned_parameter_project_manifest_sha256,
                          owned_parameter_project_runtime='fixture_cpu' if project is not None else None,
                          owned_form_manifest_sha256=owned_form_manifest_sha256,
                          owned_form_invocation_sha256=(owned_form_manifest['invocation_sha256']
                                                         if owned_form_manifest is not None else None),
                          owned_form_recipe_sha256=owned_form_recipe_sha256,
                          owned_skill_reuse_sha256=owned_skill_reuse_sha256,
                          owned_skill_source_session=(owned_source_directory.name
                                                      if owned_skill_reuse_sha256 is not None else None),
                          remote_entry_profile_sha256=remote_entry_profile_sha256,
                          remote_entry_task_sha256=remote_entry_task_sha256,
                          remote_routes_plan_sha256=remote_routes_plan_sha256,
                          remote_static_assets_plan_sha256=remote_static_assets_plan_sha256,
                          remote_route_review_sha256=remote_route_review_sha256,
                          remote_route_review_source_sha256=remote_route_review_source_sha256,
                          remote_json_review_sha256=remote_json_review_sha256,
                          remote_json_review_source_sha256=remote_json_review_source_sha256,
                          remote_form_plan_sha256=remote_form_plan_sha256,
                          remote_form_field_name=remote_form_field_name,
                          remote_form_fields_sha256=remote_form_fields_sha256,
                          remote_form_state_plan_sha256=remote_form_state_plan_sha256,
                          remote_form_cookie_sha256=remote_form_cookie_sha256,
                          phase='starting', supervisor=process_identity(os.getpid()), started_at=now())
    process = None
    owned_workspace_lock = None
    try:
        if owned_form_listener_fd is not None:
            from .owned_learning_workspace import OwnedLearningWorkspace

            owned_workspace_lock = (
                OwnedLearningWorkspace.adopt(owned_parameter_project_directory
                                            if project is not None else owned_source_directory / 'owned-form',
                                            owned_learning_lock_fd)
                if owned_learning_lock_fd is not None else
                OwnedLearningWorkspace.acquire(owned_source_directory / 'owned-form', create=True))
            if owned_learning_lock_fd is not None:
                os.close(owned_learning_lock_fd)
                owned_learning_lock_fd = None
        if (remote_entry_task_sha256 is not None
                and hashlib.sha256(private_read(directory / 'remote-entry-task.json')).hexdigest()
                != remote_entry_task_sha256):
            raise ValueError('Private remote entry task changed before managed launch')
        if (remote_routes_plan_sha256 is not None
                and hashlib.sha256(private_read(directory / 'remote-routes-plan.json', 20000)).hexdigest()
                != remote_routes_plan_sha256):
            raise ValueError('Private read-only route plan changed before managed launch')
        if remote_static_assets_plan_sha256 is not None:
            prepared_assets = prepare_remote_static_assets(
                mode, remote_entry_profile_sha256,
                directory / 'remote-entry-task.json',
                directory / 'remote-static-assets-plan.json')
            if (prepared_assets is None
                    or prepared_assets[1] != remote_static_assets_plan_sha256):
                raise ValueError('Private static asset plan changed before managed launch')
        route_review = prepare_managed_remote_route_review(
            mode, remote_entry_profile_sha256, remote_routes_plan_sha256,
            remote_route_review_source_database, remote_route_review_site_store,
            remote_route_review_store, remote_route_review_sha256)
        if (route_review is None and remote_route_review_source_sha256 is not None
                or route_review is not None
                and route_review[5] != remote_route_review_source_sha256):
            raise ValueError('Private remote route review source changed before managed launch')
        json_review = prepare_managed_remote_json_review(
            mode, remote_entry_profile_sha256, remote_static_assets_plan_sha256,
            remote_json_review_source_database, remote_json_review_site_store,
            remote_json_review_store, remote_json_review_sha256)
        if (json_review is None and remote_json_review_source_sha256 is not None
                or json_review is not None
                and json_review[5] != remote_json_review_source_sha256):
            raise ValueError('Private JSON review source changed before managed launch')
        if remote_form_plan_sha256 is not None:
            prepared_form = prepare_remote_form(
                mode, remote_entry_profile_sha256, directory / 'remote-entry-task.json',
                directory / 'remote-form-plan.json', remote_form_field_name,
                directory / 'remote-form-value.txt' if remote_form_fields_sha256 is None else None,
                remote_form_plan_sha256,
                fields_file=(directory / 'remote-form-fields.json'
                             if remote_form_fields_sha256 is not None else None))
            if (prepared_form is None or prepared_form[1] != remote_form_plan_sha256
                    or remote_form_fields_sha256 is not None
                    and hashlib.sha256(prepared_form[2]).hexdigest() != remote_form_fields_sha256):
                raise ValueError('Private HTTPS form changed before managed launch')
        if remote_form_state_plan_sha256 is not None:
            prepared_state = prepare_remote_form_state(
                mode, remote_entry_profile_sha256, directory / 'remote-entry-task.json',
                directory / 'remote-form-plan.json', remote_form_field_name,
                directory / 'remote-form-value.txt' if remote_form_fields_sha256 is None else None,
                remote_form_plan_sha256,
                directory / 'remote-form-state-plan.json', remote_form_state_plan_sha256,
                fields_file=(directory / 'remote-form-fields.json'
                             if remote_form_fields_sha256 is not None else None))
            if prepared_state is None or prepared_state[1] != remote_form_state_plan_sha256:
                raise ValueError('Private HTTPS form state changed before managed launch')
        if remote_form_cookie_sha256 is not None:
            prepared_cookie = prepare_remote_form_cookie(
                mode, remote_entry_profile_sha256, directory / 'remote-entry-task.json',
                directory / 'remote-form-plan.json', remote_form_field_name,
                directory / 'remote-form-value.txt' if remote_form_fields_sha256 is None else None,
                remote_form_plan_sha256,
                directory / 'remote-form-cookie.txt', remote_form_cookie_sha256,
                fields_file=(directory / 'remote-form-fields.json'
                             if remote_form_fields_sha256 is not None else None))
            if prepared_cookie is None or prepared_cookie[1] != remote_form_cookie_sha256:
                raise ValueError('Private HTTPS form cookie changed before managed launch')
        command = managed_backend_command(
            directory, mode, listen_fd, synthetic_staging=synthetic_staging,
            synthetic_learning=synthetic_learning,
            owned_learning_lock_fd=(owned_workspace_lock.lock_fd
                                    if owned_workspace_lock is not None else None),
            owned_skill_reuse_sha256=owned_skill_reuse_sha256,
            owned_form_listener_fd=owned_form_listener_fd,
            owned_form_manifest_sha256=owned_form_manifest_sha256,
            owned_form_recipe_sha256=owned_form_recipe_sha256,
            owned_parameter_project_directory=owned_parameter_project_directory,
            owned_parameter_project_manifest_sha256=owned_parameter_project_manifest_sha256,
            remote_entry_profile_sha256=remote_entry_profile_sha256,
            remote_entry_task_sha256=remote_entry_task_sha256,
            remote_routes_plan_sha256=remote_routes_plan_sha256,
            remote_static_assets_plan_sha256=remote_static_assets_plan_sha256,
            remote_route_review_source_database=remote_route_review_source_database,
            remote_route_review_site_store=remote_route_review_site_store,
            remote_route_review_store=remote_route_review_store,
            remote_route_review_sha256=remote_route_review_sha256,
            remote_route_review_source_snapshot_sha256=(route_review[4]
                                                        if route_review is not None else None),
            remote_json_review_source_database=remote_json_review_source_database,
            remote_json_review_site_store=remote_json_review_site_store,
            remote_json_review_store=remote_json_review_store,
            remote_json_review_sha256=remote_json_review_sha256,
            remote_json_review_source_snapshot_sha256=(json_review[4]
                                                       if json_review is not None else None),
            remote_form_plan_sha256=remote_form_plan_sha256,
            remote_form_field_name=remote_form_field_name,
            remote_form_fields_sha256=remote_form_fields_sha256,
            remote_form_state_plan_sha256=remote_form_state_plan_sha256,
            remote_form_cookie_sha256=remote_form_cookie_sha256)
        write_state(state)
        with (directory / 'backend.log').open('xb') as log:
            os.chmod(directory / 'backend.log', 0o600)
            process = subprocess.Popen(command,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       pass_fds=(listen_fd, *((owned_form_listener_fd,)
                                                              if owned_form_listener_fd is not None else ()),
                                                 *((owned_workspace_lock.lock_fd,)
                                                   if owned_workspace_lock is not None else ())),
                                       cwd=REPO_ROOT)
            os.close(listen_fd)
            if owned_form_listener_fd is not None:
                os.close(owned_form_listener_fd)
                owned_form_listener_fd = None
            state = state.model_copy(update={'backend': process_identity(process.pid)})
            write_state(state)
            deadline = time.monotonic() + 75
            expected_session = _session_readiness(state, auto_login='--local-ui-auto-login' in command)
            with httpx.Client(base_url=current_instance().origin, timeout=1, trust_env=False) as client:
                while not shutdown.is_set() and process.poll() is None:
                    if state.phase == 'starting':
                        text = private_read(directory / 'backend.log', append_log=True).decode(errors='replace')
                        match = re.search(r'Local token file \(0600\): ([^\n]+)\n', text)
                        if match:
                            token_path = Path(match.group(1))
                            if token_path.parent != REPO_ROOT / 'runs' or not re.fullmatch(r'desktop-console-[a-f0-9]{16}\.token', token_path.name):
                                raise ValueError('Unexpected token source')
                            try:
                                response = client.get('/api/session')
                                ui = client.get('/ui/')
                                if response.status_code == 200 and response.json() == expected_session and ui.status_code == 200:
                                    state = state.model_copy(update={'phase': 'running', 'token_name': token_path.name})
                                    token_value(state)
                                    write_state(state)
                            except (httpx.HTTPError, json.JSONDecodeError):
                                pass
                        if time.monotonic() > deadline:
                            raise ValueError('Backend readiness deadline exceeded')
                    shutdown.wait(.2)
            requested = shutdown.is_set()
            if process.poll() is None:
                signal_owned(state.backend)
            returncode = process.wait(timeout=120)
            if remote_form_plan_sha256 is not None:
                retire_remote_form_value(directory)
            if remote_form_cookie_sha256 is not None:
                retire_remote_form_cookie(directory)
            cleaned = requested and returncode in {0, -signal.SIGTERM} and clean_shutdown(state)
            print(f'backend_exit={returncode} owned_cleanup_verified={cleaned}', flush=True)
            state = state.model_copy(update={'phase': 'stopped' if cleaned else 'failed', 'token_name': None})
            write_state(state)
    finally:
        if process is not None and process.poll() is None:
            try:
                if state.backend is None:
                    process.terminate()
                else:
                    signal_owned(state.backend)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                pass
        try:
            if remote_form_plan_sha256 is not None and (process is None or process.poll() is not None):
                retire_remote_form_value(directory)
            if remote_form_cookie_sha256 is not None and (process is None or process.poll() is not None):
                retire_remote_form_cookie(directory)
            if state.phase in {'starting', 'running'}:
                write_state(state.model_copy(update={'phase': 'failed', 'token_name': None}))
        finally:
            if owned_workspace_lock is not None:
                owned_workspace_lock.close()
            if owned_learning_lock_fd is not None:
                os.close(owned_learning_lock_fd)
            if owned_form_listener_fd is not None:
                os.close(owned_form_listener_fd)
            os.close(lock_fd)


def stop(*, expected_session: str | None = None, expected_state: LocalAppState | None = None) -> dict:
    state = read_state()
    _check_expected_session(state, expected_session)
    if expected_state is not None and state != expected_state:
        raise ValueError('Local session changed before exact restart shutdown')
    if state is not None and state.version == '2':
        from .shared_desktop_manager import stop as shared_stop

        return shared_stop(state, expected_session=expected_session)
    if state is None or state.phase == 'stopped' and observe_process(state.supervisor) != 'same_process':
        if state is not None:
            _assert_named_state(state)
        result = current_status()
        if current_instance().project is not None:
            _assert_named_state(state)
            if result.get('session') != state.session or result.get('phase') != 'stopped':
                raise ValueError('Local session changed during stopped-session confirmation')
        return result
    if observe_process(state.supervisor) != 'same_process':
        raise ValueError('Manager identity unavailable; no unrelated PID or container will be signalled')
    _assert_named_state(state)
    if not signal_owned(state.supervisor, wait=130):
        raise ValueError('Graceful shutdown incomplete; inspect logs, no force cleanup attempted')
    result = current_status()
    if current_instance().project is not None and result.get('session') != state.session:
        raise ValueError('Local session changed during shutdown; replacement was not adopted')
    if current_instance().project is not None:
        stopped = read_state()
        if (stopped is None or stopped.model_copy(update={'phase': state.phase, 'token_name': state.token_name}) != state):
            raise ValueError('Recorded manager identity changed during shutdown')
    if result['phase'] != 'stopped':
        raise ValueError('Shutdown requires inspection: ' + str(current_instance().base / state.session))
    return result


def recover_clean_exit(*, expected_session: str | None = None) -> dict:
    if current_instance().project is not None:
        _check_expected_session(read_state(), expected_session)
    with instance_lock():
        state = read_state()
        _check_expected_session(state, expected_session)
        if state is not None and state.version == '2':
            raise ValueError('Shared lifecycle recovery requires its reviewed service generation; native recovery is forbidden')
        if state is None or state.phase != 'failed' or state.backend is None:
            raise ValueError('Recovery requires a failed session with a recorded backend')
        if any(value for key, value in state.model_dump().items()
               if key.startswith(('remote_', 'owned_', 'synthetic_'))):
            raise ValueError('Clean-exit recovery requires a plain local session')
        current_boot = process_identity(os.getpid()).boot_id
        if (state.supervisor.boot_id != current_boot or state.backend.boot_id != current_boot
                or observe_process(state.supervisor) != 'not_observed'
                or observe_process(state.backend) != 'not_observed'):
            raise ValueError('Recovery requires both recorded processes absent on this boot')
        directory = current_instance().base / state.session
        os.close(private_directory(directory))
        workspace_fd = private_directory(directory / 'workspace')
        try:
            identity = workspace_identity(directory / 'workspace', workspace_fd)
        finally:
            os.close(workspace_fd)
        log = private_read(directory / 'backend.log', append_log=True).decode(errors='strict')
        matches = re.findall(r'^Local token file \(0600\): ([^\n]+)$', log, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError('Recovery requires one recorded token identity')
        token_path = Path(matches[0])
        if (token_path.parent != REPO_ROOT / 'runs'
                or not re.fullmatch(r'desktop-console-[a-f0-9]{16}\.token', token_path.name)
                or state.token_name is not None and state.token_name != token_path.name
                or os.path.lexists(token_path)):
            raise ValueError('Recovery requires an exact retired token')
        journals = directory / '.aos-lifecycle'
        journal_fd = private_directory(journals)
        try:
            names = sorted(os.listdir(journal_fd))
            if not 1 <= len(names) <= 1000 or any(
                    not re.fullmatch(r'desktop-[a-f0-9]{32}\.jsonl', name) for name in names):
                raise ValueError('Unexpected lifecycle journal inventory')
            checksums = {}
            container_ids = set()
            for name in names:
                events, checksum = read_journal(journals / name)
                birth = events[0].birth
                container_id = events[-1].container_id
                if (birth.process != state.backend or birth.workspace != identity
                        or name != birth.runtime_id + '.jsonl'
                        or events[-1].stage != 'removed' or container_id is None
                        or container_id in container_ids):
                    raise ValueError('Recovery requires exact removed lifecycle identities')
                from .desktop import DOCKER

                inspection = subprocess.run(
                    [*(DOCKER if current_instance().project is not None else ['docker']),
                     'inspect', '--type', 'container', '--format', '{{.Id}}', container_id],
                    capture_output=True, timeout=10, env={**os.environ, 'LC_ALL': 'C'})
                expected_error = ('Error response from daemon: No such container: ' + container_id).encode()
                if (inspection.returncode != 1 or inspection.stdout.strip()
                        or inspection.stderr.strip() != expected_error):
                    raise ValueError('Recorded desktop absence could not be verified')
                checksums[name] = checksum
                container_ids.add(container_id)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(('127.0.0.1', current_instance().port))
                workspace_fd = private_directory(directory / 'workspace')
                try:
                    current_workspace = workspace_identity(directory / 'workspace', workspace_fd)
                finally:
                    os.close(workspace_fd)
                if (read_state() != state or os.path.lexists(token_path)
                        or current_workspace != identity
                        or private_read(directory / 'backend.log', append_log=True).decode(errors='strict') != log
                        or observe_process(state.supervisor) != 'not_observed'
                        or observe_process(state.backend) != 'not_observed'
                        or sorted(os.listdir(journal_fd)) != names
                        or any(read_journal(journals / name)[1] != checksum
                               for name, checksum in checksums.items())):
                    raise ValueError('Local session changed during recovery')
                record = {'previous_state': state.model_dump(), 'reason': 'verified_clean_exit',
                          'journal_sha256': checksums, 'container_ids': sorted(container_ids),
                          'retired_token_name': token_path.name, 'recovered_at': now()}
                parent = private_directory(current_instance().base)
                try:
                    descriptor = os.open('.clean-exit-' + state.session + '.json',
                                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=parent)
                    with os.fdopen(descriptor, 'w') as stream:
                        stream.write(canonical(record))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(parent)
                finally:
                    os.close(parent)
                _assert_named_state(state)
                write_state(state.model_copy(update={'phase': 'stopped', 'token_name': None}))
        finally:
            os.close(journal_fd)
        return current_status()


def recover_reboot(*, expected_session: str | None = None) -> dict:
    if current_instance().project is not None:
        _check_expected_session(read_state(), expected_session)
    with instance_lock():
        state = read_state()
        _check_expected_session(state, expected_session)
        if state is not None and state.version == '2':
            raise ValueError('Shared reboot recovery requires fresh reviewed admission; native recovery is forbidden')
        if state is None or state.phase not in {'running', 'starting', 'failed'} or state.backend is None:
            raise ValueError('No interrupted session with a recorded backend')
        current_boot = process_identity(os.getpid()).boot_id
        if (state.supervisor.boot_id == current_boot or state.backend.boot_id == current_boot
                or observe_process(state.supervisor) != 'different_boot'
                or observe_process(state.backend) != 'different_boot'):
            raise ValueError('Recovery requires both recorded processes from a previous boot')
        directory = current_instance().base / state.session
        os.close(private_directory(directory))
        workspace = directory / 'workspace'
        workspace_fd = private_directory(workspace)
        try:
            identity = workspace_identity(workspace, workspace_fd)
        finally:
            os.close(workspace_fd)
        journals = directory / '.aos-lifecycle'
        journal_fd = private_directory(journals)
        try:
            names = os.listdir(journal_fd)
            if not 1 <= len(names) <= 1000 or any(not re.fullmatch(r'desktop-[a-f0-9]{32}\.jsonl', name) for name in names):
                raise ValueError('Unexpected lifecycle journal inventory')
            recorded = {}
            for name in names:
                events, checksum = read_journal(journals / name)
                birth = events[0].birth
                if (birth.process != state.backend
                        or any(getattr(birth.workspace, field) != getattr(identity, field)
                               for field in ('path_sha256', 'inode', 'owner_uid'))
                        or events[-1].stage not in {'created', 'started', 'removed'}
                        or events[-1].container_id is None):
                    raise ValueError('Lifecycle identity or stage mismatch')
                recorded[events[-1].container_id] = (birth, checksum)
            if len(recorded) != len(names):
                raise ValueError('Duplicate lifecycle container identity')
        finally:
            os.close(journal_fd)
        template = ('[{{json .Id}},{{json .Name}},{{json .State.Status}},{{json .State.Running}},'
                    '{{json .State.Pid}},{{json .Image}},{{json (index .Config.Labels "com.aos.runtime")}}]')
        for container_id, (birth, _checksum) in recorded.items():
            from .desktop import DOCKER

            result = subprocess.run([*(DOCKER if current_instance().project is not None else ['docker']),
                                     'inspect', '--format', template, container_id],
                                    capture_output=True, timeout=10, check=True)
            if len(result.stdout) > 4096:
                raise ValueError('Docker inspection output exceeds limit')
            observed = json.loads(result.stdout)
            if observed != [container_id, '/' + birth.container_name, 'exited', False, 0,
                            birth.image_id, birth.runtime_id]:
                raise ValueError('Recorded desktop container is not safely stopped')
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', current_instance().port))
        if read_state() != state:
            raise ValueError('Local session changed during recovery')
        if state.token_name:
            token_path = REPO_ROOT / 'runs' / state.token_name
            if token_path.exists():
                private_read(token_path, 128)
        record = {'previous_state': state.model_dump(), 'reason': 'previous_host_boot',
                  'journal_sha256': {birth.runtime_id: checksum for birth, checksum in recorded.values()},
                  'container_ids': sorted(recorded), 'recovered_at': now()}
        parent = private_directory(current_instance().base)
        try:
            filename = '.recovery-' + state.session + '.json'
            descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(canonical(record))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
        finally:
            os.close(parent)
        if state.token_name:
            _assert_named_state(state)
            try:
                (REPO_ROOT / 'runs' / state.token_name).unlink()
            except FileNotFoundError:
                pass
        if state.remote_form_plan_sha256 is not None:
            retire_remote_form_value(directory)
        if state.remote_form_cookie_sha256 is not None:
            retire_remote_form_cookie(directory)
        _assert_named_state(state)
        write_state(state.model_copy(update={'phase': 'stopped', 'token_name': None}))
        return current_status()


def require_idle_restart(state: LocalAppState) -> None:
    try:
        _assert_named_state(state)
        if state.backend is None or observe_process(state.backend) != 'same_process':
            raise ValueError('restart_backend_unverified')
        token = token_value(state)
        _assert_named_state(state)
        with httpx.Client(base_url=current_instance().origin, headers={'Origin': current_instance().origin}, timeout=5, trust_env=False) as client:
            client.post('/api/login', json={'token': token}).raise_for_status()
            tasks_response = client.get('/api/tasks')
            state_response = client.get('/api/state')
            tasks_response.raise_for_status()
            state_response.raise_for_status()
            tasks = tasks_response.json()
            _check_named_http_scope(tasks, state)
            control = state_response.json()['control']
            desktop_session_id = control['session_id']
            if (type(tasks['busy']) is not bool or type(tasks['reserved']) is not bool
                    or not isinstance(tasks['jobs'], list)
                    or tasks['busy'] or tasks['reserved'] or tasks['approval'] is not None
                    or tasks['auto_approval'] is not None
                    or any(job['status'] not in {'succeeded', 'failed', 'cancelled'}
                           for job in tasks['jobs'])
                    or not isinstance(desktop_session_id, str)
                    or re.fullmatch(r'desktop-session-[a-f0-9]{32}', desktop_session_id) is None
                    or control['owner'] != 'AGENT' or control['status'] != 'running'):
                raise ValueError('restart_not_idle')
            _assert_named_state(state)
            quiesce = client.post('/api/restart/quiesce', json={'session_id': desktop_session_id})
            quiesce.raise_for_status()
            result = quiesce.json()
            if result != {'quiesced': True, 'session_id': desktop_session_id}:
                raise ValueError('restart_quiesce_unverified')
            _assert_named_state(state)
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
        raise ValueError('Restart requires an authenticated idle AGENT session; no process signalled') from error


def release_restart_quiesce(*, expected_session: str | None = None) -> dict:
    state = read_state()
    _check_expected_session(state, expected_session)
    if state is not None and state.version == '2':
        raise ValueError('Shared task admission cannot be reopened through native restart release')
    if (state is None or state.phase != 'running' or state.backend is None
            or observe_process(state.supervisor) != 'same_process'
            or observe_process(state.backend) != 'same_process'):
        raise ValueError('Inspect the running managed session before releasing restart admission')
    try:
        token = token_value(state)
        _assert_named_state(state)
        with httpx.Client(base_url=current_instance().origin, headers={'Origin': current_instance().origin}, timeout=5, trust_env=False) as client:
            client.post('/api/login', json={'token': token}).raise_for_status()
            tasks_response = client.get('/api/tasks')
            state_response = client.get('/api/state')
            tasks_response.raise_for_status()
            state_response.raise_for_status()
            tasks = tasks_response.json()
            _check_named_http_scope(tasks, state)
            control = state_response.json()['control']
            desktop_session_id = control['session_id']
            if current_instance().project is not None and (control.get('owner') != 'AGENT' or control.get('status') != 'running'):
                raise ValueError('Named restart release requires current running AGENT ownership')
            if (tasks_response.json()['restart_quiesced'] is not True
                    or not isinstance(desktop_session_id, str)
                    or re.fullmatch(r'desktop-session-[a-f0-9]{32}', desktop_session_id) is None):
                raise ValueError('Restart admission is not active for a verified desktop session')
            _assert_named_state(state)
            response = client.post('/api/restart/release', json={'session_id': desktop_session_id})
            response.raise_for_status()
            result = response.json()
        if result != {'quiesced': False, 'session_id': desktop_session_id}:
            raise ValueError('Restart admission release was not verified')
        _assert_named_state(state)
        return result
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
        raise ValueError('Restart admission release failed; inspect the managed session') from error


def restart(*, expected_session: str | None = None) -> dict:
    state = read_state()
    _check_expected_session(state, expected_session)
    if state is None:
        raise ValueError('No existing local application session to restart; use start')
    if state.version == '2':
        raise ValueError('Shared restart requires explicit clean stop, a new reviewed plan and fresh activation')
    if (state.synthetic_staging or state.synthetic_learning
            or state.owned_synthetic_form_invocation or state.owned_synthetic_form_recipe
            or state.owned_synthetic_parameter_project
            or any(value is not None for value in (
                state.remote_entry_profile_sha256, state.remote_entry_task_sha256,
                state.remote_routes_plan_sha256, state.remote_route_review_sha256,
                state.remote_json_review_sha256,
                state.remote_static_assets_plan_sha256,
                state.remote_route_review_source_sha256,
                state.remote_json_review_source_sha256, state.remote_form_plan_sha256,
                state.remote_form_field_name, state.remote_form_fields_sha256,
                state.remote_form_state_plan_sha256, state.remote_form_cookie_sha256))):
        raise ValueError('Restart requires a plain local session; stop and start with the exact opt-in options')
    if state.phase != 'stopped':
        require_local_preflight(state.mode)
    if state.phase == 'running' and observe_process(state.supervisor) == 'same_process':
        require_idle_restart(state)
        _assert_named_state(state)
        stopped = stop(expected_session=state.session, **(
            {'expected_state': state} if current_instance().project is not None else {}))
        if stopped['phase'] != 'stopped':
            raise ValueError('Graceful shutdown incomplete; no new session started')
    elif state.phase != 'stopped':
        if current_instance().project is not None:
            raise ValueError('Named interrupted session requires separate explicit recovery; no automatic recovery attempted')
        recovered = recover_reboot(expected_session=state.session)
        if recovered['phase'] != 'stopped':
            raise ValueError('Previous session recovery incomplete; no new session started')
    if current_instance().project is not None:
        stopped = read_state()
        _check_expected_session(stopped, state.session)
        if stopped.phase != 'stopped':
            raise ValueError('Named predecessor has not completed verified shutdown')
        if stopped.model_copy(update={'phase': state.phase, 'token_name': state.token_name}) != state:
            raise ValueError('Named predecessor identity changed across restart shutdown')
        return start(state.mode, expected_previous_session=state.session, expected_previous_state=stopped)
    return start(state.mode)


def main():
    parser = argparse.ArgumentParser(description='AOS v0.1 yerel pilot: başlat, denetle, token göster veya güvenle durdur')
    parser.add_argument('command', choices=['start', 'restart', 'release-restart', 'status', 'stop', 'token', 'open', 'doctor', 'prepare-ui', 'prepare-shared', 'provision-shared',
                                            'preview-remote-entry', 'plan-remote-routes',
                                            'preview-remote-routes', 'preview-remote-route-review',
                                            'preview-remote-json-review',
                                            'plan-remote-static-assets', 'preview-remote-static-assets',
                                            'plan-remote-readonly-data', 'preview-remote-readonly-data',
                                            'plan-remote-form-fields',
                                            'plan-remote-form', 'preview-remote-form',
                                            'plan-remote-form-state', 'preview-remote-form-state',
                                            'plan-remote-form-cookie', 'preview-remote-form-cookie',
                                            'preview-owned-skill-reuse', 'recover-reboot', 'recover-clean-exit', '_supervise'])
    parser.add_argument('--fixture', action='store_true')
    parser.add_argument('--project', help='Explicit isolated named manager; requires --project-port on every command')
    parser.add_argument('--project-port', type=int, help='Pinned loopback UI port, 1024..65535 except 8765')
    parser.add_argument('--synthetic-staging', action='store_true')
    parser.add_argument('--synthetic-learning', action='store_true')
    parser.add_argument('--owned-synthetic-form-invocation', action='store_true',
                        help='Start the synthetic learning pilot: demonstration, candidate review and skill selection; requires a cleanly stopped session')
    parser.add_argument('--owned-synthetic-form-recipe', action='store_true',
                        help='Start one isolated, single-use synthetic form recipe session')
    parser.add_argument('--owned-parameter-project-directory', type=Path,
                        help='Activate one fresh private synthetic parameter project outside the checkout; no task autostart')
    parser.add_argument('--owned-parameter-project-manifest-sha256',
                        help='Exact host-reviewed manifest SHA-256 paired with the project directory')
    parser.add_argument('--owned-parameter-project-engine', choices=['fixture'],
                        help='Required explicit CPU fixture engine for prepared-project activation; no native inference')
    parser.add_argument('--owned-skill-release-sha256',
                        help='Exact selected skill release for preview-owned-skill-reuse or a new start')
    parser.add_argument('--owned-skill-selection-sha256',
                        help='Exact selection receipt paired with the selected skill release')
    parser.add_argument('--owned-skill-reuse-confirm-sha256',
                        help='Exact stopped-source preview SHA-256; required with both skill hashes for start')
    parser.add_argument('--expected-session',
                        help='Exact current manager session for stop/restart/release/recovery; required for named lifecycle effects')
    parser.add_argument('--shared-template', type=Path, help='Reviewed inert shared-host template; prepare-shared only')
    parser.add_argument('--shared-template-sha256')
    parser.add_argument('--shared-plan', type=Path, help='Exact prepared shared-host plan; explicit provisioning or shared start only')
    parser.add_argument('--shared-plan-sha256')
    parser.add_argument('--shared-provision-sha256', help='Exact prepared workspace receipt SHA-256; explicit shared start only')
    parser.add_argument('--shared-activation', type=Path, help='Separately reviewed fresh shared-host activation; never minted by preparation')
    parser.add_argument('--shared-activation-sha256')
    parser.add_argument('--shared-output', type=Path, help='New private shared preparation output; never overwrite')
    parser.add_argument('--remote-entry-profile-sha256')
    parser.add_argument('--remote-entry-task-file', type=Path)
    parser.add_argument('--remote-entry-task-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-routes-plan-file', type=Path)
    parser.add_argument('--remote-routes-source-file', type=Path)
    parser.add_argument('--remote-routes-plan-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-static-assets-plan-file', type=Path)
    parser.add_argument('--remote-static-assets-plan-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-static-assets-source-file', type=Path)
    parser.add_argument('--remote-readonly-data-plan-file', type=Path)
    parser.add_argument('--remote-readonly-data-source-file', type=Path)
    parser.add_argument('--remote-route-review-source-database', type=Path)
    parser.add_argument('--remote-route-review-site-store', type=Path)
    parser.add_argument('--remote-route-review-store', type=Path)
    parser.add_argument('--remote-route-review-sha256')
    parser.add_argument('--remote-route-review-source-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-json-review-source-database', type=Path)
    parser.add_argument('--remote-json-review-site-store', type=Path)
    parser.add_argument('--remote-json-review-store', type=Path)
    parser.add_argument('--remote-json-review-sha256')
    parser.add_argument('--remote-json-review-source-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-form-plan-file', type=Path)
    parser.add_argument('--remote-form-plan-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-form-field-name')
    parser.add_argument('--remote-form-value-file', type=Path)
    parser.add_argument('--remote-form-fields-file', type=Path)
    parser.add_argument('--remote-form-fields-source-file', type=Path)
    parser.add_argument('--remote-form-fields-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-form-public-plan-sha256')
    parser.add_argument('--remote-form-submit-url')
    parser.add_argument('--remote-form-receipt-url')
    parser.add_argument('--remote-form-state-plan-file', type=Path)
    parser.add_argument('--remote-form-state-plan-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--remote-form-public-state-plan-sha256')
    parser.add_argument('--remote-form-state-url')
    parser.add_argument('--remote-form-state-before-sha256')
    parser.add_argument('--remote-form-state-after-sha256')
    parser.add_argument('--remote-form-state-marker-id')
    parser.add_argument('--remote-form-state-before-marker-sha256')
    parser.add_argument('--remote-form-state-after-marker-sha256')
    parser.add_argument('--remote-form-state-submitted-field-name')
    parser.add_argument('--remote-form-cookie-file', type=Path)
    parser.add_argument('--remote-form-cookie-sha256')
    parser.add_argument('--session', help=argparse.SUPPRESS)
    parser.add_argument('--mode', choices=['real', 'fixture'], help=argparse.SUPPRESS)
    parser.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--listen-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--base', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--owned-form-listener-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--owned-form-manifest-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--owned-learning-lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--owned-skill-reuse-sha256', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    instance_token = None
    try:
        if (arguments.project is None) != (arguments.project_port is None):
            raise ValueError('--project and --project-port must be supplied together')
        if arguments.project is not None:
            instance = LocalAppInstance.for_project(arguments.project, arguments.project_port)
            if arguments.command not in {'start', 'status', 'token', 'stop', 'open', 'doctor', 'prepare-ui', 'prepare-shared', 'provision-shared',
                                         'restart', 'release-restart', 'recover-clean-exit', 'recover-reboot',
                                         'preview-owned-skill-reuse', '_supervise'}:
                raise ValueError('Named projects support prepare-ui/start/status/token/stop/open/doctor/reuse preview, restart/release-restart and explicit recovery only')
            if (any(value is not None for name, value in vars(arguments).items() if name.startswith('remote_'))
                    or arguments.synthetic_staging or arguments.synthetic_learning
                    or arguments.owned_synthetic_form_recipe):
                raise ValueError('Named projects support explicit owned learning or plain CPU fixture scope only')
            if any(value is not None for value in (
                    arguments.owned_parameter_project_directory,
                    arguments.owned_parameter_project_manifest_sha256,
                    arguments.owned_parameter_project_engine)) and (
                    arguments.command not in {'start', '_supervise'}
                    or arguments.owned_parameter_project_directory is None
                    or re.fullmatch(r'[a-f0-9]{64}', arguments.owned_parameter_project_manifest_sha256 or '') is None
                    or arguments.owned_parameter_project_engine != 'fixture'
                    or arguments.fixture or arguments.owned_synthetic_form_invocation
                    or arguments.command == '_supervise' and arguments.mode != 'real'):
                raise ValueError('Named parameter activation requires exact source pins and explicit fixture engine')
            instance_token = _INSTANCE.set(instance)
            if arguments.command != '_supervise':
                read_state()
        shared_options = {name: value for name, value in vars(arguments).items() if name.startswith('shared_')}
        if arguments.command in {'prepare-shared', 'provision-shared'} or any(value is not None for value in shared_options.values()):
            allowed = {'command', 'expected_session', 'project', 'project_port', *shared_options}
            if any(value is not None and value is not False for name, value in vars(arguments).items() if name not in allowed):
                raise ValueError('Shared mode is explicit and cannot combine native, fixture, remote or owned-learning options')
            if arguments.command == 'prepare-shared':
                if any(shared_options[name] is not None for name in ('shared_plan', 'shared_plan_sha256', 'shared_activation', 'shared_activation_sha256', 'shared_provision_sha256')):
                    raise ValueError('Inert shared preparation cannot consume activation or launch a runtime')
                from .shared_desktop_manager import prepare_shared

                result = prepare_shared(arguments.shared_template, arguments.shared_template_sha256,
                                        arguments.shared_output, expected_session=arguments.expected_session)
            elif arguments.command == 'provision-shared':
                if any(shared_options[name] is not None for name in (
                        'shared_template', 'shared_template_sha256', 'shared_output',
                        'shared_activation', 'shared_activation_sha256', 'shared_provision_sha256')):
                    raise ValueError('Shared provisioning consumes only a pinned plan; no activation or runtime authority')
                from .shared_desktop_manager import provision_shared

                result = provision_shared(arguments.shared_plan, arguments.shared_plan_sha256,
                                          expected_session=arguments.expected_session)
            elif arguments.command == 'start':
                if any(shared_options[name] is not None for name in ('shared_template', 'shared_template_sha256', 'shared_output')):
                    raise ValueError('Shared start consumes a reviewed plan, not an inert template')
                from .shared_desktop_manager import start_shared

                result = start_shared(arguments.shared_plan, arguments.shared_plan_sha256,
                                      arguments.shared_activation, arguments.shared_activation_sha256,
                                      provision_sha256=arguments.shared_provision_sha256,
                                      expected_session=arguments.expected_session)
            else:
                raise ValueError('Shared options are accepted only for prepare-shared, provision-shared or explicit start')
            print(canonical(result))
            return
        lifecycle_commands = {'stop', 'restart', 'release-restart', 'recover-clean-exit', 'recover-reboot'}
        if arguments.expected_session is not None and arguments.command not in lifecycle_commands:
            raise ValueError('--expected-session is valid only with stop/restart/release/recovery')
        if current_instance().project is not None and arguments.command in lifecycle_commands:
            _check_expected_session(read_state(), arguments.expected_session)
        if arguments.command == '_supervise':
            if (arguments.base is None or arguments.base.parent != REPO_ROOT / 'data'
                    or not re.fullmatch('local-app-(v1|test-[a-f0-9]{32}|project-[a-z0-9][a-z0-9-]{0,47})', arguments.base.name)
                    or (arguments.owned_parameter_project_directory is not None)
                    != (arguments.owned_parameter_project_engine == 'fixture')
                    or arguments.owned_synthetic_form_invocation
                    and arguments.owned_synthetic_form_recipe
                    or (arguments.owned_synthetic_form_invocation
                        or arguments.owned_synthetic_form_recipe
                        or arguments.owned_parameter_project_directory is not None)
                    != (arguments.owned_form_listener_fd is not None)
                    or arguments.remote_entry_task_file is not None
                    or arguments.remote_routes_plan_file is not None
                    or arguments.remote_routes_source_file is not None
                    or arguments.remote_static_assets_plan_file is not None
                    or arguments.remote_static_assets_source_file is not None
                    or arguments.remote_readonly_data_plan_file is not None
                    or arguments.remote_readonly_data_source_file is not None
                    or arguments.remote_form_plan_file is not None
                    or arguments.remote_form_value_file is not None
                    or arguments.remote_form_fields_file is not None
                    or arguments.remote_form_fields_source_file is not None
                    or arguments.remote_form_public_plan_sha256 is not None
                    or arguments.remote_form_submit_url is not None
                    or arguments.remote_form_receipt_url is not None
                    or arguments.remote_form_state_plan_file is not None
                    or arguments.remote_form_public_state_plan_sha256 is not None
                    or arguments.remote_form_state_url is not None
                    or arguments.remote_form_state_before_sha256 is not None
                    or arguments.remote_form_state_after_sha256 is not None
                    or arguments.remote_form_cookie_file is not None):
                raise ValueError('Manager base must be the private local application directory')
            if arguments.project is not None:
                if arguments.base != current_instance().base:
                    raise ValueError('Inherited manager base differs from its named project')
            else:
                instance_token = _INSTANCE.set(LocalAppInstance(base=arguments.base, port=8765))
            if os.fork():
                return
            supervise(arguments.session, arguments.mode, arguments.lock_fd, arguments.listen_fd,
                      synthetic_staging=arguments.synthetic_staging,
                      synthetic_learning=arguments.synthetic_learning,
                      owned_synthetic_form_recipe=(None if arguments.owned_parameter_project_directory is not None
                                                   else arguments.owned_synthetic_form_recipe),
                      owned_form_listener_fd=arguments.owned_form_listener_fd,
                      owned_learning_lock_fd=arguments.owned_learning_lock_fd,
                      owned_skill_reuse_sha256=arguments.owned_skill_reuse_sha256,
                      owned_form_manifest_sha256=arguments.owned_form_manifest_sha256,
                      owned_parameter_project_directory=arguments.owned_parameter_project_directory,
                      owned_parameter_project_manifest_sha256=arguments.owned_parameter_project_manifest_sha256,
                      remote_entry_profile_sha256=arguments.remote_entry_profile_sha256,
                      remote_entry_task_sha256=arguments.remote_entry_task_sha256,
                      remote_routes_plan_sha256=arguments.remote_routes_plan_sha256,
                      remote_static_assets_plan_sha256=(
                          arguments.remote_static_assets_plan_sha256),
                      remote_route_review_source_database=arguments.remote_route_review_source_database,
                      remote_route_review_site_store=arguments.remote_route_review_site_store,
                      remote_route_review_store=arguments.remote_route_review_store,
                      remote_route_review_sha256=arguments.remote_route_review_sha256,
                      remote_route_review_source_sha256=arguments.remote_route_review_source_sha256,
                      remote_json_review_source_database=arguments.remote_json_review_source_database,
                      remote_json_review_site_store=arguments.remote_json_review_site_store,
                      remote_json_review_store=arguments.remote_json_review_store,
                      remote_json_review_sha256=arguments.remote_json_review_sha256,
                      remote_json_review_source_sha256=arguments.remote_json_review_source_sha256,
                      remote_form_plan_sha256=arguments.remote_form_plan_sha256,
                      remote_form_field_name=arguments.remote_form_field_name,
                      remote_form_fields_sha256=arguments.remote_form_fields_sha256,
                      remote_form_state_plan_sha256=arguments.remote_form_state_plan_sha256,
                      remote_form_cookie_sha256=arguments.remote_form_cookie_sha256)
            return
        if any(value is not None for value in (arguments.session, arguments.mode, arguments.lock_fd,
                                               arguments.listen_fd, arguments.base,
                                               arguments.owned_form_listener_fd,
                                               arguments.owned_learning_lock_fd,
                                               arguments.owned_skill_reuse_sha256,
                                               arguments.owned_form_manifest_sha256,
                                               arguments.remote_entry_task_sha256,
                                               arguments.remote_routes_plan_sha256,
                                               arguments.remote_static_assets_plan_sha256,
                                               arguments.remote_route_review_source_sha256,
                                               arguments.remote_json_review_source_sha256,
                                               arguments.remote_form_plan_sha256,
                                               arguments.remote_form_fields_sha256,
                                               arguments.remote_form_state_plan_sha256)):
            raise ValueError('Internal manager options are not accepted by public commands')
        if arguments.synthetic_staging and arguments.command != 'start':
            raise ValueError('Synthetic staging is a start-only option')
        if ((arguments.owned_parameter_project_directory is not None
             or arguments.owned_parameter_project_manifest_sha256 is not None
             or arguments.owned_parameter_project_engine is not None)
                and arguments.command != 'start'):
            raise ValueError('Owned parameter project activation is a start-only option')
        if arguments.synthetic_learning and arguments.command != 'start':
            raise ValueError('Synthetic learning is a start-only option')
        if ((arguments.owned_synthetic_form_invocation
             or arguments.owned_synthetic_form_recipe)
                and arguments.command != 'start'):
            raise ValueError('Owned synthetic form invocation is a start-only option')
        if arguments.command == 'restart' and arguments.fixture:
            raise ValueError('Restart preserves the existing mode; --fixture is a start-only option')
        if ((arguments.remote_entry_profile_sha256 is not None or arguments.remote_entry_task_file is not None)
                and arguments.command not in {'start', 'preview-remote-entry',
                                              'preview-remote-routes', 'preview-remote-route-review',
                                              'plan-remote-routes',
                                              'plan-remote-static-assets',
                                              'preview-remote-static-assets',
                                              'plan-remote-readonly-data',
                                              'preview-remote-readonly-data',
                                              'preview-remote-json-review',
                                              'plan-remote-form',
                                              'preview-remote-form',
                                              'plan-remote-form-state',
                                              'preview-remote-form-state',
                                              'plan-remote-form-cookie',
                                              'preview-remote-form-cookie'}):
            raise ValueError('Remote entry options require start or preview-remote-entry')
        if (arguments.remote_routes_plan_file is not None
                and arguments.command not in {'start', 'plan-remote-routes', 'preview-remote-routes',
                                              'preview-remote-route-review'}):
            raise ValueError('Read-only route plan requires plan, start or preview-remote-routes')
        if (arguments.remote_routes_source_file is not None
                and arguments.command != 'plan-remote-routes'):
            raise ValueError('Read-only route source is only accepted by plan-remote-routes')
        if (arguments.remote_static_assets_plan_file is not None
                and arguments.command not in {'start', 'plan-remote-static-assets',
                                              'preview-remote-static-assets'}):
            raise ValueError('Static asset plan requires plan, preview or start')
        if (arguments.remote_readonly_data_plan_file is not None
                and arguments.command not in {'start', 'plan-remote-readonly-data',
                                              'preview-remote-readonly-data',
                                              'preview-remote-json-review'}):
            raise ValueError('Read-only data plan requires plan, preview or start')
        if (arguments.remote_static_assets_plan_file is not None
                and arguments.remote_readonly_data_plan_file is not None):
            raise ValueError('Only one web bundle plan may be pinned')
        if (arguments.remote_static_assets_source_file is not None
                and arguments.command != 'plan-remote-static-assets'):
            raise ValueError('Static asset source is only accepted by plan-remote-static-assets')
        if (arguments.remote_readonly_data_source_file is not None
                and arguments.command != 'plan-remote-readonly-data'):
            raise ValueError('Read-only data source is only accepted by plan-remote-readonly-data')
        if (any(option is not None for option in (
                arguments.remote_route_review_source_database,
                arguments.remote_route_review_site_store,
                arguments.remote_route_review_store,
                arguments.remote_route_review_sha256))
                and arguments.command not in {'start', 'preview-remote-route-review'}):
            raise ValueError('Read-only route review options require start or preview')
        if (any(option is not None for option in (
                arguments.remote_json_review_source_database,
                arguments.remote_json_review_site_store,
                arguments.remote_json_review_store,
                arguments.remote_json_review_sha256))
                and arguments.command not in {'start', 'preview-remote-json-review'}):
            raise ValueError('JSON review options require start or preview')
        if (any(value is not None for value in (
                arguments.remote_form_plan_file, arguments.remote_form_field_name,
                arguments.remote_form_value_file, arguments.remote_form_fields_file,
                arguments.remote_form_public_plan_sha256))
                and arguments.command not in {'start', 'plan-remote-form-fields',
                                              'plan-remote-form', 'preview-remote-form',
                                              'plan-remote-form-state',
                                              'preview-remote-form-state',
                                              'plan-remote-form-cookie',
                                              'preview-remote-form-cookie'}):
            raise ValueError('Public HTTPS form options require plan, preview or start')
        if (arguments.remote_form_fields_source_file is not None
                and arguments.command != 'plan-remote-form-fields'):
            raise ValueError('Ordered form source is only accepted by plan-remote-form-fields')
        if (any(value is not None for value in (
                arguments.remote_form_state_plan_file,
                arguments.remote_form_public_state_plan_sha256))
                and arguments.command not in {'start', 'plan-remote-form-state',
                                              'preview-remote-form-state'}):
            raise ValueError('HTTPS form state options require plan, preview or start')
        if (any(value is not None for value in (
                arguments.remote_form_state_url,
                arguments.remote_form_state_before_sha256,
                arguments.remote_form_state_after_sha256,
                arguments.remote_form_state_marker_id,
                arguments.remote_form_state_before_marker_sha256,
                arguments.remote_form_state_after_marker_sha256,
                arguments.remote_form_state_submitted_field_name))
                and arguments.command != 'plan-remote-form-state'):
            raise ValueError('HTTPS form state URL and response hashes are plan-only')
        if (arguments.remote_form_cookie_file is not None
                or arguments.remote_form_cookie_sha256 is not None):
            if arguments.command not in {'start', 'plan-remote-form-cookie',
                                          'preview-remote-form-cookie'}:
                raise ValueError('HTTPS form cookie options require plan, preview or start')
        if (arguments.remote_form_submit_url is not None
                or arguments.remote_form_receipt_url is not None):
            if arguments.command != 'plan-remote-form':
                raise ValueError('Form target URLs are only accepted by plan-remote-form')
        if arguments.command == 'plan-remote-routes':
            if arguments.fixture:
                raise ValueError('Read-only route planning requires real mode')
            print(canonical(plan_remote_routes(
                arguments.remote_entry_profile_sha256, arguments.remote_entry_task_file,
                arguments.remote_routes_plan_file, arguments.remote_routes_source_file)))
            return
        if arguments.command == 'preview-remote-json-review':
            if arguments.fixture:
                raise ValueError('JSON review requires real mode')
            prepared = prepare_remote_static_assets(
                'real', arguments.remote_entry_profile_sha256,
                arguments.remote_entry_task_file,
                arguments.remote_readonly_data_plan_file)
            if (prepared is None
                    or json.loads(prepared[0])['schema_version'] != '2.0'):
                raise ValueError('JSON review requires a private v2 plan file')
            review = prepare_managed_remote_json_review(
                'real', arguments.remote_entry_profile_sha256, prepared[1],
                arguments.remote_json_review_source_database,
                arguments.remote_json_review_site_store,
                arguments.remote_json_review_store,
                arguments.remote_json_review_sha256)
            if review is None:
                raise ValueError('JSON review requires four exact source options')
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'plan_sha256': prepared[1], 'review_sha256': review[3],
                             'source_snapshot_sha256': review[4],
                             'status': 'validated_historical_metadata',
                             'execution_authorized': False, 'collection_authorized': False,
                             'training_ready': False}))
            return
        if arguments.command == 'plan-remote-static-assets':
            if arguments.fixture:
                raise ValueError('Static asset planning requires real mode')
            print(canonical(plan_remote_static_assets(
                arguments.remote_entry_profile_sha256, arguments.remote_entry_task_file,
                arguments.remote_static_assets_plan_file,
                arguments.remote_static_assets_source_file)))
            return
        if arguments.command == 'plan-remote-readonly-data':
            if arguments.fixture:
                raise ValueError('Read-only data planning requires real mode')
            print(canonical(plan_remote_readonly_data(
                arguments.remote_entry_profile_sha256, arguments.remote_entry_task_file,
                arguments.remote_readonly_data_plan_file,
                arguments.remote_readonly_data_source_file)))
            return
        if arguments.command == 'plan-remote-form-fields':
            if (arguments.fixture or any(option is not None for option in (
                    arguments.remote_form_plan_file,
                    arguments.remote_form_field_name,
                    arguments.remote_form_value_file,
                    arguments.remote_form_public_plan_sha256))):
                raise ValueError('Ordered form fields planning only accepts private source and destination')
            print(canonical(plan_remote_form_fields(
                arguments.remote_form_fields_source_file,
                arguments.remote_form_fields_file)))
            return
        if arguments.command == 'plan-remote-form':
            if arguments.fixture or arguments.remote_form_public_plan_sha256 is not None:
                raise ValueError('Public form planning does not accept fixture mode or a grant')
            print(canonical(plan_remote_form(
                arguments.remote_entry_profile_sha256, arguments.remote_entry_task_file,
                arguments.remote_form_plan_file, arguments.remote_form_field_name,
                arguments.remote_form_value_file, arguments.remote_form_submit_url,
                arguments.remote_form_receipt_url,
                fields_file=arguments.remote_form_fields_file)))
            return
        if arguments.command == 'plan-remote-form-state':
            if arguments.fixture or arguments.remote_form_public_state_plan_sha256 is not None:
                raise ValueError('HTTPS form state planning does not accept fixture mode or a state grant')
            print(canonical(plan_remote_form_state(
                arguments.remote_entry_profile_sha256, arguments.remote_entry_task_file,
                arguments.remote_form_plan_file, arguments.remote_form_field_name,
                arguments.remote_form_value_file, arguments.remote_form_public_plan_sha256,
                arguments.remote_form_state_plan_file, arguments.remote_form_state_url,
                arguments.remote_form_state_before_sha256,
                arguments.remote_form_state_after_sha256,
                arguments.remote_form_state_marker_id,
                arguments.remote_form_state_before_marker_sha256,
                arguments.remote_form_state_after_marker_sha256,
                arguments.remote_form_state_submitted_field_name,
                fields_file=arguments.remote_form_fields_file)))
            return
        if arguments.command == 'plan-remote-form-cookie':
            if arguments.fixture or arguments.remote_form_cookie_sha256 is not None:
                raise ValueError('HTTPS form cookie planning does not accept fixture mode or a hash')
            prepared_cookie = prepare_remote_form_cookie(
                'real', arguments.remote_entry_profile_sha256,
                arguments.remote_entry_task_file, arguments.remote_form_plan_file,
                arguments.remote_form_field_name, arguments.remote_form_value_file,
                arguments.remote_form_public_plan_sha256,
                arguments.remote_form_cookie_file, None,
                require_confirmation=False,
                fields_file=arguments.remote_form_fields_file)
            if prepared_cookie is None:
                raise ValueError('HTTPS form cookie planning requires an owner-only file')
            print(canonical({'cookie_sha256': prepared_cookie[1], 'status': 'draft',
                             'execution_authorized': False, 'collection_authorized': False}))
            return
        if arguments.command == 'preview-remote-entry':
            if arguments.fixture:
                raise ValueError('Remote entry requires real mode')
            remote_entry = prepare_remote_entry('real', arguments.remote_entry_profile_sha256,
                                                arguments.remote_entry_task_file)
            if remote_entry is None:
                raise ValueError('Remote entry requires an exact profile hash and private task file together')
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'task_sha256': remote_entry[1], 'status': 'validated_input',
                             'execution_authorized': False, 'collection_authorized': False}))
            return
        if arguments.command == 'preview-remote-routes':
            if arguments.fixture:
                raise ValueError('Read-only routes require real mode')
            prepared = prepare_remote_routes('real', arguments.remote_entry_profile_sha256,
                                             arguments.remote_entry_task_file,
                                             arguments.remote_routes_plan_file)
            if prepared is None:
                raise ValueError('Read-only routes require a private plan file')
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'plan_sha256': prepared[1], 'status': 'validated_input',
                             'execution_authorized': False, 'collection_authorized': False}))
            return
        if arguments.command == 'preview-remote-static-assets':
            if arguments.fixture:
                raise ValueError('Static assets require real mode')
            prepared = prepare_remote_static_assets(
                'real', arguments.remote_entry_profile_sha256,
                arguments.remote_entry_task_file, arguments.remote_static_assets_plan_file)
            if prepared is None:
                raise ValueError('Static assets require a private plan file')
            if json.loads(prepared[0])['schema_version'] != '1.0':
                raise ValueError('Static asset preview requires a v1 plan')
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'plan_sha256': prepared[1], 'status': 'validated_input',
                             'execution_authorized': False, 'collection_authorized': False}))
            return
        if arguments.command == 'preview-remote-readonly-data':
            if arguments.fixture:
                raise ValueError('Read-only data requires real mode')
            prepared = prepare_remote_static_assets(
                'real', arguments.remote_entry_profile_sha256,
                arguments.remote_entry_task_file, arguments.remote_readonly_data_plan_file)
            if (prepared is None
                    or json.loads(prepared[0])['schema_version'] != '2.0'):
                raise ValueError('Read-only data requires a private v2 plan file')
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'plan_sha256': prepared[1], 'status': 'validated_input',
                             'execution_authorized': False, 'collection_authorized': False}))
            return
        if arguments.command == 'preview-remote-route-review':
            if arguments.fixture:
                raise ValueError('Read-only route review requires real mode')
            routes = prepare_remote_routes('real', arguments.remote_entry_profile_sha256,
                                           arguments.remote_entry_task_file,
                                           arguments.remote_routes_plan_file)
            if routes is None:
                raise ValueError('Read-only route review requires a private plan file')
            review = prepare_managed_remote_route_review(
                'real', arguments.remote_entry_profile_sha256, routes[1],
                arguments.remote_route_review_source_database,
                arguments.remote_route_review_site_store,
                arguments.remote_route_review_store,
                arguments.remote_route_review_sha256)
            if review is None:
                raise ValueError('Read-only route review requires four exact source options')
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'plan_sha256': routes[1], 'review_sha256': review[3],
                             'source_snapshot_sha256': review[4],
                             'status': 'validated_historical_metadata',
                             'execution_authorized': False, 'collection_authorized': False,
                             'training_ready': False}))
            return
        if arguments.command == 'preview-remote-form':
            if arguments.fixture:
                raise ValueError('Public HTTPS form requires real mode')
            prepared = prepare_remote_form(
                'real', arguments.remote_entry_profile_sha256,
                arguments.remote_entry_task_file, arguments.remote_form_plan_file,
                arguments.remote_form_field_name, arguments.remote_form_value_file,
                arguments.remote_form_public_plan_sha256,
                fields_file=arguments.remote_form_fields_file)
            if prepared is None:
                raise ValueError('Public HTTPS form requires a private plan and value file')
            from .web_https_form_transport import (WebHTTPSFormPlan,
                                                   parse_form_fields_document)

            plan = WebHTTPSFormPlan.model_validate_json(prepared[0])
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'plan_sha256': prepared[1], 'entry_url': plan.entry_url,
                             'submit_url': plan.submit_url, 'receipt_url': plan.receipt_url,
                             **({'field_name': arguments.remote_form_field_name}
                                if arguments.remote_form_fields_file is None else
                                {'field_names': [name for name, _value in
                                                 parse_form_fields_document(prepared[2])]}),
                             'body_sha256': plan.body_sha256, 'status': 'validated_input',
                             'execution_authorized': False, 'collection_authorized': False}))
            return
        if arguments.command == 'preview-remote-form-state':
            if arguments.fixture:
                raise ValueError('HTTPS form state requires real mode')
            prepared = prepare_remote_form_state(
                'real', arguments.remote_entry_profile_sha256,
                arguments.remote_entry_task_file, arguments.remote_form_plan_file,
                arguments.remote_form_field_name, arguments.remote_form_value_file,
                arguments.remote_form_public_plan_sha256,
                arguments.remote_form_state_plan_file,
                arguments.remote_form_public_state_plan_sha256,
                fields_file=arguments.remote_form_fields_file)
            if prepared is None:
                raise ValueError('HTTPS form state requires an exact private plan')
            from .web_https_form_state_probe import WebHTTPSFormStatePlan

            plan = WebHTTPSFormStatePlan.model_validate_json(prepared[0])
            print(canonical({'profile_sha256': arguments.remote_entry_profile_sha256,
                             'form_plan_sha256': plan.form_plan_sha256,
                             'state_plan_sha256': prepared[1], 'state_url': plan.state_url,
                             'status': 'validated_input', 'execution_authorized': False,
                             'collection_authorized': False}))
            return
        if arguments.command == 'preview-remote-form-cookie':
            if arguments.fixture:
                raise ValueError('HTTPS form cookie requires real mode')
            prepared_cookie = prepare_remote_form_cookie(
                'real', arguments.remote_entry_profile_sha256,
                arguments.remote_entry_task_file, arguments.remote_form_plan_file,
                arguments.remote_form_field_name, arguments.remote_form_value_file,
                arguments.remote_form_public_plan_sha256,
                arguments.remote_form_cookie_file,
                arguments.remote_form_cookie_sha256,
                fields_file=arguments.remote_form_fields_file)
            if prepared_cookie is None:
                raise ValueError('HTTPS form cookie requires an exact private file and hash')
            print(canonical({'cookie_sha256': prepared_cookie[1],
                             'status': 'validated_input', 'execution_authorized': False,
                             'collection_authorized': False}))
            return
        if arguments.command == 'prepare-ui':
            if arguments.fixture:
                raise ValueError('prepare-ui builds assets only; runtime engine flags are not accepted')
            print(canonical(prepare_ui()))
            return
        if arguments.command == 'doctor':
            from .local_preflight import check_local

            mode = 'fixture' if arguments.fixture else 'real'
            report = (check_local(mode, frontend_root=current_instance().base / 'ui')
                      if current_instance().project is not None else check_local(mode))
            print(canonical(report))
            if not report['ready']:
                raise SystemExit(1)
            return
        if arguments.command == 'preview-owned-skill-reuse':
            if (arguments.fixture or arguments.synthetic_staging or arguments.synthetic_learning
                    or arguments.owned_synthetic_form_invocation or arguments.owned_synthetic_form_recipe
                    or arguments.owned_skill_reuse_confirm_sha256 is not None):
                raise ValueError('Owned skill reuse preview is a separate read-only real-source operation')
            material = prepare_owned_skill_reuse(
                read_state(), arguments.owned_skill_release_sha256,
                arguments.owned_skill_selection_sha256)
            print(canonical({'preview': material['preview'], 'preview_sha256': material['preview_sha256']}))
            return
        if arguments.command == 'start':
            result = start('fixture' if arguments.fixture else 'real',
                           synthetic_staging=arguments.synthetic_staging,
                           synthetic_learning=arguments.synthetic_learning,
                           owned_synthetic_form_invocation=(
                               arguments.owned_synthetic_form_invocation),
                           owned_synthetic_form_recipe=(
                               arguments.owned_synthetic_form_recipe),
                           owned_parameter_project_directory=arguments.owned_parameter_project_directory,
                           owned_parameter_project_manifest_sha256=arguments.owned_parameter_project_manifest_sha256,
                           owned_parameter_project_engine=arguments.owned_parameter_project_engine,
                           owned_skill_release_sha256=arguments.owned_skill_release_sha256,
                           owned_skill_selection_sha256=arguments.owned_skill_selection_sha256,
                           owned_skill_reuse_confirm_sha256=arguments.owned_skill_reuse_confirm_sha256,
                           remote_entry_profile_sha256=arguments.remote_entry_profile_sha256,
                           remote_entry_task_file=arguments.remote_entry_task_file,
                           remote_routes_plan_file=arguments.remote_routes_plan_file,
                           remote_static_assets_plan_file=(
                               arguments.remote_static_assets_plan_file),
                           remote_readonly_data_plan_file=(
                               arguments.remote_readonly_data_plan_file),
                           remote_route_review_source_database=(
                               arguments.remote_route_review_source_database),
                           remote_route_review_site_store=arguments.remote_route_review_site_store,
                           remote_route_review_store=arguments.remote_route_review_store,
                           remote_route_review_sha256=arguments.remote_route_review_sha256,
                           remote_json_review_source_database=(
                               arguments.remote_json_review_source_database),
                           remote_json_review_site_store=arguments.remote_json_review_site_store,
                           remote_json_review_store=arguments.remote_json_review_store,
                           remote_json_review_sha256=arguments.remote_json_review_sha256,
                           remote_form_plan_file=arguments.remote_form_plan_file,
                           remote_form_field_name=arguments.remote_form_field_name,
                           remote_form_value_file=arguments.remote_form_value_file,
                           remote_form_fields_file=arguments.remote_form_fields_file,
                           remote_form_public_plan_sha256=arguments.remote_form_public_plan_sha256,
                           remote_form_state_plan_file=arguments.remote_form_state_plan_file,
                           remote_form_public_state_plan_sha256=(
                               arguments.remote_form_public_state_plan_sha256),
                           remote_form_cookie_file=arguments.remote_form_cookie_file,
                           remote_form_cookie_sha256=arguments.remote_form_cookie_sha256)
        elif arguments.command == 'restart':
            result = restart(**({'expected_session': arguments.expected_session} if arguments.expected_session is not None else {}))
        elif arguments.command == 'release-restart':
            result = release_restart_quiesce(**({'expected_session': arguments.expected_session} if arguments.expected_session is not None else {}))
        elif arguments.command == 'stop':
            result = stop(expected_session=arguments.expected_session)
        elif arguments.command == 'recover-reboot':
            result = recover_reboot(**({'expected_session': arguments.expected_session} if arguments.expected_session is not None else {}))
        elif arguments.command == 'recover-clean-exit':
            result = recover_clean_exit(**({'expected_session': arguments.expected_session} if arguments.expected_session is not None else {}))
        elif arguments.command == 'token':
            state = read_state()
            if state is None:
                raise ValueError('Önce uygulamayı başlatın')
            print(token_value(state))
            return
        elif arguments.command == 'open':
            result = current_status()
            if result['phase'] != 'running':
                raise ValueError('Önce uygulamayı başlatın')
            subprocess.run(['/usr/bin/xdg-open', current_instance().url], check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            result = current_status()
        print(canonical(result))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, 'Yerel uygulama işlemi tamamlanamadı: ' + str(error) + '\n')
    finally:
        if instance_token is not None:
            _INSTANCE.reset(instance_token)


if __name__ == '__main__':
    from aos.local_app import main as canonical_main

    canonical_main()

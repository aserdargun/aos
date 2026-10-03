"""Explicit desktop startup sources for reviewed synthetic parameter projects."""

import os
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

from .contracts import digest
from .owned_form_fixture import HOST, OwnedFormFixture
from .owned_parameter_project import MODE, read_owned_parameter_project
from .site_skill_form_recipe import revalidate_site_skill_form_recipe_invocation
from .site_skill_form_recipe_audit import audit_site_skill_form_recipe_execution


def verify_owned_parameter_project_listener(listener_fd: int, manifest: dict):
    """Require the already-owned IPv4 listener at the exact reviewed fixture port."""
    if type(listener_fd) is not int or listener_fd < 0:
        raise ValueError('owned_parameter_project_listener_required')
    origin = urlsplit(manifest['origin'])
    if (origin.scheme != 'https' or origin.hostname != HOST
            or origin.port != manifest['port'] or origin.path or origin.query
            or origin.fragment or origin.username is not None or origin.password is not None
            or manifest['origin'] != f'https://{HOST}:{manifest["port"]}'):
        raise ValueError('owned_parameter_project_listener_origin_invalid')
    with socket.socket(fileno=os.dup(listener_fd)) as listener:
        if (listener.family != socket.AF_INET or listener.type != socket.SOCK_STREAM
                or listener.getsockname() != ('127.0.0.1', manifest['port'])
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
            raise ValueError('owned_parameter_project_listener_mismatch')
        metadata = os.fstat(listener.fileno())
        return (metadata.st_dev, metadata.st_ino)


class OwnedParameterProjectStartup:
    """Retain reviewed source pins, with fresh checks before execution and audit."""

    def __init__(self, bundle, listener_identity):
        self.bundle = bundle
        self.directory = bundle['directory']
        self.manifest_sha256 = bundle['manifest_sha256']
        self.listener_identity = listener_identity
        self.workspace_lock = None
        self.listener_fd = None
        self.fixture = None
        self.closed = False

    def retain_workspace(self, workspace_lock):
        if workspace_lock.root != self.directory:
            raise ValueError('owned_parameter_project_workspace_mismatch')
        workspace_lock.assert_current()
        self.workspace_lock = workspace_lock
        self.source()

    def source(self):
        if self.closed:
            raise ValueError('owned_parameter_project_startup_closed')
        if self.workspace_lock is not None:
            self.workspace_lock.assert_current()
        current = read_owned_parameter_project(self.directory, self.manifest_sha256)
        if (current['manifest'] != self.bundle['manifest']
                or current['invocation'] != self.bundle['invocation']
                or current['fields'] != self.bundle['fields']
                or current['body'] != self.bundle['body']):
            raise ValueError('owned_parameter_project_startup_source_changed')
        return current

    def revalidate(self):
        current = self.source()
        return revalidate_site_skill_form_recipe_invocation(
            self.bundle['invocation'], current['skill_store'], current['skill_plan'],
            current['case_inputs'], current['case_key'], current['profiles'],
            current['task'], current['form_plan'], current['state_plan'],
            current['field_bindings'], current['recipe'])

    def audit(self, database: Path, run_id: str):
        current = self.source()
        invocation = self.revalidate()
        return audit_site_skill_form_recipe_execution(
            current['skill_store'], current['skill_plan'], current['case_inputs'],
            current['case_key'], current['profiles'], current['task'],
            current['form_plan'], current['state_plan'], current['field_bindings'],
            current['recipe'], invocation, database, run_id)

    def create_fixture(self, listener_fd: int):
        if self.workspace_lock is None or self.fixture is not None or self.listener_fd is not None:
            raise ValueError('owned_parameter_project_workspace_required')
        current = self.source()
        if (verify_owned_parameter_project_listener(listener_fd, current['manifest'])
                != self.listener_identity):
            raise ValueError('owned_parameter_project_listener_identity_changed')
        retained = os.dup(listener_fd)
        try:
            fixture = self._fixture(current, retained)
        except BaseException:
            os.close(retained)
            raise
        self.listener_fd = retained
        self.fixture = fixture
        return fixture

    def _fixture(self, current, listener_fd):
        return OwnedFormFixture(
            listener_fd, origin=current['manifest']['origin'],
            profile_sha256=current['profile_sha256'],
            form_plan_sha256=current['form_plan_sha256'],
            state_plan_sha256=current['state_plan_sha256'],
            entry_url=current['form_plan'].entry_url,
            submit_url=current['form_plan'].submit_url,
            receipt_url=current['form_plan'].receipt_url,
            state_url=current['state_plan'].state_url, expected_body=current['body'],
            certificate_file=current['certificate_file'], key_file=current['key_file'],
            certificate_sha256=current['certificate_sha256'],
            record_config=current['record_config'])

    def create_reuse_fixture(self, bundle):
        original = self.source()
        if (self.workspace_lock is None or self.listener_fd is None or self.fixture is None
                or not self.fixture._closed
                or verify_owned_parameter_project_listener(self.listener_fd, original['manifest'])
                != self.listener_identity
                or bundle['manifest'] != original['manifest']
                or bundle['profile_sha256'] != original['profile_sha256']
                or bundle['task'] != original['task']
                or bundle['certificate_file'] != original['certificate_file']
                or bundle['key_file'] != original['key_file']
                or bundle['certificate_sha256'] != original['certificate_sha256']
                or bundle['record_config']['scope'] != original['record_config']['scope']
                or bundle['fields'] == original['fields']):
            raise ValueError('owned_parameter_project_reuse_fixture_unavailable')
        fixture = self._fixture(bundle, self.listener_fd)
        self.fixture = fixture
        return fixture

    def close(self):
        self.closed = True
        try:
            if self.fixture is not None:
                self.fixture.close()
        finally:
            if self.listener_fd is not None:
                os.close(self.listener_fd)
                self.listener_fd = None


def load_owned_parameter_project_startup(directory: Path, manifest_sha256: str,
                                        listener_fd: int):
    if (directory is None or not isinstance(directory, (str, Path))
            or '..' in Path(directory).parts
            or type(manifest_sha256) is not str
            or re.fullmatch('[a-f0-9]{64}', manifest_sha256) is None):
        raise ValueError('owned_parameter_project_startup_pins_required')
    bundle = read_owned_parameter_project(Path(directory).absolute(), manifest_sha256)
    if bundle['mode'] != MODE or digest(bundle['manifest']) != manifest_sha256:
        raise ValueError('owned_parameter_project_startup_manifest_mismatch')
    listener_identity = verify_owned_parameter_project_listener(listener_fd, bundle['manifest'])
    return OwnedParameterProjectStartup(bundle, listener_identity)

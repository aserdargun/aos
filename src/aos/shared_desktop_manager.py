from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import re

from .contracts import canonical, identifier


_HOST_FACTORY = ContextVar('aos_shared_desktop_host_factory', default=None)


@contextmanager
def shared_host_scope(factory):
    if not callable(factory):
        raise ValueError('Shared host composition requires a trusted host factory')
    token = _HOST_FACTORY.set(factory)
    try:
        yield
    finally:
        _HOST_FACTORY.reset(token)


def _host():
    from .shared_desktop_host import SharedDesktopHost

    factory = _HOST_FACTORY.get()
    host = factory() if factory is not None else SharedDesktopHost(predecessor_verifier=verify_predecessor)
    if not isinstance(host, SharedDesktopHost):
        raise ValueError('Shared manager requires its fixed reviewed host transport')
    return host


def _expected(current, expected_session):
    if expected_session == 'none':
        if current is not None:
            raise ValueError('A manager predecessor exists; explicit none cannot adopt it')
        return
    if (type(expected_session) is not str or re.fullmatch(r'app-[a-f0-9]{32}', expected_session) is None
            or current is None or current.session != expected_session):
        raise ValueError('Shared lifecycle requires the exact predecessor --expected-session, or explicit none')


def _scope(template):
    from . import local_app

    instance = local_app.current_instance()
    if (Path(template.manager_base) != instance.base or Path(template.session_root) != instance.base
            or template.project != instance.project or template.origin != instance.origin):
        raise ValueError('Shared template differs from the selected manager base, project or exact loopback origin')


def prepare_shared(template_path, template_sha256, output, *, expected_session):
    from . import local_app
    from .shared_desktop_plan import load_template, prepare_plan, verify_template_files

    if template_path is None or output is None:
        raise ValueError('Shared preparation requires a pinned template and a new private output path')
    template = load_template(Path(template_path), template_sha256)
    _scope(template)
    verify_template_files(template)
    previous = local_app.read_state()
    _expected(previous, expected_session)
    predecessor = None if previous is None else previous.model_dump(mode='json')
    snapshot_sha = None if previous is None else hashlib.sha256(
        local_app.private_read(local_app.current_instance().base / 'current.json')).hexdigest()
    plan = prepare_plan(template, predecessor=predecessor, new_session=identifier('app'),
                        predecessor_snapshot_sha256=snapshot_sha)
    if local_app.read_state() != previous:
        raise ValueError('Manager predecessor changed during inert shared preparation')
    raw = canonical(plan.model_dump(mode='json')).encode()
    local_app.write_new_private_plan(Path(output), raw)
    return {'phase': 'prepared', 'mode': 'shared', 'execution_authorized': False,
            'plan_path': str(Path(output).absolute()), 'plan_sha256': hashlib.sha256(raw).hexdigest(),
            'session': plan.app_session, 'predecessor_session': plan.predecessor_session,
            'url': template.origin + '/ui/', 'workspace': str(plan.workspace),
            'runtime_started': False, 'task_admission_enabled': False}


def verify_predecessor(plan, predecessor):
    from . import local_app
    from .shared_desktop_plan import predecessor_identity_sha256

    current = local_app.read_state()
    if predecessor is None:
        if current is not None or plan.predecessor_session is not None:
            raise ValueError('Shared start cannot adopt a newly created manager predecessor')
        return
    if (current is None or current.model_dump(mode='json') != predecessor
            or current.session != plan.predecessor_session
            or predecessor_identity_sha256(predecessor) != plan.predecessor_identity_sha256
            or current.phase != 'stopped'):
        raise ValueError('Shared start requires the exact already-stopped predecessor; no automatic interruption')
    if current.version == '2':
        if not current.cleanup_verified or not clean_shutdown(current):
            raise ValueError('Previous shared service cleanup remains unproven')
    elif (local_app.observe_process(current.supervisor) not in {'not_observed', 'different_boot'}
          or current.backend is None
          or local_app.observe_process(current.backend) not in {'not_observed', 'different_boot'}
          or not local_app.clean_shutdown(current)):
        raise ValueError('Native predecessor process and desktop cleanup remain unproven')


def provision_shared(plan_path, plan_sha256, *, expected_session):
    from . import local_app
    from .lifecycle import process_identity
    from .shared_desktop_plan import load_plan, predecessor_identity_sha256, verify_template_files
    from .shared_desktop_provision import PROVISION_NAME, provision_scope

    if plan_path is None:
        raise ValueError('Shared provisioning requires an exact pinned plan')
    plan = load_plan(Path(plan_path), plan_sha256)
    if plan.plan_sha256() != plan_sha256:
        raise ValueError('Shared provisioning requires canonical plan bytes')
    _scope(plan.template)
    verify_template_files(plan.template)
    with local_app.instance_lock():
        previous = local_app.read_state()
        _expected(previous, expected_session)
        predecessor = None if previous is None else previous.model_dump(mode='json')
        if (plan.predecessor_session != (None if previous is None else previous.session)
                or plan.predecessor_identity_sha256 != (
                    None if predecessor is None else predecessor_identity_sha256(predecessor))):
            raise ValueError('Shared provisioning requires the planned stable predecessor identity')
        provision = provision_scope(plan, current_boot_id=process_identity(os.getpid()).boot_id)
        current = local_app.read_state()
        if (current is None) != (previous is None) or current is not None and (
                current.session != plan.predecessor_session
                or predecessor_identity_sha256(current.model_dump(mode='json')) != plan.predecessor_identity_sha256):
            raise ValueError('Manager predecessor changed during provisioning; preserve scope without activation')
    return {'phase': 'provisioned', 'mode': 'shared', 'execution_authorized': False,
            'session': plan.app_session, 'predecessor_session': plan.predecessor_session,
            'provision_path': str(Path(plan.session_directory) / PROVISION_NAME),
            'provision_sha256': provision.provision_sha256(), 'workspace': plan.workspace,
            'workspace_identity': provision.workspace_identity.model_dump(mode='json'),
            'runtime_started': False, 'task_admission_enabled': False}


def start_shared(plan_path, plan_sha256, activation_path, activation_sha256, *, provision_sha256, expected_session):
    from . import local_app
    from .shared_desktop_plan import load_plan

    if plan_path is None or activation_path is None:
        raise ValueError('Shared start requires separately pinned plan and fresh activation inputs')
    plan = load_plan(Path(plan_path), plan_sha256)
    _scope(plan.template)
    previous = local_app.read_state()
    _expected(previous, expected_session)
    predecessor = None if previous is None else previous.model_dump(mode='json')
    verify_predecessor(plan, predecessor)
    host = _host()
    with local_app.instance_lock():
        if local_app.read_state() != previous:
            raise ValueError('Manager predecessor changed before shared admission')
        state = host.start(Path(plan_path), plan_sha256, Path(activation_path), activation_sha256,
                           provision_sha256=provision_sha256, predecessor=predecessor,
                           persist_state=local_app.write_state)
        local_app.current_instance().check_state(state)
        return host.status(state)


def status(state):
    from . import local_app

    local_app.current_instance().check_state(state)
    return _host().status(state)


def stop(state, *, expected_session):
    from . import local_app

    _expected(state, expected_session)
    local_app.current_instance().check_state(state)
    with local_app.instance_lock():
        if local_app.read_state() != state:
            raise ValueError('Shared session changed before exact owned shutdown')
        host = _host()
        stopped = host.stop(state, persist_state=local_app.write_state)
        return host.status(stopped)


def token_value(state):
    from . import local_app

    local_app.current_instance().check_state(state)
    return _host().token_value(state)


def clean_shutdown(state):
    if state.phase != 'stopped' or not state.cleanup_verified:
        return False
    observed = status(state)
    return observed.get('phase') == 'stopped' and observed.get('cleanup_verified') is True

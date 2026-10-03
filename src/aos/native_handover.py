import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field

from . import local_app
from .contracts import TypedModel, canonical, digest, now
from .lifecycle import ProcessIdentity, observe_process, process_identity
from .recovery_session import SessionInspection
from .shared_desktop_plan import write_new_private_file


ENDPOINTS = ('/api/state', '/api/tasks', '/api/session/binding', '/api/scientist/jobs')
RESPONSE_LIMIT = 262144


class ObservedNativeProcess(TypedModel):
    identity: ProcessIdentity
    discovery_parent_pid: int | None = Field(default=None, ge=1)
    cgroups: list[str] = Field(max_length=32)
    ownership_verified: Literal[False] = False
    signal_authorized: Literal[False] = False


class NativeControlSnapshot(TypedModel):
    session_id: str = Field(pattern=r'^desktop-session-[a-f0-9]{32}$')
    runtime_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    owner: Literal['AGENT', 'HUMAN', 'PAUSED']
    status: Literal['running', 'paused', 'stopped']


class NativeTaskSnapshot(TypedModel):
    busy: bool
    reserved: bool
    pending_approval: bool
    auto_approval: bool
    observed_job_count: int = Field(ge=0, le=20)
    unfinished_job_count: int = Field(ge=0, le=20)
    restart_quiesced: bool


class NativeScientistSnapshot(TypedModel):
    lab_configured: bool
    inference_configured: bool
    observed_lab_job_count: int = Field(ge=0, le=1000)
    unresolved_count: int = Field(ge=0)
    unresolved_lab_effect_count: int = Field(ge=0)
    pending_control_count: int = Field(ge=0)
    other_session_count: int = Field(ge=0)
    truncated: bool
    local_cleanup_pending: bool


class NativeEndpointObservation(TypedModel):
    path: Literal['/api/state', '/api/tasks', '/api/session/binding', '/api/scientist/jobs']
    response_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    available: bool


class NativeHandoverStep(TypedModel):
    stage: Literal['consent', 'fence', 'drain', 'interrupt_ui', 'physical_cleanup', 'scientist_reservation', 'shared_launch']
    instruction: str = Field(min_length=1, max_length=2048)
    command_argv: list[str] | None = Field(default=None, max_length=8)
    executed: Literal[False] = False


class NativeHandoverPreview(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['native_handover_preview'] = 'native_handover_preview'
    recorded_at: str
    manager_scope: Literal['default'] = 'default'
    manager_base: str
    origin: Literal['http://127.0.0.1:8765'] = 'http://127.0.0.1:8765'
    expected_session: str = Field(pattern=r'^app-[a-f0-9]{32}$')
    current_state_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    supervisor: ProcessIdentity
    backend: ProcessIdentity
    observed_descendants: list[ObservedNativeProcess] = Field(max_length=64)
    endpoint_observations: list[NativeEndpointObservation] = Field(min_length=4, max_length=4)
    control: NativeControlSnapshot | None
    tasks: NativeTaskSnapshot | None
    scientist: NativeScientistSnapshot | None
    session_binding: SessionInspection | None
    snapshot_continuity_verified: bool
    local_idle_observed: bool
    blockers: list[str] = Field(min_length=1, max_length=128)
    procedure: list[NativeHandoverStep] = Field(min_length=7, max_length=7)
    preparation_only: Literal[True] = True
    execution_authorized: Literal[False] = False
    user_transition_authorization_required: Literal[True] = True
    native_exclusion_verified: Literal[False] = False
    gpu_release_verified: Literal[False] = False
    scientist_reservation_verified: Literal[False] = False
    cgroup_ownership_verified: Literal[False] = False
    automatic_native_fallback_allowed: Literal[False] = False
    limitations: list[str]


def _unique_pairs(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError('Duplicate JSON fields are forbidden')
        value[name] = item
    return value


def _json(content):
    value = json.loads(content, object_pairs_hook=_unique_pairs,
                       parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if type(value) is not dict:
        raise ValueError('JSON object required')
    return value


def _read_current(expected_session):
    content = local_app.private_read(local_app.BASE / 'current.json')
    value = _json(content)
    if value.get('version') != '1':
        raise ValueError('Preview requires the native v1 default manager')
    state = local_app.LocalAppState.model_validate(value)
    local_app.current_instance().check_state(state)
    if (state.session != expected_session or state.mode != 'real' or state.phase != 'running'
            or state.project is not None or state.backend is None):
        raise ValueError('Exact running real default native session required')
    return state, hashlib.sha256(content).hexdigest()


def _proc_read(path, limit):
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError('Bounded process inspection exceeded')
    return content.decode('ascii')


def observe_descendants(supervisor, backend):
    pending = [(supervisor, None), (backend, None)]
    records = {}
    blockers = []
    while pending:
        identity, parent_pid = pending.pop(0)
        if identity.pid in records:
            if records[identity.pid].identity != identity:
                blockers.append('process_identity_changed')
            elif parent_pid is not None and records[identity.pid].discovery_parent_pid is None:
                records[identity.pid] = records[identity.pid].model_copy(update={'discovery_parent_pid': parent_pid})
            continue
        if len(records) >= 64:
            blockers.append('descendant_inventory_truncated')
            break
        try:
            if observe_process(identity) != 'same_process':
                raise ValueError('Process continuity unavailable')
            root = Path('/proc') / str(identity.pid)
            cgroups = _proc_read(root / 'cgroup', 4096).splitlines()
            if not cgroups or len(cgroups) > 32:
                raise ValueError('Cgroup observation unavailable')
            children = set()
            for thread_index, thread in enumerate((root / 'task').iterdir()):
                if thread_index >= 256:
                    raise ValueError('Thread inventory exceeded')
                for child in _proc_read(thread / 'children', 16384).split():
                    if not child.isdecimal() or not 1 <= int(child) <= 2147483647:
                        raise ValueError('Invalid descendant identity')
                    children.add(int(child))
                    if len(children) > 64:
                        raise ValueError('Descendant inventory exceeded')
            if observe_process(identity) != 'same_process':
                raise ValueError('Process changed during inspection')
            records[identity.pid] = ObservedNativeProcess(
                identity=identity, discovery_parent_pid=parent_pid, cgroups=cgroups)
            for child_pid in sorted(children):
                child = process_identity(child_pid)
                if child.uid != os.getuid():
                    raise ValueError('Foreign descendant observation')
                pending.append((child, identity.pid))
            if len(pending) > 128:
                raise ValueError('Descendant inventory exceeded')
        except (OSError, ValueError, TypeError):
            blockers.append('descendant_inventory_unavailable')
    for record in records.values():
        if observe_process(record.identity) != 'same_process':
            blockers.append('process_identity_changed')
    return list(records.values()), sorted(set(blockers))


def _response(client, method, path, **arguments):
    if (method, path) != ('POST', '/api/login') and (method != 'GET' or path not in ENDPOINTS):
        raise ValueError('Preview HTTP operation is not allowlisted')
    with client.stream(method, path, **arguments) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Exact HTTP 200 required')
        content = bytearray()
        for chunk in response.iter_bytes():
            if len(content) + len(chunk) > RESPONSE_LIMIT:
                raise ValueError('Response exceeds preview bound')
            content.extend(chunk)
    return _json(bytes(content)), hashlib.sha256(content).hexdigest()


def _http_snapshots(token):
    observations = []
    payloads = {}
    blockers = []
    with httpx.Client(base_url='http://127.0.0.1:8765', headers={'Origin': 'http://127.0.0.1:8765'},
                      timeout=5, trust_env=False, follow_redirects=False) as client:
        try:
            login, _hash = _response(client, 'POST', '/api/login', json={'token': token})
            if login.get('authenticated') is not True:
                raise ValueError('Login did not authenticate')
        except (httpx.HTTPError, ValueError, TypeError):
            return {}, [NativeEndpointObservation(path=path, available=False) for path in ENDPOINTS], ['authentication_unavailable']
        for path in ENDPOINTS:
            try:
                payloads[path], response_hash = _response(client, 'GET', path)
                observations.append(NativeEndpointObservation(path=path, available=True, response_sha256=response_hash))
            except (httpx.HTTPError, ValueError, TypeError):
                observations.append(NativeEndpointObservation(path=path, available=False))
                blockers.append('endpoint_unavailable:' + path)
        for path in ('/api/state', '/api/session/binding'):
            try:
                refreshed, _hash = _response(client, 'GET', path)
                before = payloads[path]
                if path == '/api/state':
                    unchanged = before['control'] == refreshed['control']
                else:
                    unchanged = all(before[key] == refreshed[key] for key in (
                        'session_ref', 'binding_sha256', 'snapshot_sha256', 'generation',
                        'session_status', 'session_owner', 'unresolved_inputs', 'job_count'))
                if not unchanged:
                    raise ValueError('HTTP snapshot changed')
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                blockers.append('endpoint_continuity_unverified:' + path)
    return payloads, observations, blockers


def _summarize(payloads):
    control = tasks = scientist = binding = None
    blockers = []
    try:
        source = payloads['/api/state']['control']
        control = NativeControlSnapshot.model_validate({key: source[key] for key in NativeControlSnapshot.model_fields})
        binding = SessionInspection.model_validate_json(canonical(payloads['/api/session/binding']))
        if (binding.session_ref != digest({'session_id': control.session_id})
                or binding.generation != control.generation or binding.session_owner != control.owner
                or binding.session_status != control.status
                or binding.lifecycle.runtime_ref != digest({'runtime_id': control.runtime_id})):
            raise ValueError('Controller/runtime binding differs')
        if binding.unresolved_inputs:
            blockers.append('unresolved_inputs')
        if control.owner != 'AGENT' or control.status != 'running':
            blockers.append('controller_not_idle_agent')
    except (ValueError, TypeError, KeyError):
        blockers.append('controller_binding_unavailable')
    try:
        source = payloads['/api/tasks']
        if source['available'] is not True or type(source['jobs']) is not list or len(source['jobs']) > 20:
            raise ValueError('Scheduler inventory unavailable')
        unfinished = sum(job['status'] not in {'succeeded', 'failed', 'cancelled'} for job in source['jobs'])
        tasks = NativeTaskSnapshot(busy=source['busy'], reserved=source['reserved'],
                                   pending_approval=source['approval'] is not None,
                                   auto_approval=source['auto_approval'] is not None,
                                   observed_job_count=len(source['jobs']), unfinished_job_count=unfinished,
                                   restart_quiesced=source['restart_quiesced'])
        if tasks.busy or tasks.reserved or tasks.pending_approval or tasks.auto_approval or unfinished:
            blockers.append('scheduler_not_idle')
        if binding is None or binding.job_count != len(source['jobs']):
            blockers.append('scheduler_inventory_unresolved_or_truncated')
        for key in ('owned_skill_planning', 'owned_web_goal_planning'):
            planning = source[key]
            if type(planning) is not dict or type(planning.get('available')) is not bool:
                raise ValueError('Planning inventory unavailable')
            if planning['available'] and planning.get('status') not in {'idle', 'succeeded', 'failed', 'cancelled', 'ready'}:
                blockers.append('planning_not_idle')
    except (ValueError, TypeError, KeyError):
        blockers.append('scheduler_inventory_unavailable')
    try:
        source = payloads['/api/scientist/jobs']
        inference = source['inference']
        controls = inference['evidence_controls']
        if (type(source['jobs']) is not list or len(source['jobs']) > 1000
                or controls['available'] is not True or controls['supported'] is not True):
            raise ValueError('Scientist control inventory unavailable')
        scientist = NativeScientistSnapshot(
            lab_configured=source['configured'], inference_configured=inference['configured'],
            observed_lab_job_count=len(source['jobs']), unresolved_count=inference['unresolved_count'],
            unresolved_lab_effect_count=inference['unresolved_lab_effect_count'],
            pending_control_count=controls['pending_count'], other_session_count=inference['other_session_count'],
            truncated=inference['truncated'], local_cleanup_pending=inference['local_cleanup_pending'])
        if (scientist.unresolved_count or scientist.unresolved_lab_effect_count or scientist.pending_control_count
                or scientist.other_session_count or scientist.truncated or scientist.local_cleanup_pending):
            blockers.append('scientist_unresolved_or_truncated')
        if source['jobs']:
            blockers.append('lab_job_cleanup_requires_human_review')
        if scientist.lab_configured:
            blockers.append('lab_control_inventory_unavailable')
    except (ValueError, TypeError, KeyError):
        blockers.append('scientist_inventory_unavailable')
    return control, tasks, scientist, binding, blockers


def _procedure(expected_session):
    return [
        NativeHandoverStep(stage='consent', instruction='Review this snapshot, exact session/process targets and unknown inventories. Obtain explicit user maintenance consent before any transition.'),
        NativeHandoverStep(stage='fence', instruction='A sustained native admission fence and durable exclusion producer are not supplied by this preview. Require separately reviewed enforcement; an idle snapshot or clean_shutdown is insufficient.'),
        NativeHandoverStep(stage='drain', instruction='After separate authorization, use the reviewed exact-controller /api/shared/drain then /api/shared/drain/seal protocol only on a source-verified capable runtime. Busy, pending or unknown effects require scoped human cancellation/recovery review. These POSTs are not executed here.'),
        NativeHandoverStep(stage='interrupt_ui', instruction='UI INTERRUPTION POINT: only after consent and reviewed drain/exclusion, stop this exact default session. The current UI becomes unavailable. Revalidate the pinned raw state/process identities immediately before effects.',
                           command_argv=['./scripts/aos-v1', 'stop', '--expected-session', expected_session]),
        NativeHandoverStep(stage='physical_cleanup', instruction='Independently verify exact supervisor/backend and observed owned worker absence plus lifecycle/container/token cleanup. Shared cgroup membership is never ownership or signal permission. Unknown or failed cleanup leaves admission closed; do not automatically restart native.'),
        NativeHandoverStep(stage='scientist_reservation', instruction='Obtain Scientist canonical reservation and sustained native-exclusion proof against fresh source/configuration and exact new workspace. This snapshot authorizes neither GPU release nor a reservation.'),
        NativeHandoverStep(stage='shared_launch', instruction='Only a separately reviewed fresh plan/provision/finite activation and trusted current factory may admit shared launch. No automatic native fallback or replay; failures require human review and leave admission closed.'),
    ]


def prepare_native_handover(expected_session: str, output: Path) -> NativeHandoverPreview:
    if type(expected_session) is not str or re.fullmatch(r'app-[a-f0-9]{32}', expected_session) is None:
        raise ValueError('Exact expected manager session required')
    instance = local_app.LocalAppInstance(base=local_app.BASE, port=8765)
    with local_app.instance_scope(instance):
        state, state_hash = _read_current(expected_session)
        if any(observe_process(identity) != 'same_process' for identity in (state.supervisor, state.backend)):
            raise ValueError('Exact native supervisor/backend continuity required')
        token = local_app.token_value(state)
        descendants, process_blockers = observe_descendants(state.supervisor, state.backend)
        payloads, observations, http_blockers = _http_snapshots(token)
        control, tasks, scientist, binding, inventory_blockers = _summarize(payloads)
        refreshed_descendants, refreshed_process_blockers = observe_descendants(state.supervisor, state.backend)
        process_blockers += refreshed_process_blockers
        if (sorted((record.model_dump() for record in descendants), key=lambda value: value['identity']['pid'])
                != sorted((record.model_dump() for record in refreshed_descendants), key=lambda value: value['identity']['pid'])):
            process_blockers.append('descendant_snapshot_changed')
        blockers = process_blockers + http_blockers + inventory_blockers
        refreshed, refreshed_hash = _read_current(expected_session)
        unchanged = refreshed == state and refreshed_hash == state_hash and all(
            observe_process(identity) == 'same_process' for identity in (state.supervisor, state.backend))
        if not unchanged:
            raise ValueError('Manager/process state changed; no preview published')
        for record in descendants:
            if observe_process(record.identity) != 'same_process':
                blockers.append('descendant_continuity_unverified')
        continuity = not process_blockers and not http_blockers and 'descendant_continuity_unverified' not in blockers
        preview = NativeHandoverPreview(
            recorded_at=now(), manager_base=str(local_app.BASE), expected_session=expected_session,
            current_state_sha256=state_hash, supervisor=state.supervisor, backend=state.backend,
            observed_descendants=descendants, endpoint_observations=observations,
            control=control, tasks=tasks, scientist=scientist, session_binding=binding,
            snapshot_continuity_verified=continuity, local_idle_observed=not blockers,
            blockers=sorted(set(blockers + ['user_transition_consent_required', 'sustained_native_exclusion_missing',
                                             'physical_cleanup_not_verified', 'scientist_canonical_reservation_missing'])),
            procedure=_procedure(expected_session), limitations=[
                'Historical point-in-time read-only snapshot, not live authority or sustained idle/exclusion.',
                'Observed descendants may detach or exit; unknown and shared cgroup members are not owned targets. No global GPU worker coverage.',
                'Only login POST and allowlisted authenticated GET requests occurred; no drain, control, cancellation, stop, start or logout.',
                'No database was opened by this client; server GET readbacks use their existing read-only inspectors.',
                'Not-configured Scientist components are explicit observations, not proof of peer/GPU absence.',
            ])
        write_new_private_file(output, (canonical(preview.model_dump(mode='json')) + '\n').encode())
        return preview

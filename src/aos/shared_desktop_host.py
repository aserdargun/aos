import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import time
from typing import Literal

import httpx
from pydantic import Field, model_validator

from .contracts import REPO_ROOT, TypedModel, digest, now
from .bounded_process import run_bounded
from .lifecycle import ProcessIdentity, observe_process, process_identity, read_journal
from .recovery_session import SessionInspection
from .session_binding import SessionRuntimeBinding
from .shared_desktop_plan import (
    SHARED_DESKTOP_UNIT, SharedSHA, load_activation, load_plan,
    predecessor_identity_sha256, read_pinned_file, read_private_file, verify_activation,
)
from .shared_desktop_provision import LAUNCH_INTENT_NAME, PROVISION_NAME, claim_launch, load_provision, verify_launch_intent
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


SCIENTIST_ROOT = REPO_ROOT.parent / 'ai-scientist'
FIXED_LAUNCHER = SCIENTIST_ROOT / 'scripts/aos_native_launch.py'


class SharedServiceBinding(TypedModel):
    unit: Literal['swapp-aos-gpu-shared-desktop-default.service'] = SHARED_DESKTOP_UNIT
    invocation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    process: ProcessIdentity
    control_group: str = Field(min_length=1, max_length=4096)

    @model_validator(mode='after')
    def exact_owned_group(self):
        if (not self.control_group.startswith('/') or '..' in self.control_group.split('/')
                or not self.control_group.endswith('/' + self.unit)):
            raise ValueError('Shared service requires its exact owned control group')
        return self


class SharedTokenIdentity(TypedModel):
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    uid: int = Field(ge=1)
    mode: Literal[384] = 0o600


class SharedDesktopState(TypedModel):
    version: Literal['2'] = '2'
    mode: Literal['shared'] = 'shared'
    session: str = Field(pattern=r'^app-[a-f0-9]{32}$')
    project: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9-]{0,47}$')
    url: str = Field(pattern=r'^http://127\.0\.0\.1:[0-9]{4,5}/ui/$')
    phase: Literal['starting', 'running', 'stopping', 'stopped', 'uncertain']
    started_at: str
    plan_path: str
    plan_sha256: SharedSHA
    activation_path: str
    activation_sha256: SharedSHA
    provision_sha256: SharedSHA
    launch_intent_sha256: SharedSHA
    workspace: str
    workspace_identity: WorkspaceIdentity | None = None
    service_binding: SharedServiceBinding | None = None
    token_name: str | None = Field(default=None, pattern=r'^desktop-console-[a-f0-9]{16}\.token$')
    token_identity: SharedTokenIdentity | None = None
    runtime_binding: SessionRuntimeBinding | None = None
    ui_online: bool = False
    task_admission_enabled: bool = False
    cleanup_verified: bool = False
    cleanup_evidence_sha256: SharedSHA | None = None
    last_error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode='after')
    def exact_recorded_scope(self):
        for value in (self.plan_path, self.activation_path, self.workspace):
            if not Path(value).is_absolute() or '..' in Path(value).parts:
                raise ValueError('Shared state requires explicit absolute input/workspace paths')
        if (self.token_name is None) != (self.token_identity is None):
            raise ValueError('Shared token requires its exact private file identity')
        if self.task_admission_enabled and (not self.ui_online or self.phase != 'running'):
            raise ValueError('Task admission cannot precede verified live shared UI')
        if self.phase == 'running' and any(value is None for value in (
                self.service_binding, self.workspace_identity, self.runtime_binding,
                self.token_name, self.token_identity)):
            raise ValueError('Running shared state requires actual service/runtime/token bindings')
        if self.cleanup_verified and (self.phase != 'stopped' or self.cleanup_evidence_sha256 is None):
            raise ValueError('Shared cleanup requires a stopped exact generation and physical evidence')
        return self


class SharedCleanupProof(TypedModel):
    state_sha256: SharedSHA
    service_binding: SharedServiceBinding
    native_gpu_excluded: bool
    admission_closed: bool
    owned_runtime_removed: bool
    tokens_removed: bool
    evidence_sha256: SharedSHA


def _deny_predecessor(plan, predecessor):
    raise ValueError('Trusted exact predecessor process, physical cleanup and stop verifier is unavailable')


def _deny_cleanup(state, stage):
    raise ValueError('Trusted native-worker/GPU exclusion and exact owned physical cleanup prover is unavailable')


def _boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _process_group(pid):
    value = Path('/proc') / str(pid) / 'cgroup'
    with value.open('rb') as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError('Shared service cgroup observation exceeded its bound')
    lines = raw.decode().splitlines()
    if len(lines) != 1 or not lines[0].startswith('0::'):
        raise ValueError('Exact unified service cgroup is unavailable')
    return lines[0][3:]


class SystemdSharedDesktopTransport:
    """Fixed owned transient unit transport; no arbitrary command or unit API."""

    def __init__(self, *, runner=run_bounded, identity_reader=process_identity,
                 process_observer=observe_process, group_reader=_process_group,
                 pidfd_open=os.pidfd_open, pidfd_signal=signal.pidfd_send_signal,
                 waiter=select.select, clock=_boottime, sleeper=time.sleep):
        self.runner = runner
        self.identity_reader = identity_reader
        self.process_observer = process_observer
        self.group_reader = group_reader
        self.pidfd_open = pidfd_open
        self.pidfd_signal = pidfd_signal
        self.waiter = waiter
        self.clock = clock
        self.sleeper = sleeper

    def _run(self, command, timeout=3):
        environment = {'PATH': '/usr/bin', 'LC_ALL': 'C', 'HOME': str(Path.home()),
            'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
        result = self.runner(command, input=b'', env=environment, timeout=timeout, max_output=1048576)
        stdout = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
        stderr = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
        if len(stdout.encode()) + len(stderr.encode()) > 1048576:
            raise ValueError('Shared systemd response exceeded its bound')
        return subprocess.CompletedProcess(command, result.returncode, stdout, stderr)

    def _unit(self):
        result = self._run(['/usr/bin/systemctl', '--user', 'show', SHARED_DESKTOP_UNIT,
            '--property=Id,LoadState,ActiveState,SubState,MainPID,InvocationID,ControlGroup'])
        if result.returncode not in {0, 1}:
            raise ValueError('Shared owned-unit observation unavailable')
        values = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition('=')
            if not separator or key in values:
                raise ValueError('Shared owned-unit observation is ambiguous')
            values[key] = value
        if values.get('Id') != SHARED_DESKTOP_UNIT:
            raise ValueError('Observed unit is not the exact shared owned unit')
        return values

    def stopped(self, binding):
        values = self._unit()
        if values.get('LoadState') == 'not-found':
            return True
        return (values.get('ActiveState') == 'inactive' and values.get('SubState') == 'dead'
            and values.get('MainPID') == '0' and values.get('InvocationID') == binding.invocation_id)

    def read(self):
        values = self._unit()
        if values.get('LoadState') == 'not-found':
            return None
        if values.get('ActiveState') != 'active' or values.get('SubState') != 'running':
            raise ValueError('Shared unit exists without an unambiguous running generation')
        process = self.identity_reader(int(values['MainPID']))
        if process.uid != os.getuid() or self.group_reader(process.pid) != values['ControlGroup']:
            raise ValueError('Shared MainPID owner or actual cgroup differs')
        binding = SharedServiceBinding(invocation_id=values['InvocationID'], process=process,
                                       control_group=values['ControlGroup'])
        if self.process_observer(process) != 'same_process':
            raise ValueError('Shared service MainPID identity changed')
        return binding

    @staticmethod
    def launch_command(plan, activation):
        template = plan.template
        if template.launcher_path != str(FIXED_LAUNCHER):
            raise ValueError('Only the independently reviewed fixed Scientist launcher is supported')
        session = Path(plan.session_directory)
        scoped_paths = (
            ('--trajectory-database', 'trajectory.sqlite'),
            ('--web-profiles-root', 'web-applications'),
            ('--knowledge-root', 'knowledge'),
            ('--web-task-root', 'web-tasks'),
            ('--web-form-plan-root', 'form-plans'),
            ('--web-form-value-root', 'form-values'),
            ('--web-form-state-root', 'form-states'),
            ('--web-route-root', 'routes'),
            ('--web-static-root', 'static'),
            ('--web-readonly-data-root', 'readonly-data'),
            ('--console-assets-root', 'console-assets'),
            ('--site-knowledge-root', 'site-knowledge'),
            ('--site-skills-root', 'site-skills'),
            ('--page-seed-root', 'site-page-seeds'),
            ('--route-review-root', 'remote-route-reviews'),
            ('--json-review-root', 'remote-json-reviews'),
            ('--json-page-seed-root', 'json-page-seeds'),
        )
        path_arguments = [argument for flag, name in scoped_paths for argument in (flag, str(session / name))]
        return ['/usr/bin/systemd-run', '--user', '--unit=' + SHARED_DESKTOP_UNIT, '--no-block', '--collect',
            '--property=Type=exec', '--property=Restart=no', '--property=KillMode=control-group',
            '--property=UMask=0077', '--property=Slice=swapp-gpu.slice',
            '--property=WorkingDirectory=' + str(REPO_ROOT),
            '--property=CPUQuota=' + str(template.limits.cpu_quota_percent) + '%',
            '--property=MemoryMax=' + str(template.limits.memory_max_bytes),
            '--property=MemorySwapMax=0', '--property=TasksMax=' + str(template.limits.tasks_max),
            '--property=TimeoutStopSec=' + str(template.limits.stop_timeout_seconds),
            '--setenv=PYTHONPATH=' + str(REPO_ROOT / 'src') + ':' + str(SCIENTIST_ROOT),
            template.python_path, template.launcher_path,
            '--reviewed-launch-input', activation.reviewed_launch_input_path,
            '--expected-launch-input-sha256', activation.reviewed_launch_input_sha256,
            '--port', str(template.port), '--workspace', plan.workspace, '--database', plan.database,
            *path_arguments,
            '--browser-tasks', '--desktop-browser',
            '--engine', 'scientist', '--scientist-broker-socket', template.broker_socket,
            '--vision-engine', 'bonsai']

    def start(self, plan, activation):
        if self.read() is not None:
            raise ValueError('Exact shared unit already exists; no replacement or adoption')
        result = self._run(self.launch_command(plan, activation))
        if result.returncode != 0:
            raise ValueError('Atomic shared transient-unit start did not acknowledge; never retry')
        deadline = self.clock() + 5
        while True:
            values = self._unit()
            if values.get('ActiveState') == 'active' and values.get('SubState') == 'running':
                return self.read()
            if (values.get('ActiveState') not in {'activating', 'active', 'inactive'}
                    or values.get('LoadState') not in {'loaded', 'not-found'} or self.clock() >= deadline):
                raise ValueError('Shared start acknowledgment has no actual MainPID generation; never retry')
            self.sleeper(0.1)

    def token_path(self, binding):
        deadline = self.clock() + 5
        while True:
            path = self._token_path(binding)
            if path is not None:
                return path
            if self.clock() >= deadline:
                raise ValueError('Exact shared-generation token receipt did not arrive; never retry launch')
            self.sleeper(0.1)

    def _token_path(self, binding):
        if self.read() != binding:
            raise ValueError('Shared generation changed before token provenance read')
        result = self._run(['/usr/bin/journalctl', '--user', '--no-pager', '--output=json', '-n', '100',
            '_SYSTEMD_INVOCATION_ID=' + binding.invocation_id])
        if result.returncode != 0:
            raise ValueError('Exact shared invocation journal is unavailable')
        paths = set()
        for line in result.stdout.splitlines():
            row = json.loads(line)
            if row.get('_SYSTEMD_INVOCATION_ID') != binding.invocation_id:
                raise ValueError('Shared token journal belongs to another invocation')
            match = re.fullmatch(r'Local token file \(0600\): ([^\n]+)', row.get('MESSAGE', ''))
            if match:
                if (row.get('_PID') != str(binding.process.pid) or row.get('_UID') != str(binding.process.uid)
                        or row.get('_SYSTEMD_USER_UNIT') != SHARED_DESKTOP_UNIT):
                    raise ValueError('Shared token receipt was not emitted by the exact service MainPID/owner')
                paths.add(match.group(1))
        if not paths:
            return None
        if len(paths) != 1:
            raise ValueError('One exact shared-generation token creation receipt is required')
        return Path(paths.pop())

    def stop(self, binding, *, timeout):
        if not isinstance(binding, SharedServiceBinding) or self.read() != binding:
            raise ValueError('Shared stop requires the exact original owned unit generation')
        descriptor = self.pidfd_open(binding.process.pid, 0)
        try:
            if (self.read() != binding or self.process_observer(binding.process) != 'same_process'
                    or self.group_reader(binding.process.pid) != binding.control_group):
                raise ValueError('Shared generation changed before owned pidfd termination')
            self.pidfd_signal(descriptor, signal.SIGTERM, None, 0)
            if not self.waiter([descriptor], [], [], timeout)[0]:
                raise ValueError('Owned shared process termination unacknowledged; never force-kill or retry')
        finally:
            os.close(descriptor)
        if self.process_observer(binding.process) != 'not_observed' or not self.stopped(binding):
            raise ValueError('Shared service is not independently observed absent after termination')


class SharedDesktopHost:
    def __init__(self, *, transport=None, activation_verifier=None, activation_claimer=None, predecessor_verifier=None,
                 cleanup_prover=None, readback=None, process_observer=observe_process,
                 lifecycle_reader=read_journal, clock=_boottime, boot_reader=_boot_id):
        self.transport = transport or SystemdSharedDesktopTransport()
        self.activation_verifier = activation_verifier
        self.activation_claimer = activation_claimer
        self.predecessor_verifier = predecessor_verifier or _deny_predecessor
        self.cleanup_prover = cleanup_prover or _deny_cleanup
        self.readback = readback or self._http_readback
        self.process_observer = process_observer
        self.lifecycle_reader = lifecycle_reader
        self.clock = clock
        self.boot_reader = boot_reader

    def _inputs(self, state):
        plan = load_plan(Path(state.plan_path), state.plan_sha256)
        activation = load_activation(Path(state.activation_path), state.activation_sha256)
        if (state.session != plan.app_session or state.project != plan.template.project
                or state.url != plan.template.url or state.workspace != plan.workspace):
            raise ValueError('Shared state differs from the exact selected manager plan')
        provision = self._provision(plan, activation, state.provision_sha256, require_pristine=False)
        intent = verify_launch_intent(plan, state.provision_sha256, state.activation_sha256,
                                     current_boot_id=self.boot_reader())
        if (provision.workspace_identity != state.workspace_identity
                or digest(intent.model_dump(mode='json')) != state.launch_intent_sha256):
            raise ValueError('Shared state differs from its exact provision or exclusive launch marker')
        return plan, activation

    def _provision(self, plan, activation, provision_sha256, *, require_pristine):
        path = Path(plan.session_directory) / PROVISION_NAME
        config, factory = self._launch_input(activation)
        if (activation.config_files.get(str(path)) != provision_sha256
                or factory['config_files'].get(str(path)) != provision_sha256):
            raise ValueError('Provision receipt must be pinned in both activation and reviewed factory configuration')
        retained_keys = {'retained_review_path', 'retained_review_sha256'} & config.keys()
        if retained_keys:
            if len(retained_keys) != 2:
                raise ValueError('Retained review requires its exact paired path and SHA')
            retained_path, retained_sha = config['retained_review_path'], config['retained_review_sha256']
            if (not isinstance(retained_path, str) or not isinstance(retained_sha, str)
                    or re.fullmatch(r'[a-f0-9]{64}', retained_sha) is None
                    or activation.config_files.get(retained_path) != retained_sha):
                raise ValueError('Retained review must be pinned in the actual activation config closure')
            retained = json.loads(read_pinned_file(Path(retained_path), retained_sha, limit=65536, private=True))
            if (not isinstance(retained, dict)
                    or retained.get('schema') != 'scientist.native-retained-factory-review.v1'
                    or not isinstance(retained.get('config_files'), dict)
                    or retained['config_files'].get(str(path)) != provision_sha256):
                raise ValueError('Retained review must pin the exact provision receipt in its config closure')
        return load_provision(plan, path, provision_sha256, current_boot_id=self.boot_reader(),
                              require_pristine=require_pristine)

    @staticmethod
    def _launch_input(activation):
        config = json.loads(read_pinned_file(Path(activation.reviewed_launch_input_path),
            activation.reviewed_launch_input_sha256, limit=65536, private=True))
        if not isinstance(config, dict) or not isinstance(config.get('factory_arguments'), dict):
            raise ValueError('Shared launch input requires a typed factory argument map')
        factory = config['factory_arguments']
        if not isinstance(factory.get('config_files'), dict):
            raise ValueError('Shared factory configuration pins require a typed map')
        return config, factory

    def _pristine_claim(self, state, plan, activation):
        self._inputs(state)
        provision = self._provision(plan, activation, state.provision_sha256, require_pristine=False)
        session = open_existing_workspace(Path(plan.session_directory))
        try:
            workspace = open_existing_workspace(Path(plan.workspace))
            try:
                if (workspace_identity(Path(plan.session_directory), session) != provision.session_identity
                        or workspace_identity(Path(plan.workspace), workspace) != provision.workspace_identity
                        or os.listdir(workspace)
                        or set(os.listdir(session)) != {'workspace', PROVISION_NAME, LAUNCH_INTENT_NAME}):
                    raise ValueError('Claimed shared scope gained artifacts or changed identity before launch')
            finally:
                os.close(workspace)
        finally:
            os.close(session)
        self._inputs(state)

    def _verify(self, plan, activation):
        verify_activation(plan, activation, current_boot_id=self.boot_reader(),
                          now_monotonic=self.clock(), activation_verifier=self.activation_verifier)
        config, factory = self._launch_input(activation)
        if (config.get('schema') != 'scientist.native-launch-review.v1'
                or config.get('caller_unit') != SHARED_DESKTOP_UNIT):
            raise ValueError('Peer launch review does not support the exact durable shared unit')
        for prefix in ('policy', 'profile'):
            path_key = 'policy_path' if prefix == 'policy' else 'profile_config_path'
            hash_key = prefix + '_file_sha256'
            if (not isinstance(factory.get(path_key), str) or not isinstance(factory.get(hash_key), str)
                    or activation.config_files.get(factory[path_key]) != factory[hash_key]):
                raise ValueError('Shared factory policy/profile pins are outside actual activation closure')
        policy = json.loads(read_pinned_file(Path(factory['policy_path']), factory['policy_file_sha256'], private=True))
        if (not isinstance(policy, dict) or policy.get('enabled') is not True
                or policy.get('caller_unit') != SHARED_DESKTOP_UNIT):
            raise ValueError('Shared factory policy is not enabled for the exact owned caller')

    @staticmethod
    def _token_identity(path):
        descriptor = open_existing_workspace(path.parent)
        try:
            info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError('Shared token is not an owner-only regular file')
            return SharedTokenIdentity(device=info.st_dev, inode=info.st_ino, uid=info.st_uid)
        finally:
            os.close(descriptor)

    @staticmethod
    def _workspace(path):
        descriptor = open_existing_workspace(Path(path))
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Shared workspace must be canonical private and user-owned')
            return workspace_identity(Path(path), descriptor)
        finally:
            os.close(descriptor)

    def token_value(self, state):
        self._inputs(state)
        if (state.phase != 'running' or state.service_binding is None or state.token_name is None
                or self.transport.read() != state.service_binding
                or self.process_observer(state.service_binding.process) != 'same_process'):
            raise ValueError('Shared token requires its exact live owned service generation')
        path = REPO_ROOT / 'runs' / state.token_name
        if self._token_identity(path) != state.token_identity:
            raise ValueError('Shared token file identity changed')
        value = read_private_file(path, 128).decode().strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value):
            raise ValueError('Shared token contents are invalid')
        if self._token_identity(path) != state.token_identity or self.transport.read() != state.service_binding:
            raise ValueError('Shared token/service identity changed during private read')
        return value

    def _http_readback(self, state, plan):
        with httpx.Client(base_url=plan.template.origin, timeout=2, trust_env=False) as client:
            response = client.post('/api/login', headers={'Origin': plan.template.origin},
                                   json={'token': self.token_value(state)})
            if response.status_code != 200:
                raise ValueError('Shared console authentication failed')
            result = {}
            for name, route in (('session', '/api/session'), ('state', '/api/state'),
                                ('binding', '/api/session/binding'), ('tasks', '/api/tasks'),
                                ('scientist', '/api/scientist/jobs')):
                response = client.get(route)
                if response.status_code != 200 or len(response.content) > 1048576:
                    raise ValueError('Shared authenticated readback is missing or unbounded')
                result[name] = response.json()
            response = client.get('/ui/')
            if len(response.content) > 1048576:
                raise ValueError('Shared UI response exceeded its bound')
            result['ui_status'] = response.status_code
            return result

    def _readiness(self, state, plan):
        selected, _activation = self._inputs(state)
        if selected != plan:
            raise ValueError('Shared readiness differs from the exact provision-bound plan')
        if (state.service_binding is None or self.transport.read() != state.service_binding
                or self.process_observer(state.service_binding.process) != 'same_process'
                or self._workspace(state.workspace) != state.workspace_identity):
            raise ValueError('Shared service process or workspace identity changed')
        if self._token_identity(REPO_ROOT / 'runs' / state.token_name) != state.token_identity:
            raise ValueError('Shared token file identity changed before authenticated readback')
        actual = self.readback(state, plan)
        session, observed = actual['session'], actual['state']
        control, runtime = observed['control'], observed['runtime']
        if (session.get('authenticated') is not True or actual['ui_status'] != 200
                or control.get('status') != 'running' or control.get('owner') != 'AGENT'
                or control.get('runtime_id') != runtime.get('runtime_id') or runtime.get('running') is not True
                or runtime.get('workspace_identity') != state.workspace_identity.model_dump(mode='json')):
            raise ValueError('Shared authenticated current controller/runtime differs')
        if plan.template.project is not None and (session.get('manager_session') != state.session
                or session.get('manager_scope') != {'project': state.project, 'port': plan.template.port}):
            raise ValueError('Shared console belongs to another named manager scope')
        journal = Path(state.workspace).parent / '.aos-lifecycle' / (runtime['runtime_id'] + '.jsonl')
        events, journal_sha = self.lifecycle_reader(journal)
        birth, latest = events[0].birth, events[-1]
        binding = SessionRuntimeBinding(session_id=control['session_id'], generation=control['generation'],
                                        birth=birth, container_id=latest.container_id)
        inspection = SessionInspection.model_validate(actual['binding'], strict=True)
        if (latest.stage != 'started' or birth.process != state.service_binding.process
                or birth.workspace != state.workspace_identity or birth.runtime_id != runtime['runtime_id']
                or latest.container_id != runtime['container_id']
                or inspection.binding_sha256 != digest(binding.model_dump())
                or inspection.session_ref != digest({'session_id': control['session_id']})
                or inspection.generation != control['generation'] or inspection.session_owner != control['owner']
                or inspection.session_status != control['status']
                or inspection.lifecycle.journal_sha256 != journal_sha
                or inspection.lifecycle.recorded_stage != 'started'
                or inspection.lifecycle.owner_observation != 'same_process'
                or inspection.lifecycle.container_observation != 'running'):
            raise ValueError('Shared lifecycle is not the exact service MainPID/workspace/controller generation')
        if state.runtime_binding is not None and state.runtime_binding != binding:
            raise ValueError('Shared recorded runtime/controller generation changed')
        inference = actual['scientist'].get('inference', {})
        if (inference.get('configured') is not True or inference.get('model_binding_valid') is not True
                or inference.get('wire_version') != 1):
            raise ValueError('Actual shared scheduler does not have the reviewed Scientist engine/vision binding')
        if self.transport.read() != state.service_binding or self.process_observer(birth.process) != 'same_process':
            raise ValueError('Shared generation changed during authenticated readback')
        return actual, binding

    @staticmethod
    def _idle(actual):
        tasks = actual['tasks']
        inference = actual['scientist'].get('inference', {})
        controls = inference.get('evidence_controls', {})
        jobs = tasks.get('jobs')
        terminal_jobs = (isinstance(jobs, list) and all(isinstance(job, dict)
            and job.get('status') in {'succeeded', 'failed', 'cancelled', 'completed', 'stopped'} for job in jobs))
        lab_jobs = actual['scientist'].get('jobs')
        lab_idle = (isinstance(lab_jobs, list) and all(isinstance(job, dict)
            and isinstance(job.get('actions'), list)
            and all(isinstance(action, dict) and action.get('state') in {'acknowledged', 'rejected'}
                    for action in job['actions']) for job in lab_jobs))
        grant = tasks.get('auto_approval')
        grant_inactive = (grant is None or isinstance(grant, dict) and terminal_jobs
            and any(job.get('job_id') == grant.get('job_id') and grant.get('job_id') is not None for job in jobs))
        return (tasks.get('busy') is False and tasks.get('reserved') is False
            and terminal_jobs and lab_idle and actual['binding'].get('unresolved_inputs') == 0
            and tasks.get('approval') is None and grant_inactive
            and inference.get('admission_blocked') is False and inference.get('local_cleanup_pending') is False
            and inference.get('unresolved_count') == 0 and inference.get('unresolved_lab_effect_count') == 0
            and controls.get('available') is True and controls.get('supported') is True
            and controls.get('pending_count') == 0)

    def start(self, plan_path, plan_sha256, activation_path, activation_sha256, *, provision_sha256,
              predecessor, persist_state):
        plan = load_plan(Path(plan_path), plan_sha256)
        activation = load_activation(Path(activation_path), activation_sha256)
        if plan_sha256 != plan.plan_sha256():
            raise ValueError('Shared plan must use its canonical immutable bytes')
        if (plan.predecessor_session is None) != (predecessor is None):
            raise ValueError('Shared predecessor selection differs')
        if predecessor is not None and (predecessor.get('session') != plan.predecessor_session
                or predecessor_identity_sha256(predecessor) != plan.predecessor_identity_sha256):
            raise ValueError('Shared predecessor original stable identity differs')
        if self.predecessor_verifier(plan, predecessor) is not None:
            raise ValueError('Trusted predecessor verifier must complete or raise')
        self._verify(plan, activation)
        if self.activation_claimer is None:
            raise ValueError('Trusted single-use shared launch authority claimer is unavailable')
        SystemdSharedDesktopTransport.launch_command(plan, activation)
        expected_base = REPO_ROOT / 'data' / ('local-app-v1' if plan.template.project is None
                                             else 'local-app-project-' + plan.template.project)
        if Path(plan.template.manager_base) != expected_base:
            raise ValueError('Shared manager base differs from the fixed owned AOS scope')
        provision = self._provision(plan, activation, provision_sha256, require_pristine=True)
        if self.transport.read() is not None:
            raise ValueError('Shared owned unit already exists; never adopt or retry')
        token_directory = open_existing_workspace(REPO_ROOT / 'runs')
        try:
            existing_tokens = {REPO_ROOT / 'runs' / name for name in os.listdir(token_directory)
                               if re.fullmatch(r'desktop-console-[a-f0-9]{16}\.token', name)}
        finally:
            os.close(token_directory)
        self._verify(plan, activation)
        intent = claim_launch(plan, provision, provision_sha256, activation_sha256,
                              current_boot_id=self.boot_reader())
        state = SharedDesktopState(session=plan.app_session, project=plan.template.project, url=plan.template.url,
            phase='starting', started_at=now(), plan_path=str(plan_path), plan_sha256=plan_sha256,
            activation_path=str(activation_path), activation_sha256=activation_sha256,
            provision_sha256=provision_sha256, launch_intent_sha256=digest(intent.model_dump(mode='json')),
            workspace=plan.workspace, workspace_identity=provision.workspace_identity)
        persist_state(state)
        try:
            self._inputs(state)
            self._verify(plan, activation)
            self._pristine_claim(state, plan, activation)
            if self.activation_claimer(plan, activation, state) is not None:
                raise ValueError('Trusted launch claimer must confirm a fresh claim or raise')
            self._inputs(state)
            self._verify(plan, activation)
            self._pristine_claim(state, plan, activation)
            service = self.transport.start(plan, activation)
            if service.process.boot_id != activation.boot_id:
                raise ValueError('Shared launch belongs to another actual boot')
            state = state.model_copy(update={'service_binding': service})
            persist_state(state)
            token = self.transport.token_path(service)
            if (token.parent != REPO_ROOT / 'runs' or token in existing_tokens
                    or re.fullmatch(r'desktop-console-[a-f0-9]{16}\.token', token.name) is None):
                raise ValueError('Shared token was not newly created by the exact owned invocation')
            state = state.model_copy(update={'token_name': token.name, 'token_identity': self._token_identity(token),
                                             'phase': 'running'})
            actual, binding = self._readiness(state, plan)
            self._verify(plan, activation)
            state = state.model_copy(update={'runtime_binding': binding, 'ui_online': True,
                'task_admission_enabled': not actual['scientist']['inference'].get('admission_blocked', True)})
            state = SharedDesktopState.model_validate(state.model_dump(), strict=True)
            persist_state(state)
            return state
        except Exception as error:
            state = state.model_copy(update={'phase': 'uncertain', 'ui_online': False,
                'task_admission_enabled': False, 'last_error': type(error).__name__ + ': ' + str(error)[:1800]})
            persist_state(state)
            return state

    def status(self, state):
        result = {'version': '2', 'mode': 'shared', 'session': state.session, 'project': state.project,
            'url': state.url, 'phase': state.phase, 'ui_online': False, 'task_admission_enabled': False,
            'cleanup_verified': False, 'service_binding': None if state.service_binding is None else
                state.service_binding.model_dump(mode='json')}
        try:
            plan, activation = self._inputs(state)
            if state.phase == 'stopped':
                result['cleanup_verified'] = self.clean_shutdown(state)
                if not result['cleanup_verified']:
                    result['phase'] = 'needs_inspection'
                return result
            actual, _binding = self._readiness(state, plan)
            result.update(ui_online=True, token_file=str(REPO_ROOT / 'runs' / state.token_name))
            try:
                self._verify(plan, activation)
                result['task_admission_enabled'] = not actual['scientist']['inference']['admission_blocked']
            except (ValueError, OSError) as error:
                result['admission_blocker'] = str(error)
            if state.phase != 'running':
                result.update(phase='needs_inspection', task_admission_enabled=False)
            return result
        except (ValueError, OSError, KeyError, TypeError, httpx.HTTPError) as error:
            result.update(phase='needs_inspection', error=str(error), task_admission_enabled=False)
            return result

    def _cleanup(self, state, stage):
        proof = self.cleanup_prover(state.model_copy(deep=True), stage)
        if (not isinstance(proof, SharedCleanupProof) or proof.state_sha256 != digest(state.model_dump(mode='json'))
                or proof.service_binding != state.service_binding or not proof.native_gpu_excluded):
            raise ValueError('Trusted cleanup proof does not bind exact shared state/generation and native GPU exclusion')
        if not proof.admission_closed:
            raise ValueError('Trusted cleanup proof must enforce closed admission for the exact generation')
        if stage == 'after_stop' and not (proof.owned_runtime_removed and proof.tokens_removed):
            raise ValueError('Shared physical owned cleanup remains unproven')
        return proof

    def stop(self, state, *, persist_state):
        plan, _activation = self._inputs(state)
        if state.phase != 'running' or state.service_binding is None:
            raise ValueError('Only an exact running shared generation can be stopped; uncertain state needs inspection')
        actual, _binding = self._readiness(state, plan)
        if not self._idle(actual):
            raise ValueError('Shared stop refused: busy, reserved or unresolved Scientist work/control remains')
        self._cleanup(state, 'before_stop')
        state = state.model_copy(update={'phase': 'stopping', 'task_admission_enabled': False})
        persist_state(state)
        try:
            self.transport.stop(state.service_binding, timeout=plan.template.limits.stop_timeout_seconds)
            proof = self._cleanup(state, 'after_stop')
            token_path = REPO_ROOT / 'runs' / state.token_name
            if (os.path.lexists(token_path) or not self.transport.stopped(state.service_binding)
                    or self.process_observer(state.service_binding.process) != 'not_observed'):
                raise ValueError('Shared token/process/unit is not independently observed absent')
            state = state.model_copy(update={'phase': 'stopped', 'ui_online': False, 'task_admission_enabled': False,
                'cleanup_verified': True, 'cleanup_evidence_sha256': proof.evidence_sha256, 'last_error': None})
            persist_state(state)
            return state
        except Exception as error:
            state = state.model_copy(update={'phase': 'uncertain', 'ui_online': False,
                'cleanup_verified': False, 'last_error': type(error).__name__ + ': ' + str(error)[:1800]})
            persist_state(state)
            return state

    def clean_shutdown(self, state):
        self._inputs(state)
        return (state.phase == 'stopped' and state.cleanup_verified and state.cleanup_evidence_sha256 is not None
            and state.service_binding is not None and self.transport.stopped(state.service_binding)
            and self.process_observer(state.service_binding.process) == 'not_observed'
            and state.token_name is not None and not os.path.lexists(REPO_ROOT / 'runs' / state.token_name))

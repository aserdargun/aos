from dataclasses import dataclass
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time

from .bounded_process import run_bounded
from .contracts import REPO_ROOT, digest, now
from .desktop import DOCKER
from .lifecycle import observe_process, read_journal
from .linux_cgroup_observation import require_empty_cgroup
from .shared_desktop_host import SharedDesktopState
from .workspace_identity import open_existing_workspace, workspace_identity


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _deny_scope(state, deadline):
    raise ValueError('Independent original launch fencing and cleanup scope are unavailable')


def _boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


@dataclass(frozen=True)
class ReviewedDockerDaemon:
    daemon_id: str
    socket_device: int
    socket_inode: int

    def __post_init__(self):
        _require(type(self.daemon_id) is str and re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', self.daemon_id),
                 'Original reviewed Docker daemon ID is required')
        _require(type(self.socket_device) is int and self.socket_device >= 0
                 and type(self.socket_inode) is int and self.socket_inode > 0,
                 'Original reviewed Docker socket identity is required')


@dataclass(frozen=True)
class SharedDesktopPhysicalObservation:
    state_sha256: str
    service_sha256: str
    journal_sha256: str
    daemon_sha256: str
    evidence_sha256: str
    observed_at: str


class SharedDesktopCleanupObserver:
    """Read-only entered-service observation, not a release or launch permission.

    The trusted scope verifier must independently bind the original state,
    current separately authorized cleanup scope, irreversible original spawn
    fencing and native exclusion before and after these physical reads.
    Reviewed Docker identity must have been pinned before original launch,
    never approved from the absence observation itself. Scientist worker drain
    and authoritative ledger closure remain separate requirements.
    """

    def __init__(self, daemon, *, mount_namespace, cgroup_namespace,
                 verify_scope=_deny_scope, runner=run_bounded, clock=_boottime):
        _require(isinstance(daemon, ReviewedDockerDaemon), 'Reviewed original Docker daemon is required')
        _require(type(mount_namespace) is int and mount_namespace > 0
                 and type(cgroup_namespace) is int and cgroup_namespace > 0,
                 'Reviewed original mount and cgroup namespaces are required')
        _require(callable(verify_scope), 'Cleanup scope verifier must be a trusted callable')
        self.daemon = daemon
        self.mount_namespace = mount_namespace
        self.cgroup_namespace = cgroup_namespace
        self.verify_scope = verify_scope
        self.runner = runner
        self.clock = clock

    @staticmethod
    def _read(path, *, directory=None, max_bytes=16384):
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory)
        try:
            _require(stat.S_ISREG(os.fstat(descriptor).st_mode), 'Cleanup observation file type differs')
            data = bytearray()
            while chunk := os.read(descriptor, max_bytes + 1 - len(data)):
                data.extend(chunk)
                _require(len(data) <= max_bytes, 'Cleanup observation exceeds its bound')
            return data.decode('ascii')
        finally:
            os.close(descriptor)

    def _remaining(self, deadline):
        remaining = deadline - self.clock()
        _require(remaining > 0, 'Cleanup observation deadline expired')
        return min(2.0, remaining)

    def _command(self, arguments, deadline):
        environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
            'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
        result = self.runner(arguments, input=b'', env=environment,
                             timeout=self._remaining(deadline), max_output=16384)
        self._remaining(deadline)
        _require(result.returncode == 0 and not result.stderr,
                 'Cleanup command failed or reported ambiguity')
        return result.stdout.decode('ascii')

    def _daemon(self, deadline):
        socket = os.stat('/var/run/docker.sock', follow_symlinks=False)
        _require(stat.S_ISSOCK(socket.st_mode) and socket.st_uid == 0
                 and (socket.st_dev, socket.st_ino) == (self.daemon.socket_device, self.daemon.socket_inode),
                 'Original Docker socket identity changed')
        _require(self._command([*DOCKER, 'info', '--format', '{{.ID}}'], deadline).strip() == self.daemon.daemon_id,
                 'Original Docker daemon identity changed')

    def _unit(self, service, deadline):
        output = self._command(['/usr/bin/systemctl', '--user', 'show', service.unit,
            '--property=Id,LoadState,ActiveState,SubState,MainPID,InvocationID,ControlGroup,Job', '--no-pager'], deadline)
        rows = [line.split('=', 1) for line in output.splitlines()]
        _require(all(len(row) == 2 for row in rows), 'Cleanup unit properties are malformed')
        properties = dict(rows)
        _require(len(properties) == len(rows), 'Cleanup unit properties are duplicated')
        absent = {'Id': service.unit, 'LoadState': 'not-found', 'ActiveState': 'inactive',
                  'SubState': 'dead', 'MainPID': '0', 'InvocationID': '', 'ControlGroup': '', 'Job': ''}
        if properties == absent:
            return 'collected'
        inactive = {'Id': service.unit, 'LoadState': 'loaded', 'ActiveState': 'inactive',
                    'SubState': 'dead', 'MainPID': '0', 'InvocationID': service.invocation_id,
                    'ControlGroup': service.control_group, 'Job': ''}
        failed = inactive | {'ActiveState': 'failed', 'SubState': 'failed'}
        _require(properties in (inactive, failed), 'Original service is active, replaced or has an unresolved job')
        return 'retained'

    def _sample(self, state, workspace_descriptor, token_directory, deadline):
        self._remaining(deadline)
        _require(os.stat('/proc/self/ns/mnt').st_ino == self.mount_namespace
                 and os.stat('/proc/self/ns/cgroup').st_ino == self.cgroup_namespace,
                 'Original mount or cgroup observation namespace changed')
        service = state.service_binding
        _require(observe_process(service.process) == 'not_observed', 'Original process absence is not comparable or proven')
        try:
            os.stat('/proc/' + str(service.process.pid), follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('Original PID still exists or was reused')
        unit = self._unit(service, deadline)
        require_empty_cgroup(service.control_group, deadline, collected=unit == 'collected',
                             read=self._read, require=_require, clock=self.clock)
        workspace = Path(state.workspace)
        check = open_existing_workspace(workspace)
        try:
            _require(workspace_identity(workspace, check) == state.workspace_identity
                     == workspace_identity(workspace, workspace_descriptor), 'Original workspace identity changed')
        finally:
            os.close(check)
        journal = workspace.parent / '.aos-lifecycle' / (state.runtime_binding.birth.runtime_id + '.jsonl')
        events, journal_sha256 = read_journal(journal)
        _require(events[0].birth == state.runtime_binding.birth
                 and events[-1].stage in {'started', 'removed'}
                 and events[-1].container_id == state.runtime_binding.container_id,
                 'Original lifecycle binding differs or is incomplete')
        self._daemon(deadline)
        birth = state.runtime_binding.birth
        for selector in ('id=' + state.runtime_binding.container_id,
                         'name=^/' + birth.container_name + '$', 'label=com.aos.runtime=' + birth.runtime_id):
            output = self._command([*DOCKER, 'container', 'ls', '--all', '--no-trunc',
                '--filter', selector, '--format', '{{.ID}}'], deadline)
            _require(not output.strip(), 'Original runtime container or conflicting identity remains')
        self._daemon(deadline)
        try:
            os.stat(state.token_name, dir_fd=token_directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('Original token path remains or was replaced')
        _require(read_journal(journal)[1] == journal_sha256, 'Lifecycle changed during cleanup observation')
        return {'unit': unit, 'journal_sha256': journal_sha256}

    def observe(self, state, *, deadline):
        _require(type(deadline) in (int, float) and math.isfinite(deadline)
                 and 0 < deadline - self.clock() <= 10, 'Cleanup requires an existing finite deadline within ten seconds')
        frozen = SharedDesktopState.model_validate(state.model_dump(mode='json'))
        _require(frozen.phase in {'stopping', 'stopped', 'uncertain'} and all(value is not None for value in (
            frozen.service_binding, frozen.runtime_binding, frozen.workspace_identity, frozen.token_name, frozen.token_identity)),
            'Only an originally entered, bound stopped target can be observed')
        birth = frozen.runtime_binding.birth
        _require(birth.process == frozen.service_binding.process and birth.workspace == frozen.workspace_identity
                 and birth.process.uid == os.getuid(), 'Cleanup service, lifecycle or workspace owner differs')
        _require(birth.container_name == 'aos-desktop-' + hashlib.sha256(frozen.workspace.encode()).hexdigest()[:20],
                 'Original workspace container name differs')
        original_daemon = self.daemon
        original_namespaces = (self.mount_namespace, self.cgroup_namespace)

        def authority():
            self._remaining(deadline)
            _require(self.daemon == original_daemon
                     and (self.mount_namespace, self.cgroup_namespace) == original_namespaces,
                     'Reviewed cleanup environment changed during observation')
            _require(self.verify_scope(frozen.model_copy(deep=True), deadline) is None,
                     'Original cleanup scope must verify or raise')
            self._remaining(deadline)
            _require(self.daemon == original_daemon
                     and (self.mount_namespace, self.cgroup_namespace) == original_namespaces,
                     'Reviewed cleanup environment changed during authority verification')

        authority()
        descriptor = open_existing_workspace(Path(frozen.workspace))
        try:
            metadata = os.fstat(descriptor)
            _require(metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
                     'Original workspace privacy differs')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            tokens = open_existing_workspace(REPO_ROOT / 'runs')
            try:
                metadata = os.fstat(tokens)
                _require(metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
                         'Original token directory privacy differs')
                first = self._sample(frozen, descriptor, tokens, deadline)
                authority()
                second = self._sample(frozen, descriptor, tokens, deadline)
                _require(first == second, 'Original cleanup observations changed')
                token_check = open_existing_workspace(REPO_ROOT / 'runs')
                try:
                    _require(os.fstat(token_check) == os.fstat(tokens), 'Token directory changed during observation')
                finally:
                    os.close(token_check)
                authority()
            finally:
                os.close(tokens)
        finally:
            os.close(descriptor)
        state_sha = digest(frozen.model_dump(mode='json'))
        daemon_sha = digest(original_daemon.__dict__)
        return SharedDesktopPhysicalObservation(state_sha256=state_sha,
            service_sha256=digest(frozen.service_binding.model_dump(mode='json')),
            journal_sha256=first['journal_sha256'], daemon_sha256=daemon_sha,
            evidence_sha256=digest({'state': state_sha, 'daemon': daemon_sha,
                'mount_namespace': original_namespaces[0], 'cgroup_namespace': original_namespaces[1],
                'sample': first}), observed_at=now())

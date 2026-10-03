import hashlib
import math
import os
import re
import socket
import stat
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .bounded_process import run_bounded
from .scientist_protocol import (
    MAX_FRAME_BYTES, ScientistTurnReceipt, ScientistTurnRequest,
    scientist_receipt_frame, scientist_request_frame,
)


BROKER_UNIT = 'swapp-lab-gpu-broker.service'


class ScientistAdmissionError(RuntimeError):
    pass


class ScientistUncertainTurn(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerPeer:
    pid: int
    uid: int
    start_ticks: int
    boot_id: str
    invocation_id: str
    control_group: str


class BrokerAuthenticator(Protocol):
    def authenticate(self, pid: int, uid: int, *, deadline: float | None = None) -> BrokerPeer: ...

    def still_current(self, peer: BrokerPeer, *, deadline: float | None = None) -> bool: ...


def _process_identity(pid: int) -> tuple[int, str, str]:
    fields = Path(f'/proc/{pid}/stat').read_text(encoding='ascii').rsplit(')', 1)[1].split()
    start_ticks = int(fields[19])
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    groups = Path(f'/proc/{pid}/cgroup').read_text(encoding='ascii').splitlines()
    unified = [line[3:].rstrip('/') for line in groups if line.startswith('0::/')]
    if len(unified) != 1:
        raise ScientistAdmissionError('Broker requires one unified cgroup identity')
    return start_ticks, boot_id, unified[0]


def _authentication_deadline(deadline):
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ScientistAdmissionError('Authentication outer deadline must be finite')
    current = time.monotonic()
    result = min(current + 2, deadline) if deadline is not None else current + 2
    _authentication_remaining(result)
    return result


def _authentication_remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScientistAdmissionError('Authentication deadline expired')
    return remaining


class SystemdBrokerAuthenticator:
    def authenticate(self, pid: int, uid: int, *, deadline=None) -> BrokerPeer:
        deadline = _authentication_deadline(deadline)
        if type(pid) is not int or pid <= 1 or uid != os.getuid():
            raise ScientistAdmissionError('Broker peer UID or PID differs')
        try:
            before = _process_identity(pid)
            runtime = Path('/run/user') / str(os.getuid())
            configured_runtime = os.environ.get('XDG_RUNTIME_DIR')
            if (configured_runtime != str(runtime) or runtime.is_symlink()
                    or not runtime.is_dir() or runtime.stat().st_uid != os.getuid()):
                raise ScientistAdmissionError('Broker requires the current user runtime directory')
            bus = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
            if re.fullmatch(rf'unix:path={re.escape(str(runtime / "bus"))}(?:,guid=[a-f0-9]{{32}})?', bus) is None:
                raise ScientistAdmissionError('Broker requires the current user systemd bus')
            result = subprocess.run(
                ['/usr/bin/systemctl', '--user', 'show', BROKER_UNIT,
                 '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup', '--no-pager'],
                capture_output=True, text=True, timeout=_authentication_remaining(deadline), check=False,
                env={'PATH': '/usr/bin:/bin', 'XDG_RUNTIME_DIR': str(runtime),
                     'DBUS_SESSION_BUS_ADDRESS': bus},
            )
            properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            invocation = properties.get('InvocationID', '')
            group = properties.get('ControlGroup', '').rstrip('/')
            if (result.returncode != 0 or properties.get('LoadState') != 'loaded'
                    or properties.get('ActiveState') != 'active' or properties.get('MainPID') != str(pid)
                    or re.fullmatch('[a-f0-9]{32}', invocation) is None
                    or not group.endswith('/' + BROKER_UNIT) or before[2] != group
                    or _process_identity(pid) != before):
                raise ScientistAdmissionError('Broker service generation or PID identity differs')
            _authentication_remaining(deadline)
            return BrokerPeer(pid, uid, before[0], before[1], invocation, group)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise ScientistAdmissionError('Pinned broker identity could not be verified') from error

    def still_current(self, peer: BrokerPeer, *, deadline=None) -> bool:
        try:
            return self.authenticate(peer.pid, peer.uid, deadline=deadline) == peer
        except ScientistAdmissionError:
            return False


def _caller_proc_text(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ScientistAdmissionError('Caller process metadata is not regular')
        raw = os.read(descriptor, 16385)
        if len(raw) > 16384:
            raise ScientistAdmissionError('Caller process metadata exceeds its bound')
        return raw.decode('ascii')
    finally:
        os.close(descriptor)


def _caller_process_identity(pid):
    raw = _caller_proc_text(f'/proc/{pid}/stat')
    if not raw.startswith(f'{pid} (') or ')' not in raw:
        raise ScientistAdmissionError('Caller process stat identity differs')
    fields = raw.rsplit(')', 1)[1].split()
    if (len(fields) < 20 or fields[0] not in ('R', 'S', 'D', 'T', 't', 'K', 'W', 'P', 'I')
            or re.fullmatch(r'[1-9][0-9]*', fields[19]) is None):
        raise ScientistAdmissionError('Caller process start identity is unavailable')
    start = int(fields[19])
    boot = _caller_proc_text('/proc/sys/kernel/random/boot_id').strip()
    groups = _caller_proc_text(f'/proc/{pid}/cgroup').splitlines()
    unified = [line[3:] for line in groups if line.startswith('0::/')]
    if (start > 2**53 - 1 or re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', boot) is None
            or len(unified) != 1 or '..' in Path(unified[0]).parts
            or str(Path(unified[0])) != unified[0]):
        raise ScientistAdmissionError('Caller boot or unified cgroup identity differs')
    return start, boot, unified[0]


def _caller_environment():
    runtime = Path('/run/user') / str(os.getuid())
    address = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
    directory, bus = runtime.lstat(), (runtime / 'bus').lstat()
    if (os.environ.get('XDG_RUNTIME_DIR') != str(runtime)
            or not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) & 0o077
            or not stat.S_ISSOCK(bus.st_mode) or bus.st_uid != os.getuid()
            or re.fullmatch(rf'unix:path={re.escape(str(runtime / "bus"))}(?:,guid=[a-f0-9]{{32}})?', address) is None):
        raise ScientistAdmissionError('Caller requires the private current user systemd bus')
    identity = tuple((info.st_dev, info.st_ino, info.st_mode, info.st_uid) for info in (directory, bus))
    return ({'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'XDG_RUNTIME_DIR': str(runtime),
             'DBUS_SESSION_BUS_ADDRESS': address}, identity)


class SystemdCallerAuthenticator:
    """Narrow local caller/service generation proof, not runtime authorization.

    parent_pid identifies the reviewed service MainPID, never kernel PPID.
    The current producer enumerates exact-unit cgroup.procs, so descendant
    cgroups are unsupported and denied rather than widening its semantics.
    """

    def authenticate(self, generation, *, deadline=None):
        from .scientist_admission_history import ScientistCallerGeneration

        deadline = _authentication_deadline(deadline)
        try:
            if not isinstance(generation, ScientistCallerGeneration):
                raise ScientistAdmissionError('Caller authentication requires its full typed reviewed generation')
            expected = ScientistCallerGeneration.model_validate(generation.model_dump(mode='json'), strict=True)
            if (expected.pid != os.getpid() or expected.uid != os.getuid() or expected.pid <= 1
                    or expected.parent_pid <= 1 or expected.start_ticks <= 0 or expected.parent_start_ticks <= 0
                    or re.fullmatch(r'swapp-aos-[a-z0-9-]+\.service', expected.unit) is None
                    or not expected.control_group.endswith('/' + expected.unit)
                    or str(Path(expected.control_group)) != expected.control_group):
                raise ScientistAdmissionError('Reviewed caller process or fixed service identity differs')
            caller = _caller_process_identity(expected.pid)
            parent = _caller_process_identity(expected.parent_pid)
            if (caller != (expected.start_ticks, expected.boot_id, expected.control_group)
                    or parent != (expected.parent_start_ticks, expected.boot_id, expected.control_group)):
                raise ScientistAdmissionError('Caller or service MainPID generation differs')
            environment, bus_identity = _caller_environment()
            remaining = _authentication_remaining(deadline)
            names = ('Id', 'LoadState', 'ActiveState', 'MainPID', 'InvocationID', 'ControlGroup')
            result = run_bounded(['/usr/bin/systemctl', '--user', 'show', expected.unit,
                                  '--property=' + ','.join(names), '--no-pager'],
                                 input=b'', env=environment, timeout=remaining, max_output=16384)
            if (result.returncode != 0 or type(result.stdout) is not bytes or type(result.stderr) is not bytes
                    or result.stderr or len(result.stdout) + len(result.stderr) > 16384):
                raise ScientistAdmissionError('Caller service query failed or exceeded its bound')
            properties = {}
            for line in result.stdout.decode('ascii').splitlines():
                if '=' not in line:
                    raise ScientistAdmissionError('Caller service properties are malformed')
                name, value = line.split('=', 1)
                if name not in names or name in properties:
                    raise ScientistAdmissionError('Caller service properties are ambiguous')
                properties[name] = value
            if (properties != {'Id': expected.unit, 'LoadState': 'loaded', 'ActiveState': 'active',
                    'MainPID': str(expected.parent_pid), 'InvocationID': expected.invocation_id,
                    'ControlGroup': expected.control_group}
                    or _caller_process_identity(expected.pid) != caller
                    or _caller_process_identity(expected.parent_pid) != parent
                    or _caller_environment() != (environment, bus_identity)
                    or os.getpid() != expected.pid or os.getuid() != expected.uid
                    or time.monotonic() >= deadline):
                raise ScientistAdmissionError('Caller service generation changed or is not the exact reviewed unit')
            return expected
        except (OSError, ValueError, TypeError, UnicodeError, IndexError, subprocess.SubprocessError) as error:
            raise ScientistAdmissionError('Reviewed caller identity could not be verified') from error

    def still_current(self, generation, *, deadline=None):
        try:
            return self.authenticate(generation, deadline=deadline) == generation
        except ScientistAdmissionError:
            return False


def _deny_unconfirmed(request: ScientistTurnRequest) -> None:
    raise ScientistAdmissionError('Joint runtime capabilities and authorized intent are not configured')


def _deny_unpersisted(frame: bytes, digest: str, deadline: float, peer: BrokerPeer) -> None:
    raise ScientistAdmissionError('Durable authorized intent writer is not configured')


def _deny_unresolved(request_id: str) -> None:
    raise ScientistAdmissionError('Trusted original Scientist resolution verification is not configured')


class ScientistTurnClient:
    def __init__(self, socket_path: Path, *, timeout_seconds: int = 720,
                 authenticator: BrokerAuthenticator | None = None,
                 verify_admission: Callable[[ScientistTurnRequest], None] = _deny_unconfirmed,
                 persist_intent: Callable[[bytes, str, float, BrokerPeer], None] = _deny_unpersisted,
                 record_receipt: Callable[[ScientistTurnReceipt, BrokerPeer], None] | None = None,
                 validate_receipt: Callable[[ScientistTurnRequest, ScientistTurnReceipt], None] | None = None):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 720:
            raise ValueError('Scientist turn timeout must be an integer within 1..720 seconds')
        if not socket_path.is_absolute() or '..' in socket_path.parts:
            raise ValueError('Scientist socket requires an explicitly configured absolute path')
        if validate_receipt is not None and not callable(validate_receipt):
            raise ValueError('Scientist receipt validator must be an explicit callable')
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self.authenticator = authenticator or SystemdBrokerAuthenticator()
        self.verify_admission = verify_admission
        self.persist_intent = persist_intent
        self.record_receipt = record_receipt
        self.validate_receipt = validate_receipt
        self._lock = threading.Lock()
        self._attempted: set[str] = set()
        self._uncertain_request_id: str | None = None

    @property
    def uncertain_request_id(self) -> str | None:
        return self._uncertain_request_id

    def rearm(self, request_id: str, *,
              verify_resolution: Callable[[str], None] = _deny_unresolved) -> None:
        self._rearm(request_id, verify_resolution=verify_resolution)

    def _rearm(self, request_id: str, *, verify_resolution: Callable[[str], None],
               cancelled_request_id: str | None = None) -> None:
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Active Scientist worker prevents client rearm')
        try:
            if (type(request_id) is not str or re.fullmatch('[a-f0-9]{32}', request_id) is None
                    or (self._uncertain_request_id is None and cancelled_request_id is None)
                    or (self._uncertain_request_id is not None and self._uncertain_request_id != request_id)
                    or (cancelled_request_id is not None and cancelled_request_id != request_id)):
                raise ScientistAdmissionError('Scientist rearm requires the exact latched original request')
            if not callable(verify_resolution) or verify_resolution(request_id) is not None:
                raise ScientistAdmissionError('Trusted resolution verifier must complete or raise')
            self._uncertain_request_id = None
        finally:
            self._lock.release()

    def _admit(self, request: ScientistTurnRequest) -> None:
        if self.verify_admission(request.model_copy(deep=True)) is not None:
            raise ScientistAdmissionError('Admission verifier must attest by returning None or raise')

    def _socket_identity(self) -> tuple[int, int]:
        parent = self.socket_path.parent
        parent_info = parent.lstat()
        info = self.socket_path.lstat()
        if (parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077
                or self.socket_path.is_symlink() or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ScientistAdmissionError('Scientist broker socket must be private and user-owned')
        return info.st_dev, info.st_ino

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Scientist transport deadline expired')
        return remaining

    def infer(self, request: ScientistTurnRequest, *, cancel_event: threading.Event | None = None,
              deadline: float | None = None) -> ScientistTurnReceipt:
        frame = scientist_request_frame(request)
        frozen = ScientistTurnRequest.model_validate_json(frame[:-1], strict=True)
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Scientist client already has an active turn')
        attempted = False
        try:
            outer_deadline = deadline
            if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
                raise ScientistAdmissionError('Scientist outer deadline must be finite')
            if deadline is not None:
                self._remaining(deadline)
            def current():
                if cancel_event is not None and cancel_event.is_set():
                    raise ScientistAdmissionError('Local turn waiting was cancelled; remote release is not proven')

            current()
            if self.uncertain_request_id is not None:
                raise ScientistAdmissionError('Uncertain Scientist dispatch requires trusted reconciliation')
            if frozen.request_id in self._attempted or len(self._attempted) >= 256:
                raise ScientistAdmissionError('Scientist request replay or client lifetime bound reached')
            self._admit(frozen)
            identity = self._socket_identity()
            deadline = min(time.monotonic() + self.timeout_seconds, deadline if deadline is not None else float('inf'))
            if outer_deadline is not None:
                self._remaining(deadline)
            def authenticated_peer(credentials):
                peer_pid, peer_uid, peer_gid = struct.unpack('3i', credentials)
                peer = self.authenticator.authenticate(peer_pid, peer_uid, deadline=deadline)
                if not isinstance(peer, BrokerPeer) or peer.pid != peer_pid or peer.uid != peer_uid:
                    raise ScientistAdmissionError('Authenticated broker differs from socket peer credentials')
                self._remaining(deadline)
                current()
                return peer

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(self._remaining(deadline))
                probe.connect(str(self.socket_path))
                credentials = probe.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            peer = authenticated_peer(credentials)
            if self._socket_identity() != identity:
                raise ScientistAdmissionError('Scientist socket changed during connection')
            request_digest = hashlib.sha256(frame[:-1]).hexdigest()
            if self.persist_intent(frame[:-1], request_digest, deadline, peer) is not None:
                raise ScientistAdmissionError('Intent writer must durably complete or raise')
            current()
            if self._socket_identity() != identity:
                raise ScientistAdmissionError('Scientist socket changed during intent preparation')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._remaining(deadline))
                connection.connect(str(self.socket_path))
                credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                if authenticated_peer(credentials) != peer:
                    raise ScientistAdmissionError('Scientist broker generation changed during intent preparation')
                if self._socket_identity() != identity:
                    raise ScientistAdmissionError('Scientist socket changed during connection')
                self._admit(frozen)
                current()
                if self.authenticator.still_current(peer, deadline=deadline) is not True:
                    raise ScientistAdmissionError('Scientist peer generation changed before dispatch')
                current()
                if self._socket_identity() != identity:
                    raise ScientistAdmissionError('Scientist socket changed before dispatch')
                connection.settimeout(self._remaining(deadline))
                self._attempted.add(frozen.request_id)
                attempted = True
                connection.sendall(frame)
                data = bytearray()
                while True:
                    current()
                    remaining = self._remaining(deadline)
                    connection.settimeout(remaining if cancel_event is None else min(0.1, remaining))
                    try:
                        chunk = connection.recv(min(8192, MAX_FRAME_BYTES + 1 - len(data)))
                    except socket.timeout:
                        if cancel_event is None:
                            raise
                        continue
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_FRAME_BYTES:
                        raise ValueError('Scientist broker response exceeds its frame bound')
                receipt = scientist_receipt_frame(bytes(data), frozen)
                if self.validate_receipt is not None:
                    if self.validate_receipt(frozen.model_copy(deep=True), receipt.model_copy(deep=True)) is not None:
                        raise ScientistAdmissionError('Receipt validator must complete or raise')
                current()
                if self.authenticator.still_current(peer, deadline=deadline) is not True:
                    raise ScientistAdmissionError('Scientist peer generation changed during dispatch')
                self._admit(frozen)
                self._remaining(deadline)
                if self.record_receipt is not None:
                    if self.record_receipt(receipt.model_copy(deep=True), peer) is not None:
                        raise ScientistAdmissionError('Receipt writer must durably complete or raise')
                    self._remaining(deadline)
                return receipt
        except BaseException as error:
            if attempted:
                self._uncertain_request_id = frozen.request_id
                if isinstance(error, Exception):
                    raise ScientistUncertainTurn('Scientist turn outcome is uncertain; automatic retry is forbidden') from error
            raise
        finally:
            self._lock.release()

from copy import deepcopy
import csv
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from jsonschema import SchemaError

from .bounded_process import run_bounded
from .linux_cgroup_observation import require_empty_cgroup
from .scientist_evidence_transport import (
    RESPONSE_LIMIT, SCHEMA_LIMIT, ScientistEvidenceCodec, _STRICT_VALIDATOR, _decode, _schema_boundary,
)
from .scientist_protocol import ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
from .scientist_terminal import (
    ScientistTerminalBudget, ScientistTerminalChild, ScientistTerminalReceipt, ScientistTerminalVerifier,
    _deny_resolver, _deny_result, digest,
)
from .scientist_transport import ScientistAdmissionError


def _deny_physical(original, terminal, allocation, drain, no_admission):
    raise ScientistAdmissionError('Independent physical allocation/fencing and release verification is not configured')


def _require(condition, message):
    if not condition:
        raise ScientistAdmissionError(message)


def _deny_physical_source(*arguments):
    raise ScientistAdmissionError('Independent current physical source authority is not configured')


class ScientistPhysicalReleaseVerifier:
    """Read-only Linux release observer for a retained, bound child generation.

    verify_source(original, terminal, allocation, drain, no_admission, gpu_uuid)
    must independently authenticate the original source and allocation fence,
    child intent nonce, device binding, complete observed GPU PID provenance,
    and irreversible late-start fencing. It must independently read the
    canonical current arbiter, reject an active or quarantined original target,
    and check that its current fencing token never regresses. Matching a
    historical allocation token is insufficient; retained PID provenance must
    remain immutable across observations.
    verify_current(original, terminal) must check current host authority. Both
    gates run before and after observation and must return None or raise. Hash
    equality alone cannot implement either gate. Collected units require
    repeated authoritative absence and a readable cgroup2 mount; non-allocation
    cases are unsupported. Quiet mode requires no compute processes; explicit
    shared-lane mode requires absence of every original observed PID and the
    child PID, conservatively rejecting reused owned PIDs. Neither mode grants
    GPU ownership or changes another lane. Memory is only query validation.
    """

    def __init__(self, *, gpu_uuid, verify_source=_deny_physical_source,
                 verify_current=_deny_physical_source, allow_shared_lanes=False):
        if (type(gpu_uuid) is not str
                or re.fullmatch(r'GPU-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', gpu_uuid) is None):
            raise ValueError('Physical release requires an explicit full GPU UUID')
        if not callable(verify_source) or not callable(verify_current):
            raise TypeError('Physical release authority must be trusted callables')
        if type(allow_shared_lanes) is not bool:
            raise ValueError('Shared-lane observation requires an explicit boolean host setting')
        self._gpu_uuid = gpu_uuid
        self._allow_shared_lanes = allow_shared_lanes
        self.verify_source = verify_source
        self.verify_current = verify_current

    @property
    def gpu_uuid(self):
        return self._gpu_uuid

    @property
    def allow_shared_lanes(self):
        return self._allow_shared_lanes

    @staticmethod
    def _read(path, *, directory=None, max_bytes=16384):
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                             dir_fd=directory)
        try:
            _require(stat.S_ISREG(os.fstat(descriptor).st_mode), 'Physical observation file type differs')
            data = bytearray()
            while chunk := os.read(descriptor, max_bytes + 1 - len(data)):
                data.extend(chunk)
                _require(len(data) <= max_bytes, 'Physical observation exceeds its bound')
            return data.decode('ascii')
        finally:
            os.close(descriptor)

    @staticmethod
    def _environment():
        runtime = Path('/run/user') / str(os.getuid())
        bus = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
        info = runtime.lstat()
        bus_info = (runtime / 'bus').lstat()
        _require(os.environ.get('XDG_RUNTIME_DIR') == str(runtime)
                 and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and not stat.S_IMODE(info.st_mode) & 0o077
                 and stat.S_ISSOCK(bus_info.st_mode) and bus_info.st_uid == os.getuid()
                 and re.fullmatch(rf'unix:path={re.escape(str(runtime / "bus"))}(?:,guid=[a-f0-9]{{32}})?', bus),
                 'Physical observation requires the current user systemd bus')
        return {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'XDG_RUNTIME_DIR': str(runtime),
                'DBUS_SESSION_BUS_ADDRESS': bus}

    @staticmethod
    def _command(arguments, environment, deadline):
        remaining = min(2.0, deadline - time.monotonic())
        _require(remaining > 0, 'Physical observation deadline expired')
        result = run_bounded(arguments, input=b'', env=environment, timeout=remaining, max_output=16384)
        _require(result.returncode == 0 and not result.stderr,
                 'Physical observation command failed or reported ambiguity')
        return result.stdout.decode('ascii')

    def _unit(self, child, environment, deadline):
        output = self._command(['/usr/bin/systemctl', '--user', 'show', child.unit,
            '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup', '--no-pager'],
            environment, deadline)
        rows = [line.split('=', 1) for line in output.splitlines()]
        _require(all(len(row) == 2 for row in rows), 'Physical unit observation is malformed')
        properties = dict(rows)
        _require(len(properties) == len(rows), 'Physical unit observation has duplicate properties')
        if properties == {'LoadState': 'not-found', 'ActiveState': 'inactive', 'MainPID': '0',
                          'InvocationID': '', 'ControlGroup': ''}:
            return 'collected'
        _require(properties == {
            'LoadState': 'loaded', 'ActiveState': 'inactive', 'MainPID': '0',
            'InvocationID': child.invocation_id, 'ControlGroup': child.control_group},
            'Physical unit generation is missing, changed or not inactive')
        return 'inactive'

    def _process(self, child):
        _require(self._read('/proc/sys/kernel/random/boot_id').strip() == child.boot_id,
                 'Physical observation boot generation differs')
        try:
            process = self._read(f'/proc/{child.pid}/stat')
        except FileNotFoundError:
            return
        fields = process.rsplit(')', 1)[1].split()
        _require(int(fields[19]) == child.start_ticks, 'Physical child PID was reused')
        raise ScientistAdmissionError('Physical child process generation still exists')

    def _cgroup(self, child, deadline, *, collected):
        require_empty_cgroup(child.control_group, deadline, collected=collected,
                             read=self._read, require=_require, clock=time.monotonic)

    def _gpu(self, environment, deadline, owned_pids, gpu_uuid, allow_shared_lanes):
        arguments = ['/usr/bin/nvidia-smi', '--id=' + gpu_uuid]
        processes = self._command([*arguments, '--query-compute-apps=pid,process_name,used_memory',
                                   '--format=csv,noheader,nounits'], environment, deadline)
        observed = set()
        for line in processes.splitlines():
            fields = next(csv.reader([line], strict=True, skipinitialspace=True))
            fields = [field.strip() for field in fields]
            _require(len(fields) == 3
                     and re.fullmatch(r'[1-9][0-9]{0,9}', fields[0])
                     and 1 <= int(fields[0]) <= 2**31 - 1
                     and 1 <= len(fields[1]) <= 4096
                     and all(32 <= ord(character) <= 126 for character in fields[1])
                     and re.fullmatch(r'[0-9]{1,16}', fields[2])
                     and int(fields[2]) <= 2**53 - 1,
                     'Physical GPU process observation is malformed')
            pid = int(fields[0])
            _require(pid not in observed, 'Physical GPU process observation contains duplicate PIDs')
            observed.add(pid)
        _require(not observed.intersection(owned_pids), 'An original GPU PID remains or was reused')
        _require(allow_shared_lanes or not observed, 'Quiet physical observation found another compute process')
        memory = self._command([*arguments, '--query-gpu=memory.used,memory.total',
                                '--format=csv,noheader,nounits'], environment, deadline)
        rows = memory.splitlines()
        fields = [field.strip() for field in rows[0].split(',')] if len(rows) == 1 else []
        _require(len(fields) == 2 and all(re.fullmatch(r'[0-9]+', field) for field in fields)
                 and 0 <= int(fields[0]) <= int(fields[1]) and int(fields[1]) > 0,
                 'Physical GPU identity query is unavailable or malformed')

    def __call__(self, original, terminal, allocation, drain, no_admission):
        try:
            frozen = deepcopy((original, terminal, allocation, drain, no_admission))
            gpu_uuid, allow_shared_lanes = self.gpu_uuid, self.allow_shared_lanes

            def configuration_current():
                _require((self.gpu_uuid, self.allow_shared_lanes) == (gpu_uuid, allow_shared_lanes),
                         'Physical device or lane policy changed during verification')

            def authority():
                configuration_current()
                _require(self.verify_current(deepcopy(frozen[0]), deepcopy(frozen[1])) is None,
                         'Physical current authority must complete or raise')
                configuration_current()
                _require(self.verify_source(*deepcopy(frozen), gpu_uuid) is None,
                         'Physical source authority must complete or raise')
                configuration_current()

            authority()
            original, terminal, allocation, drain, no_admission = frozen
            child = terminal.child_generation
            _require(allocation is not None and drain is not None and no_admission is None
                     and child is not None and drain['kind'] == 'physical_drain'
                     and drain['never_started'] is False and drain['late_start_fenced'] is True
                     and drain['child_generation'] == child.model_dump(mode='json')
                     and drain['child_intent']['unit'] == child.unit
                     and terminal.recorded_boot_id == child.boot_id,
                     'Physical observer supports only an independently bound drained child')
            _require(terminal.original_principal.uid == os.getuid(),
                     'Physical observation requires the original current-user principal')
            child = ScientistTerminalChild.model_validate(child.model_dump(mode='json'), strict=True)
            observed_pids = drain['observed_gpu_pids']
            _require(type(observed_pids) is list and len(observed_pids) <= 4096
                     and all(type(pid) is int and 1 <= pid <= 2**31 - 1 for pid in observed_pids)
                     and len(set(observed_pids)) == len(observed_pids),
                     'Original observed GPU PID set is malformed or unbounded')
            owned_pids = frozenset(observed_pids) | {child.pid}
            deadline = time.monotonic() + 10
            environment = self._environment()
            unit_state = self._unit(child, environment, deadline)
            self._process(child)
            self._cgroup(child, deadline, collected=unit_state == 'collected')
            self._gpu(environment, deadline, owned_pids, gpu_uuid, allow_shared_lanes)
            self._cgroup(child, deadline, collected=unit_state == 'collected')
            self._process(child)
            _require(self._unit(child, environment, deadline) == unit_state,
                     'Physical unit state changed during observation')
            self._gpu(environment, deadline, owned_pids, gpu_uuid, allow_shared_lanes)
            authority()
            _require(time.monotonic() < deadline, 'Physical observation deadline expired')
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError,
                RecursionError, csv.Error, subprocess.SubprocessError) as error:
            raise ScientistAdmissionError('Physical release observation is unavailable or ambiguous') from error


class ScientistReleaseProofVerifier:
    """Check retained v1 preimages without deriving physical truth from hashes.

    verify_physical must independently bind the retained allocation, fencing
    token and child nonce, then attest release or non-allocation. Resolver and
    physical callbacks return None or raise; no callback grants new inference.
    """

    def __init__(self, history, *, evidence_schema_bytes, evidence_schema_sha256,
                 descriptor_sha256, terminal_schema_sha256, expected_budget,
                 validate_result=_deny_result, verify_resolver=_deny_resolver,
                 verify_physical=_deny_physical):
        schema = _decode(evidence_schema_bytes, SCHEMA_LIMIT, canonical_required=False)
        _require(digest(schema) == evidence_schema_sha256
                 and schema.get('$schema') == 'https://json-schema.org/draft/2020-12/schema'
                 and type(schema.get('$defs')) is dict
                 and all(name in schema['$defs'] for name in
                         ('terminal_evidence', 'legacy_allocation', 'legacy_drain', 'legacy_no_admission')),
                 'Release proof requires its explicit reviewed complete evidence schema')
        _schema_boundary(schema, schema['$defs'])
        try:
            _STRICT_VALIDATOR.check_schema(schema)
        except (SchemaError, ValueError, TypeError, RecursionError) as error:
            raise ScientistAdmissionError('Reviewed release proof schema is invalid') from error
        self._validators = {name: _STRICT_VALIDATOR({**deepcopy(schema), '$ref': '#/$defs/' + name})
                            for name in ('terminal_evidence', 'legacy_allocation', 'legacy_drain', 'legacy_no_admission')}
        if not callable(verify_physical):
            raise TypeError('Physical release verification must be an explicit trusted callable')
        terminal = ScientistTerminalVerifier(history, descriptor_sha256=descriptor_sha256,
            schema_sha256=terminal_schema_sha256, expected_budget=expected_budget,
            validate_result=validate_result, verify_resolver=verify_resolver)
        self.history = history
        self.expected_budget = ScientistTerminalBudget.model_validate(terminal.expected_budget.model_dump(mode='json'), strict=True)
        self.descriptor_sha256 = descriptor_sha256
        self.terminal_schema_sha256 = terminal_schema_sha256
        self.validate_result = validate_result
        self.verify_resolver = verify_resolver
        self.verify_physical = verify_physical

    def _parts(self, evidence, terminal):
        parts = {}
        for name, hash_field in (('allocation', 'allocation_binding_sha256'),
                                 ('drain', 'drain_evidence_sha256'),
                                 ('no_admission', 'no_admission_evidence_sha256')):
            encoded = evidence[name + '_canonical']
            if encoded is None:
                _require(terminal[hash_field] is None, 'Terminal release preimage is missing')
                parts[name] = None
            else:
                part = _decode(encoded.encode('utf-8'), RESPONSE_LIMIT)
                ScientistEvidenceCodec._validate(self._validators['legacy_' + name], part)
                _require(digest(part) == terminal[hash_field], 'Original release preimage hash differs')
                parts[name] = part
        return parts

    def _bind(self, terminal, parts):
        allocation, drain, no_admission = (parts[name] for name in ('allocation', 'drain', 'no_admission'))
        principal, budget = terminal['original_principal'], terminal['original_budget']
        if no_admission is not None:
            _require(allocation is None and drain is None
                     and no_admission['request_id'] == terminal['request_id']
                     and no_admission['request_sha256'] == terminal['request_sha256']
                     and no_admission['principal_sha256'] == digest(principal)
                     and no_admission['reason'] == terminal['reason_code']
                     and no_admission['cancel_before_intent'] == (terminal['admission_binding'] is None),
                     'No-admission proof differs from the original target and reason')
        if allocation is None:
            _require(drain is None, 'Drain cannot exist without its original allocation')
            return
        _require(drain is not None and no_admission is None and budget is not None,
                 'Allocated release requires its drain and original budget')
        lease = allocation['lease']
        configured = ('activation_seconds', 'inference_seconds', 'total_seconds', 'queue_seconds',
                      'max_output_tokens', 'context_tokens')
        _require(allocation['original_principal'] == principal
                 and allocation['admission_binding_sha256'] == terminal['admission_binding_sha256']
                 and allocation['request_sha256'] == terminal['request_sha256']
                 and allocation['original_deadline'] == budget['envelope_deadline']
                 and allocation['original_budget'] == {name: budget[name] for name in configured}
                 and lease['request_id'] == terminal['request_id']
                 and lease['owner_identity'] == {name: principal[name] for name in ('pid', 'start_ticks', 'boot_id')}
                 and lease['owner_unit'] == principal['unit']
                 and lease['owner_invocation_id'] == principal['invocation_id'],
                 'Allocation ownership, request, admission or configured budget differs')
        _require(budget['admitted_boottime'] is not None
                 and lease['activation_deadline'] == budget['activation_deadline']
                 and lease['total_deadline'] == budget['total_deadline']
                 and lease['inference_deadline'] == lease['total_deadline']
                 and lease['slice_seconds'] == budget['inference_seconds']
                 and budget['admitted_boottime'] < lease['heartbeat_deadline'] <= lease['activation_deadline'],
                 'Original activating lease differs from retained phase budgets')
        _require(drain['allocation_binding_sha256'] == terminal['allocation_binding_sha256']
                 and drain['child_generation'] == terminal['child_generation']
                 and drain['boot_id'] == terminal['recorded_boot_id']
                 and budget['admitted_boottime'] <= drain['observed_boottime'] <= terminal['recorded_boottime'],
                 'Drain original allocation, child or observation clock differs')
        child = drain['child_generation']
        _require(child is not None and drain['kind'] == 'physical_drain' and drain['never_started'] is False,
                 'Allocated-without-child release is not supported by this verifier')
        intent = drain['child_intent']
        _require(child['boot_id'] == drain['boot_id'] and intent['unit'] == child['unit']
                 and intent['total_seconds'] == budget['total_seconds']
                 and budget['admitted_boottime'] <= intent['created_boottime'] <= drain['observed_boottime']
                 and drain['handoff_stage'] is not None
                 and drain['late_start_fence'] == intent['created_boottime'] + intent['total_seconds'] + 30,
                 'Original child intent, late-start fence or handoff differs')

    def verify(self, request, evidence_bytes):
        try:
            request = ScientistTurnRequest.model_validate_json(scientist_request_frame(request)[:-1], strict=True)
            evidence = _decode(evidence_bytes, RESPONSE_LIMIT)
            ScientistEvidenceCodec._validate(self._validators['terminal_evidence'], evidence)
            terminal_bytes = evidence['terminal_canonical'].encode('utf-8')
            terminal = _decode(terminal_bytes, RESPONSE_LIMIT)
            ScientistTerminalReceipt.model_validate(terminal, strict=True)
            _require(evidence['target'] == {'request_id': request.request_id,
                     'request_sha256': scientist_request_sha256(request),
                     'original_peer_generation_sha256': digest(terminal['original_principal'])},
                     'Release evidence envelope differs from the original target')
            parts = self._parts(evidence, terminal)
            self._bind(terminal, parts)
            result = evidence['result_canonical']
            result_bytes = None if result is None else result.encode('utf-8')
            physical = self.verify_physical

            def verify_proof(original, receipt):
                if physical(original.model_copy(deep=True), receipt.model_copy(deep=True),
                            deepcopy(parts['allocation']), deepcopy(parts['drain']),
                            deepcopy(parts['no_admission'])) is not None:
                    raise ScientistAdmissionError('Physical release verifier must complete or raise')

            verifier = ScientistTerminalVerifier(self.history, descriptor_sha256=self.descriptor_sha256,
                schema_sha256=self.terminal_schema_sha256, expected_budget=self.expected_budget,
                validate_result=self.validate_result, verify_resolver=self.verify_resolver,
                verify_proof=verify_proof)
            return verifier.verify(request, terminal_bytes, result_bytes=result_bytes)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError) as error:
            raise ScientistAdmissionError('Release proof preimages are malformed or unbound') from error

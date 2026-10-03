"""Explicit observational validation; no scheduler, transport or implicit authority."""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
from types import SimpleNamespace

from .contracts import now
from .scientist_admission_history import ScientistAdmissionHistory, ScientistCallerGeneration, ScientistServerGeneration
from .scientist_evidence_transport import ScientistEvidenceCodec, _STRICT_VALIDATOR, _decode, _schema_boundary
from .scientist_intents import ScientistIntentBinding
from .scientist_protocol import ScientistTurnRequest, scientist_request_frame
from .scientist_release_proof import ScientistPhysicalReleaseVerifier
from .scientist_source_authority import FILE_LIMIT, TOTAL_LIMIT, _read as _read_source
from .scientist_terminal import canonical, digest
from .scientist_transport import ScientistAdmissionError, _caller_process_identity


NAME = 'aos-scientist-no-admission-observation.v1'
VERSION = 1
SCHEMA = NAME
FEATURE = 'no-admission-observation.v1'
SCHEMA_SHA256 = '92791f45ef6a319a27a5aade363832b737978080826537b8a1ffa7eef700cd63'
RECOVERY_SCHEMA_SHA256 = '6d173eb1231989b3b3304ae08455f9b10c192dc0aa37afa54acccb223615690b'
FRAME_LIMIT = 1024 * 1024


def _require(condition, reason):
    if not condition:
        raise ScientistAdmissionError(reason)


def _deny(*arguments):
    raise ScientistAdmissionError('Independent observational authority is not configured')


def _readonly_authorizer(action, first, second, database, trigger):
    permitted = (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE)
    if action in permitted or action == sqlite3.SQLITE_PRAGMA and second is None:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def sqlite_store_identity(store):
    databases = store.connection.execute('PRAGMA database_list').fetchall()
    paths = [row[2] for row in databases if row[1] == 'main']
    _require(len(paths) == 1 and paths[0], 'Observation requires the original on-disk store')
    path = Path(paths[0])
    info = path.lstat()
    _require(not path.is_symlink() and stat.S_ISREG(info.st_mode)
             and info.st_nlink == 1 and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600,
             'Observation store must be private, user-owned and regular')
    return {'path_sha256': hashlib.sha256(str(path.resolve()).encode('utf-8')).hexdigest(),
            'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid}


class ScientistNoAdmissionPhysicalVerifier:
    """Bounded read-only absence checks, not an allocation or GPU-release proof.

    verify_provenance must independently bind the complete original generation
    list and irreversible late dispatch/admission fences. verify_current must
    authenticate current observer/source and cleanup authority. Both receive a
    deep copy of the observation before and after checks and return None or
    raise. A hash or self-asserted provenance flag cannot implement either gate.
    """

    def __init__(self, *, verify_provenance=_deny, verify_current=_deny):
        _require(callable(verify_provenance) and callable(verify_current),
                 'Physical observation requires explicit trusted providers')
        self.verify_provenance, self.verify_current = verify_provenance, verify_current

    @staticmethod
    def _unit(generation, environment, deadline):
        output = ScientistPhysicalReleaseVerifier._command(
            ['/usr/bin/systemctl', '--user', 'show', generation.unit,
             '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup', '--no-pager'],
            environment, deadline)
        rows = [line.split('=', 1) for line in output.splitlines()]
        _require(all(len(row) == 2 for row in rows), 'Original unit observation is malformed')
        properties = dict(rows)
        _require(len(properties) == len(rows), 'Original unit properties are duplicated')
        collected = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'MainPID': '0',
                     'InvocationID': '', 'ControlGroup': ''}
        inactive = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'MainPID': '0',
                    'InvocationID': generation.invocation_id, 'ControlGroup': generation.control_group}
        _require(properties == collected or properties == inactive,
                 'Original unit is active, replaced or ambiguous')
        return properties

    @staticmethod
    def _process(generation):
        try:
            ScientistPhysicalReleaseVerifier._read(f'/proc/{generation.pid}/stat')
        except FileNotFoundError:
            return
        raise ScientistAdmissionError('Original process PID exists or was reused')

    @staticmethod
    def _cgroup(generation, deadline):
        read = ScientistPhysicalReleaseVerifier._read
        mounts = [line.split() for line in read('/proc/self/mountinfo', max_bytes=262144).splitlines()]
        matching = [row for row in mounts if len(row) > 6 and row[4] == '/sys/fs/cgroup']
        _require(len(matching) == 1 and '-' in matching[0]
                 and matching[0][matching[0].index('-') + 1] == 'cgroup2',
                 'Original cgroup observation requires an unambiguous cgroup2 mount')
        parts = generation.control_group.split('/')[1:]
        _require(0 < len(parts) <= 128 and all(part and part not in ('.', '..') for part in parts),
                 'Original cgroup path is ambiguous or exceeds its bound')
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
        descriptor = os.open('/sys/fs/cgroup', flags)
        try:
            read('cgroup.procs', directory=descriptor)
            read('cgroup.controllers', directory=descriptor)
            for part in parts:
                _require(time.monotonic() < deadline, 'Original cgroup observation expired')
                try:
                    nested = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    return
                os.close(descriptor)
                descriptor = nested
            raise ScientistAdmissionError('Original cgroup still exists, even if empty')
        finally:
            os.close(descriptor)

    def __call__(self, proof):
        _require(self.verify_provenance is not _deny and self.verify_current is not _deny,
                 'Independent physical provenance/current authority is not configured')
        try:
            frozen = deepcopy(proof)
            providers = (self.verify_current, self.verify_provenance)
            deadline = time.monotonic() + 10

            def authority():
                _require((self.verify_current, self.verify_provenance) == providers,
                         'Physical observation provider configuration changed')
                for provider in providers:
                    _require(provider(deepcopy(frozen)) is None,
                             'Physical observation authority must complete or raise')
                    _require(time.monotonic() < deadline, 'Physical observation authority expired')
                _require((self.verify_current, self.verify_provenance) == providers,
                         'Physical observation provider configuration changed')

            authority()
            physical = frozen['physical']
            original = frozen['original']['admission_record']['admission_binding']
            _require(physical['caller_generation'] == original['caller_generation']
                     and physical['broker_generation'] == original['server_generation']
                     and physical['target'] == frozen['target']
                     and all(physical[key] is True for key in ('caller_absent', 'broker_absent',
                             'original_cgroups_absent', 'children_absent', 'provenance_complete', 'late_dispatch_fenced')),
                     'Physical observation is not bound to the original absent target')
            caller = ScientistCallerGeneration.model_validate(physical['caller_generation'], strict=True)
            broker = ScientistServerGeneration.model_validate(physical['broker_generation'], strict=True)
            children = physical['children']
            _require(type(children) is list and len(children) <= 256, 'Physical child provenance exceeds its bound')
            generations = [caller, broker]
            for child in children:
                _require(child['generation_sha256'] == digest(child['generation'])
                         and child['process_absent'] is True and child['cgroup_absent'] is True,
                         'Physical child absence or generation digest differs')
                generations.append(ScientistServerGeneration.model_validate(child['generation'], strict=True))
            _require(len({generation.pid for generation in generations}) == len(generations),
                     'Physical original generation PIDs are duplicated')
            boot = ScientistPhysicalReleaseVerifier._read('/proc/sys/kernel/random/boot_id').strip()
            _require(all(generation.uid == os.getuid() and generation.boot_id == boot for generation in generations),
                     'Physical original UID or boot differs')
            environment = ScientistPhysicalReleaseVerifier._environment()
            initial = []
            for generation in generations:
                _require(time.monotonic() < deadline, 'Physical observation expired')
                initial.append(self._unit(generation, environment, deadline))
                self._process(generation)
                self._cgroup(generation, deadline)
            for generation, state in zip(generations, initial):
                self._cgroup(generation, deadline)
                self._process(generation)
                _require(self._unit(generation, environment, deadline) == state,
                         'Original unit state changed during observation')
            _require(ScientistPhysicalReleaseVerifier._read('/proc/sys/kernel/random/boot_id').strip() == boot,
                     'Physical observation boot changed')
            authority()
        except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError,
                RecursionError, subprocess.SubprocessError) as error:
            raise ScientistAdmissionError('Original physical absence is unavailable or ambiguous') from error


class ScientistNoAdmissionRecoveryVerifier:
    """Read a separately reviewed cleanup-only authority, never one from proof.

    Pins and the absolute path are trusted host inputs. verify_current must
    independently authenticate their current authorization and source/observer
    binding; it receives a deep copy of the observation and returns None or
    raises. Private file permissions and matching hashes are not authorization.
    """

    def __init__(self, *, authority_path, authority_sha256, authority_raw_sha256, verify_current=_deny):
        path = Path(authority_path)
        _require(path.is_absolute(), 'Cleanup authority requires its explicitly reviewed absolute path')
        _require(all(type(checksum) is str and re.fullmatch('[a-f0-9]{64}', checksum)
                     for checksum in (authority_sha256, authority_raw_sha256)),
                 'Cleanup authority requires independently configured canonical and raw pins')
        _require(callable(verify_current), 'Cleanup authority requires a trusted current verifier')
        self.authority_path = path
        self.authority_sha256, self.authority_raw_sha256 = authority_sha256, authority_raw_sha256
        self.verify_current = verify_current

    @staticmethod
    def _read(path):
        _require(path.resolve(strict=True) == path, 'Cleanup authority path contains an alias or symlink')
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                     and before.st_uid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600
                     and 0 < before.st_size <= FRAME_LIMIT,
                     'Cleanup authority must be bounded, private, user-owned and regular')
            chunks = bytearray()
            while chunk := os.read(descriptor, FRAME_LIMIT + 1 - len(chunks)):
                chunks.extend(chunk)
                _require(len(chunks) <= FRAME_LIMIT, 'Cleanup authority exceeds its byte bound')
            after = os.fstat(descriptor)
            current = path.lstat()
            fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
            identity = tuple(getattr(before, field) for field in fields)
            _require(identity == tuple(getattr(after, field) for field in fields)
                     == tuple(getattr(current, field) for field in fields)
                     and path.resolve(strict=True) == path and len(chunks) == before.st_size,
                     'Cleanup authority changed during independent readback')
            return bytes(chunks), identity
        finally:
            os.close(descriptor)

    def __call__(self, proof):
        _require(self.verify_current is not _deny, 'Current cleanup authorization is not configured')
        try:
            frozen = deepcopy(proof)
            configuration = (self.authority_path, self.authority_sha256, self.authority_raw_sha256, self.verify_current)
            path, checksum, raw_checksum, provider = configuration

            def authority():
                _require(configuration == (self.authority_path, self.authority_sha256,
                                            self.authority_raw_sha256, self.verify_current),
                         'Cleanup authority provider configuration changed')
                _require(provider(deepcopy(frozen)) is None, 'Current cleanup authority must complete or raise')
                _require(configuration == (self.authority_path, self.authority_sha256,
                                            self.authority_raw_sha256, self.verify_current),
                         'Cleanup authority provider configuration changed')

            authority()
            raw, identity = self._read(path)
            value = _decode(raw, FRAME_LIMIT)
            _require(hashlib.sha256(raw).hexdigest() == raw_checksum and digest(value) == checksum,
                     'Independently reviewed cleanup authority pins differ')
            _require(set(value) == {'schema', 'version', 'target', 'cleanup_scope_without_context',
                                   'observer_unit', 'purpose', 'inference_allowed'}
                     and value['schema'] == 'aos-scientist-no-admission-cleanup-authority.v1'
                     and type(value['version']) is int and value['version'] == 1
                     and value['purpose'] == 'observe_no_admission' and value['inference_allowed'] is False,
                     'Cleanup authority is not the closed cleanup-only contract')
            scope = frozen['cleanup_scope']
            _require(value['target'] == frozen['target'] and scope['authorization_context_sha256'] == checksum
                     and value['cleanup_scope_without_context'] == {key: item for key, item in scope.items()
                                                                   if key != 'authorization_context_sha256'}
                     and scope['purpose'] == 'observe_no_admission'
                     and scope['current_status'] == 'stopped' and scope['current_owner'] == 'PAUSED'
                     and type(scope['original_generation']) is int and type(scope['current_generation']) is int
                     and scope['current_generation'] > scope['original_generation'] >= 0
                     and value['observer_unit'] == frozen['observer']['generation']['unit'],
                     'Cleanup authority differs from the original target/current scope or reviewed observer')
            authority()
            _require(self._read(path) == (raw, identity), 'Cleanup authority was changed or replaced')
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise ScientistAdmissionError('Independent cleanup authority readback was denied') from error


class ScientistNoAdmissionCanonicalVerifier:
    """Read the existing Scientist canonical DB; never create a store or fence.

    database_schema_sha256 pins the independently reviewed sorted name/type/SQL
    inventory of all tables and triggers (excluding sqlite_sequence). Current
    authority must authenticate that DB identity/schema and the normal admission
    source's transactional late-admission fence, before and after readback.
    """

    _surfaces = (
        ('aos_control_requests', 'request_id'), ('aos_control_reservations', 'request_id'),
        ('aos_cleanup_grants', 'request_id'), ('aos_control_idempotency', 'target_request_id'),
        ('gpu_turn_requests', 'request_id'), ('gpu_turn_state', 'active_request_id'),
        ('aos_gpu_child_bindings', 'request_id'), ('aos_gpu_output_bindings', 'request_id'),
        ('aos_gpu_turn_results', 'request_id'),
        ('gpu_runtime_bindings', 'request_id'),
    )

    def __init__(self, *, database_path, store_identity, database_schema_sha256, verify_current=_deny):
        self.database_path = Path(database_path)
        _require(self.database_path.is_absolute(), 'Canonical readback requires its reviewed absolute path')
        _require(type(store_identity) is dict and set(store_identity) == {'path_sha256', 'device', 'inode', 'uid'}
                 and type(database_schema_sha256) is str and re.fullmatch('[a-f0-9]{64}', database_schema_sha256),
                 'Canonical readback requires independent database identity and schema pins')
        _require(callable(verify_current), 'Canonical readback requires a trusted current verifier')
        self.store_identity = deepcopy(store_identity)
        self.database_schema_sha256 = database_schema_sha256
        self.verify_current = verify_current

    def __call__(self, proof):
        _require(self.verify_current is not _deny, 'Independent canonical current authority is not configured')
        connection = None
        try:
            frozen = deepcopy(proof)
            configuration = (self.database_path, deepcopy(self.store_identity),
                             self.database_schema_sha256, self.verify_current)
            path, identity, schema_checksum, provider = configuration
            deadline = time.monotonic() + 5

            def authority():
                _require(configuration == (self.database_path, self.store_identity,
                                            self.database_schema_sha256, self.verify_current),
                         'Canonical verifier configuration changed')
                _require(provider(deepcopy(frozen)) is None, 'Canonical authority must complete or raise')
                _require(time.monotonic() < deadline and configuration == (self.database_path, self.store_identity,
                         self.database_schema_sha256, self.verify_current), 'Canonical authority expired or changed')

            authority()
            _require(path.resolve(strict=True) == path, 'Canonical database path contains an alias or symlink')
            _require(identity == frozen['canonical']['store'], 'Canonical database differs from its independent identity')
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            store = SimpleNamespace(connection=connection)
            _require(sqlite_store_identity(store) == identity, 'Canonical database current identity differs')
            schema = [dict(row) for row in connection.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','trigger') "
                "AND name!='sqlite_sequence' ORDER BY name,type")]
            _require(digest(schema) == schema_checksum, 'Canonical database schema differs from independent review')
            tables = {row['name'] for row in schema if row['type'] == 'table'}
            _require(tables == {table for table, column in self._surfaces} | {'aos_no_admission_observations'},
                     'Canonical request surface inventory is incomplete or unknown')
            request_id = frozen['target']['request_id']

            def readback():
                for table, column in self._surfaces:
                    _require(connection.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1',
                                                (request_id,)).fetchone() is None,
                             'Original canonical request has admission/allocation or deferred evidence')
                row = connection.execute('SELECT * FROM aos_no_admission_observations WHERE request_id=?',
                                         (request_id,)).fetchone()
                _require(row is not None, 'Original committed canonical tombstone is absent')
                value = dict(row)
                dispatch = _decode(value['dispatch_provenance_json'].encode('utf-8'), FRAME_LIMIT)
                expected = {'request_id': request_id, 'request_sha256': frozen['target']['request_sha256'],
                    'original_caller_generation_sha256': frozen['target']['original_caller_generation_sha256'],
                    'admission_binding_sha256': frozen['original']['admission_binding_sha256'],
                    'proof_sha256': digest({'observer':frozen['observer'],'cleanup_scope':frozen['cleanup_scope'],
                        'original':frozen['original'],'physical':frozen['physical'],'dispatch_provenance':dispatch}),
                    'observation_sha256': frozen['observation_sha256'], 'observation_json': canonical(frozen)}
                _require(set(value) == set(expected) | {'dispatch_provenance_json'}
                         and all(value[key] == item for key, item in expected.items()),
                         'Canonical committed original observation differs')
                _require(dispatch == {'target': frozen['target'],
                    'source_fingerprints': frozen['original']['admission_record']['admission_binding']['source_fingerprints'],
                    'caller_generation_sha256': frozen['target']['original_caller_generation_sha256'],
                    'broker_generation_sha256': frozen['original']['broker_generation_sha256'],
                    'provenance_complete': True, 'deferred_execution': [], 'quarantined_allocations': []},
                    'Canonical dispatch provenance differs or is uncertain')
                return value

            before = readback()
            connection.commit()
            authority()
            connection.execute('BEGIN')
            current_schema = [dict(row) for row in connection.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE type IN ('table','trigger') "
                "AND name!='sqlite_sequence' ORDER BY name,type")]
            _require(current_schema == schema, 'Canonical schema changed between independent observations')
            _require(readback() == before and sqlite_store_identity(store) == identity
                     and time.monotonic() < deadline, 'Canonical readback changed or expired')
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError) as error:
            raise ScientistAdmissionError('Independent canonical observation readback was denied') from error
        finally:
            if connection is not None:
                connection.close()


class ScientistNoAdmissionObserverVerifier:
    """Authenticate a live observer against independently reviewed config bytes.

    verify_authority is a mandatory host authorization/source-review gate, not
    approval inferred from a private file, unit name or supplied capability.
    It receives a deep copy of the observation and returns None or raises.
    """

    def __init__(self, *, config_path, config_raw_sha256, verify_authority=_deny):
        self.config_path = Path(config_path)
        _require(self.config_path.is_absolute() and type(config_raw_sha256) is str
                 and re.fullmatch('[a-f0-9]{64}', config_raw_sha256),
                 'Observer requires independent configuration path and raw pin')
        _require(callable(verify_authority), 'Observer requires a trusted host authorization gate')
        self.config_raw_sha256, self.verify_authority = config_raw_sha256, verify_authority

    @staticmethod
    def _generation(generation, deadline):
        _require(generation.uid == os.getuid() and generation.pid > 1
                 and generation.unit == 'swapp-scientist-no-admission-observer.service'
                 and generation.control_group.endswith('/' + generation.unit),
                 'Observer UID or fixed unit scope differs')
        before = _caller_process_identity(generation.pid)
        _require(Path(f'/proc/{generation.pid}').stat().st_uid == generation.uid,
                 'Observer process UID differs')
        environment = ScientistPhysicalReleaseVerifier._environment()
        output = ScientistPhysicalReleaseVerifier._command(
            ['/usr/bin/systemctl', '--user', 'show', generation.unit,
             '--property=Id,LoadState,ActiveState,MainPID,InvocationID,ControlGroup', '--no-pager'],
            environment, deadline)
        rows = [line.split('=', 1) for line in output.splitlines()]
        _require(all(len(row) == 2 for row in rows), 'Observer unit properties are malformed')
        properties = dict(rows)
        _require(len(rows) == len(properties) and properties == {'Id':generation.unit,'LoadState':'loaded',
                 'ActiveState':'active','MainPID':str(generation.pid),'InvocationID':generation.invocation_id,
                 'ControlGroup':generation.control_group}
                 and before == (generation.start_ticks,generation.boot_id,generation.control_group)
                 and _caller_process_identity(generation.pid) == before
                 and Path(f'/proc/{generation.pid}').stat().st_uid == generation.uid,
                 'Observer live unit/process generation differs or changed')

    def __call__(self, proof):
        _require(self.verify_authority is not _deny, 'Independent observer authorization is not configured')
        try:
            frozen = deepcopy(proof)
            configuration = (self.config_path,self.config_raw_sha256,self.verify_authority)
            path, checksum, provider = configuration
            deadline = time.monotonic() + 10

            def authority():
                _require(configuration == (self.config_path,self.config_raw_sha256,self.verify_authority)
                         and provider(deepcopy(frozen)) is None
                         and configuration == (self.config_path,self.config_raw_sha256,self.verify_authority)
                         and time.monotonic() < deadline, 'Observer host authority changed, expired or denied')

            authority()
            raw, identity = ScientistNoAdmissionRecoveryVerifier._read(path)
            _require(hashlib.sha256(raw).hexdigest() == checksum == frozen['observer']['config_sha256'],
                     'Observer configuration differs from the independent raw pin')
            config = _decode(raw, FRAME_LIMIT, canonical_required=False)
            observer = frozen['observer']
            _require(config['schema'] == 'aos-scientist-no-admission-provider.v1'
                     and type(config['version']) is int and config['version'] == 1
                     and config['target'] == frozen['target'] and config['cleanup_scope'] == frozen['cleanup_scope']
                     and config['observation_schema_sha256'] == SCHEMA_SHA256
                     and config['observer']['unit'] == observer['generation']['unit']
                     and config['observer']['source_sha256'] == observer['source_sha256'] == digest(config['sources']),
                     'Observer reviewed configuration scope or source manifest differs')
            sources = config['sources']
            _require(type(sources) is list and 1 <= len(sources) <= 256, 'Observer source inventory exceeds its bound')

            def sources_current():
                paths = set()
                remaining = TOTAL_LIMIT
                for pin in sources:
                    _require(type(pin) is dict and set(pin) == {'path','sha256'} and type(pin['path']) is str
                             and type(pin['sha256']) is str and re.fullmatch('[a-f0-9]{64}',pin['sha256']),
                             'Observer reviewed source pin is malformed')
                    source_path = Path(pin['path'])
                    _require(source_path.is_absolute() and '..' not in source_path.parts and source_path not in paths,
                             'Observer reviewed source path is ambiguous or duplicated')
                    paths.add(source_path)
                    content = _read_source(source_path, FILE_LIMIT, deadline, remaining)
                    _require(hashlib.sha256(content).hexdigest() == pin['sha256'], 'Observer actual source differs from review')
                    remaining -= len(content)

            sources_current()
            generation = ScientistServerGeneration.model_validate(observer['generation'], strict=True)
            _require(observer['generation_sha256'] == digest(generation.model_dump(mode='json')),
                     'Observer generation digest differs')
            self._generation(generation, deadline)
            authority()
            self._generation(generation, deadline)
            sources_current()
            _require(ScientistNoAdmissionRecoveryVerifier._read(path) == (raw,identity)
                     and time.monotonic() < deadline, 'Observer configuration changed or observation expired')
        except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError) as error:
            raise ScientistAdmissionError('Independent live observer/source verification was denied') from error


class ScientistNoAdmissionVerifier:
    def __init__(self, history, *, reviewed_schema_bytes, schema_sha256,
                 verify_observer=_deny, verify_canonical=_deny, verify_physical=_deny,
                 verify_recovery=_deny, clock=None, boot_id=None):
        _require(isinstance(history, ScientistAdmissionHistory) and history.record_version == '2.0',
                 'Observation requires the original admission2.0 history')
        schema = _decode(reviewed_schema_bytes, FRAME_LIMIT, canonical_required=False)
        _require(schema_sha256 == SCHEMA_SHA256 == digest(schema), 'Observation schema version/pin differs')
        _schema_boundary(schema, schema['$defs'])
        _STRICT_VALIDATOR.check_schema(schema)
        self.validator = _STRICT_VALIDATOR(schema)
        self.history, self.store = history, history.store
        self.schema_sha256 = schema_sha256
        self.callbacks = (verify_observer, verify_canonical, verify_physical, verify_recovery)
        _require(all(callable(callback) for callback in self.callbacks), 'Observation providers must be trusted callables')
        self.clock = clock if clock is not None else lambda: int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000000)
        self.boot_id = boot_id if boot_id is not None else lambda: Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def _decode(self, raw):
        value = _decode(raw, FRAME_LIMIT)
        ScientistEvidenceCodec._validate(self.validator, value)
        _require(value['schema'] == SCHEMA and type(value['version']) is int and value['version'] == 1,
                 'Observation version differs')
        _require(value['observation_sha256'] == digest({key: item for key, item in value.items()
                                                      if key != 'observation_sha256'}), 'Observation digest differs')
        return value

    def _original(self, value):
        target, supplied = value['target'], value['original']
        row = self.store.connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                            (target['request_id'],)).fetchone()
        _require(row is not None and row['state'] == 'pending' and row['receipt_json'] is None,
                 'Observation target is not an original unresolved request without result')
        intent = dict(row)
        request = ScientistTurnRequest.model_validate_json(row['request_json'], strict=True)
        binding = ScientistIntentBinding.model_validate_json(row['binding_json'], strict=True)
        record, checksum = self.history.read(request.request_id)
        captured = record.model_dump(mode='json')
        original = record.admission_binding
        _require(supplied['request_canonical'] == row['request_json'] == scientist_request_frame(request)[:-1].decode('utf-8')
                 and target['request_sha256'] == row['request_sha256']
                 and target['original_caller_generation_sha256'] == digest(original.caller_generation.model_dump(mode='json'))
                 and supplied['admission_record'] == captured
                 and supplied['admission_record_sha256'] == checksum == digest(captured)
                 and supplied['admission_binding_sha256'] == record.admission_binding_sha256
                 and supplied['intent_binding'] == binding.model_dump(mode='json')
                 and supplied['intent_binding_sha256'] == record.intent_binding_sha256
                 and supplied['broker_generation'] == original.server_generation.model_dump(mode='json')
                 and supplied['broker_generation_sha256'] == digest(supplied['broker_generation']),
                 'Observation differs from independently retained original request/capture')
        capture = {key: captured[key] for key in ('admission_binding', 'capability_sha256', 'capability_freshness')}
        _require(supplied['admission_capture_sha256'] == digest(capture), 'Observation capture digest differs')
        scope = value['cleanup_scope']
        _require(scope['session_id'] == binding.session_id and scope['runtime_id'] == binding.runtime_id
                 and scope['original_generation'] == binding.generation
                 and scope['current_generation'] > binding.generation
                 and scope['store'] == supplied['store'] == sqlite_store_identity(self.store),
                 'Observation cleanup scope differs from the original store/runtime')
        return intent

    def _links(self, value):
        target, original, observer = value['target'], value['original'], value['observer']
        canonical_proof, physical = value['canonical'], value['physical']
        tombstone = canonical_proof['tombstone']
        generation = ScientistServerGeneration.model_validate(observer['generation'], strict=True)
        capability = observer['capability']
        _require(observer['generation_sha256'] == digest(generation.model_dump(mode='json'))
                 and observer['generation_sha256'] not in (target['original_caller_generation_sha256'], original['broker_generation_sha256'])
                 and observer['capability_sha256'] == digest(capability)
                 and capability['schema_sha256'] == self.schema_sha256
                 and all(capability[key] == observer[key] for key in ('generation_sha256', 'source_sha256', 'config_sha256')),
                 'Observation current observer capability/generation differs')
        _require(target == canonical_proof['target'] == physical['target'] == tombstone['target']
                 and physical['caller_generation'] == original['admission_record']['admission_binding']['caller_generation']
                 and physical['caller_generation_sha256'] == target['original_caller_generation_sha256']
                 and physical['broker_generation'] == original['broker_generation']
                 and physical['broker_generation_sha256'] == original['broker_generation_sha256'],
                 'Observation physical/canonical target differs')
        _require(physical['proof_sha256'] == digest({key: item for key, item in physical.items() if key != 'proof_sha256'})
                 and canonical_proof['tombstone_sha256'] == digest(tombstone)
                 and tombstone['store'] == canonical_proof['store']
                 and tombstone['admission_record_sha256'] == original['admission_record_sha256']
                 and tombstone['intent_binding_sha256'] == original['intent_binding_sha256']
                 and tombstone['observer_generation_sha256'] == observer['generation_sha256']
                 and tombstone['physical_proof_sha256'] == physical['proof_sha256']
                 and tombstone['freshness'] == value['freshness'], 'Observation tombstone/preimage links differ')
        children = physical['children']
        checksums = [digest(child['generation']) for child in children]
        _require(len(set(checksums)) == len(checksums), 'Observation child provenance is duplicated')
        for child, checksum in zip(children, checksums):
            child_generation = ScientistServerGeneration.model_validate(child['generation'], strict=True)
            _require(child['generation_sha256'] == checksum
                     and child_generation.boot_id == original['broker_generation']['boot_id'],
                     'Observation child provenance differs')

    def _current(self, value):
        scope = value['cleanup_scope']
        row = self.store.connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?',
                                            (scope['session_id'],)).fetchone()
        _require(row is not None and row['runtime_id'] == scope['runtime_id']
                 and row['status'] == scope['current_status'] and row['owner'] == scope['current_owner']
                 and row['generation'] == scope['current_generation'] and row['lease_id'] == scope['lease_id']
                 and sqlite_store_identity(self.store) == scope['store'],
                 'Observation cleanup authority is not the current stopped original session')
        freshness = value['freshness']
        observed, expires = freshness['observed_boottime_us'], freshness['expires_boottime_us']
        current = self.clock()
        _require(type(current) is int and 0 <= current <= 2**53 - 1
                 and freshness['boot_id'] == self.boot_id() == value['observer']['generation']['boot_id']
                 and observed <= current < expires and 0 < expires - observed <= 60000000,
                 'Observation freshness/current boot expired or differs')

    def verify(self, raw):
        _require(all(callback is not _deny for callback in self.callbacks), 'Observation proof/recovery authority is not configured')
        try:
            transaction = self.store.connection.in_transaction
            value = self._decode(raw)
            original_intent = self._original(value)
            self._links(value)
            self._current(value)
            for callback in self.callbacks:
                self.store.connection.set_authorizer(_readonly_authorizer)
                try:
                    _require(callback(deepcopy(value)) is None, 'Independent observation verifier must complete or raise')
                finally:
                    self.store.connection.set_authorizer(None)
                _require(self.store.connection.in_transaction is transaction, 'Observation callback changed transaction ownership')
                self._current(value)
                _require(self._original(value) == original_intent, 'Observation callback changed immutable original intent')
            return value
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
            raise ScientistAdmissionError('Observation readback or proof was denied') from error


class ScientistNoAdmissionJournal:
    def __init__(self, verifier):
        _require(isinstance(verifier, ScientistNoAdmissionVerifier), 'Closure requires its explicit observation verifier')
        self.verifier, self.store = verifier, verifier.store

    def append(self, raw):
        connection = self.store.connection
        _require(not connection.in_transaction, 'Closure must own its original SQL transaction')
        connection.execute('BEGIN IMMEDIATE')
        try:
            value = self.verifier.verify(raw)
            request_id = value['target']['request_id']
            intent = self.verifier._original(value)
            existing = connection.execute('SELECT * FROM scientist_no_admission_closures WHERE request_id=?', (request_id,)).fetchone()
            proposed = {'request_id': request_id, 'session_id': value['cleanup_scope']['session_id'],
                        'request_sha256': value['target']['request_sha256'],
                        'admission_record_sha256': value['original']['admission_record_sha256'],
                        'intent_snapshot_json': canonical(intent), 'observation_json': raw.decode('utf-8'),
                        'observation_sha256': value['observation_sha256'], 'schema_sha256': self.verifier.schema_sha256,
                        'recovery_scope_json': canonical(value['cleanup_scope']),
                        'created_at': existing['created_at'] if existing is not None else now()}
            if existing is not None:
                _require(dict(existing) == proposed, 'Closure retry differs from immutable original observation')
            else:
                connection.execute('INSERT INTO scientist_no_admission_closures VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(proposed.values()))
            _require(self.verifier.verify(raw) == value and self.verifier._original(value) == intent
                     and connection.in_transaction, 'Closure lost original proof, intent or transaction')
            result = dict(connection.execute('SELECT * FROM scientist_no_admission_closures WHERE request_id=?', (request_id,)).fetchone())
            _require(result == proposed, 'Closure durable readback differs')
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise


class ScientistNoAdmissionRetainedVerifier:
    def __init__(self, history, *, reviewed_schema_bytes, schema_sha256,
                 reviewed_recovery_schema_bytes, recovery_schema_sha256, expected_retained,
                 verify_current=_deny, verify_canonical=_deny, verify_physical=_deny,
                 verify_retained_source=_deny, clock=None, boot_id=None):
        self.original = ScientistNoAdmissionVerifier(history, reviewed_schema_bytes=reviewed_schema_bytes,
            schema_sha256=schema_sha256, clock=clock, boot_id=boot_id)
        schema = _decode(reviewed_recovery_schema_bytes, FRAME_LIMIT, canonical_required=False)
        _require(recovery_schema_sha256 == RECOVERY_SCHEMA_SHA256 == digest(schema),
                 'Retained recovery schema pin differs')
        _schema_boundary(schema, schema.get('$defs', {}))
        _STRICT_VALIDATOR.check_schema(schema)
        self.validator = _STRICT_VALIDATOR(schema)
        self.store = self.original.store
        self.recovery_schema_sha256 = recovery_schema_sha256
        self.expected_retained = deepcopy(expected_retained)
        _require(type(self.expected_retained) is dict, 'Retained recovery requires independent original pins')
        self.callbacks = (verify_current, verify_canonical, verify_physical, verify_retained_source)
        _require(all(callable(callback) for callback in self.callbacks), 'Retained recovery requires trusted providers')

    def _context(self, raw, value, observation_raw):
        context = _decode(raw, FRAME_LIMIT)
        ScientistEvidenceCodec._validate(self.validator, context)
        _require(context['schema'] == 'aos-scientist-no-admission-observation-recovery.v1'
                 and type(context['version']) is int and context['version'] == 1
                 and context['purpose'] == 'close_retained_no_admission'
                 and context['target'] == value['target'] and context['cleanup_scope'] == value['cleanup_scope']
                 and context['recovery_request_id'] != value['target']['request_id']
                 and all(context[key] is False for key in
                         ('inference_allowed','gpu_release_allowed','observation_reissue_allowed')),
                 'Retained recovery purpose, permissions or original scope differs')
        retained, observer = context['retained'], value['observer']
        expected = {'observation_raw_sha256': hashlib.sha256(observation_raw).hexdigest(),
                    'observation_sha256': value['observation_sha256'], 'observation_schema_sha256': SCHEMA_SHA256,
                    'canonical_store': value['canonical']['store'],
                    'canonical_schema_sha256': self.expected_retained.get('canonical_schema_sha256'),
                    'cleanup_scope_sha256': digest(value['cleanup_scope']),
                    'observer_generation_sha256': observer['generation_sha256'],
                    'producer_source_sha256': observer['source_sha256'],
                    'producer_config_sha256': observer['config_sha256']}
        _require(retained == expected == self.expected_retained,
                 'Retained recovery differs from independently pinned historical provenance')
        principal = context['principal']
        generation = ScientistServerGeneration.model_validate(principal['generation'], strict=True)
        _require(principal['generation_sha256'] == digest(generation.model_dump(mode='json'))
                 and principal['generation_sha256'] not in
                 (observer['generation_sha256'], value['target']['original_caller_generation_sha256'],
                  value['original']['broker_generation_sha256']),
                 'Retained recovery principal is stale or differs')
        identity_keys = ('boot_id','uid','pid','start_ticks')
        identity = tuple(principal['generation'][key] for key in identity_keys)
        _require(all(identity != tuple(previous[key] for key in identity_keys) for previous in
                 (observer['generation'], value['original']['broker_generation'],
                  value['original']['admission_record']['admission_binding']['caller_generation'])),
                 'Retained recovery reuses an original process identity')
        for freshness, issued_key in ((value['freshness'],'observed_boottime_us'),
                                     (context['freshness'],'issued_boottime_us')):
            _require(freshness['clock'] == 'CLOCK_BOOTTIME' and freshness['unit'] == 'microseconds'
                     and freshness['max_age_us'] == 60000000
                     and type(freshness[issued_key]) is int and type(freshness['expires_boottime_us']) is int
                     and 0 <= freshness[issued_key] < freshness['expires_boottime_us'] <= 2**53-1
                     and freshness['expires_boottime_us'] - freshness[issued_key] <= 60000000,
                     'Retained recovery or historical freshness structure differs')
        _require(context['freshness']['boot_id'] == generation.boot_id == value['freshness']['boot_id']
                 == observer['generation']['boot_id'] == value['original']['broker_generation']['boot_id']
                 and context['freshness']['issued_boottime_us'] >= value['freshness']['observed_boottime_us'],
                 'Retained recovery boot differs from the original proof')
        return context

    def _current(self, value, context):
        scope = context['cleanup_scope']
        row = self.store.connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?',
                                            (scope['session_id'],)).fetchone()
        _require(row is not None and row['runtime_id'] == scope['runtime_id']
                 and row['status'] == scope['current_status'] == 'stopped'
                 and row['owner'] == scope['current_owner'] == 'PAUSED'
                 and row['generation'] == scope['current_generation'] and row['lease_id'] == scope['lease_id']
                 and sqlite_store_identity(self.store) == scope['store'],
                 'Retained recovery is not the same stopped original scope')
        freshness, current = context['freshness'], self.original.clock()
        _require(type(current) is int and 0 <= current <= 2**53-1
                 and freshness['boot_id'] == self.original.boot_id()
                 and freshness['issued_boottime_us'] <= current < freshness['expires_boottime_us'],
                 'Current retained recovery authority expired or differs')

    def verify(self, raw, recovery_raw):
        _require(all(callback is not _deny for callback in self.callbacks),
                 'Current retained recovery authority is not configured')
        try:
            transaction = self.store.connection.in_transaction
            configuration = (self.callbacks,canonical(self.expected_retained),self.original.clock,self.original.boot_id)
            value = self.original._decode(raw)
            intent = self.original._original(value)
            self.original._links(value)
            context = self._context(recovery_raw, value, raw)
            self._current(value, context)
            for callback in self.callbacks:
                self.store.connection.set_authorizer(_readonly_authorizer)
                try:
                    _require(callback(deepcopy(value),deepcopy(context)) is None,
                             'Independent retained recovery provider must complete or raise')
                finally:
                    self.store.connection.set_authorizer(None)
                _require(configuration == (self.callbacks,canonical(self.expected_retained),
                         self.original.clock,self.original.boot_id)
                         and self.store.connection.in_transaction is transaction,
                         'Retained recovery provider changed configuration or transaction ownership')
                self._current(value, context)
                _require(self.original._original(value) == intent, 'Retained recovery changed original intent')
            return value, context
        except (ValueError,TypeError,KeyError,OSError,sqlite3.Error) as error:
            raise ScientistAdmissionError('Retained recovery verification was denied') from error


class ScientistNoAdmissionRetainedJournal:
    def __init__(self, verifier):
        _require(isinstance(verifier, ScientistNoAdmissionRetainedVerifier),
                 'Retained closure requires its dedicated verifier')
        self.verifier, self.store = verifier, verifier.store

    def append(self, raw, recovery_raw):
        connection = self.store.connection
        _require(not connection.in_transaction, 'Retained closure must own its SQL transaction')
        connection.execute('BEGIN IMMEDIATE')
        try:
            value, context = self.verifier.verify(raw, recovery_raw)
            request_id = value['target']['request_id']
            intent = self.verifier.original._original(value)
            existing = connection.execute('SELECT * FROM scientist_no_admission_closures WHERE request_id=?',
                                           (request_id,)).fetchone()
            proposed = {'request_id': request_id, 'session_id': value['cleanup_scope']['session_id'],
                        'request_sha256': value['target']['request_sha256'],
                        'admission_record_sha256': value['original']['admission_record_sha256'],
                        'intent_snapshot_json': canonical(intent), 'observation_json': raw.decode('utf-8'),
                        'observation_sha256': value['observation_sha256'], 'schema_sha256': SCHEMA_SHA256,
                        'recovery_scope_json': canonical(value['cleanup_scope'] | {'retained_recovery':context}),
                        'created_at': existing['created_at'] if existing is not None else now()}
            if existing is None:
                connection.execute('INSERT INTO scientist_no_admission_closures VALUES(?,?,?,?,?,?,?,?,?,?)',
                                   tuple(proposed.values()))
            else:
                _require(dict(existing) == proposed, 'Retained closure retry conflicts with immutable recovery context')
            _require(self.verifier.verify(raw, recovery_raw) == (value,context)
                     and self.verifier.original._original(value) == intent and connection.in_transaction,
                     'Retained closure lost context, original intent or transaction')
            result = dict(connection.execute('SELECT * FROM scientist_no_admission_closures WHERE request_id=?',
                                             (request_id,)).fetchone())
            _require(result == proposed, 'Retained closure SQL readback differs')
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

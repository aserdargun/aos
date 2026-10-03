"""Read-only configured source checks, not complete runtime/dependency attestation."""

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from types import MappingProxyType

from .scientist_admission_history import ScientistAdmissionBindingV2, ScientistProfilePinV2
from .scientist_protocol import _reject_constant, _unique_object
from .scientist_terminal import canonical, digest
from .scientist_transport import ScientistAdmissionError


SOURCE_MINIMUM = MappingProxyType({
    'aos': frozenset({'scripts/serve_desktop.py', 'src/aos/scientist_source_authority.py',
        'src/aos/scientist_bootstrap_factory.py', 'src/aos/scientist_bootstrap.py',
        'src/aos/scientist_async.py', 'src/aos/scientist_desktop.py',
        'src/aos/scientist_retained_provider.py', 'src/aos/scientist_admission_history.py',
        'src/aos/scientist_retained_host.py', 'src/aos/scientist_release_proof.py', 'services/broker_runtime.py',
        'src/aos/scientist_transport.py', 'src/aos/scientist_protocol.py',
        'src/aos/scientist_intents.py', 'services/decider/broker_worker.py', 'services/bonsai/broker_worker.py'}),
    'scientist': frozenset({'lab/llm/native_runtime.py', 'lab/llm/aos_retained_provider.py',
        'lab/llm/aos_physical_readback.py', 'lab/llm/aos_gpu_control.py',
        'lab/llm/aos_gpu_control_store.py', 'lab/llm/aos_gpu_broker.py',
        'lab/llm/aos_gpu_service.py', 'lab/llm/aos_gpu_executor.py', 'lab/llm/gpu_scheduler.py'}),
})
FILE_LIMIT = 8 * 1024 * 1024
TOTAL_LIMIT = 32 * 1024 * 1024
READ_SECONDS = 5
PROFILES = frozenset({'aos.decider.turn.v1', 'aos.bonsai.recovery.v1', 'aos.bonsai.vision.v1'})


def _require(condition, message):
    if not condition:
        raise ScientistAdmissionError(message)


def _deny_runtime(selected_bindings):
    raise ScientistAdmissionError('Independent configured runtime authority is not configured')


def _absolute(path):
    _require(isinstance(path, Path) and path.is_absolute() and '..' not in path.parts,
             'Source authority requires explicit absolute non-traversing paths')
    return Path(str(path))


def _hash(value):
    return type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _read(path, limit, deadline, remaining, *, private=False):
    def current():
        _require(time.monotonic() < deadline, 'Configured source read deadline expired')
    current()
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            current()
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                     and 0 <= before.st_size <= min(limit, remaining), 'Configured source file type or byte bound differs')
            _require(not private or before.st_uid == os.getuid() and not stat.S_IMODE(before.st_mode) & 0o077,
                     'Scientist policy must remain private and user owned')
            content = bytearray()
            while True:
                current()
                block = os.read(descriptor, min(65536, limit + 1 - len(content)))
                if not block:
                    break
                content.extend(block)
                _require(len(content) <= min(limit, remaining), 'Configured source read exceeded byte bound')
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            _require(len(content) == before.st_size and all(getattr(before, field) == getattr(value, field)
                     for value in (after, linked) for field in fields), 'Configured source changed during descriptor read')
            current()
            return bytes(content)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


class ScientistConfiguredSourceVerifier:
    """Callable source/config half of the explicit bootstrap factory gate.

    Expected bytes/hashes never come from ACKs or observed files. verify_runtime
    must independently validate configured profiles/artifacts, dependency closure,
    current caller/service/broker generations, schemas and output contract against
    the supplied full bindings. The minimum paths below are not full closure.
    No profile loader, policy enablement or runtime authority is manufactured.
    Reads have cooperative elapsed/byte limits; blocked filesystem I/O or a
    trusted callback cannot be forcibly interrupted by this synchronous reader.
    """

    def __init__(self, policy_path, source_roots, reviewed_bindings, source_files, *, config_files,
                 verify_runtime=_deny_runtime):
        self._policy = _absolute(policy_path)
        _require(type(source_roots) is dict and set(source_roots) == {'aos', 'scientist'}
                 and type(source_files) is dict and set(source_files) == set(source_roots),
                 'Configured source roots and complete explicit maps are required')
        self._roots = MappingProxyType({name: _absolute(path) for name, path in source_roots.items()})
        sources = {}
        for group, entries in source_files.items():
            _require(type(entries) is dict and SOURCE_MINIMUM[group] <= set(entries) and len(entries) <= 256,
                     'Configured source map omits a required reviewed producer')
            for relative, fingerprint in entries.items():
                _require(type(relative) is str and relative and len(relative) <= 512
                         and not PurePosixPath(relative).is_absolute() and '..' not in PurePosixPath(relative).parts
                         and PurePosixPath(relative).as_posix() == relative and '\x00' not in relative
                         and _hash(fingerprint), 'Configured source relative path or hash differs')
            sources[group] = dict(entries)
        self._sources = canonical(sources)
        _require(type(config_files) is dict and 1 <= len(config_files) <= 64
                 and all(_hash(value) for value in config_files.values()),
                 'At least one independently pinned model/profile configuration file is required')
        self._configs = MappingProxyType({_absolute(path): fingerprint for path, fingerprint in config_files.items()})
        _require(self._policy not in self._configs, 'Policy alone cannot stand in for model/profile configuration')
        _require(type(reviewed_bindings) is dict and reviewed_bindings and set(reviewed_bindings) <= PROFILES
                 and callable(verify_runtime), 'Full reviewed bindings and an explicit runtime gate are required')
        reviewed = {}
        shared = None
        for profile, value in reviewed_bindings.items():
            value = value.model_dump(mode='json') if isinstance(value, ScientistAdmissionBindingV2) else deepcopy(value)
            binding = ScientistAdmissionBindingV2.model_validate(value, strict=True)
            _require(binding.profile_id == profile and binding.source_fingerprints.model_dump(mode='json')
                     == {group: digest(entries) for group, entries in sources.items()},
                     'Reviewed source fingerprint differs from the independently supplied maps')
            identity = (binding.server_generation, binding.caller_generation, binding.policy_sha256,
                        binding.source_fingerprints)
            _require(shared is None or shared == identity, 'Reviewed profiles do not share one policy and original broker/caller generation')
            shared = identity
            reviewed[profile] = canonical(binding.model_dump(mode='json'))
        self._reviewed = MappingProxyType(reviewed)
        self._runtime = verify_runtime

    def _policy_matches(self, raw, reviewed, sources):
        policy = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        required = {'schema', 'enabled', 'caller_unit', 'control_socket', 'history_reconcile',
                    'source_files', 'profile_pins', 'infer_schema_sha256', 'control_schema_sha256'}
        optional = {'cleanup_targets', 'evidence_schema_sha256', 'evidence_transport_schema_sha256',
                    'retained_evidence_transport_schema_sha256'}
        _require(type(policy) is dict and required <= set(policy) <= required | optional
                 and policy['schema'] == 'swapp-aos-control-policy.v1' and policy['enabled'] is True
                 and policy['history_reconcile'] is False and type(policy['control_socket']) is str
                 and type(policy['caller_unit']) is str
                 and re.fullmatch(r'swapp-aos-[a-z0-9-]+\.service', policy['caller_unit']) is not None
                 and policy['source_files'] == sources and type(policy['profile_pins']) is dict
                 and set(policy['profile_pins']) == PROFILES,
                 'Configured Scientist policy or source map differs')
        evidence = {'evidence_schema_sha256', 'evidence_transport_schema_sha256'}
        _require(not set(policy) & evidence or evidence <= set(policy), 'Configured evidence policy pins must be paired')
        _require('retained_evidence_transport_schema_sha256' not in policy or evidence <= set(policy),
                 'Configured retained evidence policy requires the original evidence pin pair')
        fingerprint = digest(policy)
        for profile, binding in reviewed.items():
            _require(fingerprint == binding.policy_sha256 and policy['caller_unit'] == binding.caller_generation.unit
                     and policy['profile_pins'][profile] == binding.profile_pin.model_dump(mode='json')
                     and policy['infer_schema_sha256'] == binding.infer_schema.sha256
                     and policy['control_schema_sha256'] == binding.control_schema.sha256,
                     'Configured policy does not match the complete independently reviewed pins')
        for value in policy['profile_pins'].values():
            ScientistProfilePinV2.model_validate(value, strict=True)

    def __call__(self, selected_bindings, *, deadline=None):
        _require(deadline is None or (type(deadline) in (int, float)
                 and (type(deadline) is int or math.isfinite(deadline))),
                 'Configured source outer deadline must be finite')
        current = time.monotonic()
        deadline = min(current + READ_SECONDS, deadline) if deadline is not None else current + READ_SECONDS
        _require(time.monotonic() < deadline, 'Configured source verification deadline expired')
        _require(self._runtime is not _deny_runtime and type(selected_bindings) is dict and selected_bindings
                 and set(selected_bindings) <= set(self._reviewed), 'Configured runtime gate or reviewed profile selection is missing')
        reviewed = {profile: ScientistAdmissionBindingV2.model_validate_json(raw, strict=True)
                    for profile, raw in self._reviewed.items()}
        _require(all(isinstance(value, ScientistAdmissionBindingV2) and value == reviewed[profile]
                     for profile, value in selected_bindings.items()), 'Selected source binding is not the independently reviewed full binding')
        selected = {profile: reviewed[profile].model_copy(deep=True) for profile in selected_bindings}
        consumed = 0
        sources = json.loads(self._sources)
        try:
            _require(self._runtime(deepcopy(selected)) is None, 'Configured runtime verifier must complete or raise')
            def sweep():
                nonlocal consumed
                policy = _read(self._policy, 65536, deadline, TOTAL_LIMIT - consumed, private=True)
                consumed += len(policy)
                self._policy_matches(policy, reviewed, sources)
                for group, entries in sources.items():
                    for relative, expected in entries.items():
                        raw = _read(self._roots[group] / relative, FILE_LIMIT, deadline, TOTAL_LIMIT - consumed)
                        consumed += len(raw)
                        _require(hashlib.sha256(raw).hexdigest() == expected, 'Configured source bytes differ from the reviewed hash')
                for path, expected in self._configs.items():
                    raw = _read(path, FILE_LIMIT, deadline, TOTAL_LIMIT - consumed)
                    consumed += len(raw)
                    _require(hashlib.sha256(raw).hexdigest() == expected, 'Configured model/profile file differs from its reviewed hash')
            sweep()
            _require(self._runtime(deepcopy(selected)) is None, 'Configured runtime verifier must complete or raise')
            sweep()
            _require(self._runtime(deepcopy(selected)) is None, 'Configured runtime verifier must complete or raise')
            policy = _read(self._policy, 65536, deadline, TOTAL_LIMIT - consumed, private=True)
            consumed += len(policy)
            self._policy_matches(policy, reviewed, sources)
            _require(time.monotonic() < deadline, 'Configured source verification deadline expired')
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError) as error:
            raise ScientistAdmissionError('Configured source or policy readback was denied') from error

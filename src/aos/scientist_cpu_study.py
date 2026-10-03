import asyncio
from copy import copy
from datetime import datetime, timedelta
import hashlib
import http.client
import json
import threading
import time

from jsonschema import Draft202012Validator, ValidationError, validators

from .contracts import REPO_ROOT
from .scientist_cpu_capability import ScientistCpuCapability, ScientistCpuCapabilityVerifier, cpu_capability_sha256
from .scientist_lab import _object
from .scientist_transport import ScientistAdmissionError


CPU_STUDY_SCHEMA_SHA256 = '263f588369cdc78f1f904422c45505f097a36fdb495abbd68320eb45825bcefa'
STUDY_BOUND = 131072


def validate_cpu_study(raw, grant):
    if type(raw) is not bytes or len(raw) > STUDY_BOUND:
        raise ValueError('CPU study response exceeds its bound')
    schema_bytes = (REPO_ROOT / 'schemas/scientist_cpu_study_wire.schema.json').read_bytes()
    if hashlib.sha256(schema_bytes).hexdigest() != CPU_STUDY_SCHEMA_SHA256:
        raise ValueError('CPU study wire schema differs from reviewed source')
    schema = json.loads(schema_bytes)
    strict_types = Draft202012Validator.TYPE_CHECKER.redefine('integer',
        lambda checker, value: type(value) is int)
    validator = validators.extend(Draft202012Validator, type_checker=strict_types)(schema)
    value = _object(raw)
    validator.validate(value)
    capability = ScientistCpuCapability.model_validate(value['capability'], strict=True)
    if (capability != grant.capability
            or cpu_capability_sha256(value['capability']) != grant.capability_sha256):
        raise ValueError('CPU study capability differs from reviewed grant')
    snapshot, grid = value['snapshot'], value['grid']
    if (snapshot['entity_authority'] is not False
            or snapshot['snapshot_sha256'] != capability.snapshot_sha256
            or snapshot['rows'] != snapshot['train_rows'] + snapshot['evaluation_input_rows']
            or len(set(snapshot['sensors'])) != len(snapshot['sensors'])):
        raise ValueError('CPU study snapshot summary is inconsistent')
    timestamps = [datetime.fromisoformat(snapshot[field]) for field in ('first_utc', 'last_utc')]
    if any(timestamp.utcoffset() != timedelta(0) for timestamp in timestamps) or timestamps[0] > timestamps[1]:
        raise ValueError('CPU study timestamps must be ordered UTC values')
    configurations = grid['configurations']
    if (len(configurations) != grid['available_prefix_count']
            or len(configurations) != min(grid['registered_configuration_count'], capability.max_experiments)):
        raise ValueError('CPU study prefix counts differ from reviewed limits')
    for position, descriptor in enumerate(configurations, 1):
        configuration = descriptor['configuration']
        if (descriptor['position'] != position or descriptor['method'] != configuration.get('method')
                or set(configuration) != set(schema['$defs']['ModeConfig']['properties'])
                or configuration['lsh_merge_tables'] > configuration['lsh_tables']
                or cpu_capability_sha256(configuration) != descriptor['configuration_sha256']):
            raise ValueError('CPU study configuration order, content or hash differs')
    return value


async def read_cpu_study_async(client, grant):
    verifier = ScientistCpuCapabilityVerifier(client, grant)
    verifier._check_client()
    if client._async_active is not None or client._lock.locked() or client.uncertain_action_id is not None:
        raise ScientistAdmissionError('CPU study readback unavailable while Lab controls are active or uncertain')
    cancelled = threading.Event()
    client._async_cancel_event = cancelled
    worker = copy(client)
    deadline = time.monotonic() + client.timeout_seconds

    def read():
        if not client._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Lab control became active before CPU study readback')
        try:
            verifier._check_client()
            raw = worker._request('GET', '/v1/aos-cpu-study/' + verifier.grant.capability.suite_id,
                                  b'', deadline, bound=STUDY_BOUND)
            value = validate_cpu_study(raw, verifier.grant)
            verifier._check_client()
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise ScientistAdmissionError('CPU study readback cancelled or expired')
            return value
        except (OSError, ValueError, TypeError, OverflowError, RecursionError, ValidationError, http.client.HTTPException):
            raise ScientistAdmissionError('CPU study readback failed closed') from None
        finally:
            client._lock.release()

    pending = asyncio.create_task(asyncio.to_thread(read))
    client._async_active = pending
    client._cpu_study_active = pending

    def completed(finished):
        if not finished.cancelled():
            finished.exception()
        if client._async_active is finished:
            client._async_active = None
            client._async_cancel_event = None
        if getattr(client, '_cpu_study_active', None) is finished:
            client._cpu_study_active = None

    pending.add_done_callback(completed)
    try:
        await asyncio.wait({pending})
        return pending.result()
    except asyncio.CancelledError:
        cancelled.set()
        raise

"""Reviewed host adapter for Scientist's proposal2 client; no runtime defaults."""

import math
import os
from pathlib import Path
import socket
import stat
import threading
import time
from typing import Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, digest
from .lifecycle import ProcessIdentity, process_identity
from .scientist_admission_history import Hash, Identifier, ScientistServerGeneration
from .scientist_transport import BROKER_UNIT
from .shared_desktop_plan import load_activation, load_plan
from .shared_desktop_provision import verify_launch_intent


TRANSPORT_SCHEMA_SHA256 = '842fe08b2f7f7dbb1f0d0bcf000a5d3029eb4335114da800324d0e34952478f6'
TRANSPORT_V2_SCHEMA_SHA256 = '53844d314db2080cea681745e95179730b5083ba9e98b33528f1d7995bdeab3f'


class ScientistSharedLaunchReview(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    request_id: Identifier
    binding_sha256: Hash
    transport_schema_sha256: Literal['842fe08b2f7f7dbb1f0d0bcf000a5d3029eb4335114da800324d0e34952478f6']
    plan_path: str = Field(min_length=1, max_length=4096)
    plan_sha256: Hash
    activation_path: str = Field(min_length=1, max_length=4096)
    activation_sha256: Hash
    provision_sha256: Hash
    manager: ProcessIdentity
    broker: ScientistServerGeneration
    issued_boottime: float = Field(ge=0, allow_inf_nan=False)
    expires_boottime: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def original_scope(self):
        for value in (self.plan_path, self.activation_path):
            path = Path(value)
            if not path.is_absolute() or str(path) != value or '..' in path.parts:
                raise ValueError('Launch review requires exact absolute paths')
        if (not 0 < self.expires_boottime - self.issued_boottime <= 900
                or self.manager.boot_id != self.broker.boot_id or self.manager.uid != self.broker.uid
                or self.broker.unit != BROKER_UNIT):
            raise ValueError('Launch review requires original finite same-owner manager and broker identities')
        return self


class ScientistSharedLaunchReviewV2(ScientistSharedLaunchReview):
    schema_version: Literal['2.0'] = '2.0'
    transport_schema_sha256: Literal['53844d314db2080cea681745e95179730b5083ba9e98b33528f1d7995bdeab3f']


def _boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class SharedLaunchSocketConnector:
    """Connect only to an explicitly reviewed private path; wire authentication is separate."""

    def __init__(self, socket_path, *, clock=_boottime):
        value = str(socket_path)
        path = Path(value)
        if (not path.is_absolute() or str(path) != value or '..' in path.parts
                or any(ord(character) < 32 for character in value)
                or len(os.fsencode(value)) > 107):
            raise ValueError('Shared launch socket requires an exact absolute filesystem path')
        self.path = path
        self.clock = clock

    def _remaining(self, deadline):
        current = self.clock()
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or type(current) not in (int, float) or not math.isfinite(current)
                or not 0 < deadline - current <= 3.0):
            raise ValueError('Shared launch socket requires a finite original three-second BOOTTIME deadline')
        return deadline - current

    def _identity(self):
        if self.path.resolve(strict=True) != self.path:
            raise ValueError('Shared launch socket path must not contain aliases')
        parent, endpoint = self.path.parent.lstat(), self.path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or not stat.S_ISSOCK(endpoint.st_mode)
                or any(info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
                       for info in (parent, endpoint))):
            raise ValueError('Shared launch socket and parent must be private and user-owned')
        return tuple((info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_ctime_ns)
                     for info in (parent, endpoint))

    def __call__(self, deadline):
        self._remaining(deadline)
        identity = self._identity()
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            channel.settimeout(self._remaining(deadline))
            channel.connect(str(self.path))
            if self._identity() != identity:
                raise ValueError('Shared launch socket changed during connection')
            channel.settimeout(self._remaining(deadline))
            return channel
        except BaseException:
            channel.close()
            raise


class ScientistSharedLaunchAdapter:
    """Bind a separately reviewed authenticated Scientist client to host hooks.

    Client/source/socket composition is trusted and explicit. This adapter never
    imports sibling code, issues grants, chooses a socket, launches or cleans up.
    """

    def __init__(self, client, review, *, transport_schema_sha256, clock=_boottime,
                 identity_reader=process_identity):
        review_type = {'1.0': ScientistSharedLaunchReview, '2.0': ScientistSharedLaunchReviewV2}.get(review.schema_version)
        if review_type is None:
            raise ValueError('Unsupported explicitly reviewed Scientist launch version')
        self.review = review_type.model_validate(review.model_dump(), strict=True)
        if transport_schema_sha256 != self.review.transport_schema_sha256:
            raise ValueError('Scientist launch transport differs from the reviewed candidate wire')
        self.client = client
        self.clock = clock
        self.identity_reader = identity_reader
        self._lock = threading.Lock()
        self._claim_attempted = False
        self._client_binding()

    def _client_binding(self):
        broker = ScientistServerGeneration.model_validate(self.client.expected_broker.model_dump(), strict=True)
        if (broker != self.review.broker or self.client.broker_hash != digest(broker.model_dump(mode='json'))
                or not callable(getattr(self.client, 'request', None))):
            raise ValueError('Scientist launch client differs from the independently reviewed broker')

    def _current(self, plan, activation):
        review = self.review
        current = self.clock()
        if (type(current) not in (int, float) or not math.isfinite(current)
                or not review.issued_boottime <= current < review.expires_boottime
                or self.identity_reader(os.getpid()) != review.manager):
            raise ValueError('Original launch manager or finite window changed')
        self._client_binding()
        selected_plan = load_plan(Path(review.plan_path), review.plan_sha256)
        selected_activation = load_activation(Path(review.activation_path), review.activation_sha256)
        if (plan != selected_plan or activation != selected_activation
                or plan.plan_sha256() != review.plan_sha256
                or plan.template.broker_identity_sha256 != self.client.broker_hash
                or activation.boot_id != review.manager.boot_id
                or not activation.issued_monotonic <= review.issued_boottime
                or review.expires_boottime > activation.expires_monotonic):
            raise ValueError('Original launch plan, activation, broker or deadlines differ')
        current = self.clock()
        if (type(current) not in (int, float) or not math.isfinite(current)
                or not review.issued_boottime <= current < review.expires_boottime):
            raise ValueError('Original launch window expired during readback')
        return min(review.expires_boottime, current + 3.0)

    def _result(self, result, *, claim):
        status = result.status
        if (status.request_id != self.review.request_id or status.binding_sha256 != self.review.binding_sha256
                or type(result.consumed_now) is not bool or result.consumed_now is not claim
                or type(status.consumed) is not bool or type(status.cleanup_required) is not bool
                or type(status.expired) is not bool or status.expired
                or status.state not in ('reviewed', 'consumed')
                or status.consumed != (status.state == 'consumed')
                or status.cleanup_required != status.consumed or claim and not status.consumed
                or self._claim_attempted and not status.consumed):
            raise ValueError('Scientist launch readback is denied, uncertain or not a fresh claim')

    def verify(self, plan, activation):
        if not self._lock.acquire(blocking=False):
            raise ValueError('Launch authority call already in progress')
        try:
            deadline = self._current(plan, activation)
            result = self.client.request('verify', self.review.request_id, self.review.binding_sha256,
                                         deadline=deadline)
            self._current(plan, activation)
            if self.clock() >= deadline:
                raise ValueError('Launch verification reply exceeded its original call deadline')
            self._result(result, claim=False)
        finally:
            self._lock.release()

    def claim(self, plan, activation, state):
        if not self._lock.acquire(blocking=False):
            raise ValueError('Launch authority call already in progress')
        try:
            if self._claim_attempted:
                raise ValueError('Launch claim was already attempted; never automatically retry')
            deadline = self._current(plan, activation)
            review = self.review
            if (state.phase != 'starting' or state.service_binding is not None
                    or state.plan_path != review.plan_path or state.plan_sha256 != review.plan_sha256
                    or state.activation_path != review.activation_path
                    or state.activation_sha256 != review.activation_sha256
                    or state.provision_sha256 != review.provision_sha256
                    or state.session != plan.app_session or state.workspace != plan.workspace):
                raise ValueError('Launch claim differs from the original durable manager state')
            intent = verify_launch_intent(plan, review.provision_sha256, review.activation_sha256,
                                          current_boot_id=review.manager.boot_id)
            if state.launch_intent_sha256 != digest(intent.model_dump(mode='json')):
                raise ValueError('Launch claim differs from the durable intent bytes')
            if self.clock() >= deadline:
                raise ValueError('Launch claim deadline expired before send')
            self._claim_attempted = True
            result = self.client.request('claim', review.request_id, review.binding_sha256,
                                         state.launch_intent_sha256, deadline=deadline)
            self._current(plan, activation)
            if self.clock() >= deadline:
                raise ValueError('Launch claim reply exceeded its original call deadline')
            self._result(result, claim=True)
        finally:
            self._lock.release()

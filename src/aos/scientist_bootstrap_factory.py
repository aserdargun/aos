"""Explicit trusted bootstrap composition; desktop events are audit, not authority."""

from copy import deepcopy
from dataclasses import asdict
import hashlib
import math
import os
from pathlib import Path
import time
from types import MappingProxyType

from .contracts import now
from .desktop_control import DesktopController
from .scientist_admission_history import ScientistAdmissionBindingV2, ScientistAdmissionHistory
from .scientist_bootstrap import (
    CONTROL_DESCRIPTOR_SHA256, INFER_DESCRIPTOR_SHA256, ScientistBootstrapCapture, ScientistBootstrapCodec,
)
from .scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from .scientist_protocol import ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
from .scientist_terminal import TERMINAL_DESCRIPTOR_SHA256, canonical
from .scientist_transport import BROKER_UNIT, BrokerPeer, ScientistAdmissionError, SystemdBrokerAuthenticator


BOOTSTRAP_INTENT_KIND = 'scientist_bootstrap_intent'


def _require(condition, message):
    if not condition:
        raise ScientistAdmissionError(message)


def _deny_source(reviewed_bindings):
    raise ScientistAdmissionError('Independent current Scientist source/configuration authority is not configured')


class _FactoryBootstrapCapture(ScientistBootstrapCapture):
    def __init__(self, *arguments, authorize_control, **options):
        self._authorize_control = authorize_control
        super().__init__(*arguments, **options)

    def _authorize(self, control, peer):
        super()._authorize(control, peer)
        self._authorize_control(control, peer)


class ScientistBootstrapAdmissionFactory:
    """Trusted main factory plus expected_peer and confirm_runtime hooks.

    verify_source(selected_profile_to_full_binding) must independently verify
    current configured source/dependency closure, policy, profile/output pins,
    actual caller/service and canonical server generation against these reviewed
    bindings. Matching ACK hashes or filenames is not that verification. This
    callback cannot enable policy or replace desktop/task authorization.
    """

    def __init__(self, reviewed_bindings, control_socket_path, *, control_descriptor_sha256,
                 verify_source=_deny_source, authenticator=None, clock=None, timeout_seconds=10):
        _require(type(reviewed_bindings) is dict and 1 <= len(reviewed_bindings) <= 3
                 and callable(verify_source), 'Bootstrap factory requires explicit reviewed profile bindings')
        _require(isinstance(control_socket_path, Path) and control_socket_path.is_absolute()
                 and '..' not in control_socket_path.parts
                 and type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
                 and 0 < timeout_seconds <= 10 and (clock is None or callable(clock)),
                 'Bootstrap factory requires explicit bounded socket and clock configuration')
        _require(authenticator is None or all(callable(getattr(authenticator, name, None))
                 for name in ('authenticate', 'still_current')), 'Bootstrap factory authenticator is incomplete')
        reviewed = {}
        for profile, value in reviewed_bindings.items():
            encoded = value.model_dump(mode='json') if isinstance(value, ScientistAdmissionBindingV2) else value
            binding = ScientistAdmissionBindingV2.model_validate(deepcopy(encoded), strict=True)
            _require(profile == binding.profile_id and binding.control_schema.sha256 == CONTROL_DESCRIPTOR_SHA256
                     and binding.infer_schema.sha256 == INFER_DESCRIPTOR_SHA256
                     and binding.terminal_schema.sha256 == TERMINAL_DESCRIPTOR_SHA256
                     and binding.server_generation.unit == BROKER_UNIT
                     and binding.server_generation.uid == binding.caller_generation.uid == os.getuid()
                     and binding.caller_generation.pid == os.getpid()
                     and binding.server_generation.pid > 1
                     and binding.server_generation.boot_id == binding.caller_generation.boot_id,
                     'Reviewed bootstrap profile, descriptors or original process binding differ')
            reviewed[profile] = canonical(binding.model_dump(mode='json'))
        self._reviewed = MappingProxyType(reviewed)
        self._codec = ScientistBootstrapCodec(control_descriptor_sha256=control_descriptor_sha256)
        self._path, self._source, self._clock, self._timeout = control_socket_path, verify_source, clock, timeout_seconds
        self._authenticator = authenticator if authenticator is not None else SystemdBrokerAuthenticator()
        self._controller = self._store = self._history = None
        self._capture = None
        self._pending = None
        self._authorized = None

    def _bindings(self, profiles):
        _require(type(profiles) is dict and profiles and set(profiles) <= set(self._reviewed),
                 'Bootstrap runtime profiles are not explicitly reviewed')
        selected = {profile: ScientistAdmissionBindingV2.model_validate_json(self._reviewed[profile], strict=True)
                    for profile in profiles}
        _require(all(type(deployment) is str and deployment == selected[profile].profile_pin.deployment_digest
                     for profile, deployment in profiles.items()), 'Bootstrap runtime deployment differs from reviewed pins')
        return selected

    @property
    def output_contract(self):
        contracts = [ScientistAdmissionBindingV2.model_validate_json(value, strict=True).profile_pin.output_contract
                     for value in self._reviewed.values()]
        _require(all(contract == contracts[0] for contract in contracts),
                 'Bootstrap reviewed profiles require the same independently pinned output contract')
        return contracts[0].model_copy(deep=True)

    def confirm_runtime(self, profiles):
        selected = self._bindings(profiles)
        _require(all(binding.caller_generation.pid == os.getpid()
                     and binding.caller_generation.uid == binding.server_generation.uid == os.getuid()
                     for binding in selected.values()), 'Bootstrap reviewed caller process changed')
        _require(self._source is not _deny_source and self._source(deepcopy(selected)) is None,
                 'Bootstrap independent source verification must complete or raise')

    def __call__(self, controller):
        _require(isinstance(controller, DesktopController), 'Bootstrap factory requires the actual desktop controller')
        _require(self._controller is None or controller is self._controller and controller.store is self._store,
                 'Bootstrap factory cannot rebind another controller or original store')
        self.confirm_runtime({profile: ScientistAdmissionBindingV2.model_validate_json(value, strict=True).profile_pin.deployment_digest
                              for profile, value in self._reviewed.items()})
        if self._controller is not None:
            _require(self._history.capture is self._capture and self._capture.store is self._store,
                     'Bootstrap original capture object changed')
            return self._history
        _require(not controller.store.connection.in_transaction, 'Bootstrap factory cannot bind inside another transaction')
        state = controller.state()
        _require(state['owner'] == 'AGENT' and state['status'] == 'running', 'Bootstrap controller is not currently AGENT owned')
        capture = _FactoryBootstrapCapture(controller.store, self._path, codec=self._codec,
            authorize_control=self._authorize_control,
            verify_current=self._bootstrap_current, verify_capture=self._verify_capture, persist_intent=self._persist_intent,
            authenticator=self._authenticator, clock=self._clock, timeout_seconds=self._timeout)
        history_options = {} if self._clock is None else {'clock': self._clock}
        history = ScientistAdmissionHistory(controller.store, capture=capture, verify_current=self._history_current,
                                           record_version='2.0', **history_options)
        self._controller, self._store, self._history = controller, controller.store, history
        self._capture = capture
        return history

    def _check_current(self, request, binding, peer=None, *, admission=True):
        _require(self._controller is not None and self._controller.store is self._store
                 and self._history.store is self._store and self._history.record_version == '2.0'
                 and self._history.capture is self._capture and self._capture.store is self._store,
                 'Bootstrap original controller/history/store changed')
        request = ScientistTurnRequest.model_validate(request.model_dump(mode='json'), strict=True)
        binding = ScientistIntentBinding.model_validate(binding.model_dump(mode='json'), strict=True)
        transaction = self._store.connection.in_transaction
        def desktop_current():
            state = self._controller.state()
            _require(state['status'] == 'running' and binding.owner == 'AGENT'
                     and self._controller.runtime.runtime_id == state['runtime_id']
                     and all(state[key] == getattr(binding, key) for key in
                             ('session_id', 'runtime_id', 'lease_id', 'generation', 'owner')),
                     'Bootstrap current desktop authority differs')
            ScientistIntentJournal(self._store, binding)._current()
        desktop_current()
        selected = self._bindings({request.profile_id: request.deployment_digest})
        self.confirm_runtime({request.profile_id: request.deployment_digest})
        _require(self._store.connection.in_transaction is transaction, 'Bootstrap source verifier changed transaction ownership')
        desktop_current()
        stable = selected[request.profile_id]
        expected = BrokerPeer(**{key: value for key, value in stable.server_generation.model_dump(mode='json').items() if key != 'unit'})
        _require(peer is None or peer == expected, 'Bootstrap peer differs from reviewed original broker')
        _require(self._authenticator.authenticate(expected.pid, expected.uid) == expected
                 and self._authenticator.still_current(expected) is True, 'Bootstrap reviewed broker generation is no longer current')
        if admission:
            row = self._store.connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?', (request.request_id,)).fetchone()
            captured = self._store.connection.execute('SELECT 1 FROM scientist_admission_history WHERE request_id=?', (request.request_id,)).fetchone()
            if row is not None and captured is None:
                _require(transaction and row['state'] == 'pending' and row['binding_json'] == binding.model_dump_json()
                         and row['request_json'].encode() == scientist_request_frame(request)[:-1]
                         and row['request_sha256'] == scientist_request_sha256(request)
                         and row['broker_peer_json'] == canonical(asdict(expected))
                         and row['deadline'] > time.monotonic(), 'Bootstrap pending original capture differs')
            else:
                ScientistIntentJournal(self._store, binding, admission_history=self._history).verify_admission(request)
        _require(self._store.connection.in_transaction is transaction, 'Bootstrap current check changed transaction ownership')
        desktop_current()
        return request, binding, expected, stable

    def expected_peer(self, request, binding):
        return self._check_current(request, binding)[2]

    def _bootstrap_current(self, request, binding, peer):
        request, binding, peer, _stable = self._check_current(request, binding, peer)
        pending = (request.model_copy(deep=True), binding.model_copy(deep=True), deepcopy(peer))
        if pending != self._pending:
            self._authorized = None
        self._pending = pending

    def _authorize_control(self, control, peer):
        _require(self._pending is not None, 'Bootstrap control lacks a current original context')
        request, binding, expected = self._pending
        _require(peer == expected and control['profile_id'] == request.profile_id
                 and control['deployment_digest'] == request.deployment_digest,
                 'Bootstrap control differs from its prepared original context')
        selected = (deepcopy(self._pending), self._codec.encode_request(control))
        _require(self._authorized is None or self._authorized == selected,
                 'Bootstrap control frame changed within its original preparation')
        self._authorized = selected

    def _verify_capture(self, request, binding, peer, capture):
        _request, _binding, _peer, stable = self._check_current(request, binding, peer)
        _require(capture.admission_binding == stable, 'Bootstrap ACK differs from the complete independently reviewed binding')

    def _history_current(self, record, request, binding, peer):
        _request, _binding, _peer, stable = self._check_current(request, binding, peer, admission=False)
        _require(record.admission_binding == stable, 'Original history differs from the complete independently reviewed binding')

    def _read_event(self, control_id):
        row = self._store.connection.execute('SELECT * FROM desktop_events WHERE event_id=?', (control_id,)).fetchone()
        return None if row is None else dict(row)

    def _persist_intent(self, frame, fingerprint, deadline, peer):
        _require(self._pending is not None and not self._store.connection.in_transaction,
                 'Bootstrap audit intent requires its current prepared context outside original SQL')
        request, binding, expected = deepcopy(self._pending)
        control = self._codec.decode_request(frame)
        _require(self._authorized == (self._pending, frame)
                 and type(frame) is bytes and hashlib.sha256(frame).hexdigest() == fingerprint
                 and type(deadline) in (int, float) and math.isfinite(deadline) and deadline > time.monotonic()
                 and peer == expected and control['profile_id'] == request.profile_id
                 and control['deployment_digest'] == request.deployment_digest,
                 'Bootstrap audit frame, deadline or prepared binding differs')
        event = {'event_id': control['control_id'], 'session_id': binding.session_id,
                 'kind': BOOTSTRAP_INTENT_KIND, 'created_at': now(),
                 'payload_json': canonical({'authority': 'audit-only', 'control_frame': frame.decode('utf-8'),
                    'control_sha256': fingerprint, 'deadline': deadline, 'broker_peer': asdict(peer),
                    'intent_binding': binding.model_dump(mode='json'), 'request_id': request.request_id,
                    'request_sha256': scientist_request_sha256(request)})}
        connection = self._store.connection
        _require(connection.execute('PRAGMA synchronous').fetchone()[0] in (2, 3)
                 and connection.execute('PRAGMA journal_mode').fetchone()[0] in ('delete', 'truncate', 'persist', 'wal')
                 and any(row[1] == 'main' and row[2] for row in connection.execute('PRAGMA database_list')),
                 'Bootstrap audit writer requires durable original SQLite configuration')
        try:
            connection.execute('BEGIN IMMEDIATE')
            self._check_current(request, binding, peer)
            self._store.insert('desktop_events', **event)
            self._check_current(request, binding, peer)
            _require(connection.in_transaction and deadline > time.monotonic(), 'Bootstrap audit lost transaction or deadline')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        _require(not connection.in_transaction and self._read_event(control['control_id']) == event,
                 'Bootstrap committed audit intent readback differs')
        self._check_current(request, binding, peer)
        _require(not connection.in_transaction and deadline > time.monotonic()
                 and self._read_event(control['control_id']) == event,
                 'Bootstrap audit readback lost current authority or exact committed intent')

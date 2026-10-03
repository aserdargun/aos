import hashlib
import json
import math
import time
from copy import deepcopy
from dataclasses import asdict

from .contracts import canonical as local_canonical, now
from .scientist_admission_history import ScientistAdmissionHistory, ScientistAdmissionRecordV2
from .scientist_evidence_transport import ScientistEvidenceCodec
from .scientist_intents import ScientistIntentBinding
from .scientist_terminal import canonical, digest
from .scientist_transport import BrokerPeer, ScientistAdmissionError


def _deny_control(request, original, binding, peer):
    raise ScientistAdmissionError('Trusted original-target cleanup authorization is not configured')


class ScientistEvidenceJournal:
    def __init__(self, store, binding: ScientistIntentBinding, *, codec: ScientistEvidenceCodec,
                 admission_history: ScientistAdmissionHistory, verify_control=_deny_control):
        if (not isinstance(admission_history, ScientistAdmissionHistory)
                or admission_history.store is not store or admission_history.record_version != '2.0'
                or not isinstance(codec, ScientistEvidenceCodec) or not callable(verify_control)):
            raise ScientistAdmissionError('Evidence journal requires the same original store, history2.0 and pinned codec')
        self.store = store
        self.binding = ScientistIntentBinding.model_validate(binding.model_dump(mode='json'), strict=True)
        self.codec = codec
        self.history = admission_history
        self.verify_control = verify_control

    def _current(self):
        row = self.store.connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?',
                                            (self.binding.session_id,)).fetchone()
        if (row is None or row['status'] != 'running'
                or any(row[field] != getattr(self.binding, field)
                       for field in ('runtime_id', 'lease_id', 'generation', 'owner'))):
            raise ScientistAdmissionError('Evidence control current owner, lease or generation changed')

    @staticmethod
    def _deadline(deadline):
        observed = time.monotonic()
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not observed < deadline <= observed + 10):
            raise ScientistAdmissionError('Evidence control deadline is expired or exceeds its bounded call')

    def _original(self, request):
        original, checksum = self.history.read(request['target']['request_id'])
        intent = self.store.connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                               (original.request_id,)).fetchone()
        original_binding = ScientistIntentBinding.model_validate_json(intent['binding_json'], strict=True)
        if (not isinstance(original, ScientistAdmissionRecordV2)
                or original.session_id != self.binding.session_id
                or original_binding.runtime_id != self.binding.runtime_id
                or request['target']['request_sha256'] != original.request_sha256
                or request['target']['original_peer_generation_sha256']
                != digest(original.admission_binding.caller_generation.model_dump(mode='json'))
                or request['profile_id'] != original.admission_binding.profile_id
                or request['deployment_digest'] != original.admission_binding.profile_pin.deployment_digest):
            raise ScientistAdmissionError('Evidence control differs from its original request, admission or runtime')
        return original, checksum

    def _verify(self, request, original, peer):
        connection = self.store.connection
        self._current()
        if self.verify_control(deepcopy(request), original.model_copy(deep=True),
                               self.binding.model_copy(deep=True), deepcopy(peer)) is not None:
            raise ScientistAdmissionError('Evidence cleanup authorization must complete or raise')
        if not connection.in_transaction:
            raise ScientistAdmissionError('Evidence authorization changed transaction ownership')
        self._current()

    def _transaction(self):
        connection = self.store.connection
        if connection.in_transaction:
            raise ScientistAdmissionError('Evidence control must commit outside other transactions')
        connection.execute('BEGIN IMMEDIATE')
        return connection

    def _matching_control(self, request, frame, peer):
        connection = self.store.connection
        existing = connection.execute('SELECT control_id FROM scientist_evidence_controls WHERE control_id=?',
                                      (request['control_id'],)).fetchone()
        if existing is not None:
            row, _request, _original, original_peer = self._read(request['control_id'])
            if (row['request_json'].encode('utf-8') != frame or original_peer != peer
                    or row['current_binding_json'] != self.binding.model_dump_json()):
                raise ScientistAdmissionError('Evidence control ID belongs to different bytes, binding or peer')
        pending = connection.execute(
            'SELECT control.control_id FROM scientist_evidence_controls control '
            'LEFT JOIN scientist_evidence_responses response ON response.control_id=control.control_id '
            'WHERE control.request_id=? AND response.control_id IS NULL',
            (request['target']['request_id'],)).fetchall()
        if any(row['control_id'] != request['control_id'] for row in pending):
            raise ScientistAdmissionError('Evidence target has another unresolved control exchange')

    def authorize(self, request, peer):
        frame = self.codec.encode_request(request)
        frozen = self.codec.decode_request(frame)
        if not isinstance(peer, BrokerPeer):
            raise ScientistAdmissionError('Evidence cleanup requires an authenticated peer')
        connection = self._transaction()
        try:
            self._current()
            original, checksum = self._original(frozen)
            self._matching_control(frozen, frame, peer)
            self._verify(frozen, original, peer)
            if self._original(frozen)[1] != checksum:
                raise ScientistAdmissionError('Original admission changed during evidence authorization')
            self._matching_control(frozen, frame, peer)
            self._current()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def persist_intent(self, frame, fingerprint, deadline, peer):
        request = self.codec.decode_request(frame)
        if hashlib.sha256(frame).hexdigest() != fingerprint or not isinstance(peer, BrokerPeer):
            raise ScientistAdmissionError('Evidence control frame/hash or authenticated peer differs')
        self._deadline(deadline)
        connection = self._transaction()
        try:
            self._current()
            original, checksum = self._original(request)
            self._verify(request, original, peer)
            connection.execute('INSERT INTO scientist_evidence_controls VALUES(?,?,?,?,?,?,?,?,?,?)',
                (request['control_id'], original.request_id, original.session_id, checksum,
                 self.binding.model_dump_json(), frame.decode('utf-8'), fingerprint,
                 local_canonical(asdict(peer)), deadline, now()))
            self._verify(request, original, peer)
            if self._original(request)[1] != checksum:
                raise ScientistAdmissionError('Original admission changed during evidence control persistence')
            self._deadline(deadline)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _read(self, control_id):
        row = self.store.connection.execute('SELECT * FROM scientist_evidence_controls WHERE control_id=?',
                                            (control_id,)).fetchone()
        if row is None:
            raise ScientistAdmissionError('Original evidence control is missing')
        request = self.codec.decode_request(row['request_json'].encode('utf-8'))
        binding = ScientistIntentBinding.model_validate_json(row['current_binding_json'], strict=True)
        peer = BrokerPeer(**json.loads(row['broker_peer_json']))
        original, checksum = self.history.read(row['request_id'])
        intent = self.store.connection.execute('SELECT binding_json FROM scientist_turn_intents WHERE request_id=?',
                                               (row['request_id'],)).fetchone()
        original_binding = ScientistIntentBinding.model_validate_json(intent['binding_json'], strict=True)
        if (request['control_id'] != row['control_id'] or request['target']['request_id'] != row['request_id']
                or hashlib.sha256(row['request_json'].encode('utf-8')).hexdigest() != row['request_sha256']
                or row['admission_record_sha256'] != checksum or not isinstance(original, ScientistAdmissionRecordV2)
                or row['session_id'] != original.session_id or binding.session_id != original.session_id
                or binding.runtime_id != original_binding.runtime_id
                or binding.model_dump_json() != row['current_binding_json']
                or local_canonical(asdict(peer)) != row['broker_peer_json']
                or request['target']['request_sha256'] != original.request_sha256
                or request['target']['original_peer_generation_sha256']
                != digest(original.admission_binding.caller_generation.model_dump(mode='json'))
                or request['profile_id'] != original.admission_binding.profile_id
                or request['deployment_digest'] != original.admission_binding.profile_pin.deployment_digest):
            raise ScientistAdmissionError('Stored evidence control original identity is malformed')
        return row, request, original, peer

    def record_response(self, response, peer):
        try:
            raw = canonical(response).encode('utf-8')
            frozen = json.loads(raw)
            control_id = frozen['control_id']
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            raise ScientistAdmissionError('Evidence response is malformed') from error
        connection = self._transaction()
        try:
            self._current()
            row, request, original, original_peer = self._read(control_id)
            if (peer != original_peer or row['current_binding_json'] != self.binding.model_dump_json()
                    or row['session_id'] != self.binding.session_id):
                raise ScientistAdmissionError('Evidence response current binding or authenticated peer differs')
            self.codec.decode_response(raw, row['request_json'].encode('utf-8'))
            self._deadline(row['deadline'])
            self._verify(request, original, peer)
            connection.execute('INSERT INTO scientist_evidence_responses VALUES(?,?,?,?,?,?)',
                (control_id, row['request_sha256'], raw.decode('utf-8'), hashlib.sha256(raw).hexdigest(),
                 local_canonical(asdict(peer)), now()))
            self._verify(request, original, peer)
            self._read(control_id)
            self._deadline(row['deadline'])
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def inspect(self, control_id):
        try:
            row, request, _original, _peer = self._read(control_id)
            response = self.store.connection.execute(
                'SELECT * FROM scientist_evidence_responses WHERE control_id=?', (control_id,)).fetchone()
            decoded = None
            if response is not None:
                raw = response['response_json'].encode('utf-8')
                decoded = self.codec.decode_response(raw, row['request_json'].encode('utf-8'))
                if (hashlib.sha256(raw).hexdigest() != response['response_sha256']
                        or response['request_sha256'] != row['request_sha256']
                        or response['broker_peer_json'] != row['broker_peer_json']):
                    raise ScientistAdmissionError('Stored evidence response differs from its original control')
            return {'control_id': control_id, 'request': request, 'request_sha256': row['request_sha256'],
                    'admission_record_sha256': row['admission_record_sha256'],
                    'pending': response is None, 'response': decoded,
                    'response_sha256': None if response is None else response['response_sha256']}
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            raise ScientistAdmissionError('Stored evidence exchange is malformed') from error

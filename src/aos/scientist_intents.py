import hashlib
import json
import math
import time
from dataclasses import asdict
from typing import Literal

from pydantic import Field

from .contracts import TypedModel, canonical, now
from .scientist_protocol import (
    ScientistTurnReceipt, ScientistTurnRequest, scientist_receipt_frame, scientist_request_frame,
)
from .scientist_transport import BrokerPeer, ScientistAdmissionError


class ScientistIntentBinding(TypedModel):
    session_id: str = Field(min_length=1, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    owner: Literal['AGENT', 'HUMAN']
    authorization_context_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


def scientist_unresolved_predicate(connection):
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scientist_turn_resolutions'").fetchone() is None:
        return '1=1'
    predicate = ('NOT EXISTS (SELECT 1 FROM scientist_turn_resolutions resolution '
                 'WHERE resolution.request_id=scientist_turn_intents.request_id)')
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scientist_no_admission_closures'").fetchone() is not None:
        predicate += (' AND NOT EXISTS (SELECT 1 FROM scientist_no_admission_closures closure '
                      'JOIN scientist_admission_history history ON history.record_sha256=closure.admission_record_sha256 '
                      'WHERE closure.request_id=scientist_turn_intents.request_id '
                      'AND closure.request_sha256=scientist_turn_intents.request_sha256 '
                      'AND history.request_id=scientist_turn_intents.request_id '
                      "AND json_extract(closure.intent_snapshot_json,'$.request_json')=scientist_turn_intents.request_json "
                      "AND json_extract(closure.intent_snapshot_json,'$.binding_json')=scientist_turn_intents.binding_json "
                      "AND scientist_turn_intents.state='pending' AND scientist_turn_intents.receipt_json IS NULL)")
    return predicate


class ScientistIntentJournal:
    def __init__(self, store, binding: ScientistIntentBinding, *, admission_history=None):
        self.store = store
        self.binding = ScientistIntentBinding.model_validate(binding.model_dump(), strict=True)
        if admission_history is not None and admission_history.store is not store:
            raise ScientistAdmissionError('Scientist admission history must share the original intent store')
        self.admission_history = admission_history

    def _current(self):
        row = self.store.connection.execute('SELECT * FROM desktop_sessions WHERE session_id=?',
                                            (self.binding.session_id,)).fetchone()
        if (row is None or row['status'] != 'running'
                or any(row[field] != getattr(self.binding, field)
                       for field in ['runtime_id', 'lease_id', 'generation', 'owner'])):
            raise ScientistAdmissionError('Scientist intent session authority changed')

    def verify_admission(self, request: ScientistTurnRequest) -> None:
        connection = self.store.connection
        owns_transaction = not connection.in_transaction
        try:
            if owns_transaction:
                connection.execute('BEGIN IMMEDIATE')
            self._current()
            if self.admission_history is not None and self.binding.owner != 'AGENT':
                raise ScientistAdmissionError('Scientist original admission requires current AGENT control')
            unresolved = scientist_unresolved_predicate(connection)
            if connection.execute('SELECT 1 FROM scientist_turn_intents WHERE request_id=? AND NOT ('
                                  + unresolved + ')', (request.request_id,)).fetchone() is not None:
                raise ScientistAdmissionError('Resolved Scientist request IDs cannot be admitted again')
            row = connection.execute(
                'SELECT * FROM scientist_turn_intents WHERE session_id=? AND ' + unresolved, (self.binding.session_id,),
            ).fetchone()
            if row is not None and (row['request_id'] != request.request_id or row['state'] != 'pending'
                                    or row['binding_json'] != self.binding.model_dump_json()
                                    or row['request_json'].encode() != scientist_request_frame(request)[:-1]):
                raise ScientistAdmissionError('Scientist intent requires trusted reconciliation before new work')
            if row is not None:
                self._deadline(row)
                self._identity(request, BrokerPeer(**json.loads(row['broker_peer_json'])))
                self._current()
                self._deadline(row)
            if owns_transaction:
                connection.commit()
        except BaseException:
            if owns_transaction:
                connection.rollback()
            raise

    def _identity(self, request, peer):
        if self.admission_history is not None:
            self.admission_history.verify_intent(request, self.binding, peer)
        elif self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scientist_admission_history'").fetchone():
            if self.store.connection.execute('SELECT 1 FROM scientist_admission_history WHERE request_id=?',
                                             (request.request_id,)).fetchone():
                raise ScientistAdmissionError('Captured Scientist identity requires its configured verifier')

    def _deadline(self, row):
        if self.admission_history is not None and row['deadline'] <= time.monotonic():
            raise ScientistAdmissionError('Scientist original intent deadline expired')

    def persist_intent(self, frame: bytes, digest: str, deadline: float, peer: BrokerPeer) -> None:
        if (type(frame) is not bytes or type(deadline) not in (int, float)
                or not math.isfinite(deadline) or deadline <= time.monotonic()
                or not isinstance(peer, BrokerPeer)):
            raise ScientistAdmissionError('Scientist intent metadata or deadline is invalid')
        request = ScientistTurnRequest.model_validate_json(frame, strict=True)
        if frame != scientist_request_frame(request)[:-1] or hashlib.sha256(frame).hexdigest() != digest:
            raise ScientistAdmissionError('Scientist intent differs from canonical request digest')
        connection = self.store.connection
        if connection.in_transaction:
            raise ScientistAdmissionError('Scientist intent must commit outside other transactions')
        try:
            connection.execute('BEGIN IMMEDIATE')
            self.verify_admission(request)
            connection.execute(
                'INSERT INTO scientist_turn_intents VALUES(?,?,?,?,?,?,?,\'pending\',NULL,?)',
                (request.request_id, self.binding.session_id, self.binding.model_dump_json(),
                 frame.decode(), digest, canonical(asdict(peer)), deadline, now()),
            )
            if self.admission_history is not None:
                record = self.admission_history.capture_intent(request, self.binding, peer)
                self._current()
                self.admission_history.check_capture_freshness(record)
                if deadline <= time.monotonic():
                    raise ScientistAdmissionError('Scientist intent deadline expired during identity capture')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def record_receipt(self, receipt: ScientistTurnReceipt, peer: BrokerPeer) -> None:
        connection = self.store.connection
        if connection.in_transaction:
            raise ScientistAdmissionError('Scientist receipt must commit outside other transactions')
        receipt = ScientistTurnReceipt.model_validate(receipt.model_dump(), strict=True)
        try:
            connection.execute('BEGIN IMMEDIATE')
            self._current()
            row = connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                      (receipt.request_id,)).fetchone()
            if connection.execute('SELECT 1 FROM scientist_turn_intents WHERE request_id=? AND NOT ('
                                  + scientist_unresolved_predicate(connection) + ')',
                                  (receipt.request_id,)).fetchone() is not None:
                raise ScientistAdmissionError('Resolved Scientist original receipts cannot be changed')
            if (row is None or row['state'] != 'pending'
                    or row['binding_json'] != self.binding.model_dump_json()
                    or row['broker_peer_json'] != canonical(asdict(peer))):
                raise ScientistAdmissionError('Scientist receipt belongs to another principal or intent')
            self._deadline(row)
            request = json.loads(row['request_json'])
            self._identity(ScientistTurnRequest.model_validate(request, strict=True), peer)
            self._current()
            receipt_frame = json.dumps(receipt.model_dump(mode='json'), allow_nan=False,
                                      ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
            try:
                scientist_receipt_frame(receipt_frame, ScientistTurnRequest.model_validate(request, strict=True))
            except ValueError as error:
                raise ScientistAdmissionError('Scientist receipt differs from the persisted request') from error
            self._deadline(row)
            changed = connection.execute(
                'UPDATE scientist_turn_intents SET state=\'receipt_recorded\',receipt_json=? '
                'WHERE request_id=? AND state=\'pending\'',
                (receipt.model_dump_json(), receipt.request_id),
            ).rowcount
            if changed != 1:
                raise ScientistAdmissionError('Scientist receipt state changed before persistence')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

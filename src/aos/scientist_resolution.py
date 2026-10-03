import json
from copy import deepcopy

from .contracts import now
from .scientist_budget_witness import ScientistRetainedTerminalVerifier, ScientistOriginalBudgetWitness
from .scientist_evidence_journal import ScientistEvidenceJournal
from .scientist_intents import ScientistIntentBinding
from .scientist_protocol import ScientistTurnReceipt, ScientistTurnRequest
from .scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec
from .scientist_terminal import ScientistTerminalReceipt, canonical, digest
from .scientist_transport import ScientistAdmissionError


def _deny_resolution(original, binding, terminal, peer):
    raise ScientistAdmissionError('Trusted original-target resolution authorization is not configured')


class ScientistResolutionJournal:
    """Append verified resolution without altering original intent or ownership.

    Trusted callbacks must never change the original connection's transaction
    ownership. A committed control ACK alone cannot implement proof or authorize
    resolution. Historical inspection does not refresh any authority.
    """

    def __init__(self, evidence_journal, *, verifier=None, verify_resolution=_deny_resolution):
        if (not isinstance(evidence_journal, ScientistEvidenceJournal)
                or not isinstance(evidence_journal.codec, ScientistRetainedEvidenceCodec)
                or not callable(verify_resolution)):
            raise ScientistAdmissionError('Resolution requires the original pinned retained evidence journal')
        if verifier is not None and (not isinstance(verifier, ScientistRetainedTerminalVerifier)
                or verifier.budget_verifier.history is not evidence_journal.history):
            raise ScientistAdmissionError('Resolution verifier must retain the same original admission history')
        self.evidence = evidence_journal
        self.store = evidence_journal.store
        self.verifier = verifier
        self.verify_resolution = verify_resolution

    def _evidence(self, control_id):
        inspected = self.evidence.inspect(control_id)
        row, control, original, peer = self.evidence._read(control_id)
        response = inspected['response']
        if (inspected['pending'] or control['op'] != 'reconcile' or response['ok'] is not True):
            raise ScientistAdmissionError('Resolution requires a committed successful retained reconcile ACK')
        intent = self.store.connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                               (original.request_id,)).fetchone()
        request = ScientistTurnRequest.model_validate_json(intent['request_json'], strict=True)
        data = response['data']
        terminal = ScientistTerminalReceipt.model_validate_json(data['evidence']['terminal_canonical'], strict=True)
        witness = ScientistOriginalBudgetWitness.model_validate(data['original_budget_witness'], strict=True)
        binding = original.admission_binding
        if (canonical(terminal.model_dump(mode='json', by_alias=True)) != data['evidence']['terminal_canonical']
                or terminal.request_id != original.request_id or terminal.request_sha256 != original.request_sha256
                or terminal.admission_binding != binding
                or terminal.admission_binding_sha256 != original.admission_binding_sha256
                or terminal.profile_id != binding.profile_id
                or terminal.deployment_digest != binding.profile_pin.deployment_digest
                or terminal.profile_config_sha256 != binding.profile_pin.config_sha256
                or terminal.response_schema_sha256 != binding.profile_pin.response_schema_sha256
                or terminal.original_principal != binding.caller_generation
                or witness.original_admission_binding_sha256 != original.admission_binding_sha256
                or witness.original_cleanup_authorization_sha256 is not None):
            raise ScientistAdmissionError('Resolution terminal or witness differs from exact original admission')
        if intent['receipt_json'] is not None:
            receipt = ScientistTurnReceipt.model_validate_json(intent['receipt_json'], strict=True)
            encoded = data['evidence']['result_canonical']
            if (terminal.terminal_state != 'completed' or encoded is None
                    or receipt.request_id != request.request_id or receipt.profile_id != request.profile_id
                    or receipt.deployment_digest != request.deployment_digest
                    or canonical({key: receipt.model_dump(mode='json')[key]
                                  for key in ('response', 'usage', 'generation')}) != encoded):
                raise ScientistAdmissionError('Original recorded inference receipt contradicts retained terminal result')
        return inspected, row, control, original, peer, intent, request, terminal, witness

    def _pending(self, request_id):
        if self.store.connection.execute(
                'SELECT 1 FROM scientist_evidence_controls control '
                'LEFT JOIN scientist_evidence_responses response ON response.control_id=control.control_id '
                'WHERE control.request_id=? AND response.control_id IS NULL', (request_id,)).fetchone():
            raise ScientistAdmissionError('Resolution target has an unresolved control exchange')

    def _authorize(self, control, original, terminal, peer):
        self.evidence._verify(control, original, peer)
        if self.verify_resolution(original.model_copy(deep=True), self.evidence.binding.model_copy(deep=True),
                                  terminal.model_copy(deep=True), deepcopy(peer)) is not None:
            raise ScientistAdmissionError('Resolution authorization must complete or raise')
        if not self.store.connection.in_transaction:
            raise ScientistAdmissionError('Resolution authorization changed transaction ownership')
        self.evidence._current()

    @staticmethod
    def _record(inspected, original, binding, terminal, witness, created_at):
        return {'request_id': original.request_id, 'session_id': original.session_id,
                'admission_record_sha256': inspected['admission_record_sha256'],
                'control_id': inspected['control_id'], 'response_sha256': inspected['response_sha256'],
                'current_binding_json': binding.model_dump_json(),
                'terminal_json': inspected['response']['data']['evidence']['terminal_canonical'],
                'terminal_receipt_sha256': terminal.receipt_sha256,
                'budget_witness_json': canonical(witness.model_dump(mode='json', by_alias=True)),
                'budget_witness_sha256': digest(witness.model_dump(mode='json', by_alias=True)),
                'created_at': created_at}

    def inspect(self, request_id):
        try:
            row = self.store.connection.execute('SELECT * FROM scientist_turn_resolutions WHERE request_id=?',
                                                (request_id,)).fetchone()
            if row is None:
                raise ScientistAdmissionError('Original Scientist resolution is missing')
            inspected, _control_row, _control, original, _peer, intent, _request, terminal, witness = self._evidence(row['control_id'])
            binding = ScientistIntentBinding.model_validate_json(row['current_binding_json'], strict=True)
            original_binding = ScientistIntentBinding.model_validate_json(intent['binding_json'], strict=True)
            if (binding.session_id != original.session_id or binding.runtime_id != original_binding.runtime_id
                    or dict(row) != self._record(inspected, original, binding, terminal, witness, row['created_at'])):
                raise ScientistAdmissionError('Stored resolution differs from original admission and immutable evidence')
            return dict(row)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            raise ScientistAdmissionError('Stored resolution is malformed') from error

    def resolve(self, control_id, *, response_sha256):
        if self.verifier is None or self.verify_resolution is _deny_resolution:
            raise ScientistAdmissionError('Independent retained proof and explicit resolution authority are required')
        connection = self.evidence._transaction()
        try:
            self.evidence._current()
            values = self._evidence(control_id)
            inspected, control_row, control, original, peer, intent, request, terminal, witness = values
            if (type(response_sha256) is not str or inspected['response_sha256'] != response_sha256
                    or original.session_id != self.evidence.binding.session_id
                    or json.loads(intent['binding_json'])['runtime_id'] != self.evidence.binding.runtime_id):
                raise ScientistAdmissionError('Resolution expected ACK or current original runtime differs')
            self._pending(original.request_id)
            self._authorize(control, original, terminal, peer)
            verified = self.verifier.verify_response(request, codec=self.evidence.codec,
                response_bytes=canonical(inspected['response']).encode('utf-8'),
                control_request_bytes=control_row['request_json'].encode('utf-8'))
            if verified != terminal or not connection.in_transaction:
                raise ScientistAdmissionError('Retained proof or transaction changed during resolution')
            self._authorize(control, original, terminal, peer)
            existing = connection.execute('SELECT * FROM scientist_turn_resolutions WHERE request_id=?',
                                          (original.request_id,)).fetchone()
            proposed = self._record(inspected, original, self.evidence.binding, terminal, witness,
                                    existing['created_at'] if existing is not None else now())
            if existing is not None:
                if self.inspect(original.request_id) != proposed:
                    raise ScientistAdmissionError('Resolution retry differs from its immutable original record')
            else:
                connection.execute('INSERT INTO scientist_turn_resolutions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                   tuple(proposed.values()))
            verified = self.verifier.verify_response(request, codec=self.evidence.codec,
                response_bytes=canonical(inspected['response']).encode('utf-8'),
                control_request_bytes=control_row['request_json'].encode('utf-8'))
            if verified != terminal or not connection.in_transaction:
                raise ScientistAdmissionError('Retained proof or transaction changed before resolution commit')
            self._authorize(control, original, terminal, peer)
            self._pending(original.request_id)
            if (self._evidence(control_id)[0] != inspected
                    or dict(connection.execute('SELECT * FROM scientist_turn_intents WHERE request_id=?',
                                               (original.request_id,)).fetchone()) != dict(intent)):
                raise ScientistAdmissionError('Original intent or retained ACK changed during resolution')
            result = self.inspect(original.request_id)
            self.evidence._current()
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

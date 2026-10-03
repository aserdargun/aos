import sqlite3
import threading

from .scientist_admission_history import ScientistAdmissionHistory, ScientistAdmissionRecordV2
from .scientist_budget_witness import ScientistRetainedTerminalVerifier
from .scientist_evidence_client import ScientistEvidenceClient
from .scientist_evidence_journal import ScientistEvidenceJournal, _deny_control
from .scientist_intents import ScientistIntentBinding
from .scientist_resolution import ScientistResolutionJournal, _deny_resolution
from .scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec, SCHEMA
from .scientist_terminal import digest
from .scientist_transport import ScientistAdmissionError


class ScientistRetainedHost:
    """Explicit retained-target effects on the caller's original store and binding.

    Configuration never discovers, dispatches, resolves, renews authority or owns
    the store. Trusted callbacks must not change SQLite transaction ownership.
    A successful historical ACK is evidence, not current rights or physical proof.
    """

    def __init__(self, store, history, binding, *, socket_path, reviewed_schema_bytes,
                 transport_schema_sha256, evidence_schema_sha256, authenticator=None,
                 verifier=None, verify_control=_deny_control, verify_resolution=_deny_resolution,
                 timeout_seconds=10):
        if (not isinstance(history, ScientistAdmissionHistory) or history.store is not store
                or history.record_version != '2.0'):
            raise ScientistAdmissionError('Retained host requires the same original admission2.0 store')
        required = {'schema_migrations', 'desktop_sessions', 'scientist_turn_intents',
                    'scientist_admission_history', 'scientist_evidence_controls',
                    'scientist_evidence_responses', 'scientist_turn_resolutions'}
        try:
            tables = {row[0] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required.issubset(tables) or store.connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=26 AND name='scientist_turn_resolutions'").fetchone() is None:
                raise ScientistAdmissionError('Retained host requires the original schema26 control and resolution tables')
        except sqlite3.Error as error:
            raise ScientistAdmissionError('Retained host original schema26 metadata is unavailable') from error
        if verifier is not None and (not isinstance(verifier, ScientistRetainedTerminalVerifier)
                                    or verifier.budget_verifier.history is not history):
            raise ScientistAdmissionError('Retained host verifier must share its original history')
        if not callable(verify_control) or not callable(verify_resolution):
            raise TypeError('Retained host authority providers must be trusted callables')
        if authenticator is not None and any(not callable(getattr(authenticator, name, None))
                                             for name in ('authenticate', 'still_current')):
            raise TypeError('Retained host authenticator must verify socket peer generations')
        self.store, self.history = store, history
        self.binding = ScientistIntentBinding.model_validate(binding.model_dump(mode='json'), strict=True)
        self._schema_bytes = reviewed_schema_bytes
        self._pins = {'transport_schema_sha256': transport_schema_sha256,
                      'evidence_schema_sha256': evidence_schema_sha256}
        self._base_codec = self._codec()
        client = ScientistEvidenceClient(socket_path, codec=self._base_codec,
                                        timeout_seconds=timeout_seconds, authenticator=authenticator)
        self.socket_path, self.timeout_seconds = client.socket_path, client.timeout_seconds
        self.authenticator = authenticator
        self.verifier, self.verify_control, self.verify_resolution = verifier, verify_control, verify_resolution
        self._lock = threading.Lock()
        self._uncertain_control_id = None

    @property
    def uncertain_control_id(self):
        return self._uncertain_control_id

    def _codec(self, capability=None):
        return ScientistRetainedEvidenceCodec(self._schema_bytes, **self._pins, expected_capability=capability)

    def _journal(self, codec=None):
        return ScientistEvidenceJournal(self.store, self.binding, codec=codec or self._base_codec,
                                        admission_history=self.history, verify_control=self.verify_control)

    def _configured(self):
        if (self.authenticator is None or self.verifier is None
                or self.verify_control is _deny_control or self.verify_resolution is _deny_resolution):
            raise ScientistAdmissionError('Retained host needs explicit peer, current authority, proof and resolution providers')

    def _request(self, request_id, control_id, operation, capability=None):
        original, _checksum = self.history.read(request_id)
        if not isinstance(original, ScientistAdmissionRecordV2):
            raise ScientistAdmissionError('Retained host cannot adopt legacy admission history')
        request = {'schema': SCHEMA, 'version': 3, 'op': operation, 'control_id': control_id,
            'profile_id': original.admission_binding.profile_id,
            'deployment_digest': original.admission_binding.profile_pin.deployment_digest,
            'target': {'request_id': original.request_id, 'request_sha256': original.request_sha256,
                'original_peer_generation_sha256': digest(original.admission_binding.caller_generation.model_dump(mode='json'))},
            'expected_capability_sha256': None if capability is None else digest(capability), **self._pins}
        codec = self._codec(capability)
        frozen = codec.decode_request(codec.encode_request(request))
        self._journal(codec)._original(frozen)
        return frozen, codec

    def _capability(self, capability_control_id):
        inspected = self._inspection(self._journal(), capability_control_id)
        response, request = inspected['response'], inspected['request']
        if (inspected['pending'] or request['op'] != 'capability' or response['ok'] is not True):
            raise ScientistAdmissionError('Retained host needs an independently stored successful capability ACK')
        return request, response['data']['capability']

    @staticmethod
    def _inspection(journal, control_id):
        inspected = journal.inspect(control_id)
        journal._original(inspected['request'])
        return inspected

    def _selected_journal(self, control_id, capability_control_id):
        capability_request, capability = self._capability(capability_control_id)
        journal = self._journal(self._codec(capability))
        inspected = self._inspection(journal, control_id)
        request = inspected['request']
        if (request['op'] != 'reconcile' or request['target'] != capability_request['target']
                or request['profile_id'] != capability_request['profile_id']
                or request['deployment_digest'] != capability_request['deployment_digest']
                or request['expected_capability_sha256'] != digest(capability)):
            raise ScientistAdmissionError('Selected retained capability belongs to another original control target')
        return journal, inspected

    def _dispatch(self, request, codec, cancel_event):
        self._configured()
        if self.uncertain_control_id is not None:
            raise ScientistAdmissionError('Uncertain retained host dispatch requires explicit historical reconciliation')
        journal = self._journal(codec)
        journal._current()
        connection = self.store.connection
        if connection.in_transaction:
            raise ScientistAdmissionError('Retained host dispatch cannot run inside another transaction')
        if connection.execute('SELECT 1 FROM scientist_evidence_controls WHERE control_id=?',
                              (request['control_id'],)).fetchone():
            raise ScientistAdmissionError('Stored retained control IDs cannot be dispatched again')
        if connection.execute('SELECT 1 FROM scientist_turn_resolutions WHERE request_id=?',
                              (request['target']['request_id'],)).fetchone():
            raise ScientistAdmissionError('Resolved inference targets cannot trigger new retained control dispatch')
        ScientistResolutionJournal(journal)._pending(request['target']['request_id'])
        client = ScientistEvidenceClient(self.socket_path, codec=codec, timeout_seconds=self.timeout_seconds,
            authenticator=self.authenticator, authorize=journal.authorize,
            persist_intent=journal.persist_intent, record_response=journal.record_response)
        try:
            return client.exchange(request, cancel_event=cancel_event)
        finally:
            if client.uncertain_control_id is not None:
                self._uncertain_control_id = client.uncertain_control_id

    def discover(self, request_id, control_id, *, cancel_event=None):
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Retained host already has an active operation')
        try:
            self._configured()
            request, codec = self._request(request_id, control_id, 'capability')
            return self._dispatch(request, codec, cancel_event)
        finally:
            self._lock.release()

    def reconcile(self, request_id, capability_control_id, control_id, *, cancel_event=None):
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Retained host already has an active operation')
        try:
            self._configured()
            capability_request, capability = self._capability(capability_control_id)
            request, codec = self._request(request_id, control_id, 'reconcile', capability)
            if (request['target'] != capability_request['target']
                    or request['profile_id'] != capability_request['profile_id']
                    or request['deployment_digest'] != capability_request['deployment_digest']):
                raise ScientistAdmissionError('Retained capability ACK belongs to another original request')
            return self._dispatch(request, codec, cancel_event)
        finally:
            self._lock.release()

    def resolve(self, reconcile_control_id, capability_control_id, *, response_sha256):
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Retained host already has an active operation')
        try:
            self._configured()
            journal, _inspected = self._selected_journal(reconcile_control_id, capability_control_id)
            return ScientistResolutionJournal(journal, verifier=self.verifier,
                verify_resolution=self.verify_resolution).resolve(reconcile_control_id, response_sha256=response_sha256)
        finally:
            self._lock.release()

    def inspect(self, control_id, *, capability_control_id=None):
        if capability_control_id is None:
            return self._inspection(self._journal(), control_id)
        return self._selected_journal(control_id, capability_control_id)[1]

    def inspect_resolution(self, request_id, capability_control_id):
        _request, capability = self._capability(capability_control_id)
        journal = self._journal(self._codec(capability))
        result = ScientistResolutionJournal(journal).inspect(request_id)
        self._selected_journal(result['control_id'], capability_control_id)
        return result

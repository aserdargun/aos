from copy import deepcopy

from .scientist_evidence_transport import (
    ERRORS, REQUEST_LIMIT, RESPONSE_LIMIT, RETRYABLE_ERRORS, SCHEMA_LIMIT,
    ScientistEvidenceCodec, _STRICT_VALIDATOR, _decode, _require,
    request_schema as evidence_request_schema,
)
from .scientist_terminal import canonical, digest
from .scientist_transport import ScientistAdmissionError


SCHEMA = 'aos-scientist-control-evidence.v3'


def request_schema():
    schema = evidence_request_schema()
    for variant in schema['oneOf']:
        variant['properties']['schema']['const'] = SCHEMA
        variant['properties']['version']['const'] = 3
    return schema


class ScientistRetainedEvidenceCodec(ScientistEvidenceCodec):
    """Explicit v3 consistency checks, never source authority or physical proof.

    Successful reconcile requires an independently retained target capability.
    Its preimage is copied at construction, not learned from the response. Current
    target rights, independent budget provenance and physical proof stay external.
    """

    def __init__(self, reviewed_schema_bytes, *, transport_schema_sha256,
                 evidence_schema_sha256, expected_capability=None):
        super().__init__(reviewed_schema_bytes, transport_schema_sha256=transport_schema_sha256,
                         evidence_schema_sha256=evidence_schema_sha256)
        self._local_request_validator = _STRICT_VALIDATOR(request_schema())
        schema = _decode(reviewed_schema_bytes, SCHEMA_LIMIT, canonical_required=False)
        roots = ('legacy_terminal', 'legacy_budget', 'legacy_allocation')
        _require(all(name in schema['$defs'] for name in roots)
                 and 'original_budget_witness' in schema['$defs']
                 and 'retained_evidence' in schema['$defs'],
                 'Reviewed retained transport lacks its explicit v3 roots')
        self._nested = {name: _STRICT_VALIDATOR({'$schema': schema['$schema'],
                        '$defs': deepcopy(schema['$defs']), '$ref': '#/$defs/' + name})
                        for name in roots}
        _require(expected_capability is None or type(expected_capability) is dict,
                 'Retained capability must be an explicit preimage')
        self._expected_capability = (None if expected_capability is None else
            _decode(canonical(expected_capability).encode('utf-8'), RESPONSE_LIMIT))

    def _retained_capability(self, request):
        capability = deepcopy(self._expected_capability)
        _require(capability is not None and digest(capability) == request['expected_capability_sha256'],
                 'Successful retained reconcile requires its exact original capability preimage')
        data = {'capability': capability, 'admission': 'denied',
                'reason_code': 'cleanup_only' if 'cleanup_grant' in capability else 'retained_target_only',
                'evidence_schema_sha256': self.evidence_schema_sha256,
                'transport_schema_sha256': self.transport_schema_sha256}
        response = {'schema': SCHEMA, 'version': 3, 'control_id': request['control_id'],
                    'op': 'capability', 'ok': True, 'capability_sha256': digest(capability),
                    'data': data, 'error': None}
        self._validate(self._response_validator, response)
        self._capability(data, request, digest(capability))
        return capability

    def _budget_links(self, data, request):
        capability = self._retained_capability(request)
        witness, evidence = data['original_budget_witness'], data['evidence']
        _require(witness['target'] == evidence['target'] == request['target']
                 and witness['profile_id'] == request['profile_id']
                 and witness['deployment_digest'] == request['deployment_digest'],
                 'Retained budget target, profile or deployment differs')
        terminal = _decode(evidence['terminal_canonical'].encode('utf-8'), RESPONSE_LIMIT)
        self._validate(self._nested['legacy_terminal'], terminal)
        _require(terminal['request_id'] == request['target']['request_id']
                 and terminal['request_sha256'] == request['target']['request_sha256']
                 and digest(terminal['original_principal']) == request['target']['original_peer_generation_sha256']
                 and digest({key: value for key, value in terminal.items() if key != 'receipt_sha256'})
                 == terminal['receipt_sha256'], 'Retained terminal original identity or hash differs')
        for name in ('profile_id', 'deployment_digest', 'profile_config_sha256',
                     'response_schema_sha256', 'allocation_binding_sha256'):
            _require(witness[name] == terminal[name], 'Retained witness and terminal pins differ')
        budget = _decode(witness['budget_canonical'].encode('utf-8'), REQUEST_LIMIT)
        self._validate(self._nested['legacy_budget'], budget)
        _require(digest(budget) == witness['budget_sha256']
                 and canonical(budget) == canonical(terminal['original_budget']),
                 'Independently supplied budget differs from terminal')
        _require(budget['total_seconds'] >= budget['activation_seconds'] + budget['inference_seconds']
                 and budget['queue_seconds'] >= budget['total_seconds'] + 30,
                 'Retained original budget does not cover execution and drain')
        admitted, envelope = budget['admitted_boottime'], budget['envelope_deadline']
        deadlines = [budget[name] for name in ('activation_deadline', 'inference_deadline',
                                              'total_deadline', 'queue_deadline', 'envelope_deadline')]
        if admitted is None:
            _require(all(value is None for value in deadlines), 'Unadmitted deadlines are not supported')
        else:
            _require(envelope is not None and admitted < envelope
                     and all(value is None or admitted < value <= envelope for value in deadlines)
                     and all(budget[name] is None or budget['total_deadline'] is not None
                             and budget[name] <= budget['total_deadline']
                             for name in ('activation_deadline', 'inference_deadline')),
                     'Retained original phase deadlines differ')
        original_admission = witness['original_admission_binding_sha256']
        original_cleanup = witness['original_cleanup_authorization_sha256']
        _require((original_admission is None) != (original_cleanup is None)
                 and original_admission == terminal['admission_binding_sha256'],
                 'Retained original authority variant differs')
        if 'cleanup_grant' in capability:
            grant = capability['cleanup_grant']
            _require(grant['original_admission_binding_sha256'] == original_admission
                     and grant['original_cleanup_authorization_sha256'] == original_cleanup,
                     'Retained cleanup grant original authority differs')
            binding = grant['original_admission_binding']
            if binding is None:
                binding = grant['original_cleanup_authorization']['binding']
        else:
            binding = capability['control_binding']
            if original_admission is None:
                authorization = {'target': request['target'], 'binding': binding, 'operations': ['cancel']}
                _require(digest(authorization) == original_cleanup, 'Retained tombstone authority differs')
        _require(binding['caller_generation'] == terminal['original_principal'], 'Original retained caller differs')
        if original_admission is not None:
            _require(digest(binding) == original_admission and binding == terminal['admission_binding'],
                     'Original retained admission binding differs')
        else:
            _require(terminal['admission_binding'] is None and terminal['release_outcome'] == 'never_admitted'
                     and terminal['terminal_state'] == 'canceled', 'Retained tombstone variant differs')
        _require(witness['profile_config_sha256'] == binding['profile_pin']['config_sha256']
                 and witness['response_schema_sha256'] == binding['profile_pin']['response_schema_sha256']
                 and (admitted is not None) == (original_admission is not None),
                 'Retained original profile or admission budget differs')
        encoded = evidence['allocation_canonical']
        if encoded is None:
            _require(witness['allocation_binding_sha256'] is None, 'Retained allocation preimage is missing')
        else:
            allocation = _decode(encoded.encode('utf-8'), RESPONSE_LIMIT)
            self._validate(self._nested['legacy_allocation'], allocation)
            _require(digest(allocation) == witness['allocation_binding_sha256']
                     and allocation['admission_binding_sha256'] == original_admission
                     and allocation['request_sha256'] == request['target']['request_sha256']
                     and allocation['lease']['request_id'] == request['target']['request_id']
                     and allocation['original_principal'] == terminal['original_principal']
                     and allocation['original_deadline'] == budget['envelope_deadline']
                     and allocation['original_budget'] == {key: budget[key] for key in allocation['original_budget']},
                     'Retained allocation and original budget binding differ')

    def decode_response(self, raw, original_request_bytes):
        request = self.decode_request(original_request_bytes)
        response = _decode(raw, RESPONSE_LIMIT - 1)
        self._validate(self._response_validator, response)
        try:
            _require(set(response) == {'schema', 'version', 'control_id', 'op', 'ok',
                                      'capability_sha256', 'data', 'error'}
                     and response['schema'] == SCHEMA and type(response['version']) is int
                     and response['version'] == 3 and type(response['ok']) is bool
                     and response['control_id'] == request['control_id'] and response['op'] == request['op'],
                     'Retained response correlation differs')
            if not response['ok']:
                error = response['error']
                _require(response['data'] is None and response['capability_sha256'] is None
                         and set(error) == {'code', 'retryable'} and error['code'] in ERRORS
                         and type(error['retryable']) is bool
                         and error['retryable'] == (error['code'] in RETRYABLE_ERRORS),
                         'Retained response error variant differs')
            else:
                _require(response['error'] is None, 'Successful retained evidence cannot contain an error')
                if request['op'] == 'capability':
                    self._capability(response['data'], request, response['capability_sha256'])
                else:
                    _require(response['capability_sha256'] == request['expected_capability_sha256'],
                             'Retained reconcile capability differs')
                    self._budget_links(response['data'], request)
                    for name in ('terminal', 'allocation', 'drain', 'no_admission', 'result'):
                        encoded = response['data']['evidence'][name + '_canonical']
                        if encoded is not None:
                            _decode(encoded.encode('utf-8'), 96 * 1024 if name == 'result' else RESPONSE_LIMIT)
            return response
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as error:
            raise ScientistAdmissionError('Malformed retained evidence response') from error

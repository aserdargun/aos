import json
import math
import re
from copy import deepcopy
from typing import get_args

from jsonschema import Draft202012Validator, SchemaError, ValidationError, validators

from .scientist_protocol import Profile, _reject_constant, _unique_object
from .scientist_terminal import canonical, digest
from .scientist_transport import ScientistAdmissionError


SCHEMA = 'aos-scientist-control-evidence.v2'
REQUEST_LIMIT = 8192
RESPONSE_LIMIT = 128 * 1024
SCHEMA_LIMIT = 512 * 1024
RETRYABLE_ERRORS = {'busy', 'deadline_exceeded', 'internal_unavailable'}
ERRORS = {'invalid_frame', 'unsupported_version', 'unsupported_schema', 'unauthorized',
          'stale_generation', 'capability_mismatch', 'capability_expired', 'profile_mismatch',
          'deployment_mismatch', 'request_conflict', 'history_denied', 'busy',
          'deadline_exceeded', 'internal_unavailable'}


def _require(condition, message):
    if not condition:
        raise ScientistAdmissionError(message)


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def request_schema():
    fingerprint = {'type': 'string', 'pattern': '^[a-f0-9]{64}$', 'minLength': 64, 'maxLength': 64}
    identifier = {'type': 'string', 'pattern': '^[a-f0-9]{32}$', 'minLength': 32, 'maxLength': 32}
    common = {'schema': {'type': 'string', 'const': SCHEMA},
              'version': {'type': 'integer', 'const': 2}, 'control_id': identifier,
              'profile_id': {'type': 'string', 'enum': list(get_args(Profile))},
              'deployment_digest': fingerprint,
              'target': _object({'request_id': identifier, 'request_sha256': fingerprint,
                                'original_peer_generation_sha256': fingerprint}),
              'evidence_schema_sha256': fingerprint, 'transport_schema_sha256': fingerprint}
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'oneOf': [
        _object({**common, 'op': {'type': 'string', 'const': operation},
                 'expected_capability_sha256': {'type': 'null'} if operation == 'capability' else fingerprint})
        for operation in ('capability', 'reconcile')]}


def _pattern(validator, pattern, instance, schema):
    if isinstance(instance, str):
        matches = re.fullmatch(pattern, instance) if pattern.endswith('$') else re.search(pattern, instance)
        if matches is None:
            yield ValidationError('String does not match the exact reviewed grammar')


_STRICT_VALIDATOR = validators.extend(Draft202012Validator, {'pattern': _pattern},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many({
        'integer': lambda _checker, value: type(value) is int,
        'number': lambda _checker, value: type(value) in (int, float) and math.isfinite(value),
    }))


def _decode(raw, limit, *, canonical_required=True):
    _require(type(raw) is bytes and 0 < len(raw) <= limit, 'Evidence byte bound differs')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        _require(type(value) is dict, 'Evidence must be an object')
        encoded = canonical(value).encode('utf-8')
        _require(not canonical_required or encoded == raw, 'Evidence JSON is not exact canonical UTF-8')
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        raise ScientistAdmissionError('Malformed evidence JSON') from error


def _schema_boundary(value, definitions, depth=0):
    _require(depth <= 48, 'Reviewed evidence schema exceeds the depth bound')
    if isinstance(value, dict):
        _require(not any(key in value for key in ('$id', '$dynamicRef', '$recursiveRef')),
                 'Evidence schema cannot change reference scope')
        if '$ref' in value:
            _require(type(value['$ref']) is str and re.fullmatch(r'#/\$defs/[A-Za-z0-9_.-]+', value['$ref']),
                     'Evidence schema references must be local definitions')
            _require(value['$ref'][8:] in definitions, 'Evidence schema reference is missing')
        if value.get('type') == 'object':
            _require(value.get('additionalProperties') is False,
                     'Reviewed evidence object schemas must be closed')
        for entry in value.values():
            _schema_boundary(entry, definitions, depth + 1)
    elif isinstance(value, list):
        for entry in value:
            _schema_boundary(entry, definitions, depth + 1)


class ScientistEvidenceCodec:
    def __init__(self, reviewed_schema_bytes, *, transport_schema_sha256, evidence_schema_sha256):
        for fingerprint in (transport_schema_sha256, evidence_schema_sha256):
            _require(type(fingerprint) is str and re.fullmatch('[a-f0-9]{64}', fingerprint),
                     'Evidence pins must be explicit SHA-256 values')
        schema = _decode(reviewed_schema_bytes, SCHEMA_LIMIT, canonical_required=False)
        _require(digest(schema) == transport_schema_sha256, 'Reviewed transport schema pin differs')
        _require(schema.get('$schema') == 'https://json-schema.org/draft/2020-12/schema'
                 and type(schema.get('$defs')) is dict
                 and all(name in schema['$defs'] for name in ('evidence_request', 'evidence_response')),
                 'Reviewed transport schema lacks its explicit roots')
        _schema_boundary(schema, schema['$defs'])
        try:
            _STRICT_VALIDATOR.check_schema(schema)
            self._request_validator = _STRICT_VALIDATOR({**deepcopy(schema), '$ref': '#/$defs/evidence_request'})
            self._response_validator = _STRICT_VALIDATOR({**deepcopy(schema), '$ref': '#/$defs/evidence_response'})
        except (SchemaError, ValueError, TypeError, RecursionError) as error:
            raise ScientistAdmissionError('Invalid reviewed evidence schema') from error
        self._local_request_validator = _STRICT_VALIDATOR(request_schema())
        self.transport_schema_sha256 = transport_schema_sha256
        self.evidence_schema_sha256 = evidence_schema_sha256

    @staticmethod
    def _validate(validator, value):
        try:
            validator.validate(value)
        except (ValidationError, ValueError, TypeError, RecursionError, OverflowError) as error:
            raise ScientistAdmissionError('Evidence differs from the reviewed closed schema') from error

    def _pins(self, value):
        _require(value['evidence_schema_sha256'] == self.evidence_schema_sha256
                 and value['transport_schema_sha256'] == self.transport_schema_sha256,
                 'Evidence or transport schema pin differs')

    def encode_request(self, value):
        try:
            raw = canonical(value).encode('utf-8')
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise ScientistAdmissionError('Evidence request cannot be encoded') from error
        self.decode_request(raw)
        return raw

    def decode_request(self, raw):
        value = _decode(raw, REQUEST_LIMIT - 1)
        self._validate(self._local_request_validator, value)
        self._validate(self._request_validator, value)
        self._pins(value)
        return value

    def _capability(self, data, request, fingerprint):
        self._pins(data)
        capability = data['capability']
        _require(data['admission'] == 'denied' and digest(capability) == fingerprint,
                 'Evidence capability cannot grant inference admission')
        if 'control_binding' in capability:
            _require(data['reason_code'] == 'retained_target_only'
                     and capability['operations'] == ['cancel', 'reconcile', 'status'],
                     'Evidence retained-target capability differs')
            binding = capability['control_binding']
            target = capability['target']
            boot_id = binding['server_generation']['boot_id']
        else:
            _require(data['reason_code'] == 'cleanup_only', 'Evidence cleanup variant differs')
            grant = capability['cleanup_grant']
            _require(grant['schema'] == 'aos-scientist-cleanup-grant.v1'
                     and type(grant['version']) is int and grant['version'] == 1
                     and digest(grant) == capability['cleanup_grant_sha256'], 'Cleanup grant hash differs')
            binding = grant['original_admission_binding']
            authorization = grant['original_cleanup_authorization']
            if binding is None:
                _require(authorization is not None and grant['original_admission_binding_sha256'] is None
                         and digest(authorization) == grant['original_cleanup_authorization_sha256']
                         and authorization['target'] == grant['target'], 'Tombstone cleanup identity differs')
                binding = authorization['binding']
            else:
                _require(digest(binding) == grant['original_admission_binding_sha256']
                         and authorization is None and grant['original_cleanup_authorization_sha256'] is None,
                         'Original cleanup admission differs')
            _require('reconcile' in grant['operations'] and grant['caller_generation'] == binding['caller_generation']
                     and grant['profile_id'] == request['profile_id']
                     and grant['deployment_digest'] == request['deployment_digest'], 'Cleanup scope differs')
            target, boot_id = grant['target'], grant['resolver_generation']['boot_id']
        _require(target == request['target'] and binding['profile_id'] == request['profile_id']
                 and binding['profile_pin']['deployment_digest'] == request['deployment_digest']
                 and digest(binding['caller_generation']) == target['original_peer_generation_sha256']
                 and capability['boot_id'] == boot_id
                 and capability['issued_boottime'] < capability['expires_boottime'],
                 'Original evidence target capability differs')

    def decode_response(self, raw, original_request_bytes):
        request = self.decode_request(original_request_bytes)
        response = _decode(raw, RESPONSE_LIMIT - 1)
        self._validate(self._response_validator, response)
        try:
            _require(set(response) == {'schema', 'version', 'control_id', 'op', 'ok',
                                      'capability_sha256', 'data', 'error'}
                     and response['schema'] == SCHEMA and type(response['version']) is int
                     and response['version'] == 2 and type(response['ok']) is bool
                     and response['control_id'] == request['control_id'] and response['op'] == request['op'],
                     'Evidence response correlation differs')
            if not response['ok']:
                error = response['error']
                _require(response['data'] is None and response['capability_sha256'] is None
                         and set(error) == {'code', 'retryable'} and error['code'] in ERRORS
                         and type(error['retryable']) is bool
                         and error['retryable'] == (error['code'] in RETRYABLE_ERRORS),
                         'Evidence error variant differs')
            else:
                _require(response['error'] is None, 'Successful evidence cannot contain an error')
                if request['op'] == 'capability':
                    self._capability(response['data'], request, response['capability_sha256'])
                else:
                    evidence = response['data']
                    _require(response['capability_sha256'] == request['expected_capability_sha256']
                             and evidence['target'] == request['target']
                             and evidence['schema'] == 'aos-scientist-terminal-evidence.v2'
                             and type(evidence['version']) is int and evidence['version'] == 2,
                             'Evidence reconcile target or capability differs')
                    for name in ('terminal', 'allocation', 'drain', 'no_admission', 'result'):
                        encoded = evidence[name + '_canonical']
                        if encoded is not None:
                            _require(type(encoded) is str, 'Evidence preimages must remain strings')
                            _decode(encoded.encode('utf-8'), 96 * 1024 if name == 'result' else RESPONSE_LIMIT)
            return response
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as error:
            raise ScientistAdmissionError('Malformed evidence response') from error

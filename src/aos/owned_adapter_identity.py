"""Historical identity and per-call proof checks for explicit owned adapter runs."""

import json
import re
import sqlite3

from jsonschema import ValidationError as SchemaError

from .contracts import Option, Prediction, State, canonical, digest
from .dataset import validator
from .decision import decision_request


KIND = 'owned_episode_adapter_runtime'
PROOF_KIND = 'model.owned_adapter_inference'
HASH = re.compile(r'[a-f0-9]{64}\Z')
BINDING_KEYS = {'protocol', 'authorization_sha256', 'adaptation_report_sha256',
                'artifact_sha256', 'input_sha256', 'deployment_manifest_sha256',
                'base_deployment_id', 'runtime_worker_sha256'}


def validate_runtime_binding(binding):
    if (type(binding) is not dict or set(binding) != BINDING_KEYS
            or binding['protocol'] != 'owned-adapter-runtime-v1'
            or type(binding['base_deployment_id']) is not str
            or re.fullmatch(r'decider-[a-f0-9]{64}', binding['base_deployment_id']) is None
            or any(type(binding[key]) is not str or HASH.fullmatch(binding[key]) is None
                   for key in BINDING_KEYS - {'protocol', 'base_deployment_id'})):
        raise ValueError('owned_adapter_runtime_binding_invalid')
    return binding


def validate_adapter_identity(identity):
    if (type(identity) is not dict or set(identity) != {
            'deployment_id', 'kind', 'real_model', 'base_deployment_id',
            'adapter_binding_sha256', 'pins'}
            or identity['kind'] != KIND or identity['real_model'] is not True
            or type(identity['pins']) is not dict):
        raise ValueError('owned_adapter_identity_invalid')
    try:
        pins = identity['pins']
        binding = validate_runtime_binding(pins['owned_adapter_runtime'])
        base = {key: value for key, value in pins.items() if key != 'owned_adapter_runtime'}
        if (binding['base_deployment_id'] != 'decider-' + digest(base)
                or identity['base_deployment_id'] != binding['base_deployment_id']
                or identity['adapter_binding_sha256'] != digest(binding)
                or identity['deployment_id'] != 'owned-adapter-' + digest(binding)
                or type(base['model_files']['model.safetensors']) is not str
                or HASH.fullmatch(base['model_files']['model.safetensors']) is None
                or any(type(base[key]) is not str or re.fullmatch(r'[a-f0-9]{40}', base[key]) is None
                       for key in ('checkpoint_revision', 'tokenizer_revision', 'code_revision'))):
            raise ValueError('owned_adapter_identity_invalid')
    except (KeyError, TypeError):
        raise ValueError('owned_adapter_identity_invalid') from None
    return binding


def adapter_registry_values(identity):
    binding = validate_adapter_identity(identity)
    model_hash = identity['pins']['model_files']['model.safetensors']
    return {'adapter_id': 'owned-s1-adapter-' + digest(binding),
            'model_id': 'decider-base-' + model_hash, 'base_sha256': model_hash,
            'sha256': binding['artifact_sha256'], 'compatibility': 'unknown',
            'metadata_json': canonical({'runtime_binding': binding,
                'adapter_target': 'last_mlp_down_proj', 'adapter_rank': 4,
                'scope': 'owned_synthetic_per_run_canary', 'synthetic': True,
                'training_ready': False, 'promotion_authorized': False})}


def verify_adapter_registry(connection, identity):
    from .registries import ModelRegistry

    expected = adapter_registry_values(identity)
    adapter = connection.execute('SELECT * FROM adapters WHERE adapter_id=?',
                                 (expected['adapter_id'],)).fetchone()
    model = connection.execute('SELECT * FROM models WHERE model_id=?',
                               (expected['model_id'],)).fetchone()
    deployment = connection.execute('SELECT * FROM deployments WHERE deployment_id=?',
                                    (identity['deployment_id'],)).fetchone()
    active = connection.execute('SELECT 1 FROM active_deployments WHERE deployment_id=?',
                                (identity['deployment_id'],)).fetchone()
    return (adapter is not None and model is not None and deployment is not None
            and active is None and all(adapter[key] == value for key, value in expected.items())
            and all(model[key] == value for key, value in ModelRegistry.experiment_values(identity).items())
            and deployment['adapter_id'] == expected['adapter_id']
            and deployment['model_id'] == expected['model_id']
            and deployment['status'] == 'EXPERIMENTAL'
            and deployment['config_json'] == canonical(identity['pins'])
            and deployment['config_sha256'] == digest(identity['pins']))


def adapter_inference_proof(call, identity, metrics):
    binding = validate_adapter_identity(identity)
    if (type(metrics) is not dict or metrics.get('adapter_loaded') is not True
            or metrics.get('base_parameters_unchanged') is not True
            or type(metrics.get('adapter_hook_calls')) is not int
            or not 1 <= metrics['adapter_hook_calls'] <= 65536
            or metrics.get('adapter_sha256') != binding['artifact_sha256']
            or call['deployment_id'] != identity['deployment_id']
            or call['role'] != 'system1' or call['status'] != 'ok'):
        raise ValueError('owned_adapter_inference_proof_invalid')
    payload = {'schema_version': '1.0', 'synthetic': True,
        **{key: call[key] for key in ('call_id', 'run_id', 'step_id', 'deployment_id')},
        'binding_sha256': digest(binding), 'artifact_sha256': binding['artifact_sha256'],
        'runtime_worker_sha256': binding['runtime_worker_sha256'],
        'request_sha256': digest(json.loads(call['request_json'])),
        'response_sha256': digest(json.loads(call['response_json'])),
        'adapter_hook_calls': metrics['adapter_hook_calls'], 'adapter_loaded': True,
        'base_parameters_unchanged': True, 'training_ready': False, 'promotion_authorized': False}
    validator('owned_adapter_inference').validate(payload)
    return payload


def verify_adapter_inference(connection, call, identity):
    try:
        validate_adapter_identity(identity)
        if not verify_adapter_registry(connection, identity):
            return False
        run = connection.execute('SELECT deployment_snapshot_json FROM runs WHERE run_id=?',
                                 (call['run_id'],)).fetchone()
        if run is None or json.loads(run['deployment_snapshot_json']) != identity:
            return False
        proofs = connection.execute("SELECT * FROM observations WHERE run_id=? AND kind=?",
                                    (call['run_id'], PROOF_KIND)).fetchall()
        if len(proofs) > 6:
            return False
        matching = [row for row in proofs if json.loads(row['payload_json']).get('call_id') == call['call_id']]
        if len(matching) != 1:
            return False
        proof = matching[0]
        payload = json.loads(proof['payload_json'])
        if not validator('owned_adapter_inference').is_valid(payload):
            return False
        metrics = {'adapter_loaded': payload['adapter_loaded'],
                   'adapter_hook_calls': payload['adapter_hook_calls'],
                   'adapter_sha256': payload['artifact_sha256'],
                   'base_parameters_unchanged': payload['base_parameters_unchanged']}
        if (proof['action_id'] is not None or proof['step_id'] != call['step_id']
                or proof['payload_json'] != canonical(adapter_inference_proof(call, identity, metrics))):
            return False
        decisions = connection.execute('SELECT * FROM decisions WHERE call_id=?',
                                       (call['call_id'],)).fetchall()
        if len(decisions) != 1:
            return False
        decision = decisions[0]
        snapshot = connection.execute('SELECT * FROM state_snapshots WHERE snapshot_id=?',
                                      (decision['snapshot_id'],)).fetchone()
        if snapshot is None:
            return False
        state = State.model_validate_json(snapshot['state_json'])
        options = [Option.model_validate(item) for item in json.loads(decision['options_json'])]
        response = Prediction.model_validate_json(call['response_json'])
        response.validate_options(options)
        from .site_skill_form_invocation_audit import _timestamp

        return (state.phase.value == 'DECIDE' and state.deployment_id == identity['deployment_id']
                and state.run_id == call['run_id'] == decision['run_id'] == snapshot['run_id']
                and state.step_id == call['step_id'] == decision['step_id'] == snapshot['step_id']
                and state.state_version == snapshot['state_version']
                and snapshot['content_sha256'] == digest(state.model_dump(mode='json'))
                and call['request_json'] == canonical(decision_request(state, options))
                and decision['question'] == json.loads(call['request_json'])['question']
                and decision['selected_option'] == response.selected_option
                and json.loads(decision['probabilities_json']) == response.probabilities
                and decision['confidence'] == response.probabilities[response.selected_option]
                and _timestamp(snapshot['created_at']) <= _timestamp(call['created_at'])
                <= _timestamp(proof['created_at']) <= _timestamp(decision['created_at']))
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error, SchemaError):
        return False


def verify_adapter_run(connection, run_id, admission):
    try:
        binding = validate_runtime_binding(admission['runtime_binding'])
        run = connection.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None or run['status'] != 'succeeded' or run['outcome'] != 'passed':
            return False
        identity = json.loads(run['deployment_snapshot_json'])
        if (validate_adapter_identity(identity) != binding
                or identity['deployment_id'] != admission['adapter_deployment_id']):
            return False
        calls = connection.execute('SELECT * FROM model_calls WHERE run_id=?', (run_id,)).fetchall()
        proofs = connection.execute('SELECT * FROM observations WHERE run_id=? AND kind=?',
                                    (run_id, PROOF_KIND)).fetchall()
        states = connection.execute('SELECT state_json FROM state_snapshots WHERE run_id=?',
                                    (run_id,)).fetchall()
        return (len(calls) == len(proofs) == 6
                and bool(states) and all(json.loads(state['state_json']).get('deployment_id')
                                        == identity['deployment_id'] for state in states)
                and all(verify_adapter_inference(connection, call, identity) for call in calls))
    except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        return False

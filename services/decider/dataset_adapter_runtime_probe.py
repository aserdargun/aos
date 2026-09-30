"""Isolated synthetic adapter inference through the pinned Decider runtime path."""

import hashlib
import json
import math
from pathlib import Path
import struct
import sys

from dataset_adapter_replay import ARTIFACT_BYTES, ARTIFACT_MAGIC, private_artifact
from worker import ModelSession, validate_request, verify_environment, verify_files


def prediction_metrics(prediction, option_ids, gold_option_id):
    probabilities = prediction['probabilities']
    if (set(probabilities) != set(option_ids)
            or prediction['selected_option'] not in option_ids
            or any(type(probability) not in (int, float) or not math.isfinite(probability)
                   or not 0 <= probability <= 1 for probability in probabilities.values())
            or not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0, abs_tol=1e-3)):
        raise ValueError('adapter_runtime_prediction_invalid')
    return {'selected_index': option_ids.index(prediction['selected_option']),
            'gold_probability': probabilities[gold_option_id]}


def run(request, manifest: Path, artifact: Path):
    if (not isinstance(request, dict)
            or set(request) != {'mode', 'artifact_sha256', 'dataset_manifest_sha256',
                                'input_sha256', 'deployment_manifest_sha256',
                                'decisions', 'gold_option_ids', 'split_counts'}
            or request['mode'] != 'synthetic_adapter_runtime_probe_v1'
            or any(not isinstance(request[name], str) or len(request[name]) != 64
                   or any(character not in '0123456789abcdef' for character in request[name])
                   for name in ('artifact_sha256', 'dataset_manifest_sha256',
                                'input_sha256', 'deployment_manifest_sha256'))):
        raise ValueError('adapter_runtime_request_invalid')
    counts = request['split_counts']
    decisions = request['decisions']
    gold_option_ids = request['gold_option_ids']
    if (not isinstance(counts, dict) or set(counts) != {'train', 'validation', 'test'}
            or any(type(count) is not int or not 0 <= count <= 32 for count in counts.values())
            or not 1 <= counts['train'] <= 4 or not isinstance(decisions, list)
            or not isinstance(gold_option_ids, list) or len(decisions) != sum(counts.values())
            or len(gold_option_ids) != len(decisions) or len(decisions) > 32):
        raise ValueError('adapter_runtime_split_scope')
    options_by_decision = []
    gold_indices = []
    for decision, gold_option_id in zip(decisions, gold_option_ids):
        validate_request(decision)
        option_ids = [option['id'] for option in decision['options']]
        if (not all(option_id == 'option-' + str(index) for index, option_id in enumerate(option_ids))
                or gold_option_id not in option_ids):
            raise ValueError('adapter_runtime_options_invalid')
        options_by_decision.append(option_ids)
        gold_indices.append(option_ids.index(gold_option_id))
    manifest_bytes = manifest.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != request['deployment_manifest_sha256']:
        raise ValueError('adapter_runtime_manifest_changed')
    pins = json.loads(manifest_bytes)
    model_root = verify_environment(pins)
    payload = private_artifact(artifact)
    if (len(payload) != ARTIFACT_BYTES or payload[:8] != ARTIFACT_MAGIC
            or payload[8:40] != bytes.fromhex(request['dataset_manifest_sha256'])
            or payload[40:72] != bytes.fromhex(request['input_sha256'])
            or payload[72:104] != bytes.fromhex(request['deployment_manifest_sha256'])
            or struct.unpack_from('<III', payload, 104) != (4, 6144, 2048)
            or hashlib.sha256(payload).hexdigest() != request['artifact_sha256']):
        raise ValueError('adapter_runtime_artifact_binding')
    weights = struct.unpack_from('<32768f', payload, 116)
    if not all(math.isfinite(value) for value in weights):
        raise ValueError('adapter_runtime_artifact_values')
    session = ModelSession(model_root, pins)
    session.manifest = manifest
    base_predictions = [session.infer(decision) for decision in decisions]
    import torch

    model = session.model.m
    projection = model.lm.model.layers[-1].mlp.down_proj
    if (not isinstance(projection, torch.nn.Linear)
            or projection.in_features != 6144 or projection.out_features != 2048):
        raise ValueError('adapter_runtime_architecture_changed')
    versions = [parameter._version for parameter in model.parameters()]
    down = torch.tensor(weights[:24576], dtype=torch.float32, device='cuda').reshape(4, 6144)
    up = torch.tensor(weights[24576:], dtype=torch.float32, device='cuda').reshape(2048, 4)
    hook_invocations = 0

    def inject(_module, inputs, output):
        nonlocal hook_invocations
        hook_invocations += 1
        correction = torch.nn.functional.linear(torch.nn.functional.linear(inputs[0].float(), down), up)
        return output + correction.to(output.dtype)

    handle = projection.register_forward_hook(inject)
    try:
        candidate_predictions = [session.infer(decision) for decision in decisions]
    finally:
        handle.remove()
    if (any(result['deployment_digest'] != session.deployment_digest
            for result in base_predictions + candidate_predictions)
            or hook_invocations < len(decisions)
            or versions != [parameter._version for parameter in model.parameters()]
            or any(parameter.grad is not None for parameter in model.parameters())):
        raise ValueError('adapter_runtime_model_changed')
    if manifest.read_bytes() != manifest_bytes or private_artifact(artifact) != payload:
        raise ValueError('adapter_runtime_sources_changed')
    verify_files(pins['model_path'], pins['model_files'])
    verify_files(pins['code_path'], pins['code_files'])
    base_metrics = [prediction_metrics(result['prediction'], option_ids, gold_option_id)
                    for result, option_ids, gold_option_id in zip(
                        base_predictions, options_by_decision, gold_option_ids)]
    candidate_metrics = [prediction_metrics(result['prediction'], option_ids, gold_option_id)
                         for result, option_ids, gold_option_id in zip(
                             candidate_predictions, options_by_decision, gold_option_ids)]
    split_results = {}
    position = 0
    for split in ('train', 'validation', 'test'):
        end = position + counts[split]
        split_results[split] = {
            'count': counts[split],
            'base_correct': sum(metric['selected_index'] == gold_indices[index]
                                for index, metric in enumerate(base_metrics[position:end], position)),
            'candidate_correct': sum(metric['selected_index'] == gold_indices[index]
                                     for index, metric in enumerate(candidate_metrics[position:end], position)),
        }
        position = end
    return {'mode': 'synthetic_adapter_runtime_probe_v1',
            'artifact_sha256': request['artifact_sha256'],
            'deployment_manifest_sha256': request['deployment_manifest_sha256'],
            'input_sha256': request['input_sha256'],
            'option_count': len(options_by_decision[0]), 'train_sample_index': 0,
            'base': base_metrics[0], 'candidate': candidate_metrics[0],
            'split_results': split_results,
            'adapter_loaded': True, 'adapter_hook_calls': hook_invocations,
            'base_parameters_unchanged': True,
            'active_deployment_changed': False}


def main():
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576 or len(sys.argv) != 3:
            raise ValueError('adapter_runtime_input_limit')
        report = run(json.loads(payload), Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Pinned synthetic adapter runtime probe failed; deployment unchanged.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

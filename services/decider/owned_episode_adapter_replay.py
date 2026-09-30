"""Fresh pinned-base replay of one private owned episode adapter candidate."""

import contextlib
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import random
import sys
from types import SimpleNamespace

from owned_episode_adapter_core import (
    TOKEN_BUDGET,
    checksum,
    private_manifest_pins,
    read_manifest,
    read_private_artifact,
    validate_artifact_binding,
    validate_request,
)
from worker import verify_files


def run(request, manifest: Path, artifact: Path) -> dict:
    validate_request(request, replay=True)
    manifest_bytes = read_manifest(manifest)
    pins, _expected = private_manifest_pins(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if request['deployment_manifest_sha256'] != manifest_sha:
        raise ValueError('owned_episode_adapter_deployment_manifest_changed')
    actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != pins['dependencies']:
        raise ValueError('owned_episode_adapter_environment_mismatch')
    model_root = verify_files(pins['model_path'], pins['model_files'])
    code_root = verify_files(pins['code_path'], pins['code_files'])
    payload = read_private_artifact(artifact)
    weights = validate_artifact_binding(payload, request, manifest_sha)
    artifact_sha = hashlib.sha256(payload).hexdigest()
    input_sha = checksum(request['examples'])
    sys.path.insert(0, str(code_root.parent))
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from decider.model import DecisionModel, collate
        from decider.prompt import build

        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('owned_episode_adapter_requires_8gib_free_cuda')
        model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to('cuda')
        versions = [parameter._version for parameter in model.parameters()]
        projection = model.lm.model.layers[-1].mlp.down_proj
        if not isinstance(projection, torch.nn.Linear) or projection.in_features != 6144 or projection.out_features != 2048:
            raise ValueError('owned_episode_adapter_architecture_changed')
        batches = []
        for record in request['examples']:
            question = SimpleNamespace(**record['qs'][0])
            example = SimpleNamespace(context=record['context'], qs=[question], task=record['task'])
            if len(model.tok.encode('Context:\n' + example.context, add_special_tokens=False)) > TOKEN_BUDGET:
                raise ValueError('owned_episode_adapter_context_limit')
            item = build(example, model.tok, rng=random.Random(42), layout='state_first',
                         max_ctx_tokens=TOKEN_BUDGET, max_options=10)
            if (len(item['ids']) > TOKEN_BUDGET
                    or item['perms'][0][item['golds'][0]] != question.gold):
                raise ValueError('owned_episode_adapter_gold_binding')
            batch = collate([item], model.tok.pad_token_id)
            if batch['input_ids'].shape[1] > TOKEN_BUDGET:
                raise ValueError('owned_episode_adapter_padding_limit')
            batches.append(batch)

        def evaluate():
            losses = []
            correct = 0
            for cpu_batch in batches:
                batch = {key: value.to('cuda') if torch.is_tensor(value) else value
                         for key, value in cpu_batch.items()}
                logits = model(batch)
                losses.append(torch.nn.functional.cross_entropy(logits, batch['golds']))
                correct += (logits.argmax(dim=-1) == batch['golds']).sum().item()
                del batch
            return torch.stack(losses).mean().item(), correct / len(batches)

        with torch.inference_mode():
            base_nll, base_accuracy = evaluate()
        down = torch.tensor(weights[:24576], dtype=torch.float32, device='cuda').reshape(4, 6144)
        up = torch.tensor(weights[24576:], dtype=torch.float32, device='cuda').reshape(2048, 4)

        def inject(_module, inputs, output):
            correction = torch.nn.functional.linear(
                torch.nn.functional.linear(inputs[0].float(), down), up)
            return output + correction.to(output.dtype)

        handle = projection.register_forward_hook(inject)
        with torch.inference_mode():
            candidate_nll, candidate_accuracy = evaluate()
        handle.remove()
        metrics = (base_nll, base_accuracy, candidate_nll, candidate_accuracy)
        if (any(not math.isfinite(value) or value < 0 for value in metrics)
                or not 0 <= base_accuracy <= 1 or not 0 <= candidate_accuracy <= 1
                or versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('owned_episode_adapter_replay_invalid')
    if read_manifest(manifest) != manifest_bytes or read_private_artifact(artifact) != payload:
        raise ValueError('owned_episode_adapter_replay_source_changed')
    verify_files(pins['model_path'], pins['model_files'])
    verify_files(pins['code_path'], pins['code_files'])
    return {
        'mode': 'owned_episode_adapter_replay_v1',
        'checkpoint_revision': pins['checkpoint_revision'],
        'code_revision': pins['code_revision'],
        'weights_sha256': pins['model_files']['model.safetensors'],
        'manifest_sha256': manifest_sha,
        'input_sha256': input_sha,
        'authorization_sha256': request['authorization_sha256'],
        'conversion_sha256': request['conversion_sha256'],
        'development_decisions': len(request['examples']),
        'token_budget': TOKEN_BUDGET,
        'artifact_sha256': artifact_sha,
        'base_development_nll': base_nll,
        'candidate_development_nll': candidate_nll,
        'base_development_accuracy': base_accuracy,
        'candidate_development_accuracy': candidate_accuracy,
        'candidate_loaded': True,
        'evaluation': 'resubstitution',
        'base_parameters_unchanged': True,
        'base_gradient_count': 0,
        'training_ready': False,
    }


def main():
    try:
        payload = sys.stdin.buffer.read(1_048_577)
        if len(payload) > 1_048_576 or len(sys.argv) != 3:
            raise ValueError('owned_episode_adapter_input_limit')
        request = json.loads(payload)
        if not isinstance(request, dict):
            raise ValueError('owned_episode_adapter_request_invalid')
        report = run(request, Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Owned episode adapter replay failed; no deployment or training authorization changed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

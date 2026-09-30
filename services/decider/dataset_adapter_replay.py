"""Independent pinned-model reload of one private synthetic S1 adapter candidate."""

import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import stat
import struct
import sys
from types import SimpleNamespace

from dataset_gradient_probe import ARTIFACT_BYTES, ARTIFACT_MAGIC, checksum
from worker import verify_files


ROOT = Path(__file__).resolve().parents[2]
SPLITS = ('train', 'validation', 'test')


def private_artifact(path: Path) -> bytes:
    if path.absolute().parent.parent != ROOT / 'data':
        raise ValueError('adapter_artifact_outside_private_data')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('adapter_artifact_parent_not_private')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size != ARTIFACT_BYTES):
                raise ValueError('adapter_artifact_not_private')
            payload = os.read(descriptor, ARTIFACT_BYTES + 1)
            if len(payload) != ARTIFACT_BYTES or os.fstat(descriptor).st_size != info.st_size:
                raise ValueError('adapter_artifact_changed')
            return payload
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def run(request: dict, manifest: Path, artifact: Path) -> dict:
    if (set(request) != {'mode', 'examples', 'split_counts', 'max_tokens', 'dataset_manifest_sha256',
                         'artifact_sha256', 'deployment_manifest_sha256'}
            or request['mode'] != 'dataset_s1_adapter_replay_v1'):
        raise ValueError('adapter_replay_request')
    counts = request['split_counts']
    budget = request['max_tokens']
    if (set(counts) != set(SPLITS) or any(type(value) is not int or not 0 <= value <= 32 for value in counts.values())
            or not 1 <= counts['train'] <= 4 or sum(counts.values()) != len(request['examples'])
            or type(budget) is not int or not 64 <= budget <= 1536):
        raise ValueError('adapter_replay_scope')
    manifest_bytes = manifest.read_bytes()
    pins = json.loads(manifest_bytes)
    expected = json.loads((ROOT / 'examples/dataset_converter_pin.json').read_text())
    if (pins['code_revision'] != expected['revision'] or pins['checkpoint_revision'] != pins['tokenizer_revision']
            or pins['code_files']['prompt.py'] != expected['files']['prompt.py']):
        raise ValueError('adapter_replay_pin_mismatch')
    actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != pins['dependencies']:
        raise ValueError('adapter_replay_environment_mismatch')
    model_root = verify_files(pins['model_path'], pins['model_files'])
    code_root = verify_files(pins['code_path'], pins['code_files'])
    payload = private_artifact(artifact)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    input_sha256 = checksum(request['examples'])
    if (payload[:8] != ARTIFACT_MAGIC or payload[8:40] != bytes.fromhex(request['dataset_manifest_sha256'])
            or payload[40:72] != bytes.fromhex(input_sha256)
            or payload[72:104] != bytes.fromhex(manifest_sha256)
            or struct.unpack_from('<III', payload, 104) != (4, 6144, 2048)
            or hashlib.sha256(payload).hexdigest() != request['artifact_sha256']
            or request['deployment_manifest_sha256'] != manifest_sha256):
        raise ValueError('adapter_replay_artifact_binding')
    weights = struct.unpack_from('<32768f', payload, 116)
    if not all(math.isfinite(value) for value in weights):
        raise ValueError('adapter_replay_artifact_values')
    sys.path.insert(0, str(code_root.parent))
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from decider.model import DecisionModel, collate
        from decider.prompt import build

        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('adapter_replay_requires_8gib_free_cuda')
        model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to('cuda')
        versions = [parameter._version for parameter in model.parameters()]
        projection = model.lm.model.layers[-1].mlp.down_proj
        if not isinstance(projection, torch.nn.Linear) or projection.in_features != 6144 or projection.out_features != 2048:
            raise ValueError('adapter_replay_architecture_changed')
        batches = {split: [] for split in SPLITS}
        position = 0
        for split in SPLITS:
            for record in request['examples'][position:position + counts[split]]:
                if (set(record) != {'context', 'qs', 'task'} or len(record['qs']) != 1
                        or not 2 <= len(record['qs'][0]['options']) <= 10):
                    raise ValueError('adapter_replay_example')
                example = SimpleNamespace(context=record['context'], qs=[SimpleNamespace(**question) for question in record['qs']], task=record['task'])
                if len(model.tok.encode('Context:\n' + example.context, add_special_tokens=False)) > budget:
                    raise ValueError('adapter_replay_context_limit')
                item = build(example, model.tok, rng=random.Random(42), layout='state_first', max_ctx_tokens=budget, max_options=10)
                if len(item['ids']) > budget or item['perms'][0][item['golds'][0]] != example.qs[0].gold:
                    raise ValueError('adapter_replay_gold_binding')
                batch = collate([item], model.tok.pad_token_id)
                if batch['input_ids'].shape[1] > budget:
                    raise ValueError('adapter_replay_padding_limit')
                batches[split].append({key: value.to('cuda') if torch.is_tensor(value) else value for key, value in batch.items()})
            position += counts[split]

        def evaluate(split):
            losses = []
            correct = 0
            for batch in batches[split]:
                logits = model(batch)
                losses.append(torch.nn.functional.cross_entropy(logits, batch['golds']))
                correct += (logits.argmax(dim=-1) == batch['golds']).sum().item()
            return torch.stack(losses).mean().item(), correct / len(batches[split])

        with torch.inference_mode():
            base_metrics = {split: evaluate(split) if counts[split] else (None, None) for split in SPLITS}
        down = torch.tensor(weights[:24576], dtype=torch.float32, device='cuda').reshape(4, 6144)
        up = torch.tensor(weights[24576:], dtype=torch.float32, device='cuda').reshape(2048, 4)

        def inject(_module, inputs, output):
            correction = torch.nn.functional.linear(torch.nn.functional.linear(inputs[0].float(), down), up)
            return output + correction.to(output.dtype)

        handle = projection.register_forward_hook(inject)
        with torch.inference_mode():
            candidate_metrics = {split: evaluate(split) if counts[split] else (None, None) for split in SPLITS}
        handle.remove()
        metrics = [value for result in (base_metrics, candidate_metrics)
                   for split in SPLITS for value in result[split] if value is not None]
        if (any(not math.isfinite(value) or value < 0 for value in metrics)
                or versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('adapter_replay_model_changed')
    if manifest.read_bytes() != manifest_bytes or private_artifact(artifact) != payload:
        raise ValueError('adapter_replay_source_changed')
    verify_files(pins['model_path'], pins['model_files'])
    verify_files(pins['code_path'], pins['code_files'])
    return {'mode': 'dataset_s1_adapter_replay_v1', 'checkpoint_revision': pins['checkpoint_revision'],
            'code_revision': pins['code_revision'], 'weights_sha256': pins['model_files']['model.safetensors'],
            'manifest_sha256': manifest_sha256, 'input_sha256': input_sha256,
            'artifact_sha256': hashlib.sha256(payload).hexdigest(), 'train_decisions': counts['train'],
            'token_budget': budget,
            'base_train_nll': base_metrics['train'][0], 'candidate_train_nll': candidate_metrics['train'][0],
            'base_train_accuracy': base_metrics['train'][1],
            'candidate_train_accuracy': candidate_metrics['train'][1],
            'validation_decisions': counts['validation'],
            'base_validation_nll': base_metrics['validation'][0],
            'candidate_validation_nll': candidate_metrics['validation'][0],
            'base_validation_accuracy': base_metrics['validation'][1],
            'candidate_validation_accuracy': candidate_metrics['validation'][1],
            'test_decisions': counts['test'],
            'base_test_nll': base_metrics['test'][0], 'candidate_test_nll': candidate_metrics['test'][0],
            'base_test_accuracy': base_metrics['test'][1],
            'candidate_test_accuracy': candidate_metrics['test'][1],
            'candidate_loaded': True, 'base_parameters_unchanged': True, 'base_gradient_count': 0,
            'training_ready': False}


def main():
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576 or len(sys.argv) != 3:
            raise ValueError('adapter_replay_input_limit')
        report = run(json.loads(payload), Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Pinned synthetic adapter replay failed; no deployment changed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

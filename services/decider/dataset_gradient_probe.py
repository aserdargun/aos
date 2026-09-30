"""Pinned, synthetic-only Decider gradient smoke without a candidate artifact."""

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
import time
from types import SimpleNamespace

from worker import verify_files


ROOT = Path(__file__).resolve().parents[2]
SPLITS = ('train', 'validation', 'test')
ARTIFACT_MAGIC = b'AOSLORA1'
ARTIFACT_BYTES = 8 + 3 * 32 + 3 * 4 + 32768 * 4


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def write_candidate(path: Path, payload: bytes):
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('adapter_output_not_private')
        descriptor = os.open(path.name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size != 0):
                raise ValueError('adapter_output_not_empty_private_file')
            position = 0
            while position < len(payload):
                position += os.write(descriptor, payload[position:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def run(request, manifest: Path, output: Path | None = None):
    candidate = output is not None
    expected_keys = {'mode', 'examples', 'split_counts', 'max_tokens'}
    if candidate:
        expected_keys.add('dataset_manifest_sha256')
    if (set(request) != expected_keys
            or request['mode'] != ('dataset_s1_adapter_candidate_v1' if candidate else 'dataset_s1_gradient_smoke_v1')
            or candidate and (not isinstance(request['dataset_manifest_sha256'], str)
                              or len(request['dataset_manifest_sha256']) != 64
                              or any(character not in '0123456789abcdef' for character in request['dataset_manifest_sha256']))):
        raise ValueError('gradient_probe_request')
    counts = request['split_counts']
    budget = request['max_tokens']
    if (set(counts) != set(SPLITS) or any(type(value) is not int or not 0 <= value <= 32 for value in counts.values())
            or not 1 <= counts['train'] <= 4 or sum(counts.values()) != len(request['examples'])
            or type(budget) is not int or not 64 <= budget <= 1536):
        raise ValueError('gradient_probe_scope')
    manifest_bytes = manifest.read_bytes()
    pins = json.loads(manifest_bytes)
    expected = json.loads((ROOT / 'examples/dataset_converter_pin.json').read_text())
    if (pins['code_revision'] != expected['revision'] or pins['checkpoint_revision'] != pins['tokenizer_revision']
            or pins['code_files']['prompt.py'] != expected['files']['prompt.py']):
        raise ValueError('gradient_probe_pin_mismatch')
    actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != pins['dependencies']:
        raise ValueError('gradient_probe_environment_mismatch')
    model_root = verify_files(pins['model_path'], pins['model_files'])
    code_root = verify_files(pins['code_path'], pins['code_files'])
    sys.path.insert(0, str(code_root.parent))
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from decider.model import DecisionModel, collate
        from decider.prompt import build

        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('gradient_probe_requires_8gib_free_cuda')
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to('cuda')
        versions = [parameter._version for parameter in model.parameters()]
        projection = model.lm.model.layers[-1].mlp.down_proj
        if not isinstance(projection, torch.nn.Linear) or projection.in_features != 6144 or projection.out_features != 2048:
            raise ValueError('gradient_probe_architecture_changed')

        class Adapter(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.down = torch.nn.Linear(6144, 4, bias=False, dtype=torch.float32, device='cuda')
                self.up = torch.nn.Linear(4, 2048, bias=False, dtype=torch.float32, device='cuda')
                torch.nn.init.normal_(self.down.weight, std=0.02)
                torch.nn.init.zeros_(self.up.weight)

            def forward(self, values):
                return self.up(self.down(values.float())).to(values.dtype)

        torch.manual_seed(42)
        adapter = Adapter()
        original_up = adapter.up.weight.detach().clone()

        def inject(_module, inputs, output):
            return output + adapter(inputs[0])

        handle = projection.register_forward_hook(inject)
        batches = []
        for record in request['examples'][:counts['train']]:
            if (set(record) != {'context', 'qs', 'task'} or len(record['qs']) != 1
                    or not 2 <= len(record['qs'][0]['options']) <= 10):
                raise ValueError('gradient_probe_example')
            example = SimpleNamespace(context=record['context'], qs=[SimpleNamespace(**question) for question in record['qs']], task=record['task'])
            if len(model.tok.encode('Context:\n' + example.context, add_special_tokens=False)) > budget:
                raise ValueError('gradient_probe_context_limit')
            item = build(example, model.tok, rng=random.Random(42), layout='state_first', max_ctx_tokens=budget, max_options=10)
            if (len(item['ids']) > budget or item['perms'][0][item['golds'][0]] != example.qs[0].gold):
                raise ValueError('gradient_probe_gold_binding')
            batch = collate([item], model.tok.pad_token_id)
            if batch['input_ids'].shape[1] > budget:
                raise ValueError('gradient_probe_padding_limit')
            batches.append({key: value.to('cuda') if torch.is_tensor(value) else value for key, value in batch.items()})

        def mean_loss():
            losses = [torch.nn.functional.cross_entropy(model(batch), batch['golds']) for batch in batches]
            return torch.stack(losses).mean()

        with torch.no_grad():
            before = mean_loss().item()
        optimizer = torch.optim.SGD(adapter.parameters(), lr=0.1)
        optimizer.zero_grad(set_to_none=True)
        mean_loss().backward()
        gradients = [parameter.grad for parameter in adapter.parameters()]
        if any(gradient is None or not torch.isfinite(gradient).all() for gradient in gradients):
            raise ValueError('gradient_probe_invalid_gradient')
        norm = torch.linalg.vector_norm(torch.stack([torch.linalg.vector_norm(gradient) for gradient in gradients])).item()
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError('gradient_probe_zero_gradient')
        torch.nn.utils.clip_grad_norm_(adapter.parameters(), max_norm=1.0)
        optimizer.step()
        with torch.no_grad():
            after = mean_loss().item()
        torch.cuda.synchronize()
        if (not math.isfinite(before) or not math.isfinite(after) or before < 0 or after < 0
                or torch.equal(original_up, adapter.up.weight.detach())
                or versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('gradient_probe_invalid_update')
        handle.remove()
        elapsed = time.perf_counter() - started
        report = {'mode': 'dataset_s1_gradient_smoke_v1', 'checkpoint_revision': pins['checkpoint_revision'],
                  'code_revision': pins['code_revision'], 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                  'weights_sha256': pins['model_files']['model.safetensors'], 'input_sha256': checksum(request['examples']),
                  'train_decisions': counts['train'], 'token_budget': budget, 'adapter_target': 'last_mlp_down_proj',
                  'adapter_rank': 4, 'adapter_parameter_count': sum(parameter.numel() for parameter in adapter.parameters()),
                  'seed': 42,
                  'optimizer_step_count': 1, 'train_nll_before': before, 'train_nll_after': after,
                  'gradient_norm': norm, 'peak_vram_allocated_bytes': torch.cuda.max_memory_allocated(),
                  'peak_vram_reserved_bytes': torch.cuda.max_memory_reserved(), 'elapsed_seconds': elapsed,
                  'base_parameters_unchanged': True, 'base_gradient_count': 0, 'adapter_parameters_changed': True,
                  'candidate_checkpoint_written': False, 'training_ready': False}
        if candidate:
            weights = (adapter.down.weight.detach().cpu().float().reshape(-1).tolist()
                       + adapter.up.weight.detach().cpu().float().reshape(-1).tolist())
            payload = (ARTIFACT_MAGIC + bytes.fromhex(request['dataset_manifest_sha256'])
                       + bytes.fromhex(checksum(request['examples'])) + hashlib.sha256(manifest_bytes).digest()
                       + struct.pack('<III', 4, 6144, 2048) + struct.pack('<32768f', *weights))
            if len(payload) != ARTIFACT_BYTES:
                raise ValueError('adapter_artifact_size')
            report.update(mode='dataset_s1_adapter_candidate_v1', candidate_checkpoint_written=True,
                          candidate_artifact_sha256=hashlib.sha256(payload).hexdigest(),
                          candidate_artifact_bytes=len(payload))
    if manifest.read_bytes() != manifest_bytes:
        raise ValueError('gradient_probe_manifest_changed')
    verify_files(pins['model_path'], pins['model_files'])
    verify_files(pins['code_path'], pins['code_files'])
    if candidate:
        write_candidate(output, payload)
    return report


def main():
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576 or len(sys.argv) not in (2, 3):
            raise ValueError('gradient_probe_input_limit')
        report = run(json.loads(payload), Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) == 3 else None)
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Pinned synthetic gradient smoke failed; no checkpoint or deployment changed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

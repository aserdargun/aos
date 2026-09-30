"""One-step isolated S1 adapter candidate for private owned episode development rows."""

import contextlib
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import random
import struct
import sys
import time
from types import SimpleNamespace

from owned_episode_adapter_core import (
    ARTIFACT_BYTES,
    ARTIFACT_MAGIC,
    TOKEN_BUDGET,
    checksum,
    private_manifest_pins,
    read_manifest,
    validate_request,
    validate_empty_candidate,
    write_private_candidate,
)
from worker import verify_files


def run(request, manifest: Path, output: Path):
    validate_request(request, replay=False)
    manifest_bytes = read_manifest(manifest)
    pins, _expected = private_manifest_pins(manifest_bytes)
    validate_empty_candidate(output)
    actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != pins['dependencies']:
        raise ValueError('owned_episode_adapter_environment_mismatch')
    model_root = verify_files(pins['model_path'], pins['model_files'])
    code_root = verify_files(pins['code_path'], pins['code_files'])
    sys.path.insert(0, str(code_root.parent))
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from decider.model import DecisionModel, collate
        from decider.prompt import build

        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('owned_episode_adapter_requires_8gib_free_cuda')
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to('cuda')
        versions = [parameter._version for parameter in model.parameters()]
        projection = model.lm.model.layers[-1].mlp.down_proj
        if not isinstance(projection, torch.nn.Linear) or projection.in_features != 6144 or projection.out_features != 2048:
            raise ValueError('owned_episode_adapter_architecture_changed')

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
        original_weights = [parameter.detach().clone() for parameter in adapter.parameters()]

        def inject(_module, inputs, output):
            return output + adapter(inputs[0])

        handle = projection.register_forward_hook(inject)
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
            for cpu_batch in batches:
                batch = {key: value.to('cuda') if torch.is_tensor(value) else value
                         for key, value in cpu_batch.items()}
                losses.append(torch.nn.functional.cross_entropy(model(batch), batch['golds']))
            return torch.stack(losses).mean().item()

        with torch.no_grad():
            before = evaluate()
        optimizer = torch.optim.SGD(adapter.parameters(), lr=0.1)
        optimizer.zero_grad(set_to_none=True)
        for cpu_batch in batches:
            batch = {key: value.to('cuda') if torch.is_tensor(value) else value
                     for key, value in cpu_batch.items()}
            loss = torch.nn.functional.cross_entropy(model(batch), batch['golds']) / len(batches)
            loss.backward()
            del batch
        gradients = [parameter.grad for parameter in adapter.parameters()]
        if any(gradient is None or not torch.isfinite(gradient).all() for gradient in gradients):
            raise ValueError('owned_episode_adapter_gradient_invalid')
        norm = torch.linalg.vector_norm(torch.stack([
            torch.linalg.vector_norm(gradient) for gradient in gradients])).item()
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError('owned_episode_adapter_gradient_zero')
        torch.nn.utils.clip_grad_norm_(adapter.parameters(), max_norm=1.0)
        optimizer.step()
        with torch.no_grad():
            after = evaluate()
        torch.cuda.synchronize()
        if (not math.isfinite(before) or not math.isfinite(after) or before < 0 or after < 0
                or all(torch.equal(previous, current.detach())
                       for previous, current in zip(original_weights, adapter.parameters()))
                or versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('owned_episode_adapter_update_invalid')
        handle.remove()
        elapsed = time.perf_counter() - started
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        examples_sha = checksum(request['examples'])
        weights = (adapter.down.weight.detach().cpu().float().reshape(-1).tolist()
                   + adapter.up.weight.detach().cpu().float().reshape(-1).tolist())
        payload = (ARTIFACT_MAGIC + bytes.fromhex(request['authorization_sha256'])
                   + bytes.fromhex(examples_sha) + bytes.fromhex(manifest_sha)
                   + struct.pack('<III', 4, 6144, 2048) + struct.pack('<32768f', *weights))
        if len(payload) != ARTIFACT_BYTES:
            raise ValueError('owned_episode_adapter_artifact_size')

    if read_manifest(manifest) != manifest_bytes:
        raise ValueError('owned_episode_adapter_manifest_changed')
    verify_files(pins['model_path'], pins['model_files'])
    verify_files(pins['code_path'], pins['code_files'])
    write_private_candidate(output, payload)
    return {
        'mode': 'owned_episode_adapter_train_v1',
        'checkpoint_revision': pins['checkpoint_revision'],
        'code_revision': pins['code_revision'],
        'weights_sha256': pins['model_files']['model.safetensors'],
        'manifest_sha256': manifest_sha,
        'input_sha256': examples_sha,
        'authorization_sha256': request['authorization_sha256'],
        'conversion_sha256': request['conversion_sha256'],
        'development_decisions': len(request['examples']),
        'token_budget': TOKEN_BUDGET,
        'adapter_target': 'last_mlp_down_proj',
        'adapter_rank': 4,
        'adapter_parameter_count': 32768,
        'seed': 42,
        'optimizer_step_count': 1,
        'learning_rate': 0.1,
        'development_nll_before': before,
        'development_nll_after': after,
        'gradient_norm': norm,
        'peak_vram_allocated_bytes': torch.cuda.max_memory_allocated(),
        'peak_vram_reserved_bytes': torch.cuda.max_memory_reserved(),
        'elapsed_seconds': elapsed,
        'base_parameters_unchanged': True,
        'base_gradient_count': 0,
        'adapter_parameters_changed': True,
        'candidate_checkpoint_written': True,
        'candidate_artifact_sha256': hashlib.sha256(payload).hexdigest(),
        'candidate_artifact_bytes': len(payload),
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
        sys.stderr.write('Owned episode adapter candidate failed; no deployment or training authorization changed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

"""Isolated frozen-Decider forward with one disposable scalar optimizer step."""

import contextlib
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace

from worker import verify_files


ROOT = Path(__file__).resolve().parents[2]


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def run(request, manifest: Path):
    if (set(request) != {'mode', 'examples', 'source_sha256', 'input_sha256',
                         'train_sample_ref', 'heldout_sample_ref', 'runner_sha256',
                         'token_budget', 'step_size'}
            or request['mode'] != 'synthetic_decider_transient_calibration_v1'
            or request['token_budget'] != 1536 or request['step_size'] != 0.05
            or set(request['examples']) != {'train', 'heldout'}
            or checksum(request['examples']) != request['input_sha256']
            or request['train_sample_ref'] == request['heldout_sample_ref']):
        raise ValueError('calibration_request_invalid')
    manifest_bytes = manifest.read_bytes()
    pins = json.loads(manifest_bytes)
    converter = json.loads((ROOT / 'examples/dataset_converter_pin.json').read_text())
    if (pins['code_revision'] != converter['revision']
            or pins['checkpoint_revision'] != pins['tokenizer_revision']
            or pins['code_files']['prompt.py'] != converter['files']['prompt.py']):
        raise ValueError('calibration_model_pin_mismatch')
    actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != pins['dependencies']:
        raise ValueError('calibration_environment_mismatch')
    model_root = verify_files(pins['model_path'], pins['model_files'])
    code_root = verify_files(pins['code_path'], pins['code_files'])
    sys.path.insert(0, str(code_root.parent))
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from decider.model import DecisionModel, collate
        from decider.prompt import build

        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('calibration_requires_8gib_free_cuda')
        model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to('cuda')
        versions = [parameter._version for parameter in model.parameters()]
        evaluated = {}
        with torch.inference_mode():
            for split in ('train', 'heldout'):
                record = request['examples'][split]
                if (set(record) != {'context', 'qs', 'task'} or len(record['qs']) != 1
                        or len(record['qs'][0]['options']) != 2):
                    raise ValueError('calibration_example_invalid')
                question = SimpleNamespace(**record['qs'][0])
                example = SimpleNamespace(context=record['context'], qs=[question], task=record['task'])
                if len(model.tok.encode('Context:\n' + example.context, add_special_tokens=False)) > 1536:
                    raise ValueError('calibration_context_limit')
                item = build(example, model.tok, rng=random.Random(42), layout='state_first',
                             max_ctx_tokens=1536, max_options=10)
                if (len(item['ids']) > 1536 or item['perms'][0][item['golds'][0]] != question.gold):
                    raise ValueError('calibration_gold_or_budget_mismatch')
                batch = collate([item], model.tok.pad_token_id)
                if batch['input_ids'].shape[1] > 1536:
                    raise ValueError('calibration_padding_limit')
                batch = {key: value.to('cuda') if torch.is_tensor(value) else value for key, value in batch.items()}
                logits = model(batch)
                valid = logits[0, :batch['nopts'][0].item()]
                if (not torch.isfinite(valid).all() or not torch.isneginf(logits[0, batch['nopts'][0].item():]).all()):
                    raise ValueError('calibration_logits_nonfinite')
                evaluated[split] = (valid.detach().cpu(), batch['golds'][0].item())
        torch.cuda.synchronize()
        if (versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('calibration_model_mutated')
        train_logits, train_gold = evaluated['train']
        heldout_logits, heldout_gold = evaluated['heldout']
        train_logits = train_logits.clone().detach()
        heldout_logits = heldout_logits.clone().detach()
        log_temperature = torch.nn.Parameter(torch.zeros((), dtype=torch.float32))
        optimizer = torch.optim.SGD([log_temperature], lr=0.05)

        def nll(logits, gold):
            return torch.nn.functional.cross_entropy((logits / log_temperature.exp()).unsqueeze(0),
                                                     torch.tensor([gold], dtype=torch.long))

        train_before = nll(train_logits, train_gold)
        heldout_before = nll(heldout_logits, heldout_gold)
        optimizer.zero_grad(set_to_none=True)
        train_before.backward()
        if log_temperature.grad is None or not torch.isfinite(log_temperature.grad):
            raise ValueError('calibration_gradient_nonfinite')
        gradient_abs = abs(log_temperature.grad.item())
        optimizer.step()
        train_after = nll(train_logits, train_gold)
        heldout_after = nll(heldout_logits, heldout_gold)
        temperature_after = log_temperature.exp().item()
        values = (train_before.item(), train_after.item(), heldout_before.item(),
                  heldout_after.item(), temperature_after)
        if (not all(torch.isfinite(torch.tensor(value)) for value in values)
                or temperature_after <= 0 or versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('calibration_result_nonfinite_or_model_mutated')
        if manifest.read_bytes() != manifest_bytes:
            raise ValueError('calibration_manifest_changed')
        verify_files(pins['model_path'], pins['model_files'])
        verify_files(pins['code_path'], pins['code_files'])
    return {
        'schema_version': '1.0', 'mode': 'synthetic_decider_transient_calibration_v1',
        'source_sha256': request['source_sha256'], 'input_sha256': request['input_sha256'],
        'deployment_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'runner_sha256': request['runner_sha256'],
        'checkpoint_revision': pins['checkpoint_revision'], 'code_revision': pins['code_revision'],
        'weights_sha256': pins['model_files']['model.safetensors'],
        'train_sample_ref': request['train_sample_ref'], 'heldout_sample_ref': request['heldout_sample_ref'],
        'token_budget': 1536, 'optimizer_step_count': 1,
        'train_nll_before': values[0], 'train_nll_after': values[1],
        'heldout_nll_before': values[2], 'heldout_nll_after': values[3],
        'scalar_gradient_abs': gradient_abs, 'temperature_before': 1.0,
        'temperature_after': temperature_after, 'model_parameters_unchanged': True,
        'model_gradient_count': 0, 'transient_scalar_only': True,
        'checkpoint_written': False, 'synthetic': True,
        'training_ready': False, 'promotion_authorized': False,
    }


def main():
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576 or len(sys.argv) != 2:
            raise ValueError('calibration_input_limit')
        report = run(json.loads(payload), Path(sys.argv[1]))
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Pinned transient calibration failed; no checkpoint or promotion performed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

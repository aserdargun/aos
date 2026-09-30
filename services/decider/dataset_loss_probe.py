import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import random
import sys
import time

from tokenizer_probe import checked_file
from worker import verify_files


ROOT = Path(__file__).resolve().parents[2]
SPLITS = ('train', 'validation', 'test')


def main():
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576:
            raise ValueError('dataset_loss_input_limit')
        request = json.loads(payload)
        counts = request['split_counts']
        budget = request['max_tokens']
        if (set(request) != {'mode', 'examples', 'split_counts', 'max_tokens'}
                or request['mode'] != 'dataset_s1_forward_only_v1' or set(counts) != set(SPLITS)
                or any(type(count) is not int or count < 0 for count in counts.values())
                or not 1 <= sum(counts.values()) == len(request['examples']) <= 32
                or type(budget) is not int or not 64 <= budget <= 1536):
            raise ValueError('dataset_loss_scope')
        manifest_bytes = Path(sys.argv[1]).read_bytes()
        pins = json.loads(manifest_bytes)
        expected = json.loads((ROOT / 'examples/dataset_converter_pin.json').read_text())
        if pins['code_revision'] != expected['revision'] or pins['checkpoint_revision'] != pins['tokenizer_revision']:
            raise ValueError('dataset_loss_pin_mismatch')
        actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
                  for distribution in importlib.metadata.distributions()}
        if actual != pins['dependencies']:
            raise ValueError('dataset_loss_environment_mismatch')
        model_root = verify_files(pins['model_path'], pins['model_files'])
        code_root = verify_files(pins['code_path'], pins['code_files'])
        core_path = checked_file(Path(sys.argv[2]), 'core.py', expected['files']['core.py'])
        if pins['code_files']['prompt.py'] != expected['files']['prompt.py']:
            raise ValueError('dataset_loss_prompt_mismatch')
        spec = importlib.util.spec_from_file_location('aos_dataset_loss_core', core_path)
        core = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = core
        spec.loader.exec_module(core)
        sys.path.insert(0, str(code_root.parent))
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from decider.model import DecisionModel, collate
            from decider.prompt import build

            if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
                raise ValueError('dataset_loss_requires_8gib_free_cuda')
            torch.cuda.reset_peak_memory_stats()
            started = time.perf_counter()
            model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to('cuda')
            versions = [parameter._version for parameter in model.parameters()]
            metrics = []
            offset = 0
            with torch.inference_mode():
                for split in SPLITS:
                    records = request['examples'][offset:offset + counts[split]]
                    offset += counts[split]
                    if not records:
                        continue
                    examples = [core.Example(context=value['context'], qs=[core.Q(**question) for question in value['qs']], task=value['task'])
                                for value in records]
                    if any(len(example.qs) != 1 for example in examples):
                        raise ValueError('dataset_loss_question_count')
                    for layout in ('state_first', 'schema_first'):
                        losses = []
                        correct = 0
                        padded = 0
                        for example in examples:
                            prefix = 'Context:\n' if layout == 'state_first' else ''
                            if len(model.tok.encode(prefix + example.context, add_special_tokens=False)) > budget:
                                raise ValueError('dataset_loss_context_limit')
                            item = build(example, model.tok, rng=random.Random(42), layout=layout, max_ctx_tokens=budget, max_options=10)
                            if len(item['ids']) > budget or item['perms'][0][item['golds'][0]] != example.qs[0].gold:
                                raise ValueError('dataset_loss_gold_binding')
                            batch = collate([item], model.tok.pad_token_id)
                            padded = max(padded, batch['input_ids'].shape[1])
                            if padded > budget:
                                raise ValueError('dataset_loss_padding_limit')
                            batch = {key: value.to('cuda') if torch.is_tensor(value) else value for key, value in batch.items()}
                            logits = model(batch)
                            valid = torch.arange(logits.shape[1], device='cuda')[None, :] < batch['nopts'][:, None]
                            probabilities = logits.softmax(dim=-1)
                            loss = torch.nn.functional.cross_entropy(logits, batch['golds'])
                            if (not torch.isfinite(logits[valid]).all() or not torch.isneginf(logits[~valid]).all()
                                    or not torch.isfinite(loss) or not torch.allclose(probabilities.sum(dim=-1), torch.ones(1, device='cuda'))
                                    or not torch.all(probabilities[~valid] == 0)):
                                raise ValueError('dataset_loss_invalid_outputs')
                            losses.append(loss.item())
                            correct += (logits.argmax(dim=-1) == batch['golds']).sum().item()
                        metrics.append({'split': split, 'layout': layout, 'decisions': len(examples), 'max_padded_tokens': padded,
                                        'mean_nll': sum(losses) / len(losses), 'correct_choices': correct})
            torch.cuda.synchronize()
            if versions != [parameter._version for parameter in model.parameters()] or any(parameter.grad is not None for parameter in model.parameters()):
                raise ValueError('dataset_loss_parameter_mutation')
            report = {'mode': 'dataset_s1_forward_only_v1', 'checkpoint_revision': pins['checkpoint_revision'],
                      'code_revision': pins['code_revision'], 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                      'weights_sha256': pins['model_files']['model.safetensors'],
                      'input_sha256': hashlib.sha256(json.dumps(request['examples'], sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
                      'token_budget': budget, 'split_metrics': metrics, 'elapsed_seconds': time.perf_counter() - started,
                      'peak_vram_allocated_bytes': torch.cuda.max_memory_allocated(), 'peak_vram_reserved_bytes': torch.cuda.max_memory_reserved(),
                      'weights_loaded': True, 'parameters_unchanged': True, 'gradient_run': False, 'optimizer_run': False, 'training_ready': False}
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Pinned dataset forward/loss failed; no training or optimizer performed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

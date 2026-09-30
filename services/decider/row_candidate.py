"""Frozen pinned Decider hidden states with one isolated option-row optimizer step."""

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

from worker import verify_files


ROOT = Path(__file__).resolve().parents[2]
MAGIC = b'AOSROW1\x00'


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def write_candidate(path: Path, payload: bytes):
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('candidate_output_not_private')
        descriptor = os.open(path.name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size != 0):
                raise ValueError('candidate_output_not_empty_private_file')
            position = 0
            while position < len(payload):
                position += os.write(descriptor, payload[position:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def run(request, manifest: Path, output: Path):
    if (set(request) != {'mode', 'examples', 'source_sha256', 'input_sha256',
                         'train_sample_ref', 'heldout_sample_ref', 'runner_sha256',
                         'token_budget', 'step_size'}
            or request['mode'] != 'synthetic_decider_option_rows_v1'
            or request['token_budget'] != 1536 or request['step_size'] != 0.001
            or set(request['examples']) != {'train', 'heldout'}
            or checksum(request['examples']) != request['input_sha256']
            or request['train_sample_ref'] == request['heldout_sample_ref']):
        raise ValueError('row_candidate_request_invalid')
    manifest_bytes = manifest.read_bytes()
    pins = json.loads(manifest_bytes)
    converter = json.loads((ROOT / 'examples/dataset_converter_pin.json').read_text())
    if (pins['code_revision'] != converter['revision']
            or pins['checkpoint_revision'] != pins['tokenizer_revision']
            or pins['code_files']['prompt.py'] != converter['files']['prompt.py']):
        raise ValueError('row_candidate_model_pin_mismatch')
    actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != pins['dependencies']:
        raise ValueError('row_candidate_environment_mismatch')
    model_root = verify_files(pins['model_path'], pins['model_files'])
    code_root = verify_files(pins['code_path'], pins['code_files'])
    sys.path.insert(0, str(code_root.parent))
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from decider.model import DecisionModel, collate
        from decider.prompt import build

        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('row_candidate_requires_8gib_free_cuda')
        model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to('cuda')
        versions = [parameter._version for parameter in model.parameters()]
        token_ids = model.letters[:2].detach().cpu().tolist()
        if len(set(token_ids)) != 2 or any(not 0 <= token_id < model.lm.lm_head.weight.shape[0]
                                            for token_id in token_ids):
            raise ValueError('row_candidate_option_tokens_invalid')
        base_rows = model.lm.lm_head.weight[model.letters[:2]].detach().cpu().float().clone()
        if base_rows.shape != (2, 2048) or not torch.isfinite(base_rows).all():
            raise ValueError('row_candidate_base_rows_invalid')
        hidden = {}
        with torch.inference_mode():
            for split in ('train', 'heldout'):
                record = request['examples'][split]
                if (set(record) != {'context', 'qs', 'task'} or len(record['qs']) != 1
                        or len(record['qs'][0]['options']) != 2):
                    raise ValueError('row_candidate_example_invalid')
                question = SimpleNamespace(**record['qs'][0])
                example = SimpleNamespace(context=record['context'], qs=[question], task=record['task'])
                if len(model.tok.encode('Context:\n' + example.context, add_special_tokens=False)) > 1536:
                    raise ValueError('row_candidate_context_limit')
                item = build(example, model.tok, rng=random.Random(42), layout='state_first',
                             max_ctx_tokens=1536, max_options=10)
                if (len(item['ids']) > 1536 or item['nopts'] != [2]
                        or item['perms'][0][item['golds'][0]] != question.gold):
                    raise ValueError('row_candidate_gold_or_budget_mismatch')
                batch = collate([item], model.tok.pad_token_id)
                if batch['input_ids'].shape[1] > 1536:
                    raise ValueError('row_candidate_padding_limit')
                batch = {key: value.to('cuda') if torch.is_tensor(value) else value for key, value in batch.items()}
                states = model.lm.model(input_ids=batch['input_ids'],
                                        attention_mask=batch['attention_mask']).last_hidden_state
                vector = states[batch['slot_batch'][0], batch['slot_idx'][0]].detach().cpu().float().clone()
                if vector.shape != (2048,) or not torch.isfinite(vector).all():
                    raise ValueError('row_candidate_hidden_invalid')
                hidden[split] = (vector, batch['golds'][0].item())
        hidden = {split: (vector.clone(), gold) for split, (vector, gold) in hidden.items()}
        torch.cuda.synchronize()
        if (versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('row_candidate_base_mutated')
        candidate_rows = torch.nn.Parameter(base_rows.clone())
        optimizer = torch.optim.SGD([candidate_rows], lr=0.001)

        def nll(split):
            vector, gold = hidden[split]
            logits = torch.nn.functional.linear(vector.unsqueeze(0), candidate_rows)
            return torch.nn.functional.cross_entropy(logits, torch.tensor([gold], dtype=torch.long))

        train_before = nll('train').item()
        heldout_before = nll('heldout').item()
        optimizer.zero_grad(set_to_none=True)
        nll('train').backward()
        if candidate_rows.grad is None or not torch.isfinite(candidate_rows.grad).all():
            raise ValueError('row_candidate_gradient_nonfinite')
        gradient_norm = torch.linalg.vector_norm(candidate_rows.grad).item()
        if not math.isfinite(gradient_norm) or gradient_norm <= 0:
            raise ValueError('row_candidate_gradient_zero_or_nonfinite')
        torch.nn.utils.clip_grad_norm_([candidate_rows], max_norm=1.0)
        optimizer.step()
        train_after = nll('train').item()
        heldout_after = nll('heldout').item()
        if (not all(math.isfinite(value) and value >= 0 for value in
                    (train_before, train_after, heldout_before, heldout_after))
                or torch.equal(candidate_rows.detach(), base_rows)
                or not torch.isfinite(candidate_rows).all()
                or versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())):
            raise ValueError('row_candidate_update_invalid')
        payload = (MAGIC + bytes.fromhex(request['source_sha256'])
                   + bytes.fromhex(request['input_sha256'])
                   + hashlib.sha256(manifest_bytes).digest()
                   + struct.pack('<II', *token_ids)
                   + struct.pack('<4096f', *candidate_rows.detach().reshape(-1).tolist()))
        if len(payload) != 16496:
            raise ValueError('row_candidate_payload_size')
        if manifest.read_bytes() != manifest_bytes:
            raise ValueError('row_candidate_manifest_changed')
        verify_files(pins['model_path'], pins['model_files'])
        verify_files(pins['code_path'], pins['code_files'])
        write_candidate(output, payload)
    return {
        'schema_version': '1.0', 'mode': 'synthetic_decider_option_rows_v1',
        'source_sha256': request['source_sha256'], 'input_sha256': request['input_sha256'],
        'deployment_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'runner_sha256': request['runner_sha256'],
        'checkpoint_revision': pins['checkpoint_revision'], 'code_revision': pins['code_revision'],
        'weights_sha256': pins['model_files']['model.safetensors'],
        'train_sample_ref': request['train_sample_ref'], 'heldout_sample_ref': request['heldout_sample_ref'],
        'token_ids': token_ids, 'token_budget': 1536, 'optimizer_step_count': 1,
        'train_nll_before': train_before, 'train_nll_after': train_after,
        'heldout_nll_before': heldout_before, 'heldout_nll_after': heldout_after,
        'gradient_norm': gradient_norm,
        'candidate_artifact_sha256': hashlib.sha256(payload).hexdigest(),
        'candidate_artifact_bytes': len(payload), 'candidate_format': 'aos-option-rows-v1',
        'model_parameters_unchanged': True, 'model_gradient_count': 0,
        'candidate_only': True, 'checkpoint_written': True,
        'synthetic': True, 'training_ready': False, 'promotion_authorized': False,
    }


def main():
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576 or len(sys.argv) != 3:
            raise ValueError('row_candidate_input_limit')
        report = run(json.loads(payload), Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Pinned synthetic row candidate failed; no deployment or promotion changed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

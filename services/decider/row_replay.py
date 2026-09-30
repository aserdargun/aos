"""Read-only pinned Decider candidate-row replay on one synthetic held-out example."""

import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import struct
import sys
from types import SimpleNamespace

from worker import verify_files


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_BYTES = 16496
ARTIFACT_MAGIC = b'AOSROW1\x00'


def candidate_bytes(path: Path, token_ids: list[int], *, source_sha256: str,
                    input_sha256: str, manifest_sha256: str) -> bytes:
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('row_replay_directory_not_private')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size != ARTIFACT_BYTES):
                raise ValueError('row_replay_file_not_private')
            payload = os.read(descriptor, ARTIFACT_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
            if (len(payload) != ARTIFACT_BYTES
                    or any(getattr(before, field) != getattr(value, field)
                           for value in (after, linked) for field in fields)):
                raise ValueError('row_replay_file_changed')
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    if (payload[:8] != ARTIFACT_MAGIC
            or payload[8:40] != bytes.fromhex(source_sha256)
            or payload[40:72] != bytes.fromhex(input_sha256)
            or payload[72:104] != bytes.fromhex(manifest_sha256)
            or list(struct.unpack_from('<II', payload, 104)) != token_ids
            or any(not math.isfinite(value) for (value,) in struct.iter_unpack('<f', payload[112:]))):
        raise ValueError('row_replay_artifact_identity')
    return payload


def run(request: dict, manifest: Path, artifact: Path) -> dict:
    expected = {'mode', 'source_sha256', 'input_sha256', 'deployment_manifest_sha256',
                'artifact_sha256', 'runner_sha256', 'heldout_sample_ref', 'heldout', 'token_ids'}
    if (not isinstance(request, dict) or set(request) != expected
            or request['mode'] != 'synthetic_decider_option_rows_replay_v1'
            or any(not isinstance(request[key], str) or re.fullmatch('[a-f0-9]{64}', request[key]) is None
                   for key in expected - {'mode', 'heldout', 'token_ids'})
            or not isinstance(request['token_ids'], list) or len(request['token_ids']) != 2
            or any(type(token_id) is not int or not 0 <= token_id < 248320
                   for token_id in request['token_ids'])
            or len(set(request['token_ids'])) != 2):
        raise ValueError('row_replay_request_invalid')
    manifest_bytes = manifest.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != request['deployment_manifest_sha256']:
        raise ValueError('row_replay_manifest_changed')
    payload = candidate_bytes(artifact, request['token_ids'],
                              source_sha256=request['source_sha256'],
                              input_sha256=request['input_sha256'],
                              manifest_sha256=request['deployment_manifest_sha256'])
    if (len(payload) != ARTIFACT_BYTES
            or hashlib.sha256(payload).hexdigest() != request['artifact_sha256']):
        raise ValueError('row_replay_artifact_changed')
    pins = json.loads(manifest_bytes)
    converter = json.loads((ROOT / 'examples/dataset_converter_pin.json').read_text())
    if (pins['code_revision'] != converter['revision']
            or pins['checkpoint_revision'] != pins['tokenizer_revision']
            or pins['code_files']['prompt.py'] != converter['files']['prompt.py']):
        raise ValueError('row_replay_model_pin_mismatch')
    actual = {distribution.metadata['Name'].lower().replace('_', '-'): distribution.version
              for distribution in importlib.metadata.distributions()}
    if actual != pins['dependencies']:
        raise ValueError('row_replay_environment_mismatch')
    model_root = verify_files(pins['model_path'], pins['model_files'])
    code_root = verify_files(pins['code_path'], pins['code_files'])
    sys.path.insert(0, str(code_root.parent))
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from decider.model import DecisionModel, collate
        from decider.prompt import build

        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('row_replay_requires_8gib_free_cuda')
        model = DecisionModel(str(model_root), dtype=torch.bfloat16,
                              grad_ckpt=False).eval().requires_grad_(False).to('cuda')
        versions = [parameter._version for parameter in model.parameters()]
        token_ids = model.letters[:2].detach().cpu().tolist()
        if token_ids != request['token_ids']:
            raise ValueError('row_replay_option_tokens_changed')
        record = request['heldout']
        if (not isinstance(record, dict) or set(record) != {'context', 'qs', 'task'}
                or not isinstance(record['qs'], list) or len(record['qs']) != 1
                or not isinstance(record['qs'][0], dict)
                or not isinstance(record['qs'][0].get('options'), list)
                or len(record['qs'][0]['options']) != 2):
            raise ValueError('row_replay_heldout_invalid')
        question = SimpleNamespace(**record['qs'][0])
        example = SimpleNamespace(context=record['context'], qs=[question], task=record['task'])
        if len(model.tok.encode('Context:\n' + example.context, add_special_tokens=False)) > 1536:
            raise ValueError('row_replay_context_limit')
        item = build(example, model.tok, rng=random.Random(42), layout='state_first',
                     max_ctx_tokens=1536, max_options=10)
        if (len(item['ids']) > 1536 or item['nopts'] != [2]
                or item['perms'][0][item['golds'][0]] != question.gold):
            raise ValueError('row_replay_gold_or_budget_mismatch')
        batch = collate([item], model.tok.pad_token_id)
        if batch['input_ids'].shape[1] > 1536:
            raise ValueError('row_replay_padding_limit')
        batch = {key: value.to('cuda') if torch.is_tensor(value) else value
                 for key, value in batch.items()}
        with torch.inference_mode():
            hidden = model.lm.model(input_ids=batch['input_ids'],
                                    attention_mask=batch['attention_mask']).last_hidden_state
            vector = hidden[batch['slot_batch'][0], batch['slot_idx'][0]].cpu().float()
            base_rows = model.lm.lm_head.weight[model.letters[:2]].cpu().float()
            candidate_rows = torch.tensor(struct.unpack_from('<4096f', payload, 112),
                                          dtype=torch.float32).reshape(2, 2048)
            if (vector.shape != (2048,) or base_rows.shape != (2, 2048)
                    or not torch.isfinite(vector).all() or not torch.isfinite(base_rows).all()
                    or not torch.isfinite(candidate_rows).all()):
                raise ValueError('row_replay_nonfinite_vectors')
            gold = torch.tensor([batch['golds'][0].item()], dtype=torch.long)
            base_logits = torch.nn.functional.linear(vector.unsqueeze(0), base_rows)
            candidate_logits = torch.nn.functional.linear(vector.unsqueeze(0), candidate_rows)
            base_nll = torch.nn.functional.cross_entropy(base_logits, gold).item()
            candidate_nll = torch.nn.functional.cross_entropy(candidate_logits, gold).item()
            base_choice = base_logits.argmax(dim=-1).item()
            candidate_choice = candidate_logits.argmax(dim=-1).item()
        if (not all(math.isfinite(value) and value >= 0 for value in (base_nll, candidate_nll))
                or versions != [parameter._version for parameter in model.parameters()]
                or any(parameter.grad is not None for parameter in model.parameters())
                or manifest.read_bytes() != manifest_bytes
                or candidate_bytes(artifact, token_ids,
                                   source_sha256=request['source_sha256'],
                                   input_sha256=request['input_sha256'],
                                   manifest_sha256=request['deployment_manifest_sha256']) != payload):
            raise ValueError('row_replay_source_or_model_changed')
        verify_files(pins['model_path'], pins['model_files'])
        verify_files(pins['code_path'], pins['code_files'])
    return {'schema_version': '1.0', 'mode': request['mode'],
            'source_sha256': request['source_sha256'], 'input_sha256': request['input_sha256'],
            'deployment_manifest_sha256': request['deployment_manifest_sha256'],
            'artifact_sha256': request['artifact_sha256'], 'runner_sha256': request['runner_sha256'],
            'checkpoint_revision': pins['checkpoint_revision'], 'code_revision': pins['code_revision'],
            'weights_sha256': pins['model_files']['model.safetensors'],
            'heldout_sample_ref': request['heldout_sample_ref'], 'token_ids': token_ids,
            'heldout_base_nll': base_nll, 'heldout_candidate_nll': candidate_nll,
            'heldout_base_choice': base_choice, 'heldout_candidate_choice': candidate_choice,
            'candidate_loaded': True, 'base_parameters_unchanged': True,
            'synthetic': True, 'training_ready': False, 'promotion_authorized': False}


def main() -> None:
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576 or len(sys.argv) != 3:
            raise ValueError('row_replay_input_limit')
        report = run(json.loads(payload), Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False))
    except Exception:
        sys.stderr.write('Pinned synthetic row replay failed; no deployment or promotion changed.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

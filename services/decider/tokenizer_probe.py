import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import random
import sys


ROOT = Path(__file__).resolve().parents[2]


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_file(root, name, expected):
    path = root / name
    if path.is_symlink() or not path.is_file() or checksum(path) != expected:
        raise ValueError("tokenizer_probe_pin_mismatch")
    return path


def probe(pins, source, examples, max_tokens):
    expected = json.loads((ROOT / "examples/dataset_converter_pin.json").read_text())
    if pins["code_revision"] != expected["revision"] or pins["checkpoint_revision"] != pins["tokenizer_revision"]:
        raise ValueError("tokenizer_probe_revision_mismatch")
    for key in ("checkpoint_revision", "tokenizer_revision", "code_revision"):
        if len(pins[key]) != 40 or any(character not in "0123456789abcdef" for character in pins[key]):
            raise ValueError("tokenizer_probe_revision_mismatch")
    dependencies = {name: importlib.metadata.version(name) for name in ("torch", "transformers", "tokenizers")}
    if any(pins["dependencies"].get(name) != value for name, value in dependencies.items()):
        raise ValueError("tokenizer_probe_dependency_mismatch")
    model_root = Path(pins["model_path"]).resolve(strict=True)
    code_root = Path(pins["code_path"]).resolve(strict=True)
    names = ("tokenizer.json", "tokenizer_config.json", "config.json", "chat_template.jinja")
    for name in names:
        checked_file(model_root, name, pins["model_files"][name])
    for name in ("__init__.py", "model.py", "prompt.py"):
        checked_file(code_root, name, pins["code_files"][name])
    if pins["code_files"]["prompt.py"] != expected["files"]["prompt.py"]:
        raise ValueError("tokenizer_probe_prompt_mismatch")
    core_path = checked_file(source, "core.py", expected["files"]["core.py"])
    spec = importlib.util.spec_from_file_location("aos_tokenizer_core", core_path)
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)
    sys.path.insert(0, str(code_root.parent))
    import torch
    from transformers import AutoTokenizer
    from decider.model import collate
    from decider.prompt import build, letter_ids

    tokenizer = AutoTokenizer.from_pretrained(model_root, local_files_only=True, trust_remote_code=False)
    labels = letter_ids(tokenizer)
    if len(labels) != 255 or len(set(labels)) != 255 or tokenizer.pad_token_id is None:
        raise ValueError("tokenizer_label_or_padding_mismatch")
    stats = []
    rows = [core.Example(context=value["context"], qs=[core.Q(**question) for question in value["qs"]], task=value["task"])
            for value in examples]
    if not rows or len(rows) > 32 or not 64 <= max_tokens <= 1536:
        raise ValueError("tokenizer_probe_input_limit")
    batches = 0
    max_padded = 0
    for layout in ("state_first", "schema_first"):
        for seed in range(20):
            items = []
            for index, example in enumerate(rows):
                if len(example.qs) != 1 or not 2 <= len(example.qs[0].options) <= 10 or not 0 <= example.qs[0].gold < len(example.qs[0].options):
                    raise ValueError("tokenizer_probe_invalid_choice")
                prefix = "Context:\n" if layout == "state_first" else ""
                context_size = len(tokenizer.encode(prefix + example.context, add_special_tokens=False))
                if context_size > max_tokens:
                    raise ValueError("tokenizer_context_would_truncate")
                item = build(example, tokenizer, rng=random.Random(seed), layout=layout, max_ctx_tokens=max_tokens, max_options=10)
                if len(item["ids"]) > max_tokens:
                    raise ValueError("tokenizer_full_prompt_over_budget")
                question = example.qs[0]
                if (sorted(item["perms"][0]) != list(range(len(question.options)))
                        or item["perms"][0][item["golds"][0]] != question.gold or item["slots"] != [len(item["ids"]) - 1]):
                    raise ValueError("tokenizer_gold_or_slot_mismatch")
                changed = core.Example(context=example.context, qs=[core.Q(question.text, question.options, (question.gold + 1) % len(question.options))], task=example.task)
                if build(changed, tokenizer, rng=random.Random(seed), layout=layout, max_ctx_tokens=max_tokens)["ids"] != item["ids"]:
                    raise ValueError("tokenizer_target_leaked")
                if not tokenizer.decode(item["ids"]).endswith("Answer: ("):
                    raise ValueError("tokenizer_answer_slot_changed")
                stats.append({"example": index, "layout": layout, "seed": seed, "context_tokens": context_size,
                              "input_tokens": len(item["ids"]), "tokens_sha256": hashlib.sha256(json.dumps(item["ids"], separators=(",", ":")).encode()).hexdigest()})
                items.append(item)
            batch = collate(items, tokenizer.pad_token_id)
            if batch["input_ids"].shape[1] > max_tokens:
                raise ValueError("tokenizer_padded_batch_over_budget")
            for index, item in enumerate(items):
                length = len(item["ids"])
                if (batch["input_ids"][index, :length].tolist() != item["ids"]
                        or not torch.all(batch["attention_mask"][index, :length] == 1)
                        or not torch.all(batch["attention_mask"][index, length:] == 0)
                        or not torch.all(batch["input_ids"][index, length:] == tokenizer.pad_token_id)
                        or batch["slot_idx"][index].item() != item["slots"][0]
                        or batch["slot_batch"][index].item() != index or batch["golds"][index].item() != item["golds"][0]
                        or batch["nopts"][index].item() != item["nopts"][0]):
                    raise ValueError("tokenizer_batch_mismatch")
            max_padded = max(max_padded, batch["input_ids"].shape[1])
            batches += 1
    if torch.cuda.is_initialized():
        raise ValueError("tokenizer_probe_unexpected_cuda")
    return {"schema_version": "1.0", "probe": "decider_real_tokenizer_v1", "synthetic": True, "training_ready": False,
            "weights_loaded": False, "loss_verified": False, "cuda_initialized": False,
            "checkpoint_revision": pins["checkpoint_revision"], "tokenizer_revision": pins["tokenizer_revision"],
            "code_revision": pins["code_revision"], "dependencies": dependencies,
            "tokenizer_files": {name: pins["model_files"][name] for name in names},
            "code_files": {name: pins["code_files"][name] for name in ("__init__.py", "model.py", "prompt.py")},
            "core_sha256": expected["files"]["core.py"], "examples": len(rows), "variants": len(stats), "batches": batches,
            "max_context_tokens": max(item["context_tokens"] for item in stats),
            "max_input_tokens": max(item["input_tokens"] for item in stats), "max_padded_tokens": max_padded,
            "token_budget": max_tokens, "variant_digest": hashlib.sha256(json.dumps(stats, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def main():
    try:
        payload = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("tokenizer_probe_input_limit")
        request = json.loads(payload)
        with contextlib.redirect_stdout(sys.stderr):
            result = probe(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]), request["examples"], request["max_tokens"])
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except Exception:
        sys.stderr.write("Pinned tokenizer/batch preflight failed; no training performed.\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

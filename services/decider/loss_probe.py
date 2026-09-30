import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import math
from pathlib import Path
import random
import sys
import time

from worker import verify_files
from tokenizer_probe import checked_file


ROOT = Path(__file__).resolve().parents[2]


def main():
    try:
        payload = sys.stdin.buffer.read(65537)
        if len(payload) > 65536:
            raise ValueError("loss_probe_input_limit")
        request = json.loads(payload)
        if request["mode"] != "synthetic_forward_only" or len(request["examples"]) != 4:
            raise ValueError("loss_probe_scope")
        pins = json.loads(Path(sys.argv[1]).read_text())
        expected = json.loads((ROOT / "examples/dataset_converter_pin.json").read_text())
        if pins["code_revision"] != expected["revision"] or pins["checkpoint_revision"] != pins["tokenizer_revision"]:
            raise ValueError("loss_probe_pin_mismatch")
        actual = {distribution.metadata["Name"].lower().replace("_", "-"): distribution.version
                  for distribution in importlib.metadata.distributions()}
        if actual != pins["dependencies"]:
            raise ValueError("loss_probe_environment_mismatch")
        model_root = verify_files(pins["model_path"], pins["model_files"])
        code_root = verify_files(pins["code_path"], pins["code_files"])
        core_path = checked_file(Path(sys.argv[2]), "core.py", expected["files"]["core.py"])
        if pins["code_files"]["prompt.py"] != expected["files"]["prompt.py"]:
            raise ValueError("loss_probe_prompt_mismatch")
        spec = importlib.util.spec_from_file_location("aos_loss_core", core_path)
        core = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = core
        spec.loader.exec_module(core)
        sys.path.insert(0, str(code_root.parent))
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from decider.model import DecisionModel, collate
            from decider.prompt import build

            if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
                raise ValueError("loss_probe_requires_8gib_free_cuda")
            torch.cuda.reset_peak_memory_stats()
            started = time.perf_counter()
            model = DecisionModel(str(model_root), dtype=torch.bfloat16, grad_ckpt=False).eval().requires_grad_(False).to("cuda")
            versions = [parameter._version for parameter in model.parameters()]
            examples = [core.Example(context=value["context"], qs=[core.Q(**question) for question in value["qs"]], task=value["task"])
                        for value in request["examples"]]
            layouts = []
            with torch.inference_mode():
                for layout in ("state_first", "schema_first"):
                    items = []
                    for example in examples:
                        prefix = "Context:\n" if layout == "state_first" else ""
                        if len(model.tok.encode(prefix + example.context, add_special_tokens=False)) > 1536:
                            raise ValueError("loss_probe_context_limit")
                        item = build(example, model.tok, rng=random.Random(42), layout=layout, max_ctx_tokens=1536, max_options=10)
                        if len(item["ids"]) > 1536 or item["perms"][0][item["golds"][0]] != example.qs[0].gold:
                            raise ValueError("loss_probe_input_binding")
                        items.append(item)
                    batch = collate(items, model.tok.pad_token_id)
                    if batch["input_ids"].shape[1] > 1536:
                        raise ValueError("loss_probe_padding_limit")
                    batch = {key: value.to("cuda") if torch.is_tensor(value) else value for key, value in batch.items()}
                    logits = model(batch)
                    valid = torch.arange(logits.shape[1], device="cuda")[None, :] < batch["nopts"][:, None]
                    if not torch.isfinite(logits[valid]).all() or not torch.isneginf(logits[~valid]).all():
                        raise ValueError("loss_probe_logits_invalid")
                    loss = torch.nn.functional.cross_entropy(logits, batch["golds"], reduction="none")
                    probabilities = logits.softmax(dim=-1)
                    if (not torch.isfinite(loss).all() or not torch.allclose(probabilities.sum(dim=-1), torch.ones(len(examples), device="cuda"))
                            or not torch.all(probabilities[~valid] == 0)):
                        raise ValueError("loss_probe_probability_or_loss_invalid")
                    layouts.append({"layout": layout, "decisions": len(examples), "padded_tokens": batch["input_ids"].shape[1],
                                    "mean_nll": loss.mean().item(), "correct_choices": (logits.argmax(dim=-1) == batch["golds"]).sum().item()})
            torch.cuda.synchronize()
            if versions != [parameter._version for parameter in model.parameters()] or any(parameter.grad is not None for parameter in model.parameters()):
                raise ValueError("loss_probe_parameter_mutation")
            elapsed = time.perf_counter() - started
            report = {"schema_version": "1.0", "probe": "decider_forward_loss_v1", "synthetic": True,
                      "training_ready": False, "weights_loaded": True, "gradient_run": False, "optimizer_run": False,
                      "parameters_unchanged": True, "checkpoint_revision": pins["checkpoint_revision"], "code_revision": pins["code_revision"],
                      "manifest_sha256": hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest(),
                      "weights_sha256": pins["model_files"]["model.safetensors"], "layouts": layouts,
                      "peak_vram_allocated_bytes": torch.cuda.max_memory_allocated(), "peak_vram_reserved_bytes": torch.cuda.max_memory_reserved(),
                      "elapsed_seconds": elapsed}
            if not math.isfinite(elapsed):
                raise ValueError("loss_probe_metrics_invalid")
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except Exception:
        sys.stderr.write("Pinned synthetic forward/loss preflight failed; no optimizer or training performed.\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

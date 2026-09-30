# Experimental Laya System-1 comparison

Laya is an **opt-in candidate**, not the active Decider replacement. The current AOS execution threshold stays at `0.88`; no candidate-specific relaxation, automatic promotion, training, or managed-session change is permitted. The worker accepts only the locally pinned `convaiinnovations/laya-typed-decisions` revision `f9ab0b228f0fc0f14d873dbc99038f135c2da1b2` through Laya SDK `0.3.6`. It verifies model and SDK file hashes and dependency versions, requires CUDA, bounds inputs, and exits on a mismatched identity or malformed response. Its registry entry is disabled and `EXPERIMENTAL`.

This checkpoint is specialized for the upstream typed-decision workflows; upstream card accuracy is **not** an AOS score. Its confidence is not assumed calibrated to AOS's Decider threshold. The existing pinned Decider checkpoint and user-facing managed deployment remain unchanged.

## Reproduce privately

Install the candidate SDK only under ignored `models/`, use the existing CUDA model environment, and obtain the exact model revision and wheel hash before creating the manifest. The preparation command does not download or install dependencies:

```sh
/home/cachyos/.venv/bin/python scripts/prepare_laya_candidate.py \
  --model models/laya-typed-f9ab0b228f0fc0f14d873dbc99038f135c2da1b2 \
  --sdk models/laya-sdk-0.3.6 \
  --wheel /tmp/aos-laya-wheel/laya-0.3.6-py3-none-any.whl \
  --output models/laya-candidate-manifest.json
PYTHONPATH=src .venv/bin/python scripts/compare_decider_laya.py \
  --output data/laya-comparison-local
```

The output must be in ignored `data/`. Each model/task gets a fresh private workspace, trajectory database, and runtime. Models run sequentially; no simultaneous GPU residency. The tasks are the bounded local hello file, local Chromium form, and synthetic SAVE canvas. The last task uses **fixture System-2 scene generation** for both models to isolate the real System-1 choice; it is not a real Bonsai comparison. The report records model calls, selected options, policy results, actions, independent verification, elapsed time and PyTorch allocated peak VRAM. A separate two-choice probe repeats the same hello state without actions to distinguish cold and warm inference. A fresh output directory is required for every run. The script neither touches the managed session nor switches its deployment.

## Acceptance boundary

The comparison is a small synthetic local smoke test, not held-out accuracy, real-site competence, S2 quality, p95 latency or training readiness. A model choosing the intended option but falling below `0.88` must escalate rather than execute. Selecting the wrong option is a distinct failure even when the threshold blocks the action. Promotion requires a broader, independently reviewed held-out task set with the same authorization, calibration, safety, latency, memory, and rollback gates; a lower threshold must not be used merely to make a candidate pass.

Measured results and environment evidence are in [STATUS](STATUS.md).

# Per-model Unsloth LoRA / QLoRA preparation

**Implemented: four separate validated design recipes and an offline inspector.**
**Not implemented/verified: an authorized executable Unsloth training runner,
model-specific dataset converters, actual training, learned adapters or runtime
promotion.** LoRA/QLoRA are requested methods, not universal compatibility claims.

Each model in [MODEL_CANDIDATES](MODEL_CANDIDATES.md) owns a separate
`training/recipes/unsloth-<candidate_id>-v001.json`. Canonical validation is in
`schemas/unsloth_candidate_recipe.schema.json`. The inspector checks exact base
repository/revision, role, canonical dataset schema, custom-head requirements and
required acceptance gates. It produces no files, network calls or GPU work:

```sh
.venv/bin/python -m scripts.inspect_unsloth_candidate --model qwen35_4b_s1 --method qlora
.venv/bin/python -m scripts.inspect_unsloth_candidate --model gemma4_12b_s2 --method lora
.venv/bin/python -m scripts.inspect_unsloth_candidate --model qwen38_27b_s2 --method qlora
.venv/bin/python -m scripts.inspect_unsloth_candidate --model cloudflare_clef_s1 --method lora
```

Both method choices are representable for each model. Output always says
`training_ready=false`, with unresolved gates. Every model/method/run has a
separate private adapter namespace; no adapter stacking or cross-base reuse.
The environment needs `requirements-validation.txt`, not Unsloth or CUDA, to
inspect these designs. Unsupported model/method and cross-base recipes fail.

## Compatibility evidence — 3 October 2026

- [Qwen3.5 fine-tuning guide](https://unsloth.ai/docs/models/qwen3.5/fine-tune)
  provides upstream training guidance. This does not verify AOS's typed-choice
  converter, selected backend build, or training VRAM on this host.
- [Gemma 4 training guide](https://unsloth.ai/docs/models/gemma-4/train)
  includes the 12B family. Train from reviewed Safetensors, **not** the QAT GGUF
  inference artifact. A QLoRA loader uses a separately validated training-time
  quantization; QAT inference and QLoRA are not interchangeable.
- [Qwen3.8 guide](https://unsloth.ai/docs/models/qwen3.8) confirms local inference
  guidance; inference support alone does not prove this exact training recipe.
- [Clef model card](https://huggingface.co/Cloudflare/clef) describes a custom
  joint decision head. Unsloth training compatibility is **unverified**. Do not
  silently substitute base Qwen or causal-language-model SFT for Clef. Validate
  head preservation, loss, target modules, optimizer gradients, probability
  calibration and save/reload before any experiment.

## Data and artifact boundaries

S1 starts from canonical `system1_choice` data; S2 from `system2_supervisor`.
Reviewed data rights, redaction, verified outcomes, group/near-duplicate splits
and an untouched held-out set are mandatory. Canonical JSONL is **not** an Unsloth
trainer format: each role/model requires its own tested converter. No hidden
reasoning, raw private trajectory upload or implicit screenshot rights.

Base artifact, tokenizer/processor, custom code, Unsloth revision, dependencies,
dataset manifest and target modules remain unpinned (`null`) where unverified.
A future executable job must bind those identities, the finite training budget,
the existing Scientist GPU reservation and explicit human authorization. Inference
and training never create independent GPU ownership systems.

Adapter output must record the exact base revision/artifact hash, dataset/split
hash, method, rank/alpha, target modules, trainer/dependency identity and evaluation
report. Rank 8/alpha 16 are proposal values only. Measure training memory separately
from inference; quantization does not guarantee a 27B run fits 16 GB.

Reload and evaluate the adapter against the base on the same held-out tasks,
including safety, calibration/recovery, latency and cleanup. Use the existing
[promotion policy](EVALUATION_AND_PROMOTION.md); reject regressions and require
explicit promotion/rollback. A successful training smoke test is not learned
quality and does not activate the adapter.

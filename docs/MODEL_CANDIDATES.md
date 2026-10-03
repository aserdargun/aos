# Model alternatives — research catalog, not enabled deployments

Reviewed on 3 October 2026. The Models panel reads
[`config/model_candidates.json`](../config/model_candidates.json), validated by
[`model_candidates.schema.json`](../schemas/model_candidates.schema.json).
This is distinct from the runtime registry: no weights are bundled, no models
are downloaded or executed, and default Decider/Bonsai deployments are unchanged.
These are engineering candidates, not a measured ranking of the best models.

| Role / capacity | Candidate | Selection rationale / remaining acceptance |
|---|---|---|
| S1 / 16 GB target | [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B) | Tool use and non-thinking mode make it worth evaluating for bounded decisions; not a drop-in Decider replacement. |
| S2 / 16 GB target | [Gemma 4 12B IT](https://huggingface.co/google/gemma-4-12B-it) | Multimodal recovery/planning candidate with an [official QAT Q4_0 release](https://huggingface.co/google/gemma-4-12B-it-qat-q4_0-gguf). |
| S2 / larger-GPU reserve | [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) | Generative multimodal planning candidate; exact quantization, context and hardware review still required. |
| S1 / larger-GPU reserve | [Cloudflare Clef](https://huggingface.co/Cloudflare/clef) | Qwen3.8-27B-based typed decision model; probabilities over supplied options, not free-form generation. |

Cloudflare's new Jev-like model is **Clef**, not TypeSafe Jev. Cloudflare reports
Jev/SystemOne API compatibility and publishes local weights; its hosted Workers
AI service is a separate deployment choice. See the [official announcement](https://blog.cloudflare.com/clef-decision-models/).
Clef's custom joint head and loader require source review before execution.
API compatibility alone does not prove AOS probability, ownership, policy or
fencing compatibility. No private state is sent to a hosted provider.

Both reserve candidates remain independent entries: sharing a backbone does
not make a generative S2 and a typed S1 interchangeable. The catalog distinguishes
`role`, `execution_kind`, `capacity_tier`, immutable upstream revision, measured
resource fields and adapter acceptance. More hardware does not bypass admission.

## Capacity and backend boundaries

Gemma's upstream metadata lists 6,975,879,296 bytes for the Q4_0 file and
175,115,616 bytes for its multimodal companion. These are download sizes, **not
VRAM peaks**. Upstream LFS hashes are recorded, not claimed locally verified.
Qwen3.5-4B's quantized artifact is deliberately unset until selected and reviewed.
The larger models have no assumed 16 GB fit. Context, KV cache, vision buffers,
CUDA workspaces and other resident processes must be measured together.

Current PrismML/Bonsai and PyTorch/Decider pins do not automatically support any
candidate. A future model-specific adapter must preserve the existing
DecisionEngine/ModelRuntime boundaries. Generic S1 generation must resolve only
to supplied option IDs with validated structured results; do not invent Decider
confidence from generated text. Clef scores need per-question probability and
option-membership checks. S2 plans and vision outputs remain untrusted proposals
with independent readback, never direct tool execution authority.

## Activation checklist

Each candidate also has a separate [Unsloth LoRA/QLoRA design](UNSLOTH_CANDIDATES.md)
with base/role isolation and unresolved training gates. No trainer is launched.

1. Review license, exact artifact/tokenizer/processor/custom code, backend and
   dependency pins in a separate candidate environment. No `trust_remote_code`
   or package upgrades in the existing runtime as an automatic side effect.
2. Validate typed output, invalid/stale options, cancellation, worker shutdown,
   timeout and failed cleanup with CPU/mocks. No second GPU scheduler.
3. Obtain explicit bounded execution authority and a reservation from the existing
   Scientist scheduler. Scientist remains the sole joint-GPU test executor.
4. Compare matched tasks against the current role: independent success, errors,
   warm/cold p50/p95, peak VRAM, context budget, drain and cleanup. Separate
   upstream claims from AOS measurements; load sequentially when necessary.
5. Promote only an immutable tested deployment with explicit approval and rollback.
   A candidate catalog entry never becomes an enabled runtime by changing a name.

Qwen3.5-9B was also reviewed as a smaller multimodal comparison; Gemma 12B's
official quantized artifact provides a useful independent-family S2 candidate.
FunctionGemma and Nemotron Nano were considered, but training/format/license and
backend differences require separate assessment. No universal superiority is
claimed and no existing successful deployment is removed.

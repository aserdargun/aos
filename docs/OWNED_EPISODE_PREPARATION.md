# Owned episode conversion and readiness

This is the next explicit step after [owned episode review and local
export](OWNED_EPISODE_LEARNING.md). It creates source-bound local development
inputs, not a training dataset approval or model update. Real acceptance and
diagnostic failures are recorded in [STATUS](STATUS.md).

## Conversion contract

The original export manifest and both JSONL files must already exist. Their
canonical bytes, hashes, counts, candidate memberships, consent and current
role receipts are compared with freshly derived source content. Missing files
are rejected, not silently recreated by preview or readiness inspection. A
revoked receipt, changed source fingerprint or missing planning admission blocks
conversion and subsequent readiness inspection.

- **System 1:** the reviewed choice maps to the pinned upstream Decider
  `Example/Q` shape: `context`, one `qs` entry with `text`, ordered option labels
  and the selected option's zero-based `gold` index, plus the fixed task family.
  Reordering options changes the index correctly; option IDs, probabilities,
  request hash and selected membership are checked first. Targets and source
  metadata are not appended to the decision input.
- **System 2:** `aos-owned-plan-messages-v1` preserves the exact original native
  system/user messages and a separate structured reviewed `OwnedSkillPlan` target.
  Extra roles and incompatible model request/schema/goal/evidence bindings are
  rejected. It is not a recovery record or a proven Bonsai training format.

Converted rows retain exact source/candidate hashes and the conservative shared
fixture-family leakage group. The manifest binds the original export, execution
source fingerprint, two receipts, converter/helper/schema hashes, ordered
memberships and converted file hashes. All records remain `development_only`,
`synthetic: true`, `training_ready: false`; no train/validation/test partition is
invented. Repeated explicit publication is byte-identical.

## Tasks workflow

In a newly configured owned-reuse backend, complete an opted-in episode, inspect
its content, accept each role separately and export locally. Then:

1. Select **Preview conversion**. It does not create converted files.
2. Inspect the role formats and exact conversion hash. Check the explicit
   confirmation and select **Save conversion locally**.
3. If native prerequisites are present, separately select **Run isolated CPU
   tokenizer check**. This returns immediately with a pending status.
4. When the check finishes, select **Refresh readiness**. System-1 tokenizer
   verification is separate from System-2 compatibility and training readiness.

Scope/control changes clear local confirmation. Source errors clear stale
evidence instead of offering automatic retry. Existing ordinary sessions are
not upgraded or restarted automatically.

## Real tokenizer, not training

The worker checks **all** System-1 rows (1–32), with two prompt layouts and 20
seeds each using the actual pinned tokenizer, upstream Example/Q, prompt builder
and collator. It checks option/gold permutation, no target leakage into tokens,
answer slots, padding/masks and a 1536-token ceiling without silent truncation.
The process has a 120-second deadline, 1 MiB input bound and 64 KiB stdout bound.
No model weights are loaded, CUDA is disabled, and no optimizer, training,
download or network export is performed by this feature.

The backend uses its already configured native Decider manifest and Python,
not caller-provided executable paths. The source candidate deployment must match
that exact identity. Upstream `core.py` comes from the host-configured
`AOS_DECIDER_DATA_SOURCE`, or by default
`models/decider-data-75b00fade2dd7f353106e3f4683e56fa2481ec28/`.
Its bytes must match `examples/dataset_converter_pin.json`. Missing prerequisites
are reported as unavailable; there is no fallback tokenizer or automatic fetch.

CPU probing does not hold the console control lock. Pause, cancellation and
shutdown cancel and drain only the owned child. At publication, the original
source, export, conversion, runner, deployment and current lease/generation are
rechecked; a concurrent task or changed control rejects a late result. Old
episode inspection does not replace a newer live episode's collection state.

Private immutable report names include both conversion and runner hashes. Current
readiness revalidates the report against current pins and source; a historical
file alone cannot authorize training. Completed-report inspection does not load
a model or open a network connection. Converted files and reports remain in the
originating private episode directory and must not enter source manifests.

## API

Authenticated POSTs under `/api/tasks/owned-episode/` require
`schema_version: "1.0"` and `episode_id`:

| Operation | Additional fields |
| --- | --- |
| `conversion-preview` | `export_sha256` |
| `convert` | `export_sha256`, exact `confirm_sha256`, current `lease_id`, `generation` |
| `tokenizer-start` | `conversion_sha256`, matching `confirm_sha256`, current `lease_id`, `generation` |
| `readiness` | `conversion_sha256` |

Writes require idle current AGENT control; tokenizer start returns HTTP 202.
`GET /api/tasks` exposes only the content-free preparation state/pins. At most
one probe runs at a time, with eight starts per backend session. Readiness
reports distinct converter and tokenizer states, explicit blockers, and false
training/promotion flags. Invalid source/private schema failures are content-free.

## Still open

A separately authorized [experimental S1 adaptation](OWNED_EPISODE_ADAPTATION.md)
now follows verified tokenizer preparation. Its all-record development-cohort
gradient/reload comparison is not production trainer admission or independent
quality acceptance; current measured evidence is in STATUS. System-2 tokenizer,
loss mask, trainer/adapter compatibility, independent evaluation groups,
real-application rights/redaction review and promotion remain open. The existing
fixture dataset/trainer interfaces are not silently extended to accept these
records, and real-site W1–W6 gates remain separate.

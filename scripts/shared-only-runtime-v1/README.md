# Shared-only runtime promotion candidate

This unapplied candidate disables supported repository native/unbrokered GPU
model-effect entrypoints unconditionally. Missing configuration cannot select a
native fallback. It is **not** deployed, a cleanup receipt, native exclusion proof,
GPU release proof, reservation, or launch authority.

`manifest.json` pins every original and candidate file SHA-256, the patch bytes,
entrypoint policies, and deliberately unchanged Scientist/broker and CPU-only
preparation sources. Review every original hash before applying `source.patch.txt`
to a separate source checkout; `git apply --check` alone is not a full hash check.
The focused test applies this exact patch only to a disposable copy and verifies
all before/after hashes. Never apply it to the current live default session.

Native Decider one-shot/reusable, Bonsai calls, owned adapter/Laya/adaptation,
native CLI/managed start/supervise/restart, staged native entry, and direct GPU
probe/worker callables deny before effects. `serve_desktop` requires the existing
Scientist engine; explicit shared manager operations remain separate.
The ordinary desktop source's explicit CPU Lab fixture hook is also rejected
by this Scientist-only engine selection; it cannot bypass the promoted profile.
The candidate hashes/patch context include that source addition, without changing
the engine-selection policy or asserting new runtime acceptance. Native
`ModelSession` construction denies; the original implementation becomes
`BrokerModelSession`, explicitly imported only by the existing Decider broker
worker. Its TurnGate ordering and authority checks are unchanged.

CPU-only tokenizer/data preparation, artifact setup, metadata inspection and
read-only CLI export remain available. Their availability is not GPU exclusion
evidence. Original Scientist S1/S2 broker overrides remain unchanged, as do the
Bonsai broker and canonical broker runtime.

Promotion requires explicit maintenance consent, exact legacy process/container
cleanup, a fresh complete joint source/configuration review, and the existing
Scientist canonical reservation/current authority. Existing workers still execute
old bytes until safely retired. Do not automatically fall back to native or adopt
uncertain cleanup. Arbitrary owner code, direct vendor binaries and manual calls
to the internal broker class are outside repository-entrypoint coverage; this is
not OS-wide GPU isolation. No automatic patch application or restoration is supplied.

CPU validation: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_shared_only_runtime_candidate.py' -v`.

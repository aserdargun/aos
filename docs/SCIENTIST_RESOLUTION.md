# Original inference resolution

`ScientistResolutionJournal` closes a retained local inference fence only after
independent retained terminal verification and separate current resolution
authorization. It is not a GPU allocator, remote cancel operation or output
publisher. Existing Scientist scheduler ownership/fencing remains authoritative.

## Immutable record, not reset

Additive0026 creates `scientist_turn_resolutions` on the original store. Original
0018 intent identity, state, receipt, deadline and admission history bytes remain
unchanged. A successful resolution appends the exact original request/admission,
retained control ACK hash, current controller binding, terminal canonical bytes
and independently exported budget witness. Existing records cannot be updated
or deleted. Historical inspection does not call providers or refresh authority.

The old per-session all-row UNIQUE index becomes a same-session unresolved
anti-join insert guard. Merely recording a socket ACK cannot remove this fence.
Legacy databases without the resolution table retain their original all-row
unresolved interpretation. No migration fabricates resolutions for old rows.
Resolved request IDs cannot be reused; later inference receipt writes to them
are denied, including direct SQL. Each new request still needs fresh admission,
current control and an independently captured capability; no budget is renewed.

## Explicit authorized call

Construct `ScientistResolutionJournal(evidence_journal, verifier=...,
verify_resolution=...)` with the same original history2.0 and explicitly pinned
retained evidence-v3 codec. Both verification and separate resolution authority
are required; defaults deny. Call `resolve(control_id,
response_sha256=...)` only for an existing committed successful reconcile ACK.
No automatic polling or UI read triggers resolution.

Within one transaction, the journal checks exact original identity and ACK,
current AGENT/HUMAN owner/lease/generation, absence of pending same-target
controls, trusted current rights, independent source budget, result binding,
resolver and physical cleanup proof. It then appends and rechecks the original
records and current authorization before commit. A prior infer receipt must
match the completed terminal result; canceled evidence cannot erase it.
Failure/revoke rolls back the append and leaves admission blocked.

Exact repeat is idempotent only with the same retained ACK/record and fresh
authorization/full proof verification; it never repeats inference. Trusted
callbacks must not commit, roll back or otherwise change transaction ownership.
This boundary assumes the original database and injected trusted providers are
protected; privileged coordinated database tampering is not authenticated by
local hashes alone.

## Metadata and remaining acceptance

Startup uses the same unresolved predicate across all sessions. Inventory keeps
historical intents and resolution counts separately; UI labels them as history,
not new GPU authority. `joint_runtime_admitted` and `gpu_release_verified` are
not inferred from these counts or records.

The explicit `resolution_candidate_v3` selected-source profile includes 54 files.
It is source identity, not deployment or capability. Production rights/budget/
resolver/physical providers, abandoned-control/caller-transfer recovery and
Scientist-only coordinated real GPU acceptance remain separate gates. Synthetic
UDS/SQLite acceptance cannot establish actual Scientist or physical GPU success.

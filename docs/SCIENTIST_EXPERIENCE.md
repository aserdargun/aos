# Scientist experience readback and explicit history selection

Source implementation only. This is not a live deployment, reciprocal contract
ACK, real Scientist acceptance, training permission or GPU admission.

UI-only delivery note, 3 October 2026 18:20 UTC: the compatible frontend assets
were promoted without restarting the backend. The existing unconfigured
Scientist connection keeps new controls hidden. The experience backend remains
source-only; this static UI delivery is not actual experience acceptance.

## Reviewed producer, not an assumed deployment

The sibling checkout was observed at
`55c5300600112ab823f76ec434029d6dc23e513c`, with local changes. The actual producer
was inspected read-only in its separate prepared `aos-integration-v01` runtime
source. That prepared source must not be equated with the Git commit or running
service. Reviewed SHA-256 identities:

| Prepared source | SHA-256 |
|---|---|
| `lab/api/experience.py` | `892b555f74a0c117e6c24eba2d7eba600f6b60f0496cb3d7f42d3afd9bdd1d2e` |
| `lab/api/app.py` | `ca866cc50935c922886ea6b153ba82ef9907b79ea4e4b5dc02546c85be2e5f5a` |
| `lab/director/field_context.py` | `0ec90edff0a69bb0a3468d3295a1610598dd6a0347c97a7ef304beba8fb07c57` |
| `lab/director/history_context.py` | `35424cb64efaa23358be70beb4e77b8ea841e447ac9f70fed03baf2c91fea39f` |

The existing producer route is `GET /v1/runs/{run_id}/experience`, wire label
`run-experience.v1`. AOS's `scientist_experience.schema.json` is a strict local
consumer schema, not a newly negotiated shared schema. The local result uses
`aos.scientist-experience-readback.v1`. Inter-thread communication remains
unavailable; this document is available for independent read-only pickup.
Scientist must confirm a fresh source/configuration pair and authorized isolated
CPU scope before actual acceptance. Retired grants must not be reused.

## Authorized read path

Authenticated local `POST /api/scientist/experience` accepts only an existing
local AOS `run_id`. It cannot choose a remote URL, owner or arbitrary remote UUID.
The durable job must belong to the current controller session; existing typed
task, policy, principal, generation, capability and deadline checks apply.

The additive read-only `lab.experience` action performs four remote GETs:
terminal status, report, experience, status again. AOS independently verifies
the report content hash and binds experience to that exact run/report and the
original approved task. Authority is rechecked between reads and before return;
terminal status drift rejects the result. There is no new effect intent,
remote POST, experiment start/stop, approval or automatic retry.

The existing client exclusion and off-event-loop worker are reused. Cancellation
retains the cleanup handle until the worker actually finishes. Cleanup timeout
is not drain success. A read failure does not create an uncertain effect marker.
No new scheduler, ownership database or migration is introduced.

## Evidence boundaries

- Response is bounded to 512 KiB and 200 records. Unknown fields, duplicate JSON
  keys, non-finite scores, mismatched identities and duplicate records fail closed.
- AOS does **not** independently fetch/replay the experiment ledger or trajectory.
  `ledger_verification` explicitly says `scientist-reported-not-independently-replayed`.
- Field intent is compared with the original task and its returned context hash
  is recomputed. Prior-source run/report/count must match the original selection;
  the full prior-findings snapshot is not returned or independently rehashed.
- `admitted-only` and `context-bound` counts are distinct peer-reported facts,
  not proof of improved quality or independent verification of every trajectory.
- Training and holdout flags must remain false. A scored `DISCARD` may be useful
  measured history; it is not a successful improvement or eligible training data.

## Explicit UI selection

The EN/TR panel only exposes history readback when local inventory advertises
`experience_readback_supported`. It never automatically reads history. Records
start unselected; a new readback clears selection. Only eligible measured records
can be selected, at most eight. The explicit transfer button fills the existing
[prior-experience proposal draft](SCIENTIST_TASK_CONTEXT.md); it makes no request,
creates no proposal and never starts an experiment. A new proposal, current
authorization and separate approval remain necessary. Scientist rechecks the
references at admission, so UI selection cannot manufacture reuse authority.

The fixture `examples/scientist_experience.json` is explicitly synthetic and
contains no real experiment, private trajectory or credentials. Dated CPU/mock
and Chromium/mocked-API evidence is in [STATUS](STATUS.md). Actual user-console
acceptance, active-Scorer interruption and shared GPU acceptance remain open;
the Scientist session remains the sole integrated GPU test executor.

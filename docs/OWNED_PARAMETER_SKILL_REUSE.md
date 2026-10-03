# Separately authorized manual parameter skill reuse

A selected, human-reviewed synthetic manual release can compile a **new parameter
task**. This path does not cast manual bootstrap evidence into model learning,
train a model, promote a runtime or authorize arbitrary websites. Current host
admission supports only its already configured CPU-fixture parameter project.

## What actually executes

The original profile, task, recipe, field bindings, scope, certificate and
origin remain pinned. New bounded parameter values compile separate case inputs,
form/state plans and invocation. The original project files, bootstrap run,
candidate, review and release remain unchanged. Original parameters are rejected
rather than replaying the bootstrap.

Preview freshly validates the exact selected release, current review and original
independent bootstrap evidence. A separate confirmation binds the new parameters
and current AGENT lease/generation. A durable private execution intent is written
**before** preparing a child fixture or starting a task. The host retains the
original listener descriptor/identity; after the completed bootstrap server
closes, the new one-use fixture serves on that same owned origin/certificate.
There is no port rebinding, bootstrap reset or simultaneous fixture server.

A distinct child scheduler shares the authorized controller/store/CPU engine,
not mutable bootstrap configuration. The existing finite form operator and
policy/gateway enforce separate action approvals. Parent Tasks status, approval,
pause/cancel and shutdown delegate explicitly to that child. Source, selection,
review and authority are checked again before actions. Pending review revocation
settles the approval wait, revokes the exact target, and prevents fill/POST.

Success requires the new terminal task/run, independent recipe audit and separate
TLS whole-record readback. A task's `succeeded` status alone is insufficient;
read the journal's `accepted_verified` result. Readback is one-use and is not
repeated to service status requests.

## Authenticated console operations

POST `/api/tasks/parameter-project-skill-reuse/preview` with:

```json
{
  "schema_version": "1.0",
  "release_sha256": "EXACT_SELECTED_RELEASE_SHA256",
  "selection_sha256": "EXACT_CURRENT_SELECTION_SHA256",
  "parameters": {"record-id": "New synthetic record", "note-text": "New synthetic note"},
  "lease_id": "CURRENT_AGENT_LEASE",
  "generation": 0
}
```

Hashes and lease/generation above are placeholders, not valid admission pins.
Preview returns canonical admission/binding hashes and `confirm_sha256`. POST
`.../start` with the same fields plus that exact `confirm_sha256` and literal
boolean `human_confirmation: true`. The server recompiles from original trusted
sources; clients cannot upload an execution plan, source path or arbitrary tool.
Normal Tasks approvals remain separate. POST `.../status` with only
`{"schema_version":"1.0"}` reads existing execution evidence without dispatch.

EN/TR Tasks includes the new-parameter preview, separate unchecked confirmation,
start and read-only status panel. Fresh control changes invalidate start; an
uncertain acknowledgement remains one-attempt and requires status inspection.
The isolated Chromium test verifies actual preview, invalid evidence/fence
rejection and durable pre-task failure/status—not a complete successful GUI
task. The separate actual backend task verifies execution and readback.

## Failure and verification limits

Each task admission is one-use; a consumed/uncertain start is never reset
or replayed automatically. After an independently accepted task, an explicit
`next-preview` / `next-start` can authorize another task in the **same live
managed session**. Both requests include exact `previous_intent_sha256` and
`previous_receipt_sha256` from current accepted status. New parameters must differ
from the original bootstrap and immediately previous task. Next-start requires
its own unchecked human confirmation and current AGENT lease/generation.

The next binding's `proposal_sha256` commits the canonical transition containing
that predecessor pair and the new admission hash; it is not a Bonsai proposal.
Bounded original journal inventory reconstructs and verifies the unique chain.
No new migration, scheduler or second persistence ledger is introduced. Before
publishing the new intent, the host closes only its already completed child,
checks owned runtime/target cleanup, then rechecks source, selection and control.
Failed cleanup or takeover latches admission closed and preserves read-only old
results. Intent publication still precedes new fixture preparation.

Completed-child cleanup has a trusted maximum ten-second observation window.
A timeout retains the same owned cleanup task without cancelling it or starting
a second cleanup. Admission remains blocked even if that task later completes;
parent shutdown observes the same handle. Timeout is not cleanup or GPU-release
proof, and no next intent is published while cleanup is uncertain.

POST `.../read` with `schema_version: "1.0"` and an exact `intent_sha256` reads
historical accepted or uncertain journal evidence without dispatch; it does not
arm a new task or claim current source admission. Status/read wrappers expose
`transition_blocked` and `transition_in_progress`. Restart adoption of a live
predecessor, automatic reset and retry are unsupported; a new coordinator refuses
existing history rather than pretending it owns the old child.

A failed preparation after intent publication remains
unresolved and fences a fresh coordinator using the intact journal. Lost ACK
requires read-only status inspection. New-task execution history currently uses
the existing private execution journal, not a new external or DB-anchored witness
against privileged rollback/deletion of that entire journal. Review/release DB
anchors do not extend that guarantee implicitly.

Actual acceptance uses CPU scripted finite decisions and a real owned TLS server,
not native Decider/Bonsai or GPU. Compiler and integrated execution suites each
passed seven tests; the integrated run required six fresh approvals, one POST
and independent readback, preserving the original bootstrap. Revocation before
fill produced zero fill/POST and retained the unresolved fence. Deployment,
general multi-application/model acceptance and jointly admitted Scientist GPU
control remain separate open requirements. Sequential acceptance added two new
tasks after the original bootstrap: 21 actions, 18 separate approvals and three
submissions in total, with original source and all earlier records preserved.
An isolated Chromium UI test also executes the second child through this actual
CPU/TLS host, then verifies lost-start-ACK inspection and historical read without
retry. This is not native model, real-site or deployed-session acceptance.
See [STATUS](STATUS.md).

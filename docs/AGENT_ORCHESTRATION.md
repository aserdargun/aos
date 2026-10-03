# Delegated-agent runner v1

This is an opt-in, host-composed task runner, not another model framework or GPU
scheduler. AOS retains its Supervisor/Operator roles. The Supervisor may delegate
a bounded request; the Operator owns authorization and execution. Additional
registered executors do not acquire independent desktop ownership.

## Implemented boundary

- `AgentRegistration` pins a version, installed adapter kind, operation allowlist,
  active-instance ceiling, per-job and aggregate reservation budgets, and cleanup
  scope. Registration changes require a different explicitly registered version.
- `AgentJobRequest` binds the job ID, exact registration, principal, runtime,
  owner, lease, generation, budget and dependencies. Reusing a job ID with the
  identical request is idempotent; changing its content is rejected. Unknown
  schema versions, registrations and capabilities are rejected.
- `AgentOrchestrator` exposes `register`, `submit`, `status`, asynchronous
  `advance`, `request_cancel` and read-only-effect `reconcile`. The trusted host
  supplies the installed adapters and a current-authority provider. The default
  provider denies every request. A changed owner, lease or generation is not
  silently adopted.
- A separate private SQLite store reserves instance/budget capacity atomically
  with the preparation intent. Preparation, dispatch and cancellation intents
  are durable before their adapter calls. Interrupted effects become uncertain;
  reopening never dispatches them again. Independent result and cleanup proofs
  must bind the current exact handle before a reservation is released.
- Dependencies may be submitted while earlier jobs are pending, but dispatch
  requires independently verified success and cleanup of every dependency.
  Failed, cancelled or uncertain work cannot release a dependent job.
- Immutable audit events preserve each transition. The canonical migration is
  `database/agent_orchestration_migrations/0001_agent_jobs.sql`, not a migration of
  the active `TrajectoryStore`. Never reopen the user's original trajectory DB
  to construct this runner; opening that store can reconcile existing sessions.

Budgets here are simultaneous reservations **per registered agent version**,
not measured billing, a machine-wide resource scheduler or Scientist GPU grants.
The host still must enforce executor-specific limits and authorization. A model
cannot register an adapter, raise a quota or provide an authority/cleanup verifier.

## Adapter contract

Adapters implement six asynchronous operations:

| Operation | Required behavior |
|---|---|
| `prepare(request)` | Return the exact durable proposal/instance handle; indicate whether external approval is still required. |
| `dispatch(prepared)` | Perform the once-authorized effect; preserve its immutable handle identity. |
| `observe(handle)` | Read current state independently; persist any updated opaque control identifiers through the returned handle. |
| `request_cancel(handle)` | Cancel only this job, or propose its separately approved stop; never stop another executor/session. |
| `verify_result(handle)` | Independently verify the bound outcome; an exit code or model statement alone is not success. |
| `verify_cleanup(handle)` | Prove the registered cleanup scope, or explicitly return unverified. |

The runner checks exact authority before and after each adapter call. Adapters
must also enforce authority at their actual effect boundary; a post-call check
cannot undo an unauthorized external action. Handle job ID, request hash,
authority and handle ID remain immutable. Only validated opaque payload changes
may carry newly observed remote/control identities.

Approval waiting does not repeat preparation. Stop approval also remains a
separate step: observe readiness, then explicitly request cancellation again to
consume the already-approved exact stop. Uncertain effects use read-only
reconciliation, not a new idempotency key or automatic retry. If the underlying
adapter cannot prove a process/remote binding after restart, capacity stays held.

## Fixed synthetic CPU executor

`SyntheticAgentAdapter` accepts only `synthetic.write.v1`, bounded text and delay.
It runs a fixed, stdlib-only worker in a private workspace with an inherited
directory descriptor, distinct process, CPU/address-space/file-size/wall limits,
and no shell, model or GPU work. The parent independently reads the exact result,
checks request/content hashes, and reaps the original owned process. Handle,
workspace inode and process identity are bound together. Cancellation of one
instance does not terminate another.

These are isolated **fixed trusted CPU workers**, not an OS security sandbox for
arbitrary agent programs. Do not use this adapter to run untrusted code or network
tasks. Unknown processes are never killed or adopted from a numeric PID. An
existing dispatch marker forbids relaunch after acknowledgement loss.

Run the bounded acceptance in a new private directory:

```sh
.venv/bin/python scripts/check_agent_orchestration.py --cpu --root data/agent-orchestration-cpu-example
```

It starts two overlapping CPU workers, cancels one, verifies the other and its
dependent job, rejects a third active instance and a stale generation, then
simulates a lost dispatch acknowledgement after a real fourth subprocess launch.
The SQLite store is reopened and read-only reconciliation uses the original
live adapter's owned-process identities. This is **not a full host-process crash
recovery claim**. Reconstructing an adapter without those identities fails closed.
The private JSON receipt states these limits and is not a real-model or native
Scientist acceptance report. Existing evidence directories are never overwritten.

## Scientist host integration

The Scientist adapter must receive the existing `ScientistLabService`, controller,
client and original journal, not create parallel ownership or reopen its store.
Start and stop use the service's existing exact-envelope human approvals. The
runner never approves its own proposal. Local job ID, AOS Lab run ID and remote
Scientist UUID remain distinct. Status/report readbacks preserve the original
authority fence and session-wide uncertain-effect no-replay rule.

A verified terminal report is not evidence of physical GPU cleanup. A separately
trusted, exact-binding cleanup provider is necessary to release a Scientist
reservation; absence of that provider leaves capacity held. Normal verification
stores proof hashes, not report content. Report retention continues to require
the existing separate exact-hash consent.

This slice does **not** install a new live-console route or activate a native
Scientist host. The current public UI and its selected runtime source/config
pins remain unchanged. Admitting the runner into a reviewed real host requires
an explicit source/config update and the Scientist-managed integrated GPU run.
The existing default native UI is not broker-enforced; idle/quiesce alone is
not a GPU reservation. See [current evidence](STATUS.md) and the unchanged full
[release acceptance scope](RELEASE_ACCEPTANCE.md).

# Private Fixture Benchmark Runtime

## Implemented scope

`python -m aos.benchmark` executes the existing Operator, deterministic fixture
DecisionEngine, policy/gateway, workspace runtime and SQLite trajectory store.
It is a runnable engineering baseline, **not real Decider/Bonsai evaluation**.
Every sample gets a fresh child process, private workspace and private database;
the parent independently reads exact artifact bytes and a frozen in-memory copy
of the closed database. It checks success, exact action scope/order, independent
verification records, model-call absence and recovery evidence. It does not trust
the worker's success claim or an existing hello artifact.
Run outcome, current typed terminal state/version, task/runtime/step bindings and
the final state snapshot digest must also consistently show the same success.

The fixed suite contains:

- `file-roundtrip`: the exact authorized hello write and independent read.
- `recovery-path`: the existing controlled read-before-create failure followed by
  a **fixture** Supervisor recovery plan, write and independent read. This only
  partially covers the catalog's wrong-input-path recovery goal; it is not an
  arbitrary path correction or real Supervisor result.

One to five repeats yield two to ten executions. Repeats are not new independent
held-out tasks. Duration is parent-observed child startup + task execution + child
shutdown wall time; it excludes parent verification. Action, failed-probe,
fixture-Supervisor, real System-2 and human-request counts are checked from the
recorded trajectory. The intentional recovery read failure is not a safety
violation. No aggregate accuracy, statistical superiority, p95 comparison or
unmeasured GPU/safety score is invented.

## Commands

Run with the existing application virtualenv, without installing/downloading
models or starting Docker:

```bash
.venv/bin/python -m aos.benchmark --include-synthetic --repeats 2 \
  --output runs/benchmark-local-001
sha256sum runs/benchmark-local-001/report.json
.venv/bin/python -m aos.benchmark --output runs/benchmark-local-001 \
  --verify-sha256 EXTERNALLY_RECORDED_REPORT_SHA256
```

Output must be a new direct `runs/benchmark-*` directory. Existing directories,
aliases, unsupported tasks/repeats and implicit fixture use are rejected.
Directories are mode 0700; report/database files are mode 0600. Reports contain
hashes, sample-relative names, run identifiers and counters, not trajectories or
file contents. Database and workspace artifacts remain private under `runs/`;
none belongs in the source manifest. No upload, install, cleanup of unrelated
files, model execution, training or promotion occurs.

Each child has a 60-second wall-time limit and a combined 64-KiB stdout/stderr
limit enforced while reading; only the owned direct child is killed/waited on.
This fixture worker launches no descendants. The complete run is bounded to ten
children. It is filesystem-scoped through the existing gateway, not an OS/network
sandbox; no network-isolation or malicious same-UID host defense is claimed.

The report binds the code dependency set, migrations, application lock/config,
canonical schema, fixture recovery plan, catalog, promotion policy, Python and
installed Pydantic/JSON Schema versions. Configuration records fixed execution
limits and actual Settings defaults. Pins are rechecked before publication.
Explicit file hashes provide integrity against an independently retained digest;
they are not signatures or third-party attestations. Environment hashing is not
a complete installed-package or operating-system provenance attestation.

The read-only verifier requires the external report digest, current source/config
pins, exact sample file sets, no live WAL/journal, regular single-link files and
matching bytes/SQL evidence. SQL runs against a bounded in-memory snapshot with
query-only/trusted-schema restrictions and a progress deadline. Changed or old
source pins fail closed. Durations cannot be independently reconstructed from a
saved database; the external report digest binds their originally measured values.

If a child fails/times out, evidence mismatches or pins change, no complete report
is published. The partial private directory remains for diagnosis; rerun in a
new directory rather than silently omitting/retrying a failed sample. Such an
incomplete run is a failure, not a scored successful benchmark.

## Acceptance and remaining work

`tests/test_benchmark.py` runs actual child/workspace/SQL flows and checks repeated
independence, read-only verification, altered scope/bytes/SQL, missing independent
verification, symlink/hardlink/FIFO/size limits, unsupported scope, incomplete
child execution, source races and schema/authority boundaries. Actual environment
evidence belongs in [STATUS](STATUS.md). `examples/benchmark.json` is explicitly
synthetic schema illustration, **not** recorded benchmark evidence.

Ten catalog tasks remain unexecuted by this runner; recovery coverage is partial.
Thirty independent held-out tasks, five per critical slice, real Decider-first vs
Bonsai-per-step evaluation, available VRAM, safety/secret-leak evaluation and
rollback rehearsal remain unsatisfied. The promotion policy remains proposed,
not silently frozen. Real model execution, benchmark registry persistence,
training and promotion/rollback services are not implemented by this slice.
Every report fixes `real_model=false`, `independent_held_out_tasks=0`,
`statistical_superiority_established=false`, `promotion_authorized=false` and
`training_authorized=false` and retains explicit coverage gaps.

# Manual synthetic learning metadata checkpoint

`aos.learning_event_outbox` persists an explicitly selected role's existing audited **synthetic** learning-event metadata for one run. It does not run during tasks, subscribe to the trajectory, collect real-site data, enqueue training, or change model deployments. The module name does not imply a working incremental outbox: there is no consumer, cursor, background worker or automatic advancement.

```sh
.venv/bin/python -m aos.learning_event_outbox \
  --database /path/to/private-trajectory.sqlite --run-id RUN_ID \
  --role system1 --outbox-dir /path/to/private-checkpoints
.venv/bin/python -m aos.learning_event_outbox \
  --database /path/to/private-trajectory.sqlite --run-id RUN_ID \
  --role system1 --outbox-dir /path/to/private-checkpoints --inspect
```

Only a known synthetic policy and an audited read-only SQLite snapshot are accepted. The key binds the exact run ID, role and `learning-evidence-v2` derivation version. The record binds the entire source snapshot hash and content-free v2 events. No Bonsai call means zero System-2 events, not a fabricated result. All events remain `training_ready=false`; the checkpoint also states `collection_authorized=false`. Event metadata includes source identifiers, so keep the output and checkpoint store private; it is not a public export or a reviewed dataset.

Files are canonical JSON under a private owner-only directory and mode-0600 regular files. Writes use fsync and no-replace publication. The same unchanged source is idempotent across restarts. If any source DB content changes, the same run/role/version key conflicts rather than replacing or silently advancing a checkpoint; inspection also fails. A corrupt, symlinked, permissive or changed file fails closed. The operator must inspect conflicts; apart from the narrow publication recovery below, there is no repair, replay, queue retry or new revision protocol. This slice does not satisfy W1's live authorized dual-role collector or W4's reviewed dataset/readiness requirements.

**Narrow publication-crash recovery:** the publisher holds an exclusive private-directory lock. If it crashes after linking the final filename but before removing its temporary filename, a later explicit derive may remove exactly one matching `.checkpoint-<32 hex>` hard link and fsync the directory. Recovery requires both names to reference the same owner-only canonical file with exactly two links and content matching the current audited source. A changed source, ambiguous/missing temporary name, extra link, corrupt content or wrong permissions remains blocked without cleanup. `--inspect` stays read-only and rejects the interrupted state until an explicit derive repairs it. Unpublished orphan temporaries and general outbox replay are not repaired.

The canonical contract is `schemas/learning_event_outbox.schema.json`; `examples/learning_event_outbox.json` is explicitly synthetic. Focused tests cover role separation, no invented S2 event, restart/dedup, changed source, private-file integrity and publication failure. Actual results and remaining W1–W6 gates are in [STATUS](STATUS.md).

# W2 two-run HTTPS route metadata comparison

`aos.remote_route_change` compares two explicitly selected, completed `browser_remote_routes` runs for the **same selected registered profile and exact route plan**. Each run independently passes the full read-only approval/action/readback audit in [REMOTE_ROUTE_EVIDENCE](REMOTE_ROUTE_EVIDENCE.md). Both audited snapshots must have identical serialized SHA-256, and a final audit must still match that source; the before run must start earlier. A changed profile, plan, route URL hash, order, source row, approval or verification fails closed.

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_route_change \
  --database /path/to/private/store.sqlite \
  --before-run-id RUN_A --after-run-id RUN_B \
  --profiles /path/to/private/web-applications \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256
```

The report identifies only which planned route indices have equal or different versioned URL/title/H1 hashes. When both runs have complete, matching open/readback link samples, an optional `sampled_link_inventory_changed` reports whether the observed planned indices or count of unregistered anchors differs; if equal, `link_sample_sha256` binds that complete bounded sample and `planned_link_indices` lists observed authorized routes for later binding. Older runs and truncated samples omit those fields. This is a separate signal: `unchanged_profile_bound` means the **URL/title/H1 readbacks** match, not that links or the entire site stayed unchanged. A false link-sample delta does not prove equal unknown destinations or current state. `changed_profile_bound` does not explain why a page changed. Both are historical metadata, not current knowledge, an application outcome, account attestation or a learning label. URLs, DOM, page text, credentials and run IDs are omitted; hashes remain sensitive and should stay private. No database or site-knowledge store is written.

Canonical contract and explicitly synthetic example: `schemas/remote_route_change.schema.json` and `examples/remote_route_change.json`. The owned synthetic-TLS test runs three separate scheduler jobs: unchanged `/entry` fingerprint, changed `/details`, then a third readback with the same `/entry` fingerprint but a changed bounded link sample. Both signals, reversed run order and changed source evidence are checked. Real target W2 acceptance still needs authorization, rights, tenant/role-scoped reviewed knowledge, stale detection, and task-time retrieval with no expansion of execution authority.

An unchanged route can be compared with an exact immutable manual draft using the separate [remote route knowledge candidate](REMOTE_ROUTE_KNOWLEDGE.md) preview. That candidate still cannot authorize task retrieval or claim reviewed knowledge.

For a selected stable route with a symbolic static path, [remote page draft seed](REMOTE_PAGE_DRAFT_SEED.md) can prepare an unreviewed private draft from the same audited evidence. The page key remains a manual assertion and the output is not registered or activated.

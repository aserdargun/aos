# W2 synthetic page retrieval preview

`aos.site_page_retrieval` creates a **read-only, non-executable candidate** for one explicitly selected immutable page draft. It combines the existing W2 two-run page comparison with the W2 draft-to-evidence comparison. Both selected Start/Details verifications must be independently audited, from distinct runs in the same SQLite snapshot, with unchanged versioned semantic fingerprints. The later fingerprint and page key must equal the exact selected draft. `SiteKnowledgeStore.inspect` must still find that exact draft unsuperseded and unambiguous. Missing, changed, forked or corrupt sources fail without a report.

```sh
.venv/bin/python -m aos.site_page_retrieval \
  --database /path/to/private-trajectory.sqlite \
  --before-run-id RUN_A --before-verification-id VERIFICATION_A \
  --after-run-id RUN_B --after-verification-id VERIFICATION_B \
  --profiles /path/to/private-web-applications \
  --store /path/to/private-site-knowledge \
  --knowledge-sha256 PAGE_SHA256 \
  --selected-profile-sha256 PROFILE_SHA256
```

The output is `candidate_unbound`, not “current site knowledge.” It contains only profile/draft hashes, the symbolic page key, versioned fingerprint and hashed run/snapshot references. The explicit selected profile checks the draft's stored profile/tenant/role scope, **not** the fixture run's account, origin, route or rights. This original command does not inspect persisted run-to-profile bindings. Two selected runs are not a wall-clock freshness guarantee; a subsequent database or draft revision requires another preview. The command does not publish or review a draft, select an active revision, inject knowledge into a task, start a browser, grant execution/collection/training, or train a model. All authority flags remain false. The result may guide a future explicitly authorized, independently checked retrieval integration; no such integration is claimed here.

## Optional local-fixture provenance preview

`aos.site_page_retrieval_bound` is a separate read-only command with the **same arguments**. It first requires the original two-run result, then reopens the same frozen SQLite snapshot and requires two successful `browser_local_navigation` jobs and runs, each with one append-only `desktop_web_profile_bindings` record for the exact selected immutable `local_test` profile. Canonical pin JSON/hash, fixed task key, child runtime ID, policy, profile origin and exact `/start` or `/details` draft route are rechecked. Any missing binding, changed source snapshot, wrong pin/hash, stale profile/draft or mismatched job/run fails without a report. Prior unpinned runs are not upgraded. The report schema and explicit synthetic fixture are `schemas/site_page_retrieval_bound.schema.json` and `examples/site_page_retrieval_bound.json`.

The result `candidate_local_profile_bound` and `profile_bound=true` mean **historical local-fixture profile/job/run metadata agrees**, not that a user account, live remote origin, dynamic page route or data rights were verified. `origin_verified`, `route_verified`, `reviewed`, `execution_authorized`, `collection_authorized` and `training_ready` remain false. It never injects knowledge into a task or promotes a page draft. This is not real-site W2 acceptance. Focused tests: `PYTHONPATH=src:tests .venv/bin/python -W error::ResourceWarning -m unittest tests.test_site_page_retrieval_bound -q`. The two-run pinned Decider/Ubuntu/Chromium/MCP test is explicit opt-in and recorded in `docs/STATUS.md`.

`schemas/site_page_retrieval.schema.json` and `examples/site_page_retrieval.json` are the canonical synthetic-only report contract and fixture. The report omits raw run IDs, verification IDs, URLs, DOM, screenshots, credentials and page text. The source database is read only; the private draft store is not changed. Focused tests: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_site_page_retrieval.py -v`. Real target-site W2 remains open pending user target, authorized account/tasks, data rights, dynamic observation, review and task-time scope enforcement.

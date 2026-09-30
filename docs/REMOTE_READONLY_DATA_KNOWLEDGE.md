# Stable JSON-bundle evidence to manual page draft

`python -m aos.remote_readonly_data_knowledge` produces a **read-only candidate**, not a learned or executable site map. It re-audits two separately approved, completed v2 JSON-bundle runs under the same exact profile and plan in one bounded SQLite snapshot. Entry, JS/CSS, JSON response and page title/H1 fingerprints must all be unchanged across the historical runs. A changed JSON body rejects the candidate even if the visible title/H1 did not change.

The candidate binds an immutable, owner-only `SitePageDraft` to the exact plan's entry URL and the versioned whole-bundle fingerprint. Response hashes come from the host relay verification record, not a second browser-side hash of JSON bytes. The draft must use a literal route (no template variables), contain no outgoing-page claims (this worker does not sample links), match application/tenant/role/profile, and be the unsuperseded revision of its page key. A successor profile, changed page fingerprint, changed source approval/readback, or mismatched plan fails closed. No URL, DOM text, JSON body, cookie, or model output is emitted.

First register the exact manual page draft with its `page_fingerprint_sha256` set to the `after_fingerprint_sha256` from an **unchanged** [transport comparison](REMOTE_READONLY_DATA_CHANGE.md). A new v2-plan backend can [seed and register a private unreviewed draft from Tasks](REMOTE_READONLY_DATA_PAGE_DRAFT.md), rather than requiring a separately prepared file. That hash is not semantic evidence: the page key and landmarks remain human assertions. Then run:

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_readonly_data_knowledge \
  --database data/PRIVATE_SESSION/store.sqlite \
  --before-run-id BEFORE_RUN_ID --after-run-id AFTER_RUN_ID \
  --profiles data/web-applications --store data/site-knowledge \
  --knowledge-sha256 PAGE_DRAFT_SHA256 \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256
```

Paths and IDs above are placeholders. The preview makes no network request or DB/store write. Its `candidate_remote_profile_bound` status means only historical metadata matched; no external rights, current freshness, account identity, task outcome, human semantic review, task-time retrieval, skill activation, collection, dataset, training or promotion is authorized. A separate [metadata-only review and fresh completed-run recheck](REMOTE_READONLY_DATA_KNOWLEDGE_REVIEW.md) now exists, but task-time retrieval and real-site acceptance remain open. Canonical contract and illustrative example: `schemas/remote_readonly_data_knowledge.schema.json`, `examples/remote_readonly_data_knowledge.json`.

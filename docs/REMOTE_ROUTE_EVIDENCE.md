# W2 historical HTTPS route evidence preview

`aos.remote_route_evidence` reads one explicitly selected completed `browser_remote_routes` run from an audited, read-only SQLite snapshot. It requires the selected registered profile SHA-256 and exact persisted route-plan SHA-256. The projection revalidates the stored canonical profile/task/runtime draft and plan against the current private profile store, then requires a successful job/run, one consumed manual approval and one independently verified browser readback for every planned route. Missing, changed or extra actions, approvals, observations or verifications fail closed.

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_route_evidence \
  --database /path/to/private/store.sqlite \
  --run-id RUN_ID \
  --profiles /path/to/private/web-applications \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256
```

The report contains only a snapshot hash, hashed run/verification references and versioned URL/title/H1 SHA-256 fingerprints in plan order. New route observations also sample at most 64 DOM anchors per page, recording only sorted indices that match already approved plan URLs, the bounded number of unregistered anchors in that sample and whether the sample was truncated. Matching open/readback samples appear in the report; older runs remain readable without these optional fields. A link is browser-observed metadata, not an independently verified site relationship. Link fields do not change the v1 page fingerprint, authorize another route, populate reviewed outgoing keys or grant collection/retrieval. Unknown URLs and link text never leave the isolated browser. The report does not copy URLs, page text, DOM, screenshots, cookies or credentials into the output or a new store. The URL hash is still potentially sensitive metadata; keep the report private. Re-run the preview after a database or profile change. Its canonical schemas and explicitly synthetic examples are `schemas/web_remote_route_observation.schema.json`, `examples/web_remote_route_observation.json`, `schemas/remote_route_evidence.schema.json` and `examples/remote_route_evidence.json`.

`historical_verified_metadata` means the stored run has a bound plan, recorded manual approvals and matching independent browser readback metadata. The projection does not independently attest the remote account, current origin state, application task outcome or data rights. It does not activate site knowledge, inject retrieval into a task, create a skill or training label, authorize collection, or train either model. The existing no-collection remote task remains unchanged. A separate [two-run change preview](REMOTE_ROUTE_CHANGE.md) compares only these historical hashes. W2 real-site acceptance still requires an authorized target, account/tenant boundary, a reviewed versioned knowledge record, stale detection and scoped reuse in a later task.

Focused owned synthetic-TLS test: `AOS_DESKTOP_TESTS=1 AOS_HTTPS_RELAY_TESTS=1 PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_web_https_relay.WebHTTPSRelayTests.test_scheduler_two_routes_need_separate_approvals -v`. The test also rejects a wrong plan, revoked approval and altered readback observation. This is not an external site test.

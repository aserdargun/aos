# W4 — Read-only JSON-bundle source audit

`aos.remote_readonly_data_source` audits one completed, manually approved v2 JSON-bundle run against exact registered profile and plan hashes. A frozen trajectory snapshot rechecks the job/run/runtime/draft/plan binding, consumed approval, authenticated human intervention, exact open action, browser observation, passed independent readback and distinct entry/JS-CSS/JSON response hashes. It lists only actual pinned S1 decision and linked S2 escalation learning-event IDs requested by the profile; an absent Bonsai call stays empty.

```sh
.venv/bin/python -m aos.remote_readonly_data_source \
  --database data/<private-trajectory.sqlite> \
  --run-id <completed-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-json-plan-sha256>
```

`schemas/remote_readonly_data_source.schema.json` is the canonical report contract; `examples/remote_readonly_data_source.json` is synthetic. Output has profile/plan/run/snapshot hashes, resource counts and event IDs, not URLs, JSON, HTML, JS/CSS, prompts, responses, screenshots, credentials or cookies. The CLI makes no network request and writes no outbox/dataset. It does **not** grant W4 metadata consent or collection, or verify site outcome, account, rights, redaction or training readiness. The existing v1 static source CLI remains v1-only. This audit is a provenance prerequisite, not a live learning pipeline.

Owned synthetic TLS/Ubuntu/Chromium/MCP tests cover the completed v2 run with one approval, real pinned Decider S1 versus fixture zero calls, wrong selected plan, altered approval/verification/human records and v1 source rejection. Unit tests validate the schema's permanently false authority flags and unsafe/missing-source CLI failure. No real target site was contacted.

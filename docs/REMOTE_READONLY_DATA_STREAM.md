# W4 — Consent-bound JSON-bundle metadata stream

The v2 JSON-bundle task has a separate `remote_json_model_metadata_only` consent scope. While its first manual task approval is pending, the local operator can preview the exact run/profile/plan/role/expiry consent without writing a file, register it with the returned SHA-256 in a private `0600` file, and separately attach it in authenticated Tasks. Registration alone does not collect anything. The profile must request each role and have a data-rights reference; local attestation is **not** independent proof of external rights or account identity. V1 static and route consents cannot be reused for v2.

```sh
.venv/bin/python -m aos.remote_learning_consent \
  --database data/<private-trajectory.sqlite> --run-id <bound-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-json-plan-sha256> \
  --scope remote_json_model_metadata_only \
  --roles system1 system2 --expires-at <UTC-ISO8601-plus-00:00> \
  --attest-data-rights --store data/remote-learning-consents
# Inspect the preview, then repeat with --confirm-sha256 <preview-hash>.
```

After separate attach, the scheduler polls at committed safe points and settlement. The host may also poll once or run a bounded watch:

```sh
.venv/bin/python -m aos.remote_readonly_data_stream \
  --database data/<private-trajectory.sqlite> \
  --profiles data/web-applications \
  --consents data/remote-learning-consents \
  --consent-sha256 <exact-consent-sha256> \
  --outbox-dir data/remote-learning-outbox \
  --watch-seconds 300 --interval-seconds 1
```

The poller rechecks frozen source/consent, exact profile/task/plan/job/runtime, the successful open action, consumed task approval, authenticated human record and actually pinned S1 decision or linked S2 escalation. Before open it records zero events; after open it writes only content-free, deduplicated event IDs and source hashes into a private, consent-bound append-only outbox. An uncalled Bonsai remains zero. `schemas/remote_readonly_data_stream_{entry,report}.schema.json` and `examples/remote_readonly_data_stream.json` define the synthetic contract. Each entry deliberately has `transport_readback_verified=false`; the separate [completed-run source audit](REMOTE_READONLY_DATA_SOURCE.md) may verify transport but does not turn stream metadata into a gold task result.

Exact revocation and retention use the existing [consent lifecycle](REMOTE_LEARNING_CONSENT.md). A metadata failure is isolated from the browser task; after restart, missing settlement polling requires explicit CLI inspection, not replay. Outbox entries exclude URL, JSON body, HTML, JS/CSS, prompts, model responses, screenshots, credentials and weights; linkable hashes remain private. Site outcome, external rights, redaction review, dataset eligibility and training readiness remain false. Owned synthetic TLS/Ubuntu/Chromium/Playwright MCP, authenticated EN/TR Tasks and real pinned Decider tests cover two-stage attach, pre-action zero, final one S1/zero S2, wrong scope, dedup and revocation. No real target site or account was tested.

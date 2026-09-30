# W4 — Static-bundle metadata stream

An operator can now make a separate, exact, local metadata-only consent for one already bound `browser_remote_static_assets` run. The existing consent store, revocation, and retention rules apply; the scope is `remote_static_model_metadata_only`, so a route consent cannot be used for a static task or vice versa. The profile must have requested each selected model role and recorded a `data_rights_ref`. This is an operator attestation, **not** independent proof of site rights or account identity. In a **new** static-plan backend session, authenticated Tasks provides the same two-stage preview/register flow and a separate attach action; registration alone does not start collection.

After the task starts and its first manual approval is pending, preview the consent without writing a file. Repeat with the returned hash to register it in a private `0600` file:

```sh
.venv/bin/python -m aos.remote_learning_consent \
  --database data/<private-trajectory.sqlite> --run-id <bound-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-static-plan-sha256> \
  --scope remote_static_model_metadata_only \
  --roles system1 system2 --expires-at <UTC-ISO8601-plus-00:00> \
  --attest-data-rights --store data/remote-learning-consents
# Inspect the preview, then repeat with --confirm-sha256 <preview-hash>.
```

Poll once or run a bounded watch in another terminal while the task runs:

```sh
.venv/bin/python -m aos.remote_static_learning_stream \
  --database data/<private-trajectory.sqlite> \
  --profiles data/web-applications \
  --consents data/remote-learning-consents \
  --consent-sha256 <exact-consent-sha256> \
  --outbox-dir data/remote-learning-outbox \
  --watch-seconds 300 --interval-seconds 1
```

The poller rechecks the frozen audited database schema, exact profile/plan/job/run/runtime, immutable consent, requested roles, pinned model identity, successful `browser.static.open` action, matching consumed approval, and authenticated human approval. Before that action it records zero entries. Afterwards it records only actual S1 decision calls and linked S2 escalation calls; an uncalled Bonsai remains zero. Each consent gets a separate private append-only outbox with restart-safe event deduplication. Existing v1 route outboxes remain bound to their own consent. A changed source, approval, event or consent fails closed. CLI watch is bounded to five minutes and can be rerun. In a new managed static-plan session, after the operator explicitly registers **and attaches** an exact consent while the single approval is pending, the scheduler polls at safe committed points and on settlement. A metadata failure is reported separately and does not redefine the browser task result; after backend restart, a missing settlement poll needs explicit CLI inspection rather than replay.

Entries carry no URL, HTML, asset body, prompt/response, secret, screenshot or model weight, but linkable hashes remain private. An entry's `transport_readback_verified=false` is deliberate even when the separate [completed-run source audit](REMOTE_STATIC_LEARNING_SOURCE.md) verifies the browser readback: the stream does not silently turn metadata into an outcome label. Site outcome, external rights, redaction review and training readiness remain false. The source audit and stream are separate evidence, not a reviewed dataset or activated skill.

The existing [exact revocation and retention commands](REMOTE_LEARNING_CONSENT.md) apply to this consent/outbox pair. They perform logical deletion in the given private outbox root; they do not erase backups or guarantee physical secure erase. Owned synthetic TLS/Ubuntu/Chromium/Playwright MCP fixture and real pinned Decider tests cover pre-action zero, final one S1/zero S2, API 401/400/409 gates, explicit attach, automatic settlement poll, deduplication, wrong-scope consent, changed approval/human source, and revocation; EN/TR Tasks control is tested separately. The current live plain backend has **no static plan pin**, so it does not expose this task or its attach control. No user target site, account, rights or W4 product acceptance was verified.

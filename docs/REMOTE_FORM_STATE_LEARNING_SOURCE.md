# HTTPS form declared-state source audit

`aos.remote_form_learning_source` accepts an optional, exact `--selected-state-plan-sha256` for a completed six-action HTTPS form run. The four-action report and its schema remain unchanged. A state-bound run cannot be inspected as a four-action run, and an unrelated or missing state hash is rejected.

```sh
.venv/bin/python -m aos.remote_form_learning_source \
  --database data/<private-session>/trajectory.sqlite \
  --run-id <exact-run-id> \
  --profiles data/<private-profile-root> \
  --selected-profile-sha256 <exact-profile-hash> \
  --selected-plan-sha256 <exact-form-plan-hash> \
  --selected-state-plan-sha256 <exact-state-plan-hash>
```

The read-only audit opens one frozen, schema-checked trajectory snapshot. It rechecks the registered profile, form draft/plan, state-plan binding and runtime; all six exact actions and separate consumed human approvals; increasing state-version order; the single POST; the independent receipt DOM readback; and the declared before/after HTML and optional marker transition with its separate verification and observation. Source event IDs are returned only for model calls actually bound to those approved decisions or escalations. The report contains hashes and counts, not form values, Cookie bytes, HTML or page text. It neither executes a browser request nor writes an outbox.

The state transition is **operator-declared transport evidence**, not an independent business-outcome oracle. Even a matching marker does not prove the intended account/tenant or persisted application state. `site_outcome_verified`, `account_verified`, rights/redaction review, collection authority and training readiness remain false. Static-Cookie form runs are not supported by this source audit. This report is not a W4 reviewed dataset or W1–W6 product acceptance.

Canonical contracts are `schemas/remote_form_state_learning_source.schema.json` and explicitly synthetic `examples/remote_form_state_learning_source.json`. Owned `.invalid` Ubuntu/Chromium/Playwright MCP tests exercise six approvals and a tampered state verification; optional pinned Decider mode verifies six real S1 calls and no invented S2 call. See `docs/STATUS.md` for the executed scope.

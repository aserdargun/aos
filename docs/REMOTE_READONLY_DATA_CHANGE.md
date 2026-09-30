# Historical JSON-bundle transport change preview

`python -m aos.remote_readonly_data_change` compares two completed v2 JSON-bundle runs for one exact registered profile and plan. It uses one bounded, integrity-checked SQLite snapshot and re-audits each run's job/runtime, plan, consumed manual approval, authenticated human record, browser observation, and passed transport readback. The earlier run must precede the later run.

The report exposes only run references, source snapshot/profile/plan hashes, a versioned transport fingerprint for each run, and change bits or indexes for the entry response, JS/CSS responses, JSON responses, and page title/H1 hashes. It never emits URLs, JSON bodies, DOM text, cookies, or model output. Response hashes originate in the host relay verification record, not a separate browser-side hash of JSON bytes. A changed JSON response can be visible even when title/H1 stay identical. An unchanged report means only that these recorded hashes match at those two historical moments; it does not establish current site state, semantic meaning, account identity, or successful application outcome.

After two separately approved runs in a private session:

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_readonly_data_change \
  --database data/PRIVATE_SESSION/store.sqlite \
  --before-run-id BEFORE_RUN_ID --after-run-id AFTER_RUN_ID \
  --profiles data/web-application-profiles \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256
```

The path and IDs above are placeholders, not a bundled data source. No site request, authorization, collection, review, retrieval, skill activation, dataset generation, training, or promotion occurs. The existing v2 [source audit](REMOTE_READONLY_DATA_SOURCE.md) and [consent-bound metadata stream](REMOTE_READONLY_DATA_STREAM.md) remain separate. A real target application, authorized account and rights, task-specific outcome oracle, and W1–W6 acceptance are still open. The schema and example are synthetic: `schemas/remote_readonly_data_change.schema.json`, `examples/remote_readonly_data_change.json`.

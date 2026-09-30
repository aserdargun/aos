# W4 — Static-bundle learning source audit

`aos.remote_static_learning_source` inspects one completed `browser_remote_static_assets` run against an exact registered profile and static plan. A single frozen trajectory snapshot rechecks the immutable job/run/runtime/draft/plan binding, one consumed manual approval, its authenticated human intervention, the exact open action, the separate browser readback observation and the passed transport verification. Only actual pinned S1 decision calls from that step and linked S2 escalations requested by the profile are listed as content-free event IDs. An absent Bonsai call remains an empty S2 list.

```sh
.venv/bin/python -m aos.remote_static_learning_source \
  --database data/<private-trajectory.sqlite> \
  --run-id <completed-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-static-plan-sha256>
```

The canonical report is `schemas/remote_static_learning_source.schema.json`; `examples/remote_static_learning_source.json` is explicitly synthetic and illustrative. Output contains profile/plan/run/snapshot hashes, asset count and event IDs, not URLs, HTML, JS/CSS, model prompts or responses, screenshots, credentials or cookies. It is still linkable metadata and belongs in a private location. The CLI reads only; it performs no network request and writes no outbox or dataset.

This re-audits **recorded transport and readback**, not an independent site outcome, account identity, rights or redaction decision. The generic learning event may retain `outcome_not_attributed` because transport verification is not a gold task outcome. Every authority flag stays false. A separate [explicit, consent-bound metadata stream](REMOTE_STATIC_LEARNING_STREAM.md) now exists; this read-only report does not grant it permission or turn its entries into reviewed data. Skill review, S1/S2 dataset and fine-tune remain disabled.

Tests include schema/authority and missing/unsafe-source negatives; an owned synthetic TLS/Ubuntu/Chromium/MCP task with one manual approval and exact JS/CSS, altered approval/verification/human records, and the same task with a real pinned Decider. The real-model test proves one S1 source ID and zero S2 IDs on the synthetic fixture; it does not prove real-site rights or success. Run it only with GPU headroom:

```sh
AOS_DESKTOP_TESTS=1 AOS_HTTPS_RELAY_TESTS=1 AOS_STATIC_REAL_DECIDER_TESTS=1 \
  PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_web_https_relay.WebHTTPSRelayTests.test_scheduler_static_bundle_requires_approval_and_readback -v
```

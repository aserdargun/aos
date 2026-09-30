# W2b synthetic local-page fingerprint evidence

`aos.site_page_evidence` projects a **read-only, content-free** semantic fingerprint for the fixed Start and Details pages after a real run-pinned System-1 call, a successful action, and an independently matching `browser.fixture.snapshot` readback. It uses the audited SQLite snapshot and the existing learning-event verifier, not the MCP tool's success response alone. The fingerprint hashes the verified `{page, heading, links}` projection with a version; it excludes the ephemeral snapshot ID. The report carries only page keys, hashes and source event/verification/observation IDs, never DOM, URLs, headings, link labels, screenshots, credentials or form data.

```bash
.venv/bin/python -m aos.site_page_evidence --database /path/to/private-trajectory.sqlite --run-id RUN_ID
```

The command requires one explicit existing run, uses a bounded read-only snapshot, prints canonical JSON and fails without partial output on missing, corrupt or unsafe sources. `schemas/site_page_evidence.schema.json` is the page contract; `examples/site_page_evidence.json` is explicitly synthetic. Only the `browser-local-navigation-policy-v1` task and its two fixed outcomes are supported. The reported fingerprint is a **synthetic semantic projection**, not a general DOM or authenticated-site fingerprint. A model call's pinned identity is a local run claim, not remote attestation.

This evidence is not registered as site knowledge and has no profile, tenant, role, URL, account or rights binding. `profile_bound`, `reviewed`, execution, collection and training flags remain false. It neither changes `aos.site_knowledge` drafts nor selects an active revision. No migration, live collector, retrieval, skill, dataset or trainer is added. Real-site W2 work still requires authorized scope, independent dynamic-page observation/redaction, profile binding and explicit review; a matching hash alone cannot authorize execution.

Focused tests: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_learning_events.py -v`. The opt-in owned Ubuntu/MCP/real Decider local-navigation test also checks that two separately verified pages produce two entries; evidence is recorded in `docs/STATUS.md`.

## W2c: explicit draft comparison, still unbound

`aos.site_page_comparison` compares one **explicitly selected** immutable `site_knowledge` draft with one verification-selected W2b local-page result. It reads the private draft through its content hash/lineage/profile checks and independently recomputes the page evidence from a bounded trajectory snapshot. It does not write either source, select a latest revision, invoke a model/browser, or create a site observation.

```bash
.venv/bin/python -m aos.site_page_comparison \
  --database /path/to/private-trajectory.sqlite --run-id RUN_ID \
  --profiles /path/to/private-web-applications --store /path/to/private-site-knowledge \
  --knowledge-sha256 PAGE_SHA256 --selected-profile-sha256 PROFILE_SHA256 \
  --verification-id VERIFICATION_ID
```

The result is `page_key_mismatch_unbound`, `hash_different_unbound`, or `hash_equal_unbound`. Even equality is **only equality of two SHA-256 strings**: the manual draft has no fingerprint-algorithm/version field, and W2b observes only an isolated networkless synthetic Start/Details fixture. It does not observe the draft's origin, route, tenant, account or rights. All reports force `profile_bound`, `origin_verified`, `route_verified`, `fingerprint_semantics_verified`, `reviewed`, execution, collection and training flags to false. The selected profile hash checks the draft's immutable scope; it does not bind the fixture run to that profile. Wrong hash/profile, missing verification, or invalid source fails without partial output. The report carries only hashes, page keys, source IDs and the DB snapshot hash; never URL, DOM or credentials. `schemas/site_page_comparison.schema.json` and `examples/site_page_comparison.json` describe an explicitly synthetic comparison.

Focused tests: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_site_page_comparison.py -v`. Real-site attestation requires a separate authorized runtime/profile/route binding and independent site observation; this command cannot supply it.

## Exact-verification review CLI

`aos.site_page_evidence_cli` offers a single explicit verification selection over the existing read-only W2b/W2c projections. It does not add a browser, collector, store or authority. `inspect` returns the existing evidence report shape with exactly one selected page; `compare` returns the existing comparison contract. All source paths and hashes for comparison must be supplied explicitly, and neither command selects a latest draft.

```bash
.venv/bin/python -m aos.site_page_evidence_cli inspect \
  --database /path/to/private-trajectory.sqlite --run-id RUN_ID \
  --verification-id VERIFICATION_ID
.venv/bin/python -m aos.site_page_evidence_cli compare \
  --database /path/to/private-trajectory.sqlite --run-id RUN_ID \
  --verification-id VERIFICATION_ID \
  --profiles /path/to/private-web-applications \
  --store /path/to/private-site-knowledge \
  --knowledge-sha256 PAGE_SHA256 --selected-profile-sha256 PROFILE_SHA256
```

The selected verification must occur exactly once in an independently audited, bounded synthetic navigation run. Missing/ambiguous evidence, unsafe source or invalid arguments fail without a partial JSON report or private source text. Output remains metadata-only and deterministic for the same immutable sources. Equality remains unbound; no real-site/profile/route, execution, collection, review or training authorization follows.

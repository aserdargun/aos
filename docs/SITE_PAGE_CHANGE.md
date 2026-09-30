# W2 synthetic page change inspection

`aos.site_page_change` compares one explicitly chosen, independently verified
Start **or** Details page in each of two distinct audited local-navigation runs.
Both run IDs and both verification IDs are required. The command never selects
the latest run, scans for a matching page on the user's behalf, or writes to the
trajectory, site knowledge, skill, dataset or model stores.

```bash
.venv/bin/python -m aos.site_page_change \
  --database /path/to/private-trajectory.sqlite \
  --before-run-id RUN_A --before-verification-id VERIFICATION_A \
  --after-run-id RUN_B --after-verification-id VERIFICATION_B
```

The two observations must use the same audited database snapshot, page key and
fingerprint version. Duplicate page keys or verification IDs within either run,
missing selections, changed snapshots and unsafe sources fail without a partial
JSON report. `unchanged_unbound` and `changed_unbound` compare only the fixed
networkless fixture's semantic fingerprint hashes; they do **not** establish
that a real site remained stable or changed. The output contains hashed run and
snapshot references, page key and fingerprint hashes, but no raw run IDs,
verification/observation IDs, URL, DOM, credentials or screenshots.

`schemas/site_page_change.schema.json` and `examples/site_page_change.json`
are the canonical report contract and an explicitly synthetic fixture. Every
authority flag is false: no profile/tenant/origin binding, review, execution,
collection or training permission follows. Dynamic real-site change detection
still requires an authorized target, independent observation, redaction and
review. Focused validation: `.venv/bin/python -W error::ResourceWarning -m unittest
discover -s tests -p test_site_page_change.py -v`.

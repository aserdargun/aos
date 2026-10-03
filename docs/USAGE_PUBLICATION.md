# Hourly aggregate publication

`scripts/publish_usage.py` pairs the existing private hourly collector with a
separate, explicit publication step. It publishes development usage, not invoices.
Runtime consumption, subscription payments and the dated goal counter remain
separate. The last authoritative goal observation remains historical between
interactive handoffs; the publisher cannot obtain an authoritative live goal API
counter and does not invent or sum overlapping observations.

## Scope and safety

- Reads only the current UTC hour's private collector snapshot. Missing, future
  or stale snapshots fail closed; there is no silent reuse of an older report.
- Constructs a new allowlisted public aggregate. No messages, session identities,
  account identifiers, private file paths, arbitrary nested fields or runtime DB
  contents are copied. Unknown model/provider labels are rejected or explicitly
  unpriced by the reviewed summarizer, never treated as zero cost.
- Updates exactly `README.md`'s marked usage block, `docs/usage_latest.json`, and
  the corresponding `MANIFEST.sha256` entries. Dated snapshots are preserved.
- Uses a dedicated private **bare Git repository**. It never stages development
  worktrees, picks up unrelated changes, resets branches or runs project hooks.
- Pins the fetch/push remote, branch and reviewed price-file SHA-256. Changed
  prices require operator review. The 3 October tariff remains a dated
  standard/short-context counterfactual, not a claim of current provider billing.
- Fetches the latest branch, creates a three-file commit with that parent and
  uses ordinary non-forced push. A concurrent remote change rejects publication;
  the next hourly run rebuilds from the new remote head. No merge/rebase or force.
- A local publication lock prevents overlapping publishers. Unchanged usage does
  not produce date-only commits. Counter decreases require manual review.
- Git/snapshot errors return nonzero without printing private source contents.
  Systemd preserves failed status and a sanitized journal message.

## Operation

Create and review an owner-only bare repository, configure its `origin` and
commit identity, and supply explicit paths and pins. The command without
`--publish` fetches and previews, but creates no commit and performs no push:

```sh
python -m scripts.publish_usage \
  --git-dir /private/path/accounting.git \
  --snapshots /private/path/accounting \
  --remote https://github.com/OWNER/aos.git --branch main \
  --price-sha256 REVIEWED_PRICE_FILE_SHA256
```

`--publish` explicitly authorizes the allowlisted commit and normal push. Git
credentials must already be configured; unattended jobs cannot prompt. Initial
`docs/usage_latest.json`, README markers and manifest entries must be published
through a reviewed source handoff first.

On this host the separate `aos-usage-publish.timer` is intended for five minutes
past each UTC hour, after the existing `aos-usage-record.timer` at the hour.
Missed hours are not backfilled. The user manager, host and network must be
available. This does not promise execution while the computer is off or grant
permission to restart AOS/GPU services. Inspect actual activation in STATUS.

```sh
systemctl --user status aos-usage-record.timer aos-usage-publish.timer
systemctl --user status aos-usage-publish.service
journalctl --user -u aos-usage-publish.service -n 20 --no-pager
```

Source validation uses temporary synthetic repositories, including concurrent
remote updates, idempotent retry, wrong remote/price, stale/missing input and
nested private-field exclusion. These tests are not real billing validation.

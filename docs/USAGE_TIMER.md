# Private hourly usage recording

`scripts/install_usage_timer.py` stages a Linux **user** systemd oneshot service
and hourly UTC timer for the existing [safe collector](USAGE_ACCOUNTING.md).
Source implementation is not evidence of enabled units. The dated host activation
below is separate from the portable source tests and initial failed attempt.

Default invocation only prints/checks the plan; it does not write files or run
`systemctl`:

```bash
.venv/bin/python scripts/install_usage_timer.py
```

After reviewing the pinned repository `.venv/bin/python` and collector paths,
explicit installation writes only `aos-usage-record.service` and
`aos-usage-record.timer` under the current user's `~/.config/systemd/user/`, then
reloads that user's systemd definitions. **It does not enable the timer.**

```bash
.venv/bin/python scripts/install_usage_timer.py --install
.venv/bin/python scripts/install_usage_timer.py --enable
```

Enablement is separate and requires both exact matching installed units.
After disk and effective-path checks, enablement reloads user definitions before
`enable --now`; a failed reload prevents activation of a cached definition.
Directories newly created are `0700`, unit files `0600`, and service umask
`0077`; an existing user unit directory must be current-user-owned and not
group/other-writable (`0755` is accepted; `0775` is refused).
Unknown/conflicting files, symlinks and control characters are refused, never
overwritten. Literal paths are quoted with systemd `%` and ExecStart `$`
escaping; spaces are supported. The executable token uses literal `$`, while
subsequent arguments escape environment expansion with `$$`. Identical units
can be installed idempotently.
Before installation or enablement, read-only `systemd-analyze --user unit-paths`
discovers the effective search paths. Other same-name units or nonempty
specific, dash-prefix or type-wide drop-in directories are refused for separate
review. Unavailable effective-path discovery also fails closed. Default planning
does not invoke this command or establish absence of effective conflicts.
The planned unit directory must itself occur in those effective paths;
an alternate `XDG_CONFIG_HOME` cannot silently produce an unreachable install.

The timer runs at UTC hour boundaries while the user manager is available;
`Persistent=false` means missed hours are not backfilled. Login persistence/
lingering is not changed. The collector retains its immutable one-snapshot-per-
UTC-hour semantics under ignored `data/accounting/`; service output is discarded
and only safe errors reach the journal. No public report, Git command, network,
model invocation, runtime DB query or application lifecycle operation is added.

An unsuccessful systemd reload/enable may leave staged units; it is reported as
incomplete rather than treated as active. Review actual state with
`systemctl --user status aos-usage-record.timer` after explicit activation.
Automatic commit/push is **not implemented by this collector helper**.
The separately reviewed [aggregate publisher](USAGE_PUBLICATION.md) uses its own
timer and private bare repository; it never publishes private collector files.
See STATUS for its actual activation evidence.

Validation uses synthetic temporary paths and mocked systemctl only:
`.venv/bin/python -W error::ResourceWarning -m unittest tests.test_install_usage_timer -v`.

Real syntax verification is opt-in and checks temporary units only, without
installing or activating anything:
`AOS_VERIFY_SYSTEMD=1 .venv/bin/python -m unittest tests.test_install_usage_timer -v`.

3 October 2026 activation attempt: installation succeeded, but enablement's
service failed because the former quoted `WorkingDirectory=` value was treated
as non-absolute by systemd. The source removes that unnecessary directive:
ExecStart arguments are already absolute and the collector derives its own
repository root. Installed failed units require explicitly reviewed replacement;
this source correction is not proof that the timer or service is active.

Corrected source validation: 13 timer-helper tests and 15 collector tests passed
(28 total, ResourceWarnings treated as errors), including actual
`systemd-analyze --user verify` of temporary synthetic units with spaces, `%`
and `$` in their pinned paths. No units were installed by that test.

Source validation before this correction: 12 timer-helper tests passed; combined
with the collector's 15 tests, 27 passed with ResourceWarnings treated as errors.
Actual host read-only planning and effective-conflict discovery passed; its
existing user unit directory is `0755`. No units were installed or enabled by
these checks. Hourly recording activation was still pending at that observation.

**3 October 2026, 13:14 UTC host activation:** the exact failed unit was corrected,
both installed units passed actual `systemd-analyze --user verify`, and explicit
install/enable succeeded. Readback showed `enabled`, `active/waiting`, no drop-ins,
and the next trigger at 14:00 UTC. A manually triggered first service run finished
with `Result=success`, `ExecMainStatus=0`, producing the private `0600` snapshot
`data/accounting/2026-10-03T130000Z.json`. The 28-test corrected suite also passed
independently at root. This is actual accounting-service evidence, not an AOS
runtime restart, GPU/model test, provider invoice or automatic Git publication.

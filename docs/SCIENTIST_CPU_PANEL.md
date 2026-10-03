# Portable reviewed Scientist CPU panel

`python -m scripts.scientist_cpu_panel` makes the existing typed CPU composition
available to a fresh Linux checkout. The default operation is a local read-only
check. **It does not start a backend, Docker, an experiment or a GPU worker.**
The separate `--start` operation requires the deployment owner's explicit
authorization; UI-only permission is not sufficient.

This does not replace the previously frozen private
[CPU panel preparation](SCIENTIST_CPU_PANEL_HANDOFF.md). Its source, unit and
review directory remain unchanged. Moving that preparation to this launcher is
a new source/config review, not an automatic upgrade.

## Review and prepare

1. Use a separate source checkout and its own environment from
   [LOCAL_SETUP](LOCAL_SETUP.md). Do not update an active pilot's source.
2. Agree a fresh Scientist principal, exact loopback authority, single synthetic
   mode-grid suite and authorization context with the Scientist operator.
   Obtain an independently reviewed `ScientistCpuReviewedGrant` and matching
   `ScientistLabStartup`, using their existing canonical schemas. Never turn an
   unreviewed capability response into a grant. The proposal ceiling here is
   one experiment, at most 600 seconds and zero model tokens; the Scientist
   operator must also enforce the agreed total scope/budget.
3. Select a fresh project named `scientist-NAME`, a fresh explicit
   `app-<32 lowercase hex>` session identity and a non-default port. Port 8765
   is refused. Existing `app-*` entries cause refusal, even after a crash or
   clean shutdown: inspect them rather than deleting/adopting them blindly.
4. Prepare `<checkout>/data/local-app-project-scientist-NAME/` and `review/`
   as owned, non-symlink `0700` directories. Place reviewed `startup.json`,
   `grant.json` and `scientist.token` in `review/`, owned regular files at
   `0600`. The startup token path must name this exact private token. No secrets
   belong in command arguments, public docs or Git.
5. Prepare the project-private `ui/` using the existing named-project
   `prepare-ui` workflow; review its output before use. The UI root must be
   `0700`; files must be owned, non-writable by other users, single-link regular
   files. No links are accepted. Review the existing desktop image preparation
   and `<checkout>/models/desktop-manifest.json`; this command does not build or
   download an image, nor prove Docker availability during check-only.
6. Record and independently review exact SHA256 values for `startup.json`,
   `grant.json`, `MANIFEST.sha256` and `models/desktop-manifest.json`.
   `grant.aos_source_manifest_sha256` must equal the source manifest pin. The
   source manifest must match the full allowlisted source tree. The UI pin is
   `aos.contracts.digest({relative_asset_name: sha256(file_bytes), ...})` over
   all project-private UI files, including `index.html`. For local read-only
   calculation, `scripts.scientist_cpu_panel.ui_artifacts_sha256(Path(...))`
   implements that bounded inventory. Computing a hash is not approval.

The checkout, environment, manifests, private UI and review inputs must remain
frozen throughout the service lifetime. This is a trusted deployment-owner
boundary, not protection against that same OS user modifying running Python.
The launch command does not establish a fresh remote source-pair ACK.

Resource review must count the desktop container separately: the current
`DesktopRuntime` requests3GiB Docker memory even with fixture S1 and disabled
vision. It is outside the Python service's cgroup; `MemoryMax=2G` on that
service does not include or replace the container cap. Count all simultaneous
Scientist stages, AOS service, desktop and host reserve before authorizing
start. This portable launcher does not implement combined memory admission;
the reviewed deployment must supply that gate and finite CPU/runtime limits.
Insufficient RAM is not permission to shrink caps/reserve or stop user work.

## Check and explicitly start

Run from that checkout with its installed environment. Values below are
placeholders from the deployment review, not published identities or grants:

```sh
.venv/bin/python -m scripts.scientist_cpu_panel \
  --project scientist-NAME --session app-REVIEWED_32_HEX --port REVIEWED_PORT \
  --startup-sha256 REVIEWED_STARTUP_SHA256 \
  --grant-sha256 REVIEWED_GRANT_SHA256 \
  --source-manifest-sha256 REVIEWED_SOURCE_MANIFEST_SHA256 \
  --desktop-manifest-sha256 REVIEWED_DESKTOP_MANIFEST_SHA256 \
  --ui-artifacts-sha256 REVIEWED_UI_INVENTORY_SHA256
```

Without `--start`, success reports local configuration verification and explicit
`runtime_started: false`, `remote_requested: false`, `gpu_authorized: false`.
It creates no DB, directory or port reservation. It is not live API, runtime,
model or integration acceptance. The source identities and credential are
checked again on each invocation.

Only after separate start authorization, repeat the same reviewed command with
`--start`. Add `--local-ui-auto-login` only if reviewed same-origin loopback
tokenless access is intended. Otherwise the existing console prints the private
local login-token file path, not its contents. The backend currently creates
that unique `0600` temporary token beneath the separate checkout's `runs/` and
removes it during normal cleanup; it is not the Scientist API credential.

Startup reserves the exact loopback port before runtime creation and holds a
project-directory lock for the foreground lifetime. This lock serializes this
local launcher only; it is not a GPU scheduler or remote ownership authority.
The session directory is created exclusively and retained on failure. There is
no automatic orphan cleanup, restart, service installation or retry.

The existing entrypoint receives fixture S1, disabled vision, CUDA visibility
disabled, all console data stores scoped beneath the fresh session, and the
existing named-project UI/cookie isolation. It preserves typed policy, human
approval, persisted intent and independent readback. Native/shared GPU hooks,
model activation and experiment start are not launcher options. After launch,
the URL is `http://127.0.0.1:REVIEWED_PORT/ui/`; tunnel the same port if needed.
This is not the active pilot's URL or an automatically created Mac tunnel.

Local shutdown is not remote experiment cleanup. Resolve uncertain starts and
verify remote terminal/report/cleanup independently before retiring the scope.
Do not reuse the former CPU grant or run a second experiment outside the agreed
budget. Shared GPU acceptance remains Scientist-led under the existing broker.

## Validation scope

Synthetic tests exercise the public command through the existing console and
ASGI lifecycle, including project-private UI/cookies and local client cleanup;
Docker and the HTTP server process are substituted. Other cases cover unsafe
files, hash/source drift, mismatched owner/suite, excess budget, busy port,
duplicate startup/crash retention and no side effects in check-only mode.
These tests do not prove real Docker startup, a Scientist experiment, Mac
access, active-Scorer cancellation or GPU handover.

# W1b — Draft web application profiles

Implemented: local CLI registration and inspection of immutable profile revisions, plus an authenticated console form for new and successor drafts. The panel lists only canonical draft reports; it does not display URL, tenant/account-role fields, task details, credentials or raw stored profiles. The caller-controlled application key can itself reveal context, so it must remain non-sensitive. A missing store appears empty and a corrupt store fails closed. A console backend started before the new POST routes needs its next managed start; the UI reports that registration did not occur. This is onboarding configuration, not a general web task runner. Profiles do not change the offline Ubuntu browser, scheduler, model deployments or dataset eligibility. Full product gates remain in [WEB_APPLICATION_LEARNING](WEB_APPLICATION_LEARNING.md).

## Authenticated console draft flow

The Web applications panel also prepares a **separate task draft** from a registered staging/production profile. Select the exact profile, enter one task key already in that profile, a non-secret independent-verifier reference and a 1–8 page budget. `POST /api/web-applications/task-preview` constructs a canonical `WebTaskContract` on the server from the stored profile: exact entry URL/origin, `navigate`/`snapshot`/`verify`, matching page/navigation/action budgets, bounded duration, fresh per-action approval, and false network/collection authority. It returns only metadata and its SHA-256, without writing a file or making a request. A separate exact SHA-256 confirmation to `POST /api/web-applications/task-register` writes a content-addressed owner-only `0600` file under ignored `data/web-task-drafts/`; the directory is `0700`, existing mismatched/permissive files fail closed. The response gives the private relative path for `aos-v1 preview-remote-entry`. Both routes require the authenticated same-origin session and reject client-chosen paths, query/duplicate/extra keys, local-test profiles and unknown task keys. For 2–8 pages, separate `routes-preview` and `routes-register` calls require ordered exact same-origin canonical URLs and another SHA-256 confirmation before creating `data/web-route-drafts/<sha256>.json`; see [read-only routes](WEB_READONLY_ROUTES.md). The verifier reference is a label, **not a registered independent application oracle**. These drafts do not hot-configure a running session or grant site, account, execution, collection or training permission. Only a later explicit managed start with exact profile, task and route-plan pins can expose the separately approved one-GET or ordered-route pilot; form tasks still need their own CLI plans and budgets.

The Web applications panel accepts a new revision-1 scope draft or an explicitly selected successor of a registered draft. Selecting a parent fixes its application key, revision increment and exact `previous_sha256`; the other scope fields remain client-entered because the report-only inventory intentionally does not expose the stored URL, tenant, role or task details. The server checks the complete private parent lineage during both preview and registration, rejects mismatched tenant/role or missing/changed parent without exposing stored scope, and rechecks at registration to close preview-to-write races. Enter canonical URL and exact allowed origin, non-secret application/tenant/role/task keys, and optional separate S1/S2 learning requests. A learning request requires a non-secret data-rights reference, but the reference is not a reviewed grant. Do not enter passwords, session cookies, tokens or private data in any field. The form keeps values only in the current page state, sends a canonical sorted profile to `POST /api/web-applications/preview`, and requires a separate checkbox before `POST /api/web-applications/register` with the exact returned SHA-256. Editing any field or changing the selected parent invalidates preview/confirmation. Both routes require the authenticated same-origin session; no client-selected store path, unknown or duplicate JSON fields, execution/collection flags, or oversized request is accepted. Preview does not write. Registration is idempotent and uses the same immutable lineage checks as the CLI. Only a draft report is returned; no site is visited. No revision silently becomes active or changes an existing task/session. The CLI remains available for immutable revisions.

An isolated desktop test may select a separate host-controlled profile root via `scripts/serve_desktop.py --web-profiles-root PATH`; the web request cannot choose a store. The normal default remains `data/web-applications`. Current managed sessions do not hot-reload backend code; do not restart an active user task merely to expose the new routes.

## Profile contract

The canonical [profile schema](../schemas/web_application_profile.schema.json) and [report schema](../schemas/web_application_report.schema.json) accompany an explicitly [synthetic fixture](../examples/web_application_profile.json).

- `application_key`, `tenant_key` and `account_role` identify one application/account scope. These are non-secret labels, not authenticated account or tenant evidence.
- `entry_url` is a canonical absolute URL with an explicit slash/path; `allowed_origins` contains exact scheme/host/optional-port origins without paths. No wildcard, userinfo, query, fragment, percent encoding, backslash, path traversal or explicit default port is accepted. Remote addresses require HTTPS. Only exact `localhost`, `127.0.0.1` and `::1` are allowed for `local_test`; staging/production reject these and non-global IP literals.
- DNS names are validated syntactically only. DNS resolution, rebinding, redirects, subrequests, cookies and tenant isolation are **not enforced by this profile module**. Runtime network enforcement is still required before visiting any site. Internal/private-IP applications and query-based entry URLs need a future explicit policy, not bypasses to these validators.
- `task_keys` names intended workflows, not executable instructions or authorization. Task contracts, finite tools, action budgets and independent verification must be bound separately.
- `learning.system1` and `learning.system2` independently record `disabled` or `requested`. Either request requires a non-secret `data_rights_ref`; a reference is **not** a reviewed permission. `retention_days` is a requested policy, not an implemented purge service. `raw_screenshots`, `automatic_training` and `automatic_promotion` must all be false.
- Revision 1 has no parent. Later revisions reference the exact previous SHA-256, increment by one, and preserve application/tenant/role. Origins/tasks/preferences may change only through a new content identity. Revisions are bounded to 100. Branches can coexist; there is deliberately no implicit latest/active revision.

Every report says `draft`, with execution/collection/training/promotion closed. Requested roles add unverified-rights and missing-learning-pipeline blockers; they never imply model calls or generated training examples. Reports omit URLs and tenant/task details. Unknown fields, including credential fields, are rejected; this is **not a general secret detector**. Never put credentials or personal data into permitted labels or paths either.

## CLI walkthrough with synthetic input

From the repository root, using the installed application virtualenv:

```bash
umask 077
.venv/bin/python -c 'import json; from pathlib import Path; fixture=json.loads(Path("examples/web_application_profile.json").read_text()); destination=Path("data/synthetic-web-profile.json"); destination.open("x").write(json.dumps(fixture["profile"]))'
.venv/bin/python -m aos.web_application preview --profile data/synthetic-web-profile.json
```

The sample extraction uses exclusive creation and refuses to replace an existing file. The CLI expects the bare `profile` object, not the fixture wrapper. The `.invalid` site is synthetic and is never opened. Preview is read-only and prints the normalized profile checksum; it does not validate a parent in a store. Review the input and copy that checksum explicitly:

```bash
.venv/bin/python -m aos.web_application register \
  --profile data/synthetic-web-profile.json --confirm-sha256 <reviewed-profile-sha256>
.venv/bin/python -m aos.web_application inspect --sha256 <reviewed-profile-sha256>
.venv/bin/python -m aos.web_application list
```

Use `--store /absolute/private/store` **before** the subcommand to select another store. Its parent must already exist. Default: ignored local `data/web-applications`. A mismatched confirmation creates no store or file. Registration and inspection validate the complete stored parent chain. Re-registering the same valid content is idempotent; existing destination bytes are never overwritten. CLI failures are generic and do not echo submitted secrets.

## Persistence and failure boundary

The private store is owner-only mode `0700`; canonical JSON files are owner-only `0600` and named by their SHA-256. Descriptor-relative access rejects symlink directories/files, non-regular files, hardlinks, permissive modes, changed bytes and mismatched identities. Inputs are limited to 64 KiB, origins to 16, tasks to 32, and the store to 1,000 entries. Scope lists are sorted before hashing. These are integrity checks, not protection against an attacker already controlling the same host user.

Publication writes/fsyncs a unique temporary file, links it without replacing a destination, removes the temporary name and fsyncs the directory. Ordinary pre-publication failures clean up their own temporary file. A process crash between link and unlink can leave two names for one inode: reads then fail closed rather than repair or repeat registration. Orphan unpublished temporary files are ignored by listing but count against capacity. No automatic crash recovery, garbage collection or concurrent-writer availability guarantee is provided. Inspect interrupted stores manually; do not remove files blindly. This fail-closed crash window is covered by a real subprocess exit test.

No SQL schema changes were needed for these immutable local profile artifacts. The separate additive `0009_local_navigation.sql` migration widens only the fixed desktop task kind; it does not persist profile or learning-event relationships. Future run/profile and learning-event relationships require canonical schemas, append-only migrations and their own acceptance tests.

## Verified scope and next step

### W1c task/runtime binding draft

`aos.web_application_binding.bind_web_task` now checks one explicitly selected stored profile SHA-256 and its complete lineage before binding a finite task contract to a declared Ubuntu Chromium/Playwright MCP runtime pin. The task must name a profile task key, use the exact profile entry URL, and keep its origins within the profile. The contract bounds pages (1–8), navigations and actions (1–16 each), duration (1–900 seconds), a finite browser-tool list, an independent verifier reference, fresh per-action approval, and no automatic retry after an uncertain write. The pin declares the actual MCP runtime kind, container/image IDs, MCP bundle, worker source and runtime-pins digests. Revalidation reopens the immutable profile and recomputes task, runtime and combined hashes.

This is a **draft admission envelope, not a live admission**. Its runtime IDs, image and MCP hash are declared pins, not attested running-container evidence; `runtime_identity_verified`, execution and collection remain false. The current runtime is explicitly `network_mode=none`, and this module does not connect it to the task scheduler, resolve DNS, enforce redirects/subrequests, establish account/tenant identity, register a verifier, or open a site. A staging profile can therefore be bound on paper while the site remains unreachable. New profile revisions never silently update an existing binding. The canonical schemas are `web_task_contract`, `web_runtime_pin` and `web_task_admission_draft`; `examples/web_application_binding.json` is synthetic only.

`attest_web_task_runtime` adds a separate **read-only, point-in-time** observation for an owned `DesktopMCPBrowserRuntime`: it reopens the profile, checks the draft hashes, rechecks Docker ownership/network-none through the runtime, and compares the running browser/desktop IDs, image, MCP bundle, worker source, runtime pins and launch handshake. It rereads the pinned MCP bundle and returns only an `observed_match` report with `execution_authorized=false`, `collection_authorized=false` and `network_access_authorized=false`. Stopped or changed runtimes fail; the observation is not reusable as an execution lease or proof that a remote site/account is safe. The report schema is `web_runtime_attestation.schema.json`; the example attestation in `examples/web_application_binding.json` is explicitly synthetic, not a live result. Runtime stop after the observation can still race any future action, so actual execution must recheck identity at each action boundary.

An opt-in local-only [navigation pin](LOCAL_NAVIGATION_ADMISSION.md) and [POST-bearing staging pin](LOCAL_STAGING_ADMISSION.md) now recheck exact synthetic profile/runtime identity before task admission and gateway actions. They are not the generic admission described here. The next real-site admission milestone must bind the observed runtime and selected profile to the target task with per-action identity rechecks, enforce HTTPS/redirect/subresource/DNS/IP and account scope at every request, register the independent verifier, and only then provide a separate explicit execution grant. A profile, draft or point-in-time observation alone cannot authorize any action or learning collection.

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_web_application.py -v
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_web_application_binding.py -v
.venv/bin/python scripts/validate_package.py
```

Tests cover schema parity, separate S1/S2 preferences, URL/scope rejection, strict false authority, lineage, exact confirmation, restart, corruption, destination collision, interrupted publication and secret-safe CLI errors. Separate opt-in tests also inspect a real owned Docker/MCP runtime and reject it after stop. Actual counts and regression evidence are in [STATUS](STATUS.md). None prove real-site safety or learning.

Next W1 work: connect a selected profile/task/runtime binding to live execution with a separate explicit grant, implement network/authentication/tenant enforcement, then add minimized role/provenance-bound learning events while tasks run. Profile requests alone cannot authorize that collection. Real-site acceptance still needs the target URL, authorized account/tasks and reviewed data scope.

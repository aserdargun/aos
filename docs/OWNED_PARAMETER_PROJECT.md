# Owned multi-field parameter projects

This source CLI prepares private **synthetic** CRM or inventory fixtures offline.
It provides plan → exact request confirmation → provision → manifest verification.
Preparation does not start a listener, browser, desktop, model, task or managed
application. It does not admit execution, publish a skill, fabricate release
lineage, approve actions, train a model or promote a deployment. Real account or
intranet integration and two-application native acceptance remain separate work.

## Explicit managed bootstrap activation

After preparation and verification, a fresh project can be explicitly bound to
one managed session:

```sh
./scripts/aos-v1 start \
  --owned-parameter-project-directory /tmp/aos-parameter-review/crm-project \
  --owned-parameter-project-manifest-sha256 EXACT_PROVISIONED_MANIFEST_SHA256 \
  --owned-parameter-project-engine fixture
```

This new path is **CPU fixture-engine only**, not native Decider/Bonsai inference.
The explicit engine flag is required; no native model fallback or GPU prewarm is
allowed. Existing scalar modes are unchanged. The desktop/browser host still
requires the normal pinned local runtime prerequisites. Starting the session
does not automatically start a task or grant a released skill. Scientist remains
the sole shared GPU allocation authority; native project admission is pending.

An explicitly named manager can host the same freshly provisioned CPU project
without changing the ordinary port8765 session. Prepare its private UI first,
then carry the exact name/port on every manager command:

```sh
./scripts/aos-v1 prepare-ui --project parameter-demo --project-port 18766
./scripts/aos-v1 start --project parameter-demo --project-port 18766 \
  --owned-parameter-project-directory /tmp/aos-parameter-review/crm-project \
  --owned-parameter-project-manifest-sha256 EXACT_PROVISIONED_MANIFEST_SHA256 \
  --owned-parameter-project-engine fixture
./scripts/aos-v1 token --project parameter-demo --project-port 18766
./scripts/aos-v1 stop --project parameter-demo --project-port 18766 \
  --expected-session EXACT_APP_SESSION
```

Do not add `--fixture` to parameter-project startup: its existing persisted
manager mode is `real`, but the explicit parameter engine and vision are CPU
fixtures. This historical wire label is not a real-model claim. Missing source
pins or engine are rejected; no automatic native fallback or reuse. The source
TLS port must differ from both8765 and the selected UI port. Backend web profiles
come only from the exact verified project source; DB/UI/token remain named-local.
Generic named restart does not restart a one-use parameter activation. Use
explicit current-session shutdown; do not delete activation markers or replay.
The [Mac connector](PILOT_QUICKSTART.md) can attach to this already-running named
project using the same local/remote port and manual token login.

The launcher retains the source workspace lock and exact provisioned loopback
listener, verifies the source again, and forwards the exact original sources
without copying them into the tool workspace. It refuses another running managed
session rather than replacing or reusing it. Do not automatically stop existing
user work. The fixture port must be available and different from managed port
8765. A source activation marker binds this project to one session and remains
after a failed launch; a previously activated source is not automatically
reopened, cleaned up, overwritten or replayed. A separate newly authored project
is a new experiment, not reconciliation of an uncertain previous task.

In Tasks, the advertised task is the scoped `browser_remote_form`. Review the
private inputs and explicit task/action approvals before running. The EN/TR
multi-field project card distinguishes prepared, unresolved and accepted
bootstrap evidence; consumed or uncertain projects cannot be started again.
The existing finite operator, leased gateway and SQLite trajectory remain the
execution path. No unrestricted tool runner or new scheduler is introduced.

Before tools, an immutable private bootstrap intent binds source, manager
authority, workspace and original job. The original child run is bound before
actions. Only an exact succeeded original task, independently audited recipe
trajectory and one-use whole-record TLS readback can create an immutable accepted
receipt. Invalid source/control/journal, timeout, cancellation or partial outcome
stays unresolved; no blind retry. Read-only receipt metadata is exposed in the
Tasks snapshot. A receipt does not validate or release a skill, verify a real
site/account, prove native-model use, authorize training or prove GPU release.

Run from an installed development checkout with `.venv`. The existing application
dependencies and local `openssl` executable are required for provisioning; no
weights, GPU or network download is required. The shell command forwards to
`.venv/bin/python -m aos.owned_parameter_project_cli`.

Choose an existing owner-only (`0700`) parent outside the checkout and a **new**
project directory within it. Keep input and generated artifacts private and out of
Git. The CLI rejects existing project destinations during plan/provision; it does
not overwrite a project or clean up a failed preparation automatically.

For example, create an explicitly synthetic host-authored request:

```sh
umask 077
mkdir -m 700 /tmp/aos-parameter-review
.venv/bin/python - <<'PY'
from pathlib import Path
from aos.contracts import canonical
request = {
    'application_key': 'synthetic-crm-note',
    'port': 19443,
    'parameters': {'record-id': 'Synthetic Ada', 'note-text': 'Synthetic CRM note'},
}
path = Path('/tmp/aos-parameter-review/request.json')
with path.open('x') as target:
    target.write(canonical(request) + '\n')
path.chmod(0o600)
PY
scripts/aos-parameter-project plan \
  --request /tmp/aos-parameter-review/request.json \
  --directory /tmp/aos-parameter-review/crm-project
```

Inputs are UTF-8 canonical JSON: sorted keys, compact separators, optional final
newline, no duplicate keys or extra fields, and at most 8192 bytes. The input must
be a current-user-owned regular `0600` file with one hard link and no symlink path
components. Supported application keys are exactly `synthetic-crm-note` and
`synthetic-inventory-note`. Both require exactly `record-id` and `note-text`; each
value is bounded to 1–128 characters and 256 UTF-8 bytes. The CRM binds
`contact_name`/`note`; inventory binds `item_code`/`note`. These are owned fixture
fields, not discovered external application endpoints. A port is an integer
1–65535 except 443; preparation neither reserves nor opens it.

The plan prints scope, field names, request/directory/parameter hashes and explicit
false runtime/admission/training flags. It creates no project files. Review the
private request itself to inspect values: they are intentionally absent from CLI
output. The request SHA binds the application, complete parameter map, port and
absolute destination. Reformatting a canonical input with a final newline does
not change its semantic request hash; changing any bound value requires review
again.

After reviewing, copy the exact `request_sha256` from the plan into this command:

```sh
scripts/aos-parameter-project provision \
  --request /tmp/aos-parameter-review/request.json \
  --directory /tmp/aos-parameter-review/crm-project \
  --confirm-request-sha256 EXACT_REVIEWED_REQUEST_SHA256 \
  --human-confirmation PROVISION
```

The explicit literal `PROVISION` and SHA are required; a changed request or target
fails before provisioning. This command creates private immutable fixture
artifacts and a manifest; it prints the manifest hash without parameter values or
credentials. Keep that hash independently as the reviewed integrity anchor.

```sh
scripts/aos-parameter-project verify \
  --directory /tmp/aos-parameter-review/crm-project \
  --manifest-sha256 EXACT_PROVISIONED_MANIFEST_SHA256
```

Verification checks current artifacts against that exact manifest and fails
closed on a mismatched pin or altered project. It does not start anything or
grant execution admission. Commands print canonical JSON and return 0 on success,
1 on rejected filesystem/request/artifact checks, and 2 on invalid CLI arguments.
Rejections omit raw exceptions and private values.

Targeted CPU checks:

```sh
.venv/bin/python -m unittest discover -s tests -p test_owned_parameter_project_cli.py
```

The tests exercise both synthetic applications, changed review inputs, malformed
or duplicate JSON, file privacy, checkout exclusion, fresh-process public wrapper
invocation, manifest-pin mismatch and changed current source rejection. CPU
fixture preparation is not real S1/S2 execution or
native application acceptance.

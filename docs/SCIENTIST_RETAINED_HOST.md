# Explicit retained-control host

`ScientistRetainedHost` groups the existing evidence-v3 client, original-store
journal and proof-backed resolution API. It does not implement a scheduler,
invent a socket endpoint, read Scientist's private database, admit inference or
claim GPU cleanup. The host/store belong to the caller; the adapter does not
restart the application or close its database.

## Construction

Supply the original `TrajectoryStore`, immutable admission history2.0 and
current `ScientistIntentBinding`; an explicitly configured absolute private
control socket; reviewed full schema bytes and transport/evidence SHA pins;
broker authenticator; independent retained terminal verifier; current target
authorization and separate resolution authorization callbacks. Defaults deny
dispatch before connecting. Model output cannot supply trusted callbacks or
expand any of these permissions.

Constructor inspection requires existing canonical26 metadata and original
control/resolution tables. Missing or malformed metadata is denied with a clear
error before connection; no automatic migration or new database is created.

`ScientistDesktopBinding.create_retained_host(current_binding, **host_options)`
constructs the same adapter only when the supplied session/runtime/owner/lease/
generation matches the current controller and original history/store. This
read-only construction is not a new grant. Fresh HUMAN cleanup still requires
explicit trusted rights; inference's old AGENT lease is not inherited.

## Separate operations

- `discover(request_id, control_id)` explicitly requests a retained-target capability for an existing original inference.
- `reconcile(request_id, capability_control_id, control_id)` explicitly requests evidence using the exact capability preimage in that same store's successful discovery ACK.
- `resolve(reconcile_control_id, capability_control_id, response_sha256=...)` uses the existing stored reconcile ACK and resolution verifier; it does not contact the socket.
- `inspect(control_id, capability_control_id=...)` and `inspect_resolution(request_id, capability_control_id)` are historical reads, not dispatch or fresh authority.

These are trusted host calls, not new HTTP endpoints or automatic UI effects.
There is no method that silently chains discovery, reconciliation, resolution
and new inference. Each effect has its own durable intent and explicit ID.
Current providers, original target/profile/deployment/schema pins and retained
capability hashes are checked by the existing components.

## Ambiguity and restart

Single-flight prevents simultaneous mutating host effects. A send with uncertain
outcome latches that host; it cannot dispatch again. A fresh host cannot bypass
the original SQL pending-target fence when no response was recorded. Reusing
any existing control ID for dispatch is denied even when its ACK exists.
An independently validated immutable ACK can be read or explicitly resolved
after lost publication; this is not blind replay of the remote operation.
Existing exact resolution retry still requires fresh authority/full proof.

No budget, owner, lease, generation or original receipt is reset. Resolution
only makes a fresh authorized request eligible; it never publishes model output
or obtains a new GPU allocation. Caller transfer and abandoned controls without
independent ACK evidence remain unsupported.

## Explicit original-client rearm

Durable resolution alone does not clear an existing inference client's uncertain
or cancelled latch. `ScientistDesktopBinding.rearm_retained_client(host,
request_id, reconcile_control_id=..., capability_control_id=...,
response_sha256=...)` checks the original store/history2.0, live controller fence,
original broker client and exact ACK target. It invokes `host.resolve` again with
fresh current authorization and full independent proof before clearing the
matching local latch. Historical inspection or a saved resolution hash alone
cannot authorize rearm. This operation does not contact the socket.

The lower sync/async `rearm(request_id, verify_resolution=trusted_callback)`
defaults to denial; its trusted callback must complete with `None` or raise.
It must implement fresh independent resolution, not a model-supplied boolean.
The exact original request must be latched. Active local tasks or a busy native
transport lock deny rearm, even if a wrapper task was cancelled. Failure leaves
the latch intact. Successful rearm preserves attempted request IDs and the
256-attempt lifetime limit, socket/authentication, callbacks, schema/deployment
pins, original records and budgets. No request is retried or dispatched; a fresh
task still needs normal typed authorization/admission. Missing original proof
after cancellation before durable dispatch remains denied by the desktop hook.

## Acceptance boundary

`retained_host_candidate_v3` is an explicit55-source observation, not runtime
admission. Production provider/source/config review and Scientist-only real
coordinated GPU acceptance remain mandatory. Synthetic UDS/SQLite tests cover
host composition and negative gates; they do not execute Scientist or prove
physical GPU release. No live deployment is performed by adding this adapter.

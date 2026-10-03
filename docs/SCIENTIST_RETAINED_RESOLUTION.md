# Successful Scientist turn resolution hook

## Implemented AOS interface

Trusted native bootstrap may call `scripts/serve_desktop.py`:

```python
main(scientist_retained_resolver_factory=reviewed_factory, ...)
```

This is a Python host configuration parameter, not an HTTP, CLI, model or tool
input. Factory configuration requires Scientist mode, original admission/output
configuration, and a callable `verify_configuration()` returning `None` or
raising before Desktop starts. Scientist supplies reviewed runtime/source/schema
verification; AOS does not import Scientist code or grant authority by accepting
a callable. The factory is passed as `resolver_factory` to
`create_scientist_desktop_scheduler`.

On the original owner event loop/thread, AOS invokes:

```python
resolver = reviewed_factory(binding, current_intent_binding)
result = resolver.resolve_successful(original_request_id)
```

The trusted method may return its typed result synchronously or as an awaitable
on that same loop. SQLite/controller ownership is never moved to another thread.
Result type: `aos.scientist_successful_resolution.ScientistRetainedResolutionResult`;
canonical internal schema: `schemas/scientist_successful_resolution.schema.json`.
Fields bind original request/admission, reconcile control/ACK, terminal receipt,
current intent binding, resolved state and unchanged original intent.

`ScientistAsyncTurnClient` waits for the configured hook **after** durable receipt
recording and **before** returning successful inference. It uses the original
turn deadline; no retry or renewed deadline. AOS checks current task authority,
exact active request ID/task/context before and after resolver invocation,
same binding/history, idle transaction ownership, unchanged original receipt,
and exact typed-result equality with the committed resolution row. An absent
factory installs no automatic resolver: unresolved admission remains denied.
Misconfiguration, missing resolution, wrong target, owner change, timeout or
cleanup failure never becomes a successful resolved turn. Cancellation retains
the existing conservative cancelled-client behavior; success does not rearm.

## Scientist responsibility and acceptance boundary

Scientist composes the existing retained host discover/reconcile/resolve chain,
authenticated channel, original budget witness and independent physical proof.
Default authority callbacks deny. Canonical done, historical ACK or GPU idle
alone cannot substitute for proof or control/resolution authorization. No new
listener/scheduler or manual SQL resolution is introduced by this AOS hook.

CPU synthetic original-SQLite tests cover the hook and existing resolution
library. Actual new native factory/channel integration, two successive shared
calls, fairness and cancellation/recovery remain **unverified** until both
projects agree new pins and Scientist executes a fresh bounded acceptance run.
The earlier real hello/research/report run is preserved; its ambiguous second
POST must not be replayed. HTTP JSON409 improves denial reporting, not GPU proof.

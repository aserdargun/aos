# Authenticated original-source provider composition

AOS implementation: `src/aos/scientist_retained_provider.py`, with
`ScientistRetainedProviderClient` and `ScientistRetainedProviderAdapter`.
The adapter exposes `create_budget_verifier(schema_sha256=...)`, `read_source`,
`verify_source`, `verify_resolver` and `verify_physical` for the existing proof
composition. Construction does not activate a channel or grant authority.

Producer reference: Scientist `db344fcd5bb2cae15a0f2b616e0be1e2666d49ae`.
Its existing canonical broker exposes an explicitly inherited Linux Unix
`SOCK_SEQPACKET` channel with private protocol
`aos-scientist-retained-provider.v1`, integer version1. There is no new pathname
listener, discovery endpoint, scheduler, lease authority or private DB access.

## Authority boundaries

The producer handles only `read_budget` and `verify_physical`, through its existing
`LabAOSControl` and original store. The target, deployment, original generation
and exact retained-discovery capability are checked under current rights. These
reads do not release resources, stop workers, grant a cleanup right or resolve an
AOS inference. AOS control and resolution rights remain independently required.

Each packet's kernel `SCM_CREDENTIALS` identifies the actual sender. Creation-time
`SO_PEERCRED` cannot authenticate an inherited channel. Explicit pinned broker
generation and current authenticator checks are necessary; a broker restart must
deny, not silently rebind the original request. Sequence numbers are one-use with
one outstanding exchange. Packets/deadlines are bounded; truncation, received
FDs, wrong credentials, mismatch or uncertainty must fail closed without retry.

The original admission history must remain version2.0 on the same controller
store. Capability and reconcile responses are selected by exact immutable
successful ACK IDs and hashes, then read back before/after source observations.
The complete reconcile evidence, including `result_canonical`, must be forwarded;
reconstruction from terminal/drain callback arguments would lose its closure.

## Budget and physical meanings

Budget verification must pair the same adapter's `verify_source` with its
`read_source`. The former checks candidate consistency/current rights only;
it is not independent source observation. A complete verifier must independently
read and compare the whole witness, including allocation and original budget.

Successful `verify_physical` is an authenticated assertion that the pinned
canonical producer's independent OS observer completed for the exact retained
evidence. Snapshot v1 has no GPU UUID, observation transcript or separate physical
status field. Do not describe it as AOS UUID-specific observation or zero VRAM.
This composition must not replace physical proof with idle flags or claimed hashes.

The independently reviewed source/config closure must cover producer
`aos_retained_provider.py`, `aos_physical_readback.py`, **`native_runtime.py`** and
their actual dependencies, together with the AOS client/adapter. The producer's
minimum required source set alone is insufficient: native runtime owns systemd,
NVIDIA and deadline behavior but is not required by that minimum. The host must
verify the expected closure, not simply trust whatever fingerprints are advertised.

## Remaining production admission

Root116 CPU/synthetic tests passed in35.230s:
`data/scientist-retained-provider-root-20261001.log`. The19 new tests use actual
kernel packet credentials and owned socketpairs. A full original SQLite resolution
performs two five-read proofs and records one append-only resolution without
changing the original intent/receipt. An exact resolution retry performs fresh
proofs again; only historical `inspect_resolution` adds no network packets.
Wrong capability ACK, source/right revocation, original active/quarantined target,
child provenance, fence regression and postphysical budget mismatch retain zero
resolutions. Packet timeout/truncation/sender/FD/sequence errors close without retry.
The first root run found an incorrect test expectation of10 packets after a fresh
retry (actual20); the expectation was corrected, not runtime proof weakened.

These are synthetic provider responses, not actual Scientist/GPU execution. The
current58-member `retained_provider_candidate_v1` source profile adds this module
without rewriting older membership. Independent review found no blocking protocol
regression under the mandatory expected-source/current-rights contract.

Trusted bootstrap must explicitly provide the inherited FD to the same broker
and authorized AOS process; it must not open a replacement listener or another
scheduler. Async host callback/thread ownership and bounded sequencing must be
reviewed before activation. Matching source versions and CPU packet fixtures are
not actual GPU release, deployment or coordinated acceptance. Only the Scientist
session may manage the agreed real GPU/cancellation acceptance, without stopping
user work. Integration remains **partial** until these production gates are proven.

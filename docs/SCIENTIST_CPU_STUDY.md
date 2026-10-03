# Scientist CPU study description

This source-only addition consumes the sibling's proposed
`scientist.lab-cpu-study.v1` contract. It does not change the published v0.1.0
tag, install a runtime, grant execution, or certify a joint experiment.

## Wire review and peer handoff

AOS independently reviewed the descriptor producer, parameter-grid canonical
hashing, and the peer's schema and explicitly synthetic example. The schema
SHA256 is `263f588369cdc78f1f904422c45505f097a36fdb495abbd68320eb45825bcefa`;
producer SHA256 is `7aa5e98413e7964e757adde7be64a76d4760bda70fd58e0a046c79a9a6930e00`.
The exact schema is retained as
[`scientist_cpu_study_wire.schema.json`](../schemas/scientist_cpu_study_wire.schema.json).
The [example](../examples/scientist_cpu_study.json) contains no actual owner,
runtime grant, dataset or experiment. Its identities are inert.

Scientist's working checkout was observed at
`55c5300600112ab823f76ec434029d6dc23e513c` with local changes. The producer was
reviewed in its separate prepared runtime source, not assumed to exist in that
Git tree or to be running. Its hash matches the peer's descriptor source record.
This distinction is essential: a checkout HEAD is not the runtime identity.

**AOS source-side wire review:** accepted for bounded metadata readback with
the stricter checks below. This is not a reciprocal peer ACK, fresh source-pair
freeze, deployment approval or runtime capability attestation. The inter-thread
transport is unavailable; the Scientist session can read this document without
waiting or modifying AOS. Before a real run, Scientist must confirm the same
schema, current producer/configuration identities and a new explicit CPU scope.
Previous acceptance credentials, ports and retired scopes must not be reused.
GPU acceptance remains Scientist-owned and on HOLD.

## Authenticated read-only flow

Only `ScientistCpuLabService` advertises `cpu_study_supported: true` in local
inventory. Inventory remains local and performs no remote request. The user
explicitly selects **Read study description** in the EN/TR Scientist panel.
Authenticated `GET /api/scientist/cpu-study`, without query parameters, reads
exactly `GET /v1/aos-cpu-study/{reviewed_suite}` through the existing configured
loopback client, credential and reviewed grant. No supplied URL, suite or token
is accepted from the browser. Legacy/non-CPU services reject this route.

The descriptor must match the entire reviewed CPU capability and its hash.
Strict schema/version, duplicate-key, integer/Boolean, response-size, deadline,
owner and transport checks fail closed. Snapshot hash, unique sensors, split
counts and ordered UTC timestamps must be coherent. Ordered prefix length must
match the registered count and experiment ceiling. Every configuration must
contain the complete normalized parameter set, match its method/position and
canonical content hash; the LSH merge-table bound is preserved. Omitted defaults
are deliberately rejected rather than reconstructed into an unobserved response.

The HTTP read runs off the event loop, shares the existing client's exclusion
and tracked cleanup handle, and refuses active/uncertain controls. Cancellation
retains that handle until the bounded worker finishes; cleanup timeout is not
successful drain. Controller/session/generation and transport are rechecked
before returning. Shared drain or closing disables new reads. There is no POST,
job, approval, action intent, reservation, second scheduler or automatic retry.

## What the panel does not prove

The result is explicitly metadata-only. Snapshot content and candidate source
are not downloaded or independently rehashed. A partial registered prefix cannot
reconstruct the full provider-grid hash, so no full-grid verification is claimed.
Candidate hashes and source summaries are reported metadata, not code or raw-data
verification. There is no automatic experience retrieval, method reordering,
dataset registration, model selection, training or hidden task submission.

`budget.experiments=N` still selects the first N registered configurations;
display order is not a performance ranking. Evaluation input rows precede
embargo/masking and are not scored sample counts. Metadata never grants approval:
the unchanged proposal/action path freshly verifies the capability and persists
the approved intent before any experiment effect. A changed grant fails closed.

CPU/mock and real Chromium-with-mocked-API evidence are in dated [STATUS](STATUS.md).
The prepared source needs separate deployment and actual authenticated remote
readback before claiming user-console integration. Owner-bound
[`/experience` readback](SCIENTIST_EXPERIENCE.md) is now implemented in source;
its real acceptance, optional-context real acceptance and active-Scorer
cancellation remain open.

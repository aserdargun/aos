# Isolated Scientist CPU panel — prepared, not started

3 October 2026: the existing AOS pilot remains untouched. A separate, inert
CPU console is prepared under `<AOS_ROOT>/data/scientist-cpu-panel-20261003/`.
This is the next actual panel acceptance preparation, not another GPU scheduler,
runtime admission, a completed experiment or full-product acceptance.

## Prepared artifacts

| Artifact | Identity / state |
|---|---|
| Frozen AOS source | `a892a8926e6916c97532f4732c4127a6e412ef71` |
| Source archive SHA256 | `aa176c6cb3735eb1bde2c14a9cccf73133014d30e18beab7a5a9e61db9bafa7d` |
| Source manifest SHA256 | `2044a3d88ec79daba3fc6bda50d10a1750e2b54c5d721b49b14234dfbec2d0e5` |
| Workspace | `workspace/`, separate and empty |
| Private runtime/config manifest | `preparation.private.json`, preparation only |
| Service draft | `aos-cpu-panel-20261003.service.draft`, not installed |
| Launcher | `launch.py`, pinned source/UI; reviewed config hashes required |
| Review inputs | `review/startup.json`, `review/grant.json`, `review/scientist.token`: absent |
| Proposed AOS port | Loopback `8771`, observed free, not reserved |

The private preparation manifest records exact launcher/unit hashes and the
remaining inputs. It is not a complete authorized runtime/config manifest:
there is deliberately no invented Scientist URL, owner, principal or credential.
The source freeze predates the accounting-only `4a2b9c0` publication. Do not
silently replace it with current main after a grant has named its manifest.

## Reciprocal source evidence

Scientist's dated `126-aos-next-source-handoff.md` and
`review-evidence/aos-cpu-panel-source-pair-20261003.json` report narrow source
alignment for Scientist `bdad5b24eaa62519af7ca09a7ee9acaf17fc8355` and AOS
`ed209498afa1e9c6a4c4573d0c0e8daff2a2ec17`. The Scientist commit is peer-reported,
not the operational Scientist checkout HEAD. AOS independently matched all
eight consumer file hashes in that record against this frozen console source.
Record SHA256: `e12a59b96545edeb4d669ba81f3447785aea5b639576bb342a5156e5f26ce655`.

Contracts remain `scientist.lab-cpu-study.v1` and `run-experience.v1`.
Source compatibility is not a fresh scope agreement or runtime/GPU ACK.

## Evidence and next action

Offline setup used existing caches in the separate environment; 5729 source
package checks and 15 focused CPU tests passed during preparation. A subsequent
check-only invocation rejected the draft's missing reviewed hashes before any
database creation. The proposed unit read back `not-found`/`inactive`/PID 0;
port 8771 had no listener. These are dated inert checks, not launch acceptance.

Scientist must supply a fresh proposed owner/principal and isolated API/PG/study
scope; both sides then review exact startup/grant identities. Starting this
separate backend still requires explicit authorization: UI-only permission does
not authorize it. Do not reuse the retired CPU grant or the active user pilot.

The bounded proposal is one synthetic `mode-grid` candidate, at most 600 seconds,
zero model tokens and no GPU allocation. Actual browser acceptance must cover
descriptor → field intent → proposal/human approval → experiment → independently
verified report/experience → explicit history selection into a new draft.
A second experiment is outside that budget. Clean up only this new scope.
Active-Scorer cancellation and joint GPU acceptance remain separate, unproven
requirements; GPU HOLD and Scientist-only GPU execution remain unchanged.

## Later portable source addition — not applied to this freeze

The public [CPU panel launcher](SCIENTIST_CPU_PANEL.md) now provides the reviewed
composition without a machine-specific Python launcher. It requires a fresh
named project, exact source/config/private-UI pins and separate start consent.
This frozen workspace, unit draft and absent grant remain unchanged; the new
source is not an implicit replacement, deployment or reciprocal runtime ACK.

Scientist's 19:42 UTC handoff was read on 3 October: it reports a separate CPU
deployment at `8f9a10bc6e6b7e665df48f74348b30e9d3ad6dff` and later source-only
native-launch fencing work, explicitly without reopening the retired AOS scope.
Those peer-reported observations do not replace the source pair recorded above.
The session message transport still failed at `127.0.0.1:34321`; no new message
delivery or ACK is claimed. A fresh CPU scope and shared finite-activation
contract remain the next reciprocal inputs.

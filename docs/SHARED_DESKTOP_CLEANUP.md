# Shared desktop physical cleanup observer

Source implementation, **not enabled runtime cleanup**. The AOS
`SharedDesktopCleanupObserver` supplies concrete read-only Linux observations
for an originally entered desktop service. It never stops a process, removes a
container/token, releases a lease, writes a closure or launches another job.
Its return value is not `SharedCleanupProof` and cannot discharge the host's
default-deny cleanup gate by itself.

## Required original review

Trusted composition supplies the original complete `SharedDesktopState`, a
`ReviewedDockerDaemon` (daemon ID and root-owned fixed Docker socket device /
inode), and the original mount/cgroup namespace inode identities. Capture and
review these **before original launch**. Observing whichever daemon is available
after failure and approving that identity would create false absence evidence.
The observer has no automatic capture/adoption fallback.

The default-deny `verify_scope(state, deadline)` must independently authenticate
the original request/binding and current finite cleanup rights, native exclusion
and irreversible **original spawn-path fencing**. Same UID, a state hash or an
absent unit is not sufficient. Its immutable/deep-copied state may not be replaced
by a wire-supplied target. The callback must return `None` or raise, not a boolean.
It is checked before, between and after the two physical samples. The original
BOOTTIME deadline is capped at ten seconds; individual commands at two seconds
and16KiB. Late responses and uncertainty fail closed without retry.

## Concrete observations

- Original boot/PID namespace/UID comparability, original process absence and
  conservative rejection of any remaining/reused original PID.
- Exact systemd unit/invocation/control group, terminal state and no pending Job;
  collected units require an exact absent-property set, not a missing MainPID alone.
- Reviewed observer mount/cgroup namespaces and real cgroup2 mount; bounded,
  no-follow recursive original cgroup inspection, including descendants and
  `populated`. Missing directories only pass for a collected unit.
- Original workspace inode/UID/path, private permissions and held nonblocking
  workspace lock; complete linked private lifecycle journal and exact original
  process/container binding. A crash need not have written `removed`, but all
  physical checks still apply. A journal marker alone never proves removal.
- Same reviewed Docker socket and daemon before/after bounded read-only container
  queries; original full ID, original name and original runtime label all absent.
  A different daemon, name reuse, label conflict or unavailable Docker fails.
- Original token path absent, including rejection of dangling symlinks; original
  private token-directory identity unchanged. Lifecycle and both physical samples
  remain unchanged through the final authority check.

Result digests bind the original state/service, journal, reviewed daemon and
observer namespaces. This is an in-process observation record, not a new public
wire schema or durable release authority. Existing lifecycle/state schemas and
migrations remain unchanged.

## Still required for actual integration

Scientist must independently verify its authoritative original-generation
worker drain/no-admission/quarantine state and implement `close_launch` against
the original immutable consumption. The original manager/descendant retirement
and unresolved start-job fencing contract must be implemented, not replaced
with a no-op callback. **Consumed but never entered** targets remain unsupported
by this observer; missing service/lifecycle/token bindings deny.

The existing host cleanup hook remains default-deny. Pre-launch trusted review
capture, durable original environment binding, separately authorized stop,
final host/protocol composition and reciprocal complete source/policy review
are still open. This code does not permit the current user runtime to be stopped.

The reused cgroup scanner now lives in `aos.linux_cgroup_observation`; existing
Scientist physical-release checks delegate to it without weakening their
GPU/source/fencing gates. Include this **new transitive source file** in future
reviewed source closures. Do not mutate frozen CPU preparation pins.

CPU/synthetic checks cover owner/generation, revoke/fencing failure, repeated
readback, late responses, crashes, failed cleanup, container/token reappearance,
foreign daemon/namespace, populated descendants and directory replacement.
These tests do not establish real systemd/Docker cleanup or GPU release.
See [STATUS](STATUS.md) for the dated executed suite and local evidence.

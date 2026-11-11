# Staged attempt adapter

`StagedAttemptAdapter` is an injection-only join of the existing Sandbox lifecycle,
CoderLoop workspace interface, SnapshotWorkspace and StagedRunnerBridge.
It does not select a provider, activate a CLI, execute host commands or contact Docker.

## Explicit dependencies and states

Supply a trusted lifecycle (`create/start/remove`), factory
`attempt_id -> SnapshotWorkspace`, already-open private staging-parent descriptor,
manifest tuple, and trusted staged runner. Construction stores dependencies without
dispatch or filesystem staging. The caller retains ownership of its descriptor.
No mutable snapshot or bridge is exposed as part of the public adapter interface.

Create consumes the sole attempt before lifecycle creation, forwards the exact
policy object and timeout unchanged, then invokes the factory and constructs the
bridge. Normal states are new → creating → created → starting → running →
removing → removed. Failed create/start/execute use corresponding *_failed states.
A create/start/execute failure still permits one removal using the same attempt ID.
Wrong IDs, repeat create/start/remove, pre-start execution and post-removal workspace
operations are rejected. This is single-caller coordination, not a thread-safe API.

While running, context, patch and diff use the same snapshot. Execute goes only
through the bridge; assess goes only through its exact-result/current-generation
gate, never directly to the snapshot assessor. Bridge bounds are passed unchanged
to its existing validation; operation bounds are passed unchanged to delegation.
The existing bridge may reduce a timeout/output cap to its own configured budget.
Python checks cannot preempt a blocked dependency.

## Cleanup and unknown outcomes

Removal is consumed before either cleanup step, with no automatic retry.
Bridge close runs first; lifecycle removal is still attempted if close raises.
Both failures are preserved in SandboxCleanupError (lifecycle cleanup_error,
descriptor original_error). Either failure leaves remove_failed, not removed.
The enclosing Sandbox retains its original failure and this cleanup error.

Closing the bridge only releases its duplicated descriptor. The owning lifecycle
must confirm removal of stage resources/processes; this adapter performs no
filesystem deletion. A bridge/runner failure is terminal; no alternate backend,
generation retry or implicit dispatch is selected.

## Verification scope

Three pytest functions cover a real synthetic CoderLoop/Sandbox/snapshot/bridge
vertical slice (baseline suite pass, reproduction failure, one patch, reproduction
and suite pass, fresh staged hashes and confirmed lifecycle cleanup); lifecycle
ordering, wrong IDs and partial create failures; and unknown runner/binding outcomes,
unconfirmed cleanup and simultaneous close/removal errors.

Fixtures read/hash regular bytes only and use fixed synthetic model receipts.
They never execute staged source or contact a real provider. These tests establish
coordination behavior, not real Docker isolation, runner execution, process cleanup,
source acquisition or production readiness. Real lifecycle/runner implementation
and production integration remain unfinished.

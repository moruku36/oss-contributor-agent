# Docker Engine lifecycle wire adapter

This increment encodes Engine API lifecycle requests. It is NOT a bundled Docker
backend and is not selected by the CLI or any production configuration.
An explicitly injected trusted transport is the only dispatch capability. No
socket, HTTP client, subprocess, credentials, image pull, host execution fallback
or retry is provided here.

## Contract and acceptance boundary

The adapter is single-use and single-caller. Construction does not dispatch.
Create/start/remove consume their operation before dispatch; an unknown result is
terminal for that operation, but a failed create/start may still be removed once.
The container name is derived from a validated attempt ID and a random per-instance
ownership token. Cleanup looks up that exact name, verifies ownership and immutable
container ID, deletes only that ID, and confirms an inspect 404. An accepted delete
request alone is not cleanup evidence. Ownership mismatch must never cause deletion.

Only digest-pinned pre-provisioned images are accepted. The create payload fixes
non-root user, no network, read-only root, all capabilities dropped, no-new-privileges,
CPU/memory/swap/PID limits, and no mounts, credentials or inherited host environment.
A trusted explicit idle entrypoint replaces image defaults. Inspect checks the
requested settings after create and before start, and verifies running state after start.
Cleanup verifies identity/ownership only: security drift must not prevent deleting
our own container. It never deletes a foreign or unidentifiable container. Unsupported
or mismatched settings are errors, not permission to relax the policy.

The trusted transport must implement API versioning, bound response capture and
JSON parsing in flight, honor a decreasing deadline, and perform no implicit retries
or redirects. Local elapsed-time/response checks are additional validation only.
Daemon authentication, image provenance and request/response integrity are external
trust requirements. Inspect is consistency evidence from a trusted daemon, not
cryptographic attestation or protection against concurrent privileged mutation.

## Not implemented or accepted yet

- Actual Engine socket/HTTP transport and a real Docker acceptance test.
- Test execution, staged-generation transfer/binding, bounded streaming output,
  structured test-result assessment, process deadlines and idle-lifetime watchdog.
- Staging-directory cleanup; container absence says nothing about host stage files.
- Source/dependency acquisition, runtime patch provider and production CLI wiring.

Therefore this wire adapter alone does NOT satisfy the complete SandboxBackend
contract, and must not be used to advertise isolated execution or cleanup of an
entire staged attempt. The current collect-only gate remains unchanged.

## Bounded verification

Three new pytest cases are written to cover the lifecycle request sequence/security payload,
unknown creation and ownership-safe cleanup, and rejection/terminal failure paths.
They use a deterministic in-memory Engine transport only; no source or shell is
executed and no daemon is contacted. Planned budget: local3 + PR3 + future
owner-merged main3 = 9/10 executions, one invocation at each stage. Prior trials
remain sealed and are not repeated. Current actual executions: **0**. The first
local pytest request was denied before dispatch because approval could not safely
bind the interpreter/runtime command; no alternate interpreter or CI execution
was used. The owner then approved the exact test command, but the unchanged
native approval submission was again denied before dispatch. Owner approval is
received; supported native command binding remains unresolved.

At the owner's request this increment is being published as a **draft for review**,
not as verified work. The publication commit uses GitHub's standard `[skip ci]`
instruction to prevent the PR event from executing the blocked tests. CI workflow
and test definitions are retained unchanged; skipped/pending checks are not passes.
Do not mark ready or merge this unverified draft. Resolve the native binding issue
and verify the exact three cases before a separately reviewed CI-enabled update.
Actual test executions remain **0**; no Docker daemon has been contacted.

## Initial-version progress

Provisional overall completion: **35%**. Completion means collection -> isolated
correction -> verification -> human-review report -> approval-gated draft PR.
Phase 1 collection and the synthetic correction loop are verified. Real isolation
and the production end-to-end path are not. Wire-only checks do not increase this
percentage; it is a functional estimate, not a file/test-count calculation.

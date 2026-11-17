# Durable execution attempt consumption

`SQLiteAttemptJournal` and `journaled_stage_and_run` in
`src/sandbox/execution_recovery.py` add an opt-in trusted host wrapper around the
actual `seatbelt_stage.stage_and_run` API. Existing callers and delivery code are
untouched. This work is newly authored against baseline `2bef2b8`; it does not
resume an earlier uncertain invocation.

```python
journal = SQLiteAttemptJournal("/private/host-state/attempts.sqlite")
staged = journaled_stage_and_run(
    journal, "job-42.attempt-1", "/private/stages", candidate, driver,
    executable, runtime_roots, timeout=3.0,
)
```

The host supplies a stable, explicit identity: 1–128 ASCII characters matching
`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. It must reuse that identity when revisiting
the same logical attempt. Choosing a new identity, another database, deleting a
record or restoring an older backup defeats consumption; these are trusted host
decisions, never automatic recovery actions. Inputs remain exact `bytes`, each
1–65,536 bytes. Their SHA256 hashes are bound in a committed `STARTED` record
before staging is invoked. An existing identity always blocks execution,
including when supplied hashes differ and for every stored outcome.

Each transaction explicitly begins, commits or rolls back. Claims use SQLite
`BEGIN IMMEDIATE` plus a primary key, so independent journal handles/connections
cannot both claim one identity. Connections require DELETE rollback journaling
and set and verify `synchronous=FULL`. Initialization commits the schema and
fsyncs the containing directory. Schema/application versions, exact schema and
record versions are checked; no migration or repair is attempted. Records hold
only bounded IDs, two 64-character lowercase hashes, a state, an optional bounded
canonical stage path, and bounded exit/timeout metadata. Paths are at most 4096
UTF-8 bytes, exit codes are signed 32-bit integers, and SQLite's record-length
limit is 8192 bytes. Malformed or oversized records fail closed when accessed.
No candidate bytes, driver bytes, stdout, stderr, exception text or credentials
are stored. There is no execution rehydration API.

A returned `StagedRun` is the actual staging result, released only after a
terminal record commits successfully. A normal process exit stores `SUCCEEDED`
or `FAILED` according to `Result.success`; a failed process can therefore still
return its original `StagedRun`. A `StageError` stores `UNKNOWN`, retaining a
valid bounded stage path when available, then propagates. Invalid optional error
paths are omitted. Storage failure while recording an error propagates instead
of claiming persistence. Other exceptions, interruption, abrupt death or
ambiguous commits can leave `STARTED`. Read-only `lookup(attempt_id)` projects
that as `UNKNOWN` without updating stored history. Neither state permits another
execution. A persistence failure after execution never yields an inferred
successful return, even if a terminal commit may actually have reached disk.
Transaction errors make that handle unusable, except ordinary consumed-ID
rejections. Reopening can inspect durable history but never reruns its IDs.

Use a canonical absolute filename in an existing private caller-owned directory
on a local filesystem with working SQLite locks and reliable fsync semantics;
the file itself may be newly created. Existing files must be caller-owned,
private regular files with one hard link. The wrapper rejects a journal anywhere
inside the staging parent. Keep the journal and its rollback sidecar inaccessible
to candidate writes, and outside sandbox runtime grants if their confidentiality
matters. The host must preserve directory ancestry, file identity, permissions,
database contents and stage paths; checks cannot defend against an equally
privileged host actor or pathname replacement races. Initialize once before
sharing handles. Concurrent established handles serialize claims using SQLite;
lock timeouts fail closed and do not trigger automatic retries. No cross-host or
network-filesystem coordination, pruning or backup rollback protection is claimed.
Keep uncertain journals and stages for host investigation.

This wrapper performs no automatic reexecution, execution reconciliation,
cleanup or external dispatch. A new explicit host decision and identity are
required for any genuinely new attempt; unknown work is not made safe to repeat
by changing its name. Hashes and exit metadata are history, not authenticated
test reports, cleanup evidence, delivery approval or OS acceptance. Existing
`isolated_delivery.deliver_staged`, its authenticated request builder, preflight,
authority and separate submission journal remain unchanged. Stages retain the
existing staging API's filesystem-durability and cleanup limitations.

Exactly four unittest methods are authored in `tests/test_execution_recovery.py`:
real journal/staging success with durable intent observed by another connection;
runner interruption and blocked reopen; a competing journal handle rejected
while the first runner is active; and malformed stored metadata rejected before
staging. Each scenario calls the wrapper at most twice and mocks only the runner.
These new tests have **not been executed** during this authoring task. No old
tests, project/Python execution, live Seatbelt run, network call or external
delivery has been performed; the scenarios provide no measured acceptance yet.

# Injected draft submission boundary

`src/review/draft_submission.py` connects an actual `CandidateAttemptOutcome`
or `ReviewReport` to exact draft preparation, an explicitly trusted external
human-approval authority, and an explicitly trusted injected draft-only transport.
There is no concrete authority, production transport, network call, source push,
CLI/config activation or automatic upstream submission in this change.

`prepare_draft_submission(source, target=DraftTarget(...), title=..., pr_template=...)`
validates successful complete report evidence and confirmed cleanup, and renders
the body through the existing literal report renderer. Candidate outcomes must
be pending review, unsubmitted, and have an attempt that rebuilds exactly their
report. Failure, unknown evidence, stale attempts and malformed reports reject.
After preparation the request is a detached historical snapshot: later changes
to the candidate cannot change the proposal. Preparation is not approval.

The frozen `DraftRequest` contains one canonical JSON string, bounded at 4 MiB.
It binds the entire report JSON, exact rendered body, title, quoted template,
`draft: true`, explicit destination, destination repository, head repository,
full head ref and SHA, full base ref and SHA. Existing report/body/template bounds
also apply (512 KiB / 1 MiB / 32 KiB). The current deliberately narrow destination
is `https://github.com`; branch names use a conservative ASCII full-ref subset.
Report repository and base SHA must equal the target. Reading `request.data`
returns a newly validated detached dictionary. Public `binding_sha256` is only
a content binding and reconciliation key, never an approval credential.

Construct `DraftSubmission(request, authority=trusted_authority,
transport=trusted_transport)` and call `run()` once. Alternatively the existing
`submit_draft_pr(report, body, request=request, authority=..., transport=...)`
checks the exact report/body and invokes that orchestration. Its original
two-argument call still raises `NotImplementedError`; partial injection rejects.
There is no approval bool or bearer-token argument. The enum returned by the
trusted authority is a decision from that boundary, not a caller credential.

## Optional durable single-use journal

Trusted embedding code can inject the stdlib-only
`src.review.submission_journal.SQLiteSubmissionJournal`. Supply a secure absolute
local DB filename and a stable operation/idempotency identity bound to the exact
immutable request, outside model/candidate-controlled input. Identities are 1–128
ASCII characters, starting with an alphanumeric, then alphanumerics or `_ . : -`
(without spaces). A changed request requires a different identity. An already
consumed exact request cannot bypass this journal with a fresh identity/object.
Semantic duplicates with different canonical bytes remain the authority's policy.

```python
from src.review.draft_submission import DraftSubmission
from src.review.submission_journal import SQLiteSubmissionJournal

# Provision a private parent directory first, outside the repository.
journal = SQLiteSubmissionJournal("/absolute/private/app-state/drafts.sqlite")
operation = DraftSubmission(
    request, authority=trusted_authority, transport=trusted_transport,
    journal=journal, operation_id="review-2026-001",
)
outcome = operation.run()  # One attempt; never a retry entry point.
record = journal.lookup("review-2026-001")  # Historical evidence, not permission.
```

Alternatively use the existing seam with the same opt-in pair:

```python
outcome = submit_draft_pr(
    report, request.data["body"], request=request,
    authority=trusted_authority, transport=trusted_transport,
    journal=journal, operation_id="review-2026-001",
)
```

These are alternative entry points, not a sequence for the same request. Both
`journal` and `operation_id` are required together. Omitting both retains legacy
local single-use behavior and the authority's existing durable obligations. The
journal has no authenticated owner, approval decision, authority implementation or
remote transport. Its records and hashes are not approval credentials.

Before validation/authority entry, `run()` atomically claims the identity AND exact
request, storing the complete snapshot, digest and UNKNOWN consumed
`authority_intent`. Unique constraints cover identity, exact snapshot and digest;
a digest collision conservatively blocks too. A committed UNKNOWN
`transport_intent` precedes transport entry. Terminal SUBMITTED, REJECTED and UNKNOWN
results are committed before returning. Terminal persistence failure raises
`JournalError` and leaves `operation.outcome` UNKNOWN, even if transport reported
success. A failed commit can have committed: the record may contain a terminal
result despite the caller receiving an error. No error permits replay.

`lookup(operation_id)` reads fresh storage and returns a frozen `JournalEntry`
containing `operation_id`, `snapshot_json`, `stage`, and `outcome`, or `None`.
Absence is not authorization. `JournalReplayError` rejects consumed identities or
exact requests regardless of outcome, including confirmed denial/no-creation,
concurrent workers and reopened journals. It never returns a cached success as a
new submission. Cancellation/`BaseException` propagates, leaving the last committed
UNKNOWN intent. For a persisted transport intent, `dispatched=True` conservatively
means transport *may* have been entered: a crash between commit and method entry
cannot distinguish that gap. It never proves network delivery. Each boundary gets
a separate request from captured bytes; bound boundary methods are captured too.

### Storage, concurrency and crash limits

This is single-host trusted local SQLite storage: short `BEGIN IMMEDIATE`
transactions, DELETE rollback journaling and `synchronous=EXTRA`. Boundary calls
run outside transactions. Separate connections/processes contend through SQLite
locks with a one-second busy timeout and no retry. Provision the journal before
starting workers; racing initial schema creation can fail closed. Every transaction
checks exact application/schema versions and DDL; incompatible or existing empty
DBs reject without migration/reset. Interrupted initialization leaves its file
preserved, never automatically reinitialized.

The embedding must secure the whole parent path against untrusted users, symlink
changes and renames, and protect the DB, rollback sidecars and backups as sensitive
evidence. New files use mode 0600; existing permissions/ownership and directories
remain the embedding's responsibility. Only absolute filename strings are accepted
with `uri=False`; relative paths, SQLite URIs and `:memory:` reject. The final file
must be regular, non-symlink and have one link. Each object pins device/inode and
rejects replacement/removal. Connections and cached absence/outcomes are never
reused. Storage/schema/commit errors or cancellation during storage poison the
journal object. Preserve storage and evidence rather than treating a new object
as a retry route.

Durability assumes SQLite, OS, filesystem and device correctly implement locking,
atomic rollback and synchronization, including directory fsync (unsupported
directory synchronization fails construction). Network filesystems, hostile local
writers, device loss, arbitrary corruption and restoring an older valid DB are
outside this guarantee. Schema/entry checks detect some damage, not authenticated
tamper or all corruption. Never interpret an error as absence. Deleting/replacing
the DB or choosing another DB loses replay protection; use one authoritative
persistent DB per scope. There is no cross-host/global deduplication. Records
accumulate without pruning/expiry; provision capacity. No timestamps or clock
decisions are used, so wall-clock trust and clock-based recovery are unnecessary.

A crash before claim commit enters neither external boundary and may leave no
record. After committed claim, interruption consumes the operation permanently,
even if neither boundary ultimately acts. There is no atomic transaction with
authority consumption/remote creation, authenticated owner guarantee or remote
exactly-once claim. There are no retries, refunds, expiry recovery, automatic
reconciliation or record deletion/reset/recovery APIs. Trusted operators can inspect
records and independent upstream evidence; this increment provides no action that
makes a consumed request dispatchable again.

## Authority and transport trust

The embedding application, outside candidate/model-controlled input, must choose
and secure both dependencies. A structurally compatible Python object does not
establish trust. The authority's `authorize_and_consume(request)` must independently:

- Authenticate the human and their authority to approve this project/action.
- Verify authentic execution/cleanup evidence, exact diff/head contents, project
  compliance, and independently verified upstream destination/repository identity,
  permissions and base/head identity. Successful report checks establish internal
  consistency only; caller-declared evidence and SHAs do not prove these facts.
- Verify approval of the entire exact request, including body, report, template,
  title, destination, head and base; never accept a public hash as authentication.
- Atomically and durably authorize/consume once before returning `AUTHORIZED`.
  Prevent replay across instances, concurrent workers, processes and restarts.
  Decide policy for semantically duplicate proposals and changed bindings; changing
  text or a digest must not reset consumed approval. A denial creates no draft.

The trusted transport receives a separate request reconstructed from the same
captured immutable bytes. It must verify exact destination and head/base identities
again at dispatch, account for moving refs, and enforce required atomic upstream
preconditions. If the upstream cannot meet that contract, reject rather than
pretend that an earlier SHA check closes the race. Only create the exact draft:
no hidden retry, fallback, push, non-draft creation or promotion. It must enforce
resource/deadline bounds during execution; Python cannot preempt a blocked injected
dependency. A matching reply hash proves consistency only. Confirming actual
creation, draft state, identity and PR number is the trusted transport's job.

Frozen Python objects are not a security sandbox against hostile in-process code.
The coordinator captures one string and gives each boundary a separate frozen
value, avoiding mutable dictionary/request sharing between validation, approval
and transport. Trusted dependencies must not inspect or modify coordinator internals.

## Outcomes and recovery

| Result | Meaning |
| --- | --- |
| `SUBMITTED` | Trusted transport confirms the exact draft, binding and positive PR number. |
| `REJECTED` | Local validation/approval refused, or transport explicitly confirms no creation. |
| `UNKNOWN` | Authority consumption or transport outcome is ambiguous, exceptional or malformed. |

`dispatched` means the transport method was entered, not proof of network delivery.
Invalid/misbound replies, missing PR identity, non-draft responses, boolean/string
approval assertions and exceptions cannot become success. An authority exception
is `UNKNOWN` with `dispatched=False`; it may have consumed authorization. Transport
exceptions are `UNKNOWN` with `dispatched=True`. Cancellation such as
`KeyboardInterrupt` propagates, but `operation.outcome` already records UNKNOWN.

Every `run()` consumes its local operation before validation or external calls,
including errors, denial, reentry and cancellation. There is no automatic retry,
rollback or approval refund, including after a confirmed rejection. A second local
call raises. A fresh object cannot replace the authority's durable replay controls,
or, when injected, the journal's persistent consumption record.

Without journal injection there is no local crash durability. The optional journal
persists the exact request, consumption, dispatch intent and outcomes. The authority
must still retain independent authenticated approval evidence, and the transport
independent upstream receipts. Consumption and a remote create are not one transaction: a
crash can leave consumed approval without a draft or an unrecorded created draft.
Operators must reconcile UNKNOWN/crashes using trusted read-only upstream evidence
and the durable record before any explicit recovery decision. Never blindly resend
or issue replacement approval after dispatch/UNKNOWN. This seam supplies no
recovery/retry adapter. Preserve the historical preparation disclosure in the body;
it does not assert current authenticated approval or verified isolation.

## Offline review plan

The earlier `tests/test_draft_submission.py` remains unchanged. This increment adds
ten self-contained offline unittest methods in `tests/test_durable_draft_submission.py`:
real preparation/report/submission seam, intents/outcomes, duplicate/misbound
identity, exact-request replay, rejection, UNKNOWN/cancellation, process crash and
restart, independent concurrent claims, pre/post-commit failures at every write,
incompatible/stale storage, opt-in/legacy behavior and captured immutable bindings.
All authority/transport decisions are synthetic. The restart scenario kills only
its own offline child process inside a synthetic authority. Nothing was executed
during authoring (no tests, builds, imports, CI, APIs or model backend).

After independent diff review, the coordinator's single targeted invocation is:

```sh
python -m unittest discover -s tests -p 'test_durable_draft_submission.py' -q
```

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
call raises. A fresh object cannot replace the authority's durable replay controls.

This module has no durable journal and cannot provide crash-safe exactly-once
delivery. The authority/application must persist the exact approved request,
consumption, dispatch intent and outcomes; the transport must retain independent
upstream receipts. Consumption and a remote create are not one transaction: a
crash can leave consumed approval without a draft or an unrecorded created draft.
Operators must reconcile UNKNOWN/crashes using trusted read-only upstream evidence
and the durable record before any explicit recovery decision. Never blindly resend
or issue replacement approval after dispatch/UNKNOWN. This seam supplies no
recovery/retry adapter. Preserve the historical preparation disclosure in the body;
it does not assert current authenticated approval or verified isolation.

## Offline review plan

Exactly ten new unittest methods are in `tests/test_draft_submission.py`, with
self-contained fixtures and synthetic authority/transport/lifecycle/runner/model
objects. One scenario traverses the real candidate loop, staged workspace and
report path; another exercises actual cleanup failure. Nothing was executed during
authoring (no tests, builds, imports, CI, APIs or model backend).

After independent diff review, the coordinator's single targeted invocation is:

```sh
python -m unittest discover -s tests -p 'test_draft_submission.py' -q
```

# Candidate attempt orchestration

`src/agent/candidate_attempt.py` connects one already collected
`src.collector.digest.CandidatePoolEntry` to the existing `CoderLoop`,
`StagedAttemptAdapter`, `Sandbox`, `SnapshotWorkspace` and `build_review_report`.
It returns a local review artifact whose human approval is always `pending`.
It adds no engine, backend, default configuration or submission capability.

## What the caller supplies

Everything is injected explicitly: the candidate, a caller-declared base SHA
(40 lowercase hex), a `TestPlan`, a fresh `StagedAttemptAdapter` (trusted
lifecycle, prepared snapshot workspace factory, staging parent, runner), a patch
model (any object structurally providing the `PatchModel` protocol, meaning a
callable `propose` method), an explicit `SandboxPolicy`, a caller-declared
candidate status string and an execution kind that is exactly the built-in
string `synthetic` or `unverified`. Nothing is acquired from the network, a host
shell or the filesystem by this module.

## Order of operations

1. `CandidateAttempt(...)` validates the candidate identity, base SHA, plan,
   policy, adapter type and freshness, model, execution kind and bounds. It then
   runs a no-action preflight by calling the real `build_review_report` with an
   empty `AttemptResult`, the detached issue, the base SHA, the plan and the
   execution kind. This reuses the existing report validator (for example
   duplicate suite commands in the plan or control characters in the title)
   instead of a duplicate schema. Any failure raises `CandidateAttemptError`
   before any lifecycle, workspace, runner or model action. Candidate fields are
   copied, so later mutation of the candidate does not affect the run.
2. `run()` is single-use. A second call, or a new attempt on a used adapter, is
   refused with no action.
3. A fresh `Sandbox` is built from the explicit policy with the adapter as backend
   and the existing `CoderLoop` runs. The loop itself may perform bounded
   corrections, limited by its explicit `max_attempts` and model-call bounds.
   This orchestration never automatically redispatches or replays an attempt:
   unknown outcomes, unknown model usage and cleanup failures stop the run, and
   the adapter cannot be reused.
4. After the loop and cleanup finish, `build_review_report` produces the canonical
   immutable report. A success without confirmed cleanup is never reported as
   success. A report-building failure yields status `report_failed` with no retry.

Status is `pending_human_review`, `failed` or `report_failed`. `submitted` is
always false. The outcome holds a deep copy of the attempt, detached from the
report.

## Limits and honesty

- Candidate status and the base SHA are caller-declared and unverified. Declared
  paths or a report do not prove diff authenticity or that the base is the target.
- `max_text_bytes` must be 16384 or less to match report bounds. The preflight
  checks only the inputs known before the run. The generated diff can still
  exceed the report bounds even when `max_patch_bytes` is respected, so output is
  not guaranteed to be report compatible; `report_failed` is an explicitly
  allowed outcome.
- Tests use synthetic lifecycle, runner and model doubles over the real loop,
  adapter, workspace, staging and report builder. They do not show Docker
  isolation, source acquisition, model behavior or any production backend.
- Not production-enabled: collector targets stay collect-only and nothing is wired
  into a CLI, service or schedule.

## Separate injected submission seam

The [draft submission boundary](draft-submission.md) can prepare an immutable
request from this outcome and pass it through an explicitly trusted external
human-approval authority and injected draft-only transport. The original
two-argument `submit_draft_pr` still raises `NotImplementedError`. Candidate
execution itself grants no approval and submits nothing. No concrete authority,
transport, durable approval store or production activation is supplied.

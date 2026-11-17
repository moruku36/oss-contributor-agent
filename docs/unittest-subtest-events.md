# unittest subtest event aggregation

`SubtestEventResult` extends the round-1 `EventTestResult` callback recorder
(`src.agent.unittest_events`) without modifying it. It never runs tests; a
trusted runner drives the callbacks. It does not authenticate the runner or
isolate candidate code.

## Behaviour

- Every `addSubTest` callback is folded into ONE final event for the parent test,
  emitted at `stopTest`. Subtests are never reported as standalone tests.
- unittest calls neither `addFailure` nor `addError` for a failed subtest, so the
  parent record is deferred to `stopTest`.
- Failed subtests are classified with `issubclass(err[0], parent.failureException)`.
  The parent must expose a valid `failureException`; otherwise the result fails closed.
- Passing subtests (`err=None`) retain only their bounded identities, used for
  duplicate detection; no outcomes are retained. The usual `addSuccess(parent)`
  records the pass.
- Strongest outcome wins: error over assertion failure over skip over pass. Details
  are sanitized, joined with ` | ` and bounded by `detail_limit`. No traceback is kept.
- Top-level `addFailure` / `addError` merge into the same record, including after
  a top-level success (a genuine teardown error); `addSkip` never erases a failure.
- `wasSuccessful()` is false while any failed subtest is pending, even before `stopTest`.

## Limits and fail-closed cases

- `max_subtests` is an exact int in 1..128, counted per parent test.
- A duplicate subtest identity in one parent is rejected (conservative).
- A subtest whose identity equals the parent identity is rejected, so a subtest
  callback cannot masquerade as the parent.
- Any `addSubTest` after a top-level `addSuccess` or `addSkip` is rejected.
- Subtest identities go through the base identity validation. Real unittest ids
  containing spaces or parameters are accepted only if the reducer accepts them.
- Subtest skips (`addSkip(subtest, reason)`), expected failures and unexpected
  successes are unsupported and poison the result.
- All inherited lifecycle poisoning is retained; `finish()` refuses a poisoned result.

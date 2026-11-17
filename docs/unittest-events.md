# unittest event recorder

`src/agent/unittest_events.py` provides `EventTestResult`, a `unittest.TestResult`
subclass that turns real unittest lifecycle callbacks into a
`RunnerEventCapture`. `finish()` builds the terminal record from its own counts
and verifies the capture with `reduce_runner_events` before returning it.

## Contract

- `EventTestResult(max_tests, detail_limit)`: exact ints, `1..128` and `1..256`.
- Exactly one final event per test: `addSuccess`, `addFailure`, `addError` or
  `addSkip`, between `startTest` and `stopTest`.
- Failure and error details are the exception type name plus a sanitized message,
  truncated on a UTF-8 boundary. Tracebacks are never read or retained.
- Text is sliced to the detail limit before it is joined or encoded, so the
  sanitizing copies are bounded. `str()` of a trusted callback value is still
  called first and may allocate; there is no hard memory enforcement.
- Test ids are checked with the reducer's own rules before they are retained.
- Duplicate ids, nested or out-of-order callbacks, a test stopped without an
  outcome, and exceeding `max_tests` raise `RunnerEventError` and poison the
  result permanently. `finish()` then raises and can be called only once.
- `addSubTest`, `addExpectedFailure` and `addUnexpectedSuccess` are explicitly
  unsupported and fail closed.
- `wasSuccessful()` is overridden. The inherited failure and error lists are not
  populated, so it uses the recorder's own state and is never `True` after a
  recorded failure or error, after misuse (poison), or while a test is active.
- Reducer limits still apply, for example the total assertion detail budget.

## Limits

This records callbacks only; it captures callback completion and nothing more.
unittest's `shouldStop` flag is not consulted, so an externally requested stop is
not recorded and does not by itself make `finish()` fail.
It does not establish that a runner is authentic, that a candidate was isolated,
or that the callbacks came from a real run. The caller must trust the runner
instrumentation that invokes the callbacks.
The module is not wired into any default path and executes no code or processes.

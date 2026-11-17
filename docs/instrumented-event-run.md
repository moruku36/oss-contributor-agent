# Instrumented event run

`InstrumentedEventRun` joins three existing pieces into one single-use
transaction: unittest lifecycle callbacks (`SubtestEventResult` from
`src.agent.unittest_subtest_events`), bounded stdout/stderr capture
(`BoundedRunnerOutput` from `src.agent.runner_output_buffer`) and the channel
codec (`encode_runner_event_response`).

## Contract

- `InstrumentedEventRun(driver, *, max_tests, detail_limit, max_subtests, clock)`
  takes a trusted, injected driver and clock. Bounds are validated at
  construction.
- `run(ticket, *, timeout_seconds, max_output_bytes, max_wire_bytes)` returns
  the encoded response bytes, or raises. It never returns a partial payload.
- The driver is called as
  `driver(ticket, result, stdout, stderr, timeout_seconds=remaining)` and must
  return the real runner exit code as an exact `int` (`bool` is refused).
- `ticket.command` is the exact command string bound into the response.
- An instance can run once. The claim is taken before validation and is not
  released after an error or cancellation (`BaseException` propagates).

## Order of checks

1. Claim the single use.
2. Read the clock; the value must be a finite `int` or `float` (not `bool`).
3. Validate the timeout (finite, positive, at most 2700 seconds).
4. Preflight the whole ticket and both limits with the real encoder using an
   throwaway synthetic preflight capture. The throwaway payload is not execution evidence and
   is never returned.
5. Compute the remaining budget. It must be finite and satisfy
   `0 < remaining <= timeout` before the driver is called, so a malformed,
   backwards or NaN clock can never dispatch the driver.
6. After the driver returns: refuse on `shouldStop`, an unfinished or poisoned
   result, a poisoned output buffer, or elapsed time outside `0 <= elapsed <= timeout`.
7. Bind the ticket command and exit code into a `CommandResult` and encode it.
8. Re-check the elapsed time after encoding, because serialization can consume
   the remaining budget; refuse instead of returning a late payload.

## Responsibilities

The codec only validates the wire format and binding. It does not decide what
the run means. Conservative outcome classification (exit code versus event
outcomes, never inferring PASS from output) belongs downstream in
`StructuredRunnerEvidenceAdapter`.

## Limits

- Errors swallowed by the driver still poison the result or output, so the
  final capture is refused.
- The clock is a trusted injection. A deadline is only measured; an expired one
  refuses the response after the driver returns. There is no hard termination
  of a running driver.
- Nothing here loads suites, starts processes, uses the network, redirects
  global streams, authenticates the driver or isolates candidate code.

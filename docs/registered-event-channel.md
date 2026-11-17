# Registered instrumented channel

`src/agent/registered_event_channel.py` is the last integration step of this series. It connects the real callback instrumentation to the existing staged candidate -> local review-package path. It does not change that path, and nothing in it is wired in by default.

## Related pieces

The channel builds on the earlier increments. Each has its own document next to this one:

- [Runner events](runner-events.md): the event vocabulary and reducer.
- [Runner event channel](runner-event-channel.md): the wire codec.
- [Unittest events](unittest-events.md): base callback recording.
- [Unittest subtest events](unittest-subtest-events.md): unittest callback collection and subtest aggregation.
- [Runner output buffer](runner-output-buffer.md): bounded stdout and stderr.
- [Instrumented event run](instrumented-event-run.md): the single-use transaction that turns callbacks and output into one response.

## What it adds

- `CommandInstrumentation(command, driver, expected_test_ids)` is a frozen declaration. `command` is a string, `driver` is a trusted callable, and `expected_test_ids` is a tuple of 1..128 unique test ids.
- `RegisteredInstrumentedChannel(registrations, *, max_dispatches, detail_limit, max_subtests, clock)` implements `.dispatch(ticket, *, timeout_seconds, max_output_bytes, max_wire_bytes) -> bytes`.
  - The command string is only a literal map key. It is never parsed, split, executed or imported.
  - Constructor validation covers the command text (non-blank, NUL-free, at most 4096 bytes, unique) and the expected ids. The ids are validated through the real event reducer, so control characters and other malformed ids are refused there.
  - It also covers the counter bounds and `max_dispatches` (an exact int, 1..64).
  - Validation never calls a driver.
- Each accepted dispatch validates the timeout, the wire and output limits and the ticket with the real codec helpers before any ticket attribute is used. It then creates a new `InstrumentedEventRun` for the registered driver.
- The returned bytes are decoded again with the real codec against the full ticket. The top-level test id set and count must equal the declared expectation exactly. Order may differ and skipped tests count. Missing or unexpected tests refuse the response, so a driver finishing cannot stand in for the declared suite finishing.
- The deadline starts once the channel is claimed, before validation. The remaining time is forwarded to the run, and the final elapsed time after decode and coverage must not exceed the timeout.

## Single-attempt rules

- The first valid ticket binds the attempt through its literal `attempt_id`. The receipt digest is not used, because each staged dispatch of the same attempt has a different receipt and stage name.
- Generations must run contiguously from 1 and must not exceed `max_dispatches`. Dispatch ids must be unique.
- The generation and dispatch id are consumed before the driver is invoked.
- Any rejection, validation failure, clock fault, error, cancellation, late or malformed response, or coverage mismatch poisons the channel for good. An invalid request poisons it too. There is no retry.
- The lock covers only state transitions, never a driver call. An overlapping call is refused and poisons the channel.

## Not provided

- Drivers are trusted, injected callables. Declarations and coverage checks say nothing about where or how a driver actually ran. That is not authenticity or provenance.
- A driver may read staged file bytes. This module does not execute, import or shell out to candidate code, starts no process and uses no network.
- Real isolation, an authenticated runner and runtime acceptance are still absent.
- An in-flight driver cannot be killed. An expired deadline only refuses the response after the driver returns.
- Nothing is activated by default. Callers must construct and pass a channel explicitly.
- These increments are not a measure of completion, and the PR count says nothing about it.

## Tests

`tests/test_registered_event_channel.py` has six new offline methods. They cover registration validation, full coverage refusal, unknown command and bad bounds before the driver, generation, replay, attempt and budget handling, and cancellation, reentry, deadline and poisoning. Non-integration methods use self-contained synthetic tickets built from the actual dispatch fields. One method runs the full `EventCandidatePackage` path with injected lifecycle and model fixtures. It drives real callbacks from staged byte comparisons and checks the four commands in order: baseline suite passes, reproduction fails, the patch is applied, then reproduction and suite pass. It then confirms cleanup and that the exported review package reads back with the expected diff, pending human approval and no submission.

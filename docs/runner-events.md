# Explicit runner events

`src/agent/runner_events.py` reduces structured records into the existing
`StructuredTestSummary`. It performs no process execution, stdout parsing,
backend selection, retries or default wiring. This is the first local runner
automation increment: an aggregation seam, with no production instrumentation
or runtime activation supplied here.

## Records and reduction

The records are frozen dataclasses with slots. `RunnerEventCapture` requires an
exact tuple of exact supported record types and three explicit boolean flags:
`complete`, `truncated`, `outcome_known`. Constructors do not validate annotations;
`reduce_runner_events(capture)` validates the entire capture before returning a
summary. Invalid input raises `RunnerEventError`, without a partial summary.

Each record has a contiguous, one-based `sequence`:

| Record | Meaning |
| --- | --- |
| `TestEvent(sequence, test_id, outcome, detail="")` | One final outcome for one test identity. Outcomes are `passed`, `assertion_failed`, `error`, `skipped`. |
| `RunnerErrorEvent(sequence, category, detail)` | One `collection` or `infrastructure` error. |
| `RunCompleted(sequence, tests_run, passed, assertion_failures, test_errors, skipped, collection_errors, infrastructure_errors)` | Mandatory sole terminal record; its explicit counts must exactly match the preceding events. |

Assertion and test-error events require nonblank detail. Passed tests must have
empty detail; skips may include a reason. Runner errors require nonblank detail.
Identities are exact, nonblank, case-sensitive strings; there is no normalization.
Duplicate identities are rejected across every test outcome, including skips.
Multiple failed assertions within a test must be combined by instrumentation
into its single final assertion event. Setup/teardown and other test errors must
be represented as `error`; they must never become assertion failures. A test
that has both an assertion failure and a teardown error is a single `error`
event. Subtests, parametrization, retries and multi-phase runner reports need
an explicit upstream identity/outcome policy; this reducer does not merge them.

Collection errors must precede test outcomes. Infrastructure errors may occur
anywhere before completion. Unknown record types/outcomes/categories, gaps,
reordering, events after completion, duplicate completion and missing completion
are rejected. Counts and sequence numbers must be exact integers, excluding
booleans; negative, excessive and inconsistent counts are rejected.

Executed tests equal passed tests plus assertion failures plus test errors.
Skips are excluded. Collection and infrastructure errors have separate counts;
their text is validated but is not retained in `StructuredTestSummary`. Only
assertion identities/details survive reduction, matching the existing model.

Flags describe the whole command/capture and are copied without inference.
The terminal record ends the structured stream; it does not prove that all
tests ran or that capture was complete. An honestly interrupted run can have
a terminal record counting observed events and `complete=False`. Missing
terminal completion is always rejected, even with conservative flags; callers
must not manufacture a terminal record to repair a lost stream.

## Bounds

Limits are fixed and are checked without silently truncating records or text:

- At most 4096 records including completion, so each terminal count is at most
  4095 and must agree with the bounded events.
- Test identities: 256 UTF-8 bytes; each detail: 1024 UTF-8 bytes.
- Aggregate event text: 65,536 UTF-8 bytes, including identity, outcome/category
  and detail text. Character lengths are checked before bounded encoding.
- At most 128 assertion records, with at most 3072 UTF-8 bytes of rendered
  `test_id: detail` lines, including newlines. This leaves room for the existing
  adapter's 4096-byte assessment detail limit.
- Invalid UTF-8 text and control characters are rejected. Details permit newline
  and tab; identities permit neither. DEL is rejected in both.

These limits bound reduction and retained evidence. Instrumentation must bound
collection/capture in flight; this reducer cannot undo prior allocation, stop a
blocked runner or make an unbounded external event source safe.

## Adapter integration and trust

Inside an explicitly injected backend's existing `run_structured` method, use:

```python
def run_structured(self, ticket, *, timeout_seconds, max_output_bytes):
    result, capture = self.instrumentation.run(
        ticket, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes,
    )
    return structured_run_from_events(ticket, result, capture)
```

Import `structured_run_from_events` from `src.agent.runner_events`. The
`instrumentation.run` method above is an external trusted dependency contract,
not an implementation included in this increment. It must execute exactly this
ticket generation once, produce a new exact `CommandResult`, enforce timeout
and output limits in flight, and collect authoritative structured events for
that same invocation. It must raise when invocation outcome is uncertain.

The helper checks exact ticket/result types and matching command strings, then
returns `StructuredRun` holding those same ticket and result objects and the
reduced summary. It does not copy or mutate them. Existing
`StructuredRunnerEvidenceAdapter(backend=...)` consumes that reply and remains
responsible for full ticket/receipt binding, result validation, output/deadline
checks, generation and result reuse checks, and one-time assessment. See
[structured runner evidence](structured-runner-evidence.md) for the existing
staged runner/assessor injection path. No existing module or default is changed.

The existing classifier gives test/collection/infrastructure errors
`INFRA_ERROR`; incomplete, truncated, unknown, empty and all-skipped runs give
`UNKNOWN` in the absence of those errors. A complete known run with executed
tests and no errors passes with exit code zero, or reports assertions with a
positive exit code and assertion events. Contradictory exit status is `UNKNOWN`.
Stdout and stderr remain opaque command-result fields, regardless of any test
summary text they contain.

Record consistency is not authenticity. Independently trusted runner
instrumentation and its capture channel must supply the records. This module
cannot detect a forged but consistent capture, prove execution/isolation, or
establish that an arbitrary capture belongs to the supplied ticket/result.
Sequence numbers, terminal counters, frozen objects and existing public hashes
provide no provenance. There is no authentication flag or digest added here.

## Verification scope

`tests/test_runner_events.py` defines exactly six new offline unittest methods
covering actual reduction, exact-object integration through the real existing
adapter, conservative classification, malformed/order/completion rejection and
bounds. Instrumentation fixtures are explicitly synthetic; they do not execute
tests in a runner or establish real instrumentation provenance. No historical
test module is imported. Tests, project imports, builds and runtime checks were
not executed while authoring this increment. No process, Docker, target, network,
model, CI, configuration or authentication changes are part of this work.

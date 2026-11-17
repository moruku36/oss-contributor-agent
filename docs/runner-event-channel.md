# Runner event channel, increment 2

`src/agent/runner_event_channel.py` connects the existing event reducer to the
existing `StructuredRunnerEvidenceAdapter` through an explicitly injected,
independently trusted channel. It adds no transport, execution implementation,
authentication, configuration, default wiring or runtime integration.

The intended dependency chain is:

```text
StructuredRunnerEvidenceAdapter.run_staged(full DispatchTicket, bounds)
  -> EventChannelBackend.run_structured(full ticket, bounds)
  -> injected TrustedEventChannel.dispatch(full ticket, bounds), once
  <- versioned response bytes
  -> strict decode and full ticket comparison
  -> structured_run_from_events -> reduce_runner_events -> StructuredRun
  -> existing adapter result/binding checks -> single-use assess(...)
```

Use the same existing adapter instance as staged runner and workspace assessor.
The backend implements the actual `StructuredBackend.run_structured` interface;
it does not bypass the adapter or manufacture an assessment from stdout.

## Explicit injection

Composition is a caller decision; this module creates no channel by default:

```python
backend = EventChannelBackend(
    channel=independently_trusted_channel,
    max_dispatches=8,
    max_wire_bytes=262_144,
)
adapter = StructuredRunnerEvidenceAdapter(backend=backend, max_dispatches=8)
```

The injected object must implement:

```python
def dispatch(
    self, ticket: DispatchTicket, *, timeout_seconds: float,
    max_output_bytes: int, max_wire_bytes: int,
) -> bytes:
    ...
```

It receives the actual full ticket object and all three bounds. It must execute
exactly that command against that staged generation once, collect authoritative
events and a fresh `CommandResult`, enforce bounds during execution/capture,
and return bytes or raise if the outcome is uncertain. It must never retry or
fall back. This package supplies no host process, socket, network, Docker,
credential or other real transport implementation.

Trusted instrumentation can produce a response with:

```python
payload = encode_runner_event_response(
    ticket, result, capture,
    max_wire_bytes=max_wire_bytes,
    max_output_bytes=max_output_bytes,
)
```

Encoding validates the exact input objects and produces deterministic UTF-8
bytes using sorted keys, compact separators and unescaped Unicode. It retains
manifest/event order and numeric types. It raises on an invalid or over-bound
response instead of trimming any field. Control characters in output are
escaped by JSON and restored exactly when decoded.

## Version 1 wire schema

Every field below is mandatory; every object rejects all other fields. JSON
arrays represent the immutable manifest/event tuples. Their order is retained.

| Object | Exact fields |
| --- | --- |
| Envelope | `version` (integer `1`), `ticket`, `result`, `capture` |
| Ticket | `attempt_id`, `dispatch_id`, `generation`, `receipt`, `receipt_sha256`, `command` |
| Complete receipt | `attempt_id`, `stage_name`, `files`, `total_bytes`, `outcome` (string `complete`) |
| Staged file | `path`, `role`, `sha256`, `size_bytes` |
| Command result | `command`, `exit_code`, `stdout`, `stderr`, `duration_seconds` |
| Capture | `events`, `complete`, `truncated`, `outcome_known` |
| Test event | `type` (string `test`), `sequence`, `test_id`, `outcome`, `detail` |
| Runner error | `type` (string `runner_error`), `sequence`, `category`, `detail` |
| Completion | `type` (string `completed`), `sequence`, `tests_run`, `passed`, `assertion_failures`, `test_errors`, `skipped`, `collection_errors`, `infrastructure_errors` |

`decode_runner_event_response(payload, expected_ticket=actual_ticket, ...)`
accepts only an exact `bytes` object. Strings, byte arrays, memory views and
already-decoded objects are rejected. The byte cap is checked before UTF-8
decoding or JSON parsing. A quote/escape-aware scan rejects excessive container
nesting before parsing; brackets inside stdout do not count as nesting.
UTF-8 decoding is strict, including rejection of invalid byte sequences. A BOM,
trailing data, duplicate keys (including escaped equivalent names), unknown or
missing fields, unsupported versions and lone surrogate text are rejected.

Counters and integer fields require exact integers, excluding bools, strings
and floats. Flags require exact bools. Seconds accept exact ints or floats in
range, excluding bools and nonfinite values. The float parser also rejects
overflow, underflow and decimal precision that would be silently lost relative
to the float's round-trip decimal representation. Fields are never coerced.

The decoder validates the complete captured ticket, including every staged file
field, manifest order, byte total, receipt attempt, stage and complete outcome.
It checks the receipt digest for internal consistency and then compares the
entire validated ticket with the actual expected ticket. Matching a public
digest alone is insufficient. The backend also snapshots these full scalar
values before calling the channel and rejects ticket mutation during the call.

The existing reducer validates the exact capture records: contiguous one-based
sequences, supported outcomes/categories, unique test identities, required
details, collection ordering, a sole final completion record and exact terminal
counts. It retains all three explicit capture flags. Neither the codec nor the
reducer infers anything from stdout or stderr. Arbitrary output is opaque,
including NULs, terminal controls, JSON-looking content and apparent test logs;
only exact string type, valid UTF-8 and byte bounds apply to output.

## Bounds and failure behavior

| Limit | Hard ceiling / behavior |
| --- | --- |
| Response bytes | 1,048,576; caller may choose a smaller positive cap |
| JSON container nesting | 8, scanned before JSON parsing |
| Combined stdout + stderr | 524,288 UTF-8 bytes; per-dispatch positive cap is forwarded unchanged |
| Timeout | Positive finite seconds, at most 2,700; forwarded unchanged |
| Attempt and dispatch IDs / stage | 128 UTF-8 bytes each |
| Command | 4,096 UTF-8 bytes, nonempty and NUL-free |
| Receipt | 1..256 files, at most 4,194,304 total bytes |
| Staged file | Path 240 bytes, role 16 bytes, lowercase SHA-256, at most 262,144 file bytes |
| Dispatches | Default 32; configurable 1..64; one attempt per backend |
| Events | Existing reducer ceiling of 4,096 including completion |
| Event text and assertions | Existing reducer limits apply unchanged |

JSON escaping can make wire bytes exceed the response cap even when output is
within its own cap; that response is rejected without truncation. Memory use is
bounded by hard wire/input limits; no streaming transport is provided.

Accepted generations must start at one and advance by exactly one, and every
dispatch ID must be fresh. Replay state is bounded by the dispatch ceiling.
Preflight rejection performs no channel call. A nonblocking gate rejects
concurrent/reentrant dispatches. After admission the generation is consumed
before the one channel call; there are no retries or fallback calls.

Any exception after consumption makes the backend terminal, including
cancellation (`BaseException`), malformed/nonbytes replies, mismatched tickets,
local bound violations and late replies. Replayed response bytes fail full
ticket binding. Incomplete, truncated or outcome-unknown flags can round-trip
through the codec for inspection, but make the execution backend terminal.
No partial `StructuredRun` or assessment is returned in those cases. A new
backend must never be used to automatically repeat an uncertain dispatch.

Reported duration must be strictly less than the dispatch timeout. A local
monotonic deadline covers the channel call, decoding and reduction. These
checks detect late completion only after control returns: **Python-side checks
cannot stop blocked I/O or guarantee an in-flight deadline.** The independently
trusted channel is responsible for execution, capture and resource enforcement
in flight. This limitation is unchanged by supplying a timeout argument.

Bytes, schema validity, sequence numbers, a matching ticket and public digests
are consistency checks. They do not authenticate instrumentation, execution,
event origin or the channel. Independent trust of the injected channel is a
prerequisite; stdout and attacker-supplied JSON are never a substitute.

## Offline tests

`tests/test_runner_event_channel.py` defines six self-contained unittest methods
with local fixtures and injected recording channels. They cover all event
types and full receipt binding (including simulated equal public digests), the
real existing adapter/assessment path, malformed schema and bytes, replay and
uncertain/cancelled calls, bound forwarding and local limits, and strict encoder
rejection. No prior test module is imported. No real transport is used.

These tests were authored without execution for this change, as requested.

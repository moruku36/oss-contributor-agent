# Runner output buffer

`src/agent/runner_output_buffer.py` provides `BoundedRunnerOutput`, a small
in-memory capture primitive for runner stdout and stderr text. It is meant to
compose with the event test result and the event-channel codec: a trusted
driver, added in a later increment, writes output here and collects it once.

## API

- `BoundedRunnerOutput(max_output_bytes)`: exact `int`, `1..524288`.
- `.stdout` / `.stderr`: sinks with `write(str) -> len(str)` (characters),
  `flush()` and `writable()`. There is no `close()`.
- `write_stdout(str)` / `write_stderr(str)`: same behavior as the sinks.
- `finish() -> (stdout, stderr)`: single use, returns immutable `str` values.

## Rules

- One UTF-8 byte budget is shared by both streams.
- Only exact `str` is accepted. Empty writes are skipped and never stored.
- A cheap `len(str)` check against the remaining budget runs before a bounded
  encode, so oversized input is never fully encoded.
- A rejected write (wrong type, lone surrogate, over budget) is not retained and
  permanently poisons the capture. Later writes, `flush()` and `finish()` raise
  `RunnerOutputError`. There is no truncation and no automatic retry.
- After `finish()`, writes, `flush()` and a second `finish()` raise. Returned
  values cannot change.
- A single lock guards all state; no callbacks run under it.

## Limits

The buffer does not redirect global streams, start processes or use the
network. It cannot see output that bypasses its writers and does not bound
execution time, memory outside its own storage, or other resources. Trusted
drivers must route output through it and enforce those bounds themselves.

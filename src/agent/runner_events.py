"""Bounded reduction of independently trusted, structured runner records.

This module neither runs tests nor parses stdout. Record shape, ordering and
counts establish consistency only; callers must independently trust the runner
instrumentation and its capture channel. No event field authenticates a run.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.agent.staged_runner_bridge import DispatchTicket
from src.agent.structured_runner_evidence import (
    AssertionFailure, StructuredRun, StructuredTestSummary,
)
from src.sandbox.docker_manager import CommandResult


MAX_EVENTS = 4096  # Includes the required terminal record.
MAX_TEXT_BYTES = 65_536
MAX_TEST_ID_BYTES = 256
MAX_DETAIL_BYTES = 1024
MAX_ASSERTIONS = 128
MAX_ASSERTION_BYTES = 3072  # Leaves room for the adapter's 4096-byte assessment.


class RunnerEventError(ValueError):
    """Malformed, inconsistent, unsupported or over-bound event capture."""


@dataclass(frozen=True, slots=True)
class TestEvent:
    """One final outcome per identity; combine assertions within that test.

    Outcomes: passed, assertion_failed, error, skipped. Only assertion_failed
    and error require detail; passed forbids detail. Skips may give a reason.
    """

    sequence: int
    test_id: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RunnerErrorEvent:
    """One collection or infrastructure error, never an assertion failure."""

    sequence: int
    category: str  # collection | infrastructure
    detail: str


@dataclass(frozen=True, slots=True)
class RunCompleted:
    """Required final record, reporting counts independently of reduction.

    Completion of the record stream does not imply successful/full execution;
    capture flags describe that separately. Every counter must match events.
    """

    sequence: int
    tests_run: int
    passed: int
    assertion_failures: int
    test_errors: int
    skipped: int
    collection_errors: int
    infrastructure_errors: int


@dataclass(frozen=True, slots=True)
class RunnerEventCapture:
    """Immutable bounded-input contract, validated by reduce_runner_events.

    All flags are mandatory runner attestations, not inferred from output or
    presence of RunCompleted. Only an exact tuple of exact records is accepted.
    """

    events: tuple[TestEvent | RunnerErrorEvent | RunCompleted, ...]
    complete: bool
    truncated: bool
    outcome_known: bool


def _integer(value: object, ceiling: int) -> None:
    if type(value) is not int or not 0 <= value <= ceiling:
        raise RunnerEventError("bounded nonnegative integer required, excluding bool")


def _text(value: object, cap: int, *, required: bool = False, identity: bool = False) -> int:
    if type(value) is not str or len(value) > cap or (required and not value.strip()):
        raise RunnerEventError("invalid bounded event text")
    if any((ord(c) < 32 and (identity or c not in "\n\t")) or ord(c) == 127
           for c in value):
        raise RunnerEventError("control character in event text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise RunnerEventError("event text must encode as UTF-8") from exc
    if size > cap:
        raise RunnerEventError("event text exceeds byte limit")
    return size


def reduce_runner_events(capture: RunnerEventCapture) -> StructuredTestSummary:
    """Reduce a complete record stream, failing closed on invalid structure.

    Sequence numbers are contiguous and one-based. Collection errors precede
    test outcomes; infrastructure errors may occur anywhere before the terminal
    record. No partial summary is returned on rejection. Explicit incomplete,
    truncated or unknown flags are preserved for the existing conservative
    classifier, even when the observed counts are internally consistent.
    """
    if type(capture) is not RunnerEventCapture:
        raise RunnerEventError("exact RunnerEventCapture required")
    if any(type(flag) is not bool for flag in (
        capture.complete, capture.truncated, capture.outcome_known,
    )):
        raise RunnerEventError("capture flags must be explicit bools")
    events = capture.events
    if type(events) is not tuple or not 1 <= len(events) <= MAX_EVENTS:
        raise RunnerEventError("nonempty bounded tuple of event records required")

    counts = dict(passed=0, assertion_failures=0, test_errors=0, skipped=0,
                  collection_errors=0, infrastructure_errors=0)
    identities: set[str] = set()
    assertions: list[AssertionFailure] = []
    text_bytes = assertion_bytes = 0
    tests_started = False
    terminal = None
    for sequence, event in enumerate(events, 1):
        if type(event) not in (TestEvent, RunnerErrorEvent, RunCompleted):
            raise RunnerEventError("unknown event record type")
        _integer(event.sequence, MAX_EVENTS)
        if event.sequence != sequence:
            raise RunnerEventError("event sequence must be contiguous and one-based")
        if type(event) is RunCompleted:
            if sequence != len(events):
                raise RunnerEventError("completion must be the sole final event")
            terminal = event
            continue
        if type(event) is TestEvent:
            tests_started = True
            text_bytes += _text(event.test_id, MAX_TEST_ID_BYTES, required=True, identity=True)
            if type(event.outcome) is not str or event.outcome not in (
                "passed", "assertion_failed", "error", "skipped",
            ):
                raise RunnerEventError("unsupported test outcome")
            text_bytes += len(event.outcome)
            text_bytes += _text(event.detail, MAX_DETAIL_BYTES,
                                required=event.outcome in ("assertion_failed", "error"))
            if event.test_id in identities:
                raise RunnerEventError("duplicate test identity, including skipped tests")
            identities.add(event.test_id)
            if event.outcome == "passed":
                if event.detail:
                    raise RunnerEventError("passed tests must not carry failure detail")
                counts["passed"] += 1
            elif event.outcome == "assertion_failed":
                assertion_bytes += len(f"{event.test_id}: {event.detail}\n".encode("utf-8"))
                if len(assertions) >= MAX_ASSERTIONS or assertion_bytes > MAX_ASSERTION_BYTES:
                    raise RunnerEventError("assertion identities or rendered detail exceed limit")
                assertions.append(AssertionFailure(event.test_id, event.detail))
                counts["assertion_failures"] += 1
            elif event.outcome == "error":
                counts["test_errors"] += 1
            else:
                counts["skipped"] += 1
        else:
            if type(event.category) is not str or event.category not in (
                "collection", "infrastructure",
            ):
                raise RunnerEventError("unsupported runner error category")
            if event.category == "collection" and tests_started:
                raise RunnerEventError("collection errors must precede test outcomes")
            text_bytes += len(event.category)
            text_bytes += _text(event.detail, MAX_DETAIL_BYTES, required=True)
            counts[event.category + "_errors"] += 1
        if text_bytes > MAX_TEXT_BYTES:
            raise RunnerEventError("aggregate event text exceeds byte limit")

    if terminal is None:
        raise RunnerEventError("missing terminal completion record")
    tests_run = counts["passed"] + counts["assertion_failures"] + counts["test_errors"]
    for name, expected in dict(tests_run=tests_run, **counts).items():
        actual = getattr(terminal, name)
        _integer(actual, MAX_EVENTS - 1)
        if actual != expected:
            raise RunnerEventError("terminal counts disagree with event records")
    return StructuredTestSummary(
        tests_run=tests_run, passed=counts["passed"], assertions=tuple(assertions),
        test_errors=counts["test_errors"], skipped=counts["skipped"],
        collection_errors=counts["collection_errors"],
        infrastructure_errors=counts["infrastructure_errors"],
        complete=capture.complete, truncated=capture.truncated,
        outcome_known=capture.outcome_known,
    )


def structured_run_from_events(
    ticket: DispatchTicket, result: CommandResult, capture: RunnerEventCapture,
) -> StructuredRun:
    """Return a backend reply retaining the exact supplied ticket and result.

    Call inside an explicitly injected backend's run_structured, immediately
    after trusted instrumentation returns this dispatch's result and capture.
    The existing adapter validates the full ticket, result fields, deadline,
    output budget and lifecycle. This helper cannot prove where events came
    from or bind an arbitrary capture to execution by inspecting its contents.
    """
    if type(ticket) is not DispatchTicket or type(result) is not CommandResult:
        raise RunnerEventError("exact DispatchTicket and CommandResult required")
    if (type(ticket.command) is not str or type(result.command) is not str
            or result.command != ticket.command):
        raise RunnerEventError("command result must match the dispatch ticket")
    return StructuredRun(ticket, result, reduce_runner_events(capture))

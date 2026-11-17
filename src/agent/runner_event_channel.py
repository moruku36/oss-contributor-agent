"""Versioned, bounded event bytes for an explicitly trusted injected channel.

JSON and receipt digests establish consistency, never authenticity. There is no
transport or default wiring here. The channel must independently be trusted to
execute/capture once and enforce bounds in flight; local checks cannot stop I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
import json
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Protocol

from src.agent.filesystem_staging import StageReceipt, StagedFile
from src.agent.runner_events import (
    MAX_EVENTS, RunCompleted, RunnerErrorEvent, RunnerEventCapture,
    RunnerEventError, TestEvent, reduce_runner_events, structured_run_from_events,
)
from src.agent.staged_runner_bridge import DispatchTicket, receipt_digest
from src.agent.structured_runner_evidence import StructuredBackend, StructuredRun
from src.sandbox.docker_manager import CommandResult


WIRE_VERSION = 1
MAX_WIRE_BYTES = 1_048_576
MAX_WIRE_DEPTH = 8
MAX_OUTPUT_BYTES = 524_288
MAX_DISPATCHES = 64
MAX_TIMEOUT_SECONDS = 2700
_COMPLETED_FIELDS = (
    "sequence", "tests_run", "passed", "assertion_failures", "test_errors",
    "skipped", "collection_errors", "infrastructure_errors",
)


class EventChannelError(RunnerEventError):
    """Rejected bytes, binding, bounds or channel lifecycle; never retry here."""


@dataclass(frozen=True, slots=True)
class RunnerEventResponse:
    ticket: DispatchTicket
    result: CommandResult
    capture: RunnerEventCapture


class TrustedEventChannel(Protocol):
    """Independently trusted once-only execution and event-capture dependency.

    Enforce all forwarded bounds while executing/capturing. Return one bytes
    response, without truncation, or raise on any uncertain outcome. Never retry
    or fall back. No host/process/network implementation is supplied here.
    """

    def dispatch(
        self, ticket: DispatchTicket, *, timeout_seconds: float,
        max_output_bytes: int, max_wire_bytes: int,
    ) -> bytes: ...


def _integer(value: object, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise EventChannelError("exact bounded integer required, excluding bool")


def _number(value: object, *, positive: bool = False) -> None:
    if (type(value) not in (int, float) or not 0 <= value <= MAX_TIMEOUT_SECONDS
            or not isfinite(value) or (positive and value == 0)):
        raise EventChannelError("bounded finite seconds required, excluding bool")


def _text(value: object, cap: int, *, required: bool = False) -> int:
    if type(value) is not str or len(value) > cap or (required and not value.strip()):
        raise EventChannelError("exact bounded UTF-8 text required")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise EventChannelError("text must encode as strict UTF-8") from exc
    if size > cap:
        raise EventChannelError("text exceeds byte bound")
    return size


def _object(value: object, fields: tuple[str, ...]) -> dict:
    if type(value) is not dict or set(value) != set(fields):
        raise EventChannelError("exact object fields required")
    return value


def _sha256(value: object) -> None:
    if (type(value) is not str or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)):
        raise EventChannelError("lowercase SHA-256 text required")


def _ticket_document(ticket: DispatchTicket) -> dict:
    if type(ticket) is not DispatchTicket or type(ticket.receipt) is not StageReceipt:
        raise EventChannelError("exact ticket and complete staged receipt required")
    _text(ticket.attempt_id, 128, required=True)
    _text(ticket.dispatch_id, 128, required=True)
    _integer(ticket.generation, 1, MAX_DISPATCHES)
    _text(ticket.command, 4096, required=True)
    if "\0" in ticket.command:
        raise EventChannelError("command must be NUL-free")
    receipt = ticket.receipt
    _text(receipt.stage_name, 128, required=True)
    if (type(receipt.attempt_id) is not str or receipt.attempt_id != ticket.attempt_id
            or type(receipt.outcome) is not str or receipt.outcome != "complete"
            or type(receipt.files) is not tuple or not 1 <= len(receipt.files) <= 256):
        raise EventChannelError("complete bounded receipt for this attempt required")
    files = []
    for item in receipt.files:
        if type(item) is not StagedFile:
            raise EventChannelError("exact staged file required")
        _text(item.path, 240, required=True)
        _text(item.role, 16, required=True)
        _sha256(item.sha256)
        _integer(item.size_bytes, 0, 262_144)
        files.append(dict(path=item.path, role=item.role, sha256=item.sha256,
                          size_bytes=item.size_bytes))
    _integer(receipt.total_bytes, 0, 4_194_304)
    _sha256(ticket.receipt_sha256)
    if (receipt.total_bytes != sum(item.size_bytes for item in receipt.files)
            or ticket.receipt_sha256 != receipt_digest(receipt)):
        raise EventChannelError("receipt byte total or digest mismatch")
    return dict(
        attempt_id=ticket.attempt_id, dispatch_id=ticket.dispatch_id,
        generation=ticket.generation, command=ticket.command,
        receipt_sha256=ticket.receipt_sha256,
        receipt=dict(attempt_id=receipt.attempt_id, stage_name=receipt.stage_name,
                     files=files, total_bytes=receipt.total_bytes, outcome=receipt.outcome),
    )


def _result_document(result: CommandResult, ticket: DispatchTicket, cap: int) -> dict:
    if type(result) is not CommandResult:
        raise EventChannelError("exact CommandResult required")
    if type(result.command) is not str or result.command != ticket.command:
        raise EventChannelError("result command must equal ticket command")
    _integer(result.exit_code, -(2**31), 2**31 - 1)
    _number(result.duration_seconds)
    # Output is opaque: controls, JSON-looking content and test-looking text
    # retain their exact values. Only type, strict UTF-8 and byte bounds apply.
    stdout_bytes = _text(result.stdout, cap)
    stderr_bytes = _text(result.stderr, cap)
    if stdout_bytes + stderr_bytes > cap:
        raise EventChannelError("combined output exceeds byte bound")
    return dict(command=result.command, exit_code=result.exit_code,
                stdout=result.stdout, stderr=result.stderr,
                duration_seconds=result.duration_seconds)


def _capture_document(capture: RunnerEventCapture) -> dict:
    try:
        reduce_runner_events(capture)
    except RunnerEventError as exc:
        raise EventChannelError("invalid bounded event capture") from exc
    events = []
    for event in capture.events:
        if type(event) is TestEvent:
            events.append(dict(type="test", sequence=event.sequence,
                               test_id=event.test_id, outcome=event.outcome, detail=event.detail))
        elif type(event) is RunnerErrorEvent:
            events.append(dict(type="runner_error", sequence=event.sequence,
                               category=event.category, detail=event.detail))
        else:
            events.append(dict(type="completed", **{
                name: getattr(event, name) for name in _COMPLETED_FIELDS
            }))
    return dict(events=events, complete=capture.complete, truncated=capture.truncated,
                outcome_known=capture.outcome_known)


def _limits(max_wire_bytes: int, max_output_bytes: int) -> None:
    _integer(max_wire_bytes, 1, MAX_WIRE_BYTES)
    _integer(max_output_bytes, 1, MAX_OUTPUT_BYTES)


def encode_runner_event_response(
    ticket: DispatchTicket, result: CommandResult, capture: RunnerEventCapture, *,
    max_wire_bytes: int = MAX_WIRE_BYTES, max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> bytes:
    """Deterministic version-1 UTF-8 JSON bytes for trusted instrumentation.

    Reject invalid/over-bound input in full; never coerce, sanitize or truncate.
    Key order and whitespace are canonical; event and manifest order are kept.
    """
    _limits(max_wire_bytes, max_output_bytes)
    document = dict(version=WIRE_VERSION, ticket=_ticket_document(ticket),
                    result=_result_document(result, ticket, max_output_bytes),
                    capture=_capture_document(capture))
    payload = json.dumps(document, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > max_wire_bytes:
        raise EventChannelError("encoded response exceeds wire byte bound")
    return payload


def _check_depth(payload: bytes) -> None:
    # Scan before parsing so deeply nested hostile JSON never reaches the JSON
    # decoder's recursion. Quotes/escapes hide brackets in opaque output text.
    depth = 0
    quoted = escaped = False
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_WIRE_DEPTH:
                raise EventChannelError("wire nesting exceeds limit")
        elif byte in (93, 125):
            depth -= 1
            if depth < 0:
                raise EventChannelError("unbalanced wire nesting")
    if depth or quoted:
        raise EventChannelError("incomplete wire JSON")


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EventChannelError("duplicate JSON object key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise EventChannelError("nonfinite JSON number forbidden")


def _float(token: str) -> float:
    if len(token) > 64:
        raise EventChannelError("JSON number exceeds precision bound")
    try:
        value = float(token)
        valid = isfinite(value) and Decimal(token) == Decimal(str(value))
    except (ValueError, OverflowError, DecimalException) as exc:
        raise EventChannelError("invalid bounded JSON number") from exc
    if not valid:
        raise EventChannelError("nonfinite or precision-losing JSON number")
    return value


def _decode_ticket(value: object) -> DispatchTicket:
    obj = _object(value, ("attempt_id", "dispatch_id", "generation", "command",
                          "receipt_sha256", "receipt"))
    receipt = _object(obj["receipt"], ("attempt_id", "stage_name", "files",
                                       "total_bytes", "outcome"))
    if type(receipt["outcome"]) is not str or receipt["outcome"] != "complete":
        raise EventChannelError("complete receipt outcome required")
    files = receipt["files"]
    if type(files) is not list or not 1 <= len(files) <= 256:
        raise EventChannelError("bounded staged file array required")
    staged = tuple(StagedFile(**_object(item, ("path", "role", "sha256", "size_bytes")))
                   for item in files)
    ticket = DispatchTicket(
        attempt_id=obj["attempt_id"], dispatch_id=obj["dispatch_id"],
        generation=obj["generation"], command=obj["command"],
        receipt_sha256=obj["receipt_sha256"],
        receipt=StageReceipt(receipt["attempt_id"], receipt["stage_name"],
                             staged, receipt["total_bytes"]),
    )
    _ticket_document(ticket)
    return ticket


def _decode_capture(value: object) -> RunnerEventCapture:
    obj = _object(value, ("events", "complete", "truncated", "outcome_known"))
    records = obj["events"]
    if type(records) is not list or not 1 <= len(records) <= MAX_EVENTS:
        raise EventChannelError("bounded nonempty event array required")
    events = []
    for record in records:
        if type(record) is not dict or type(record.get("type")) is not str:
            raise EventChannelError("typed event object required")
        kind = record["type"]
        if kind == "test":
            fields = ("sequence", "test_id", "outcome", "detail")
            event_type = TestEvent
        elif kind == "runner_error":
            fields = ("sequence", "category", "detail")
            event_type = RunnerErrorEvent
        elif kind == "completed":
            fields = _COMPLETED_FIELDS
            event_type = RunCompleted
        else:
            raise EventChannelError("unknown event type")
        _object(record, ("type", *fields))
        events.append(event_type(**{name: record[name] for name in fields}))
    capture = RunnerEventCapture(tuple(events), obj["complete"], obj["truncated"],
                                 obj["outcome_known"])
    _capture_document(capture)
    return capture


def decode_runner_event_response(
    payload: bytes, *, expected_ticket: DispatchTicket,
    max_wire_bytes: int = MAX_WIRE_BYTES, max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> RunnerEventResponse:
    """Decode explicit bytes only, binding the entire capture to the full ticket.

    Wire bytes are capped before UTF-8 decoding or JSON parsing. All fields are
    mandatory, objects reject duplicate/unknown keys, and arrays remain bounded.
    Stdout/stderr never supply events or fallback evidence.
    """
    _limits(max_wire_bytes, max_output_bytes)
    if type(payload) is not bytes or not 1 <= len(payload) <= max_wire_bytes:
        raise EventChannelError("explicit bounded nonempty bytes required")
    _check_depth(payload)
    expected = _ticket_document(expected_ticket)
    try:
        document = json.loads(payload.decode("utf-8", errors="strict"),
                              object_pairs_hook=_pairs, parse_constant=_constant,
                              parse_float=_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise EventChannelError("invalid strict UTF-8 JSON response") from exc
    obj = _object(document, ("version", "ticket", "result", "capture"))
    _integer(obj["version"], WIRE_VERSION, WIRE_VERSION)
    ticket = _decode_ticket(obj["ticket"])
    # Compare every validated scalar, including the full manifest and complete
    # receipt. A digest comparison alone is deliberately insufficient.
    if _ticket_document(ticket) != expected:
        raise EventChannelError("response does not echo the full expected ticket")
    result = CommandResult(**_object(obj["result"], (
        "command", "exit_code", "stdout", "stderr", "duration_seconds",
    )))
    _result_document(result, ticket, max_output_bytes)
    return RunnerEventResponse(ticket, result, _decode_capture(obj["capture"]))


class EventChannelBackend(StructuredBackend):
    """A real StructuredBackend for the existing evidence adapter, by injection.

    One attempt, contiguous generations and fresh dispatch IDs; bounded retained
    replay state. Any exception after dispatch consumption is terminal, including
    cancellation. There is no retry or fallback. A late check cannot stop blocked
    I/O: the independently trusted channel must enforce deadlines in flight.
    """

    def __init__(
        self, *, channel: TrustedEventChannel, max_dispatches: int = 32,
        max_wire_bytes: int = MAX_WIRE_BYTES,
    ) -> None:
        if not callable(getattr(channel, "dispatch", None)):
            raise EventChannelError("explicit trusted event channel required")
        _integer(max_dispatches, 1, MAX_DISPATCHES)
        _integer(max_wire_bytes, 1, MAX_WIRE_BYTES)
        self._channel = channel
        self._limit, self._wire_limit = max_dispatches, max_wire_bytes
        self._attempt_id: str | None = None
        self._generation = 0
        self._dispatch_ids: set[str] = set()
        self._state = "ready"
        self._gate = Lock()

    def run_structured(
        self, ticket: DispatchTicket, *, timeout_seconds: float, max_output_bytes: int,
    ) -> StructuredRun:
        if not self._gate.acquire(blocking=False):
            raise EventChannelError("event channel backend is busy; no retry")
        try:
            if self._state != "ready":
                raise EventChannelError("event channel backend is terminal; no retry")
            _number(timeout_seconds, positive=True)
            _limits(self._wire_limit, max_output_bytes)
            expected = _ticket_document(ticket)
            if (ticket.generation != self._generation + 1 or ticket.generation > self._limit
                    or self._attempt_id not in (None, ticket.attempt_id)
                    or ticket.dispatch_id in self._dispatch_ids):
                raise EventChannelError("wrong attempt, replay or exhausted generation bound")
            self._attempt_id, self._generation = ticket.attempt_id, ticket.generation
            self._dispatch_ids.add(ticket.dispatch_id)
            self._state = "dispatching"
            try:
                deadline = monotonic() + timeout_seconds
                payload = self._channel.dispatch(
                    ticket, timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes, max_wire_bytes=self._wire_limit,
                )
                if _ticket_document(ticket) != expected:
                    raise EventChannelError("ticket changed during channel call")
                reply = decode_runner_event_response(
                    payload, expected_ticket=ticket, max_wire_bytes=self._wire_limit,
                    max_output_bytes=max_output_bytes,
                )
                if (not reply.capture.complete or reply.capture.truncated
                        or not reply.capture.outcome_known):
                    raise EventChannelError("channel execution/capture outcome is uncertain")
                if reply.result.duration_seconds >= timeout_seconds:
                    raise EventChannelError("reported duration exceeds dispatch deadline")
                run = structured_run_from_events(reply.ticket, reply.result, reply.capture)
                if monotonic() >= deadline:
                    raise TimeoutError("event channel returned after deadline; no retry")
                self._state = "ready"
                return run
            except BaseException:
                self._state = "failed"
                raise
        finally:
            self._gate.release()

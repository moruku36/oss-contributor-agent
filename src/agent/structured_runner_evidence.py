"""Injection-only structured runner AND assessor for the staged attempt path.

The backend is explicitly trusted to report actual execution and enforce bounds
in flight. Echoes establish consistency, not provenance or isolation. No process,
stdout parser, backend selection, retries or production activation live here.
Single caller, one attempt, at most 64 dispatches; not hostile-Python protection.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from math import isfinite
from time import monotonic
from typing import Protocol

from src.agent.core import TestAssessment, TestCommand, TestOutcome
from src.agent.evidence_summary import (
    RunnerEvidenceSummary, result_digest, structured_classification, summary_digest,
)
from src.agent.filesystem_staging import StageReceipt, StagedFile
from src.agent.snapshot_workspace import SnapshotError, _utf8
from src.agent.staged_runner_bridge import (
    DispatchTicket, EvidenceRejected, RunnerEvidence, receipt_digest,
)
from src.sandbox.docker_manager import CommandResult


@dataclass(frozen=True)
class AssertionFailure:
    """One actually executed failed test, with authoritative assertion detail.

    Multiple assertions within one test must be combined into its single entry.
    Collection, syntax, dependency and runner errors are never assertion entries.
    """

    test_id: str
    detail: str


@dataclass(frozen=True)
class StructuredTestSummary:
    """Disjoint executed-test counts; skipped tests are excluded from tests_run.

    tests_run == passed + len(assertions) + test_errors. Flags describe the whole
    command, including collection and capture, not just the reported test subset.
    The trusted backend must explicitly attest all three flags.
    """

    tests_run: int
    passed: int
    assertions: tuple[AssertionFailure, ...]
    test_errors: int
    skipped: int
    collection_errors: int
    infrastructure_errors: int
    complete: bool
    truncated: bool
    outcome_known: bool


@dataclass(frozen=True)
class StructuredRun:
    """Backend response: full echoed ticket, exact result and structured summary."""

    ticket: DispatchTicket
    result: CommandResult
    summary: StructuredTestSummary


class StructuredBackend(Protocol):
    """Explicitly trusted dependency; once against the exact ticket generation.

    Enforce timeout and combined output bounds during execution/capture. Return
    a new CommandResult, echo the whole ticket and report authoritative test
    events. Never retry/fallback; raise on uncertain invocation outcome. No
    snapshot/adapter access. Python-side checks cannot interrupt a blocked call.
    """

    def run_structured(
        self, ticket: DispatchTicket, *, timeout_seconds: float, max_output_bytes: int,
    ) -> StructuredRun: ...


def _count(value: object, ceiling: int = 1_000_000) -> None:
    if type(value) is not int or not 0 <= value <= ceiling:
        raise SnapshotError("expected a bounded nonnegative integer, excluding bool")


def _seconds(value: object, ceiling: float) -> None:
    try:
        valid = (type(value) in (int, float) and isfinite(value) and 0 < value <= ceiling)
    except OverflowError:
        valid = False
    if not valid:
        raise SnapshotError("expected a positive finite timeout within the limit")


def _required(value: object, cap: int) -> None:
    _utf8(value, cap)
    if not value.strip():
        raise SnapshotError("nonempty structured evidence text required")


def _ticket_key(ticket: DispatchTicket) -> tuple:
    if type(ticket) is not DispatchTicket or type(ticket.receipt) is not StageReceipt:
        raise SnapshotError("exact DispatchTicket and complete StageReceipt required")
    _count(ticket.generation, 64)
    if ticket.generation == 0:
        raise SnapshotError("generation is one-based")
    _required(ticket.attempt_id, 128)
    _required(ticket.dispatch_id, 128)
    _required(ticket.command, 4096)
    receipt = ticket.receipt
    _required(receipt.stage_name, 128)
    if (type(receipt.attempt_id) is not str or receipt.attempt_id != ticket.attempt_id
            or type(receipt.files) is not tuple or not 1 <= len(receipt.files) <= 256):
        raise SnapshotError("receipt does not bind this attempt and bounded manifest")
    files = []
    for item in receipt.files:
        if type(item) is not StagedFile:
            raise SnapshotError("exact staged file records required")
        _required(item.path, 240)
        _required(item.role, 16)
        _required(item.sha256, 64)
        _count(item.size_bytes, 262_144)
        files.append((item.path, item.role, item.sha256, item.size_bytes))
    _count(receipt.total_bytes, 4_194_304)
    if (receipt.total_bytes != sum(item.size_bytes for item in receipt.files)
            or type(ticket.receipt_sha256) is not str
            or ticket.receipt_sha256 != receipt_digest(receipt)):
        raise SnapshotError("receipt bytes or digest mismatch")
    # Copy scalar values; do not retain a mutable result/response as its own key.
    return (ticket.attempt_id, ticket.dispatch_id, ticket.generation, receipt.stage_name,
            tuple(files), receipt.total_bytes, receipt.outcome, ticket.receipt_sha256,
            ticket.command)


def _result_key(result: CommandResult) -> tuple:
    # Include types: bool/int and int/float equality must not hide later mutation.
    return tuple((type(value), value) for value in (
        result.command, result.exit_code, result.stdout, result.stderr, result.duration_seconds,
    ))


def _assessment(summary: StructuredTestSummary, exit_code: int) -> TestAssessment:
    if type(summary) is not StructuredTestSummary:
        raise SnapshotError("exact structured test summary required")
    counts = (summary.tests_run, summary.passed, summary.test_errors, summary.skipped,
              summary.collection_errors, summary.infrastructure_errors)
    for count in counts:
        _count(count)
    if any(type(flag) is not bool for flag in (
        summary.complete, summary.truncated, summary.outcome_known,
    )):
        raise SnapshotError("summary flags must be explicit bools")
    if type(summary.assertions) is not tuple or len(summary.assertions) > 128:
        raise SnapshotError("bounded tuple of assertion identities required")
    details, identities = [], set()
    for failure in summary.assertions:
        if type(failure) is not AssertionFailure:
            raise SnapshotError("exact assertion records required")
        _required(failure.test_id, 256)
        _required(failure.detail, 1024)
        if failure.test_id in identities:
            raise SnapshotError("duplicate failed-test identity")
        identities.add(failure.test_id)
        details.append(f"{failure.test_id}: {failure.detail}")
    failures = len(details)
    if (summary.tests_run != summary.passed + failures + summary.test_errors
            or summary.tests_run + summary.skipped + summary.collection_errors
            + summary.infrastructure_errors > 1_000_000):
        raise SnapshotError("inconsistent or over-limit summary counts")
    classification, reason = structured_classification(
        summary.tests_run, failures, summary.test_errors, summary.collection_errors,
        summary.infrastructure_errors, summary.complete, summary.truncated,
        summary.outcome_known, exit_code,
    )
    outcome = TestOutcome(classification)
    detail = (f"{reason}; executed={summary.tests_run}, skipped={summary.skipped}, "
              f"test_errors={summary.test_errors}, collection_errors={summary.collection_errors}, "
              f"infrastructure_errors={summary.infrastructure_errors}")
    if details:
        detail += "\n" + "\n".join(details)
    _utf8(detail, 4096)  # also compatible with the existing review-report bound
    return TestAssessment(outcome, summary.tests_run, failures, detail)


class StructuredRunnerEvidenceAdapter:
    """Use the SAME instance as staged runner and SnapshotWorkspace assessor.

    Only the latest result/ticket can be assessed, once. Result identities are
    retained up to max_dispatches to reject backend reuse; only one assessment
    is retained. A dispatched error is terminal, including malformed/late replies.
    """

    def __init__(self, *, backend: StructuredBackend, max_dispatches: int = 32) -> None:
        if not callable(getattr(backend, "run_structured", None)):
            raise SnapshotError("explicit trusted structured backend required")
        _count(max_dispatches, 64)
        if not max_dispatches:
            raise SnapshotError("positive dispatch bound required")
        self._backend, self._limit = backend, max_dispatches
        self._attempt_id: str | None = None
        self._generation = 0
        self._state = "ready"
        self._seen: list[CommandResult] = []
        self._current: tuple | None = None

    def run_staged(
        self, ticket: DispatchTicket, *, timeout_seconds: float, max_output_bytes: int,
    ) -> RunnerEvidence:
        if self._state != "ready":
            raise EvidenceRejected("structured adapter is busy or terminal; no retry")
        _seconds(timeout_seconds, 2700)
        _count(max_output_bytes, 524_288)
        if not max_output_bytes:
            raise SnapshotError("positive output cap required")
        key = _ticket_key(ticket)
        TestCommand(ticket.command, timeout_seconds)
        if (ticket.generation != self._generation + 1 or ticket.generation > self._limit
                or self._attempt_id not in (None, ticket.attempt_id)):
            raise EvidenceRejected("wrong attempt, stale generation or exhausted dispatch bound")
        self._attempt_id, self._generation = ticket.attempt_id, ticket.generation
        self._current = None  # supersede before the sole potentially uncertain call
        self._state = "dispatching"
        deadline = monotonic() + timeout_seconds
        try:
            reply = self._backend.run_structured(
                ticket, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes,
            )
            if (type(reply) is not StructuredRun or _ticket_key(ticket) != key
                    or _ticket_key(reply.ticket) != key):
                raise EvidenceRejected("backend did not echo the exact dispatch ticket")
            result = reply.result
            if type(result) is not CommandResult or any(result is old for old in self._seen):
                raise EvidenceRejected("backend must return a new exact CommandResult")
            duration = result.duration_seconds
            if (type(result.command) is not str or result.command != ticket.command
                    or type(result.exit_code) is not int or not -(2**31) <= result.exit_code < 2**31
                    or type(duration) not in (int, float) or not isfinite(duration)
                    or not 0 <= duration < timeout_seconds
                    or type(result.stdout) is not str or type(result.stderr) is not str
                    or len(result.stdout) + len(result.stderr) > max_output_bytes
                    or len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
                    > max_output_bytes):
                raise EvidenceRejected("mismatched, malformed or over-bound command result")
            assessment = _assessment(reply.summary, result.exit_code)
            summary = reply.summary
            # Only immutable scalar copies leave this adapter: no result, ticket,
            # receipt, assertion tuple or backend response aliases are retained.
            scalar = RunnerEvidenceSummary(
                1, key[0], key[1], key[2], key[3], key[7], key[8], result_digest(result),
                result.exit_code, result.duration_seconds, assessment.outcome.value,
                summary.tests_run, summary.passed, assessment.assertion_failures,
                summary.test_errors, summary.skipped, summary.collection_errors,
                summary.infrastructure_errors, summary.complete, summary.truncated,
                summary.outcome_known, sha256(assessment.detail.encode("utf-8")).hexdigest(),
            )
            assessment = replace(assessment, runner_evidence=replace(
                scalar, binding_sha256=summary_digest(scalar),
            ))
            if monotonic() >= deadline:
                raise TimeoutError("structured backend returned after deadline; no retry")
            self._seen.append(result)
            self._current = (key, result, _result_key(result), assessment)
            self._state = "ready"
            return RunnerEvidence(ticket.attempt_id, ticket.dispatch_id, ticket.receipt.stage_name,
                                  ticket.receipt_sha256, result)
        except BaseException:
            self._state = "failed"
            self._current = None
            raise

    def assess(
        self, attempt_id: str, result: CommandResult, *, timeout_seconds: float, max_bytes: int,
    ) -> TestAssessment:
        _seconds(timeout_seconds, 2700)
        _count(max_bytes, 524_288)
        if not max_bytes:
            raise SnapshotError("positive assessment byte bound required")
        current = self._current
        if (self._state != "ready" or current is None or type(attempt_id) is not str
                or attempt_id != current[0][0] or result is not current[1]):
            raise EvidenceRejected("wrong attempt, stale, replayed or unrecognized result")
        self._current = None  # consume even on mutation or an insufficient output bound
        if _result_key(result) != current[2]:
            raise EvidenceRejected("command result changed after structured dispatch")
        assessment = current[3]
        _utf8(assessment.detail, min(max_bytes, 4096))
        return assessment

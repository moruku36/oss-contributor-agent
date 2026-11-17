"""Six focused offline scenarios using declared synthetic instrumentation events.

No runner process, staged source, model, network or historical test suite is used.
These fixtures demonstrate aggregation and adapter consistency, not provenance.
"""
from dataclasses import FrozenInstanceError, replace
import unittest

from src.agent.core import TestOutcome
from src.agent.filesystem_staging import StageReceipt, StagedFile
from src.agent.runner_events import (
    MAX_EVENTS, RunnerErrorEvent, RunnerEventCapture, RunnerEventError,
    RunCompleted, TestEvent, reduce_runner_events, structured_run_from_events,
)
from src.agent.staged_runner_bridge import DispatchTicket, receipt_digest
from src.agent.structured_runner_evidence import (
    AssertionFailure, StructuredRun, StructuredRunnerEvidenceAdapter, StructuredTestSummary,
)
from src.sandbox.docker_manager import CommandResult


def _passed():
    return RunnerEventCapture((
        TestEvent(1, "suite::passed", "passed"),
        RunCompleted(2, 1, 1, 0, 0, 0, 0, 0),
    ), True, False, True)


def _ticket():
    receipt = StageReceipt("attempt", "stage", (
        StagedFile("tests/test_sample.py", "test", "a" * 64, 1),
    ), 1)
    return DispatchTicket("attempt", "dispatch", 1, receipt,
                          receipt_digest(receipt), "runner-events-fixture")


class _EventBackend:
    """Synthetic source; the real reducer supplies the adapter's summary."""

    def __init__(self, capture, exit_code):
        self.capture, self.exit_code = capture, exit_code
        self.calls = []
        self.reply = None

    def run_structured(self, ticket, *, timeout_seconds, max_output_bytes):
        self.calls.append((ticket, timeout_seconds, max_output_bytes))
        result = CommandResult(ticket.command, self.exit_code,
                               "FAILED (failures=999): untrusted stdout\n", "", 0.01)
        self.reply = structured_run_from_events(ticket, result, self.capture)
        return self.reply


def _assess(capture, exit_code=0):
    backend, ticket = _EventBackend(capture, exit_code), _ticket()
    adapter = StructuredRunnerEvidenceAdapter(backend=backend)
    evidence = adapter.run_staged(ticket, timeout_seconds=2, max_output_bytes=4096)
    assessment = adapter.assess(ticket.attempt_id, evidence.result,
                                timeout_seconds=1, max_bytes=4096)
    return ticket, backend, evidence, assessment


class RunnerEventsTests(unittest.TestCase):
    def test_reduces_all_explicit_outcomes_to_existing_summary(self):
        capture = RunnerEventCapture((
            RunnerErrorEvent(1, "collection", "module could not be collected"),
            TestEvent(2, "suite::pass", "passed"),
            TestEvent(3, "suite::assert", "assertion_failed", "expected café"),
            TestEvent(4, "suite::error", "error", "fixture setup raised"),
            TestEvent(5, "suite::skip", "skipped", "platform unavailable"),
            RunnerErrorEvent(6, "infrastructure", "worker channel closed"),
            RunCompleted(7, 3, 1, 1, 1, 1, 1, 1),
        ), True, False, True)
        summary = reduce_runner_events(capture)
        self.assertIs(type(summary), StructuredTestSummary)
        self.assertEqual(summary, StructuredTestSummary(
            3, 1, (AssertionFailure("suite::assert", "expected café"),),
            1, 1, 1, 1, True, False, True,
        ))
        for record, field, value in ((capture, "complete", False),
                                     (capture.events[2], "detail", "changed"),
                                     (capture.events[-1], "tests_run", 0)):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(record, field, value)

    def test_actual_adapter_consumes_exact_ticket_result_and_reduced_summary(self):
        failed = RunnerEventCapture((
            TestEvent(1, "suite::assert", "assertion_failed", "expected 2; observed 1"),
            TestEvent(2, "suite::skip", "skipped"),
            RunCompleted(3, 1, 0, 1, 0, 1, 0, 0),
        ), True, False, True)
        for capture, code, outcome, failures in (
            (_passed(), 0, TestOutcome.PASS, 0),
            (failed, 1, TestOutcome.ASSERTION_FAILED, 1),
        ):
            with self.subTest(outcome=outcome):
                ticket, backend, evidence, assessment = _assess(capture, code)
                self.assertIs(type(backend.reply), StructuredRun)
                self.assertIs(backend.reply.ticket, ticket)
                self.assertIs(backend.reply.result, evidence.result)
                self.assertEqual(backend.calls, [(ticket, 2, 4096)])
                self.assertEqual(assessment.outcome, outcome)
                self.assertEqual(assessment.tests_run, 1)
                self.assertEqual(assessment.assertion_failures, failures)
                scalar = assessment.runner_evidence
                self.assertEqual(scalar.dispatch_id, ticket.dispatch_id)
                self.assertEqual(scalar.assertion_failures, failures)
                self.assertEqual(scalar.skipped, backend.reply.summary.skipped)
                self.assertNotIn("failures=999", assessment.detail)
        result = CommandResult("another-command", 0)
        for ticket, supplied in ((_ticket(), result), (object(), result),
                                 (_ticket(), object())):
            with self.assertRaises(RunnerEventError):
                structured_run_from_events(ticket, supplied, _passed())

    def test_flags_empty_skipped_errors_and_exit_conflicts_remain_conservative(self):
        cases = [
            (replace(_passed(), complete=False), 0, TestOutcome.UNKNOWN),
            (replace(_passed(), truncated=True), 0, TestOutcome.UNKNOWN),
            (replace(_passed(), outcome_known=False), 0, TestOutcome.UNKNOWN),
            (_passed(), 1, TestOutcome.UNKNOWN),
            (RunnerEventCapture((RunCompleted(1, 0, 0, 0, 0, 0, 0, 0),),
                                True, False, True), 0, TestOutcome.UNKNOWN),
            (RunnerEventCapture((TestEvent(1, "skipped", "skipped"),
                                 RunCompleted(2, 0, 0, 0, 0, 1, 0, 0)),
                                True, False, True), 0, TestOutcome.UNKNOWN),
        ]
        # Each error category dominates assertions, even with uncertain capture.
        for category in ("test", "collection", "infrastructure"):
            error = (TestEvent(1, "error", "error", "setup raised") if category == "test"
                     else RunnerErrorEvent(1, category, "runner raised"))
            counts = (2, 0, 1, 1, 0, 0, 0) if category == "test" else (
                1, 0, 1, 0, 0, int(category == "collection"), int(category == "infrastructure"))
            capture = RunnerEventCapture((error,
                TestEvent(2, "failed", "assertion_failed", "values differ"),
                RunCompleted(3, *counts)), False, True, False)
            cases.append((capture, 1, TestOutcome.INFRA_ERROR))
        for capture, code, expected in cases:
            with self.subTest(capture=capture, exit_code=code):
                _, _, _, assessment = _assess(capture, code)
                self.assertEqual(assessment.outcome, expected)
                scalar = assessment.runner_evidence
                self.assertEqual((scalar.complete, scalar.truncated, scalar.outcome_known),
                                 (capture.complete, capture.truncated, capture.outcome_known))

    def test_rejects_duplicate_identities_order_unknown_and_missing_terminal(self):
        base = _passed()
        streams = [(), (base.events[0],),
            (replace(base.events[0], sequence=0), base.events[-1]),
            (replace(base.events[0], sequence=True), base.events[-1]),
            (base.events[0], replace(base.events[-1], sequence=3)),
            (RunCompleted(1, 0, 0, 0, 0, 0, 0, 0), base.events[-1]),
            (RunCompleted(1, 0, 0, 0, 0, 0, 0, 0), TestEvent(2, "late", "passed")),
            (base.events[0], RunnerErrorEvent(2, "collection", "late collection"),
             RunCompleted(3, 1, 1, 0, 0, 0, 1, 0)),
            ({"sequence": 1, "outcome": "passed"}, base.events[-1]),
            list(base.events),
        ]
        for outcome in ("passed", "assertion_failed", "error", "skipped"):
            detail = "reported detail" if outcome in ("assertion_failed", "error") else ""
            streams.append((base.events[0], TestEvent(2, "suite::passed", outcome, detail),
                            RunCompleted(3, 2, 2, 0, 0, 0, 0, 0)))
        for index, stream in enumerate(streams):
            for complete in (True, False):
                with self.subTest(stream=index, complete=complete):
                    with self.assertRaises(RunnerEventError):
                        reduce_runner_events(replace(base, events=stream, complete=complete))

    def test_rejects_malformed_fields_flags_and_terminal_counts(self):
        base, malformed = _passed(), []
        for field in ("complete", "truncated", "outcome_known"):
            for value in (0, 1, None, "true"):
                malformed.append(replace(base, **{field: value}))
        for field, value in (("test_id", 7), ("test_id", " "), ("outcome", True),
                             ("outcome", "unknown"), ("detail", None), ("detail", "failure")):
            malformed.append(replace(base, events=(replace(base.events[0], **{field: value}),
                                                   base.events[-1])))
        for event in (TestEvent(1, "id", "assertion_failed"), TestEvent(1, "id", "error"),
                      RunnerErrorEvent(1, "assertion", "not a runner error category"),
                      RunnerErrorEvent(1, "collection", ""),
                      RunnerErrorEvent(1, False, "bad category")):
            malformed.append(replace(base, events=(event, base.events[-1])))
        for field in ("tests_run", "passed", "assertion_failures", "test_errors", "skipped",
                      "collection_errors", "infrastructure_errors"):
            for value in (True, -1, 1.0, "1", MAX_EVENTS):
                terminal = replace(base.events[-1], **{field: value})
                malformed.append(replace(base, events=(base.events[0], terminal)))
        malformed.append(replace(base, events=(base.events[0],
                                               replace(base.events[-1], passed=0))))
        malformed.append({"events": base.events})
        for index, capture in enumerate(malformed):
            with self.subTest(case=index):
                with self.assertRaises(RunnerEventError):
                    reduce_runner_events(capture)

    def test_bounds_records_utf8_text_and_assertion_detail_without_truncating(self):
        base = _passed()
        streams = [base.events * (MAX_EVENTS // 2 + 1)]
        for field, text in (("test_id", "x" * 257), ("test_id", "é" * 129),
                            ("test_id", "id\nline"), ("detail", "x" * 1025),
                            ("detail", "é" * 513), ("detail", "\ud800"),
                            ("detail", "bad\x00text"), ("detail", "bad\x7ftext")):
            event = replace(TestEvent(1, "id", "skipped"), **{field: text})
            streams.append((event, RunCompleted(2, 0, 0, 0, 0, 1, 0, 0)))
        streams.append(tuple(TestEvent(i + 1, str(i), "skipped", "x" * 900)
                             for i in range(80)) + (RunCompleted(81, 0, 0, 0, 0, 80, 0, 0),))
        for count, detail in ((129, "bad"), (4, "x" * 1024)):
            streams.append(tuple(TestEvent(i + 1, str(i), "assertion_failed", detail)
                                 for i in range(count))
                           + (RunCompleted(count + 1, count, 0, count, 0, 0, 0, 0),))
        for index, stream in enumerate(streams):
            with self.subTest(case=index):
                with self.assertRaises(RunnerEventError):
                    reduce_runner_events(replace(base, events=stream))
        boundary = tuple(TestEvent(i + 1, str(i), "passed") for i in range(MAX_EVENTS - 1))
        summary = reduce_runner_events(replace(base, events=boundary + (
            RunCompleted(MAX_EVENTS, MAX_EVENTS - 1, MAX_EVENTS - 1, 0, 0, 0, 0, 0),)))
        self.assertEqual(summary.tests_run, MAX_EVENTS - 1)
        unicode_capture = replace(base, events=(
            TestEvent(1, "é" * 128, "assertion_failed", "é" * 512),
            RunCompleted(2, 1, 0, 1, 0, 0, 0, 0),))
        self.assertEqual(len(reduce_runner_events(unicode_capture).assertions), 1)

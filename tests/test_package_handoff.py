"""Authored, bounded local tests; no processes, models, credentials or network."""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.agent.core import (
    AttemptResult, CheckEvidence, LoopState, ModelReceipt, TestAssessment,
    TestCommand, TestOutcome, TestPlan,
)
from src.review import package_handoff as handoff
from src.review.draft_submission import (
    ApprovalDecision, DraftRequest, DraftSubmission, DraftTarget,
    DraftTransportReply, SubmissionStatus, prepare_draft_submission,
)
from src.review.package_selection import load_selected_review_request
from src.review.pr_builder import build_review_report
from src.review.review_package import export_review_package
from src.review.submission_journal import (
    JournalError, JournalReplayError, SQLiteSubmissionJournal,
)
from src.review.target_preflight import (
    ObservationStatus, TargetObservation, preflight_target,
)


def _request(title="Review synthetic evidence"):
    # These labels are inert evidence strings. No runner or command is invoked.
    plan = TestPlan(TestCommand("inert reproduction label", 1),
                    (TestCommand("inert suite label", 1),))

    def check(phase, command, outcome=TestOutcome.PASS):
        failed = outcome is TestOutcome.ASSERTION_FAILED
        return CheckEvidence(
            phase, command, int(failed), 0.001, "Synthetic local evidence",
            TestAssessment(outcome, 1, int(failed), "Synthetic assertion evidence"),
        )

    attempt = AttemptResult(
        success=True, attempts_used=1, state=LoopState.SUCCEEDED,
        states=[LoopState.READY, LoopState.STARTING, LoopState.BASELINE,
                LoopState.REPRODUCING, LoopState.CONTEXT, LoopState.PROPOSING,
                LoopState.APPLYING, LoopState.VERIFYING_REPRODUCTION,
                LoopState.VERIFYING_SUITE, LoopState.EXPORTING,
                LoopState.CLEANING_UP, LoopState.SUCCEEDED],
        checks=[check(LoopState.BASELINE, plan.relevant_suite[0].command),
                check(LoopState.REPRODUCING, plan.reproduction.command,
                      TestOutcome.ASSERTION_FAILED),
                check(LoopState.VERIFYING_REPRODUCTION, plan.reproduction.command),
                check(LoopState.VERIFYING_SUITE, plan.relevant_suite[0].command)],
        cleanup_confirmed=True, sandbox_attempt_id="synthetic-local-fixture",
        model_calls=1, tokens_used=7, known_tokens_used=7,
        model_receipts=[ModelReceipt("synthetic", "inert-fixture", 7)],
        elapsed_seconds=0.01,
        diff="diff --git a/sample.txt b/sample.txt\n--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-old\n+new\n",
        notes=["Synthetic local fixture; no code was executed."],
    )
    report = build_review_report(
        attempt, {"repo": "example/project", "number": 1, "title": "Synthetic issue"},
        base_commit="a" * 40, plan=plan, execution_kind="synthetic",
    )
    target = DraftTarget("https://github.com", "example/project", "example/project",
                         "refs/heads/review", "b" * 40, "refs/heads/main", "a" * 40)
    return prepare_draft_submission(report, target=target, title=title)


@contextmanager
def _workspace():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            yield root, fd, SQLiteSubmissionJournal(str(root / "journal.sqlite3"))
        finally:
            os.close(fd)


def _dependencies(events):
    def inspect(request):
        events.append("inspect")
        return TargetObservation(
            ObservationStatus.OBSERVED, DraftTarget(**request.data["target"]),
            request.snapshot_json, True, True,
        )

    def authorize(request):
        events.append("authority")
        return ApprovalDecision.AUTHORIZED

    def submit(request):
        events.append("transport")
        return DraftTransportReply(SubmissionStatus.SUBMITTED, request.binding_sha256, True, 7)

    return (SimpleNamespace(inspect_target=inspect),
            SimpleNamespace(authorize_and_consume=authorize),
            SimpleNamespace(submit_draft=submit))


def _operation(fd, request, journal, identity, dependencies, name="selected"):
    inspector, authority, transport = dependencies
    return handoff.ReviewPackageSubmission(
        fd, name, request, inspector=inspector, authority=authority,
        transport=transport, journal=journal, operation_id=identity,
    )


class PackageHandoffTests(unittest.TestCase):
    def test_actual_prepare_export_selection_preflight_submission_and_reopen(self):
        request = _request()
        events = ["prepare"]
        with _workspace() as (root, fd, journal):
            export_review_package(request, parent_fd=fd, name="selected")
            events.append("export")
            inspector, authority, transport = _dependencies(events)
            authorize, submit = authority.authorize_and_consume, transport.submit_draft

            def checked_authorize(seen):
                entry = journal.lookup("happy")
                self.assertEqual(entry.stage, "authority_intent")
                self.assertEqual(entry.snapshot_json, request.snapshot_json)
                return authorize(seen)

            def checked_submit(seen):
                self.assertEqual(journal.lookup("happy").stage, "transport_intent")
                self.assertEqual(seen.snapshot_json, request.snapshot_json)
                return submit(seen)

            def select(*args, **kwargs):
                events.append("selection")
                return load_selected_review_request(*args, **kwargs)

            def preflight(*args, **kwargs):
                events.append("preflight")
                return preflight_target(*args, **kwargs)

            authority.authorize_and_consume, transport.submit_draft = checked_authorize, checked_submit
            with patch.object(handoff, "DraftSubmission", wraps=DraftSubmission) as constructor:
                operation = _operation(fd, request, journal, "happy", (inspector, authority, transport))
                constructor.assert_called_once()
                self.assertEqual(events, ["prepare", "export"])
                self.assertIsNone(operation.outcome)
                self.assertIsNone(journal.lookup("happy"))
                with patch.object(handoff, "load_selected_review_request", side_effect=select), \
                        patch.object(handoff, "preflight_target", side_effect=preflight):
                    result = operation.run()
            self.assertEqual(events, ["prepare", "export", "selection", "preflight",
                                      "inspect", "authority", "transport"])
            self.assertIs(result, operation.outcome)
            self.assertIs(result.status, SubmissionStatus.SUBMITTED)
            self.assertTrue(result.dispatched)
            self.assertEqual(result.pull_request_number, 7)
            reopened = SQLiteSubmissionJournal(str(root / "journal.sqlite3"))
            record = reopened.lookup("happy")
            self.assertEqual(record.stage, "finished")
            self.assertEqual(record.snapshot_json, request.snapshot_json)
            self.assertEqual(record.outcome, result)

    def test_wrong_incomplete_and_mismatched_packages_reject_before_boundaries(self):
        request, other = _request(), _request("Different exact expected bytes")
        with _workspace() as (root, fd, journal):
            export_review_package(request, parent_fd=fd, name="selected")
            export_review_package(other, parent_fd=fd, name="wrong")
            (root / "incomplete").mkdir(mode=0o700)
            export_review_package(request, parent_fd=fd, name="damaged")
            (root / "damaged" / "request.json").write_text("{}", encoding="utf-8")
            cases = (("missing", request), ("incomplete", request), ("wrong", request),
                     ("selected", other), ("damaged", request),
                     ("selected", DraftRequest("{}")), ("selected", object()))
            for index, (name, expected) in enumerate(cases):
                with self.subTest(name=name, index=index):
                    events = []
                    identity = "invalid:" + str(index)
                    operation = _operation(fd, expected, journal, identity,
                                           _dependencies(events), name=name)
                    with patch.object(journal, "_claim", side_effect=AssertionError("early claim")):
                        result = operation.run()
                    self.assertIs(result.status, SubmissionStatus.REJECTED)
                    self.assertFalse(result.dispatched)
                    self.assertIs(result, operation.outcome)
                    self.assertEqual(events, [])
                    self.assertIsNone(journal.lookup(identity))
                    with self.assertRaises(ValueError):
                        operation.run()

    def test_inspector_rejection_and_unknown_never_claim_the_journal(self):
        request, other = _request(), _request("Other observation")
        observed = TargetObservation(ObservationStatus.OBSERVED,
                                     DraftTarget(**request.data["target"]),
                                     request.snapshot_json, True, True)
        cases = (
            (replace(observed, target=replace(observed.target, head_commit="c" * 40)),
             SubmissionStatus.REJECTED, "target_mismatch"),
            (replace(observed, request_snapshot_json=other.snapshot_json),
             SubmissionStatus.REJECTED, "request_mismatch"),
            (replace(observed, draft_only=False), SubmissionStatus.REJECTED, "unsupported_capability"),
            (replace(observed, atomic_head_base_preconditions=False),
             SubmissionStatus.REJECTED, "unsupported_capability"),
            (replace(observed, draft_only=1), SubmissionStatus.UNKNOWN, "observation_unknown"),
            (replace(observed, status=ObservationStatus.UNKNOWN),
             SubmissionStatus.UNKNOWN, "observation_unknown"),
            (None, SubmissionStatus.UNKNOWN, "observation_unknown"),
            (RuntimeError("inspection failed"), SubmissionStatus.UNKNOWN, "observation_unknown"),
        )
        with _workspace() as (_, fd, journal):
            export_review_package(request, parent_fd=fd, name="selected")
            for index, (reply, status, reason) in enumerate(cases):
                with self.subTest(index=index):
                    events = []
                    inspector, authority, transport = _dependencies(events)

                    def inspect(seen):
                        events.append("inspect")
                        self.assertEqual(seen.snapshot_json, request.snapshot_json)
                        if isinstance(reply, Exception):
                            raise reply
                        return reply

                    inspector.inspect_target = inspect
                    identity = "observe:" + str(index)
                    operation = _operation(fd, request, journal, identity,
                                           (inspector, authority, transport))
                    with patch.object(journal, "_claim", side_effect=AssertionError("early claim")):
                        result = operation.run()
                    self.assertIs(result.status, status)
                    self.assertEqual(result.reason, reason)
                    self.assertFalse(result.dispatched)
                    self.assertEqual(events, ["inspect"])
                    self.assertIsNone(journal.lookup(identity))

    def test_captured_mutation_reentry_and_single_use(self):
        request, other = _request(), _request("Caller mutation")
        snapshot, digest = request.snapshot_json, request.binding_sha256
        events = []
        with _workspace() as (root, fd, journal):
            export_review_package(request, parent_fd=fd, name="selected")
            inspector, authority, transport = _dependencies(events)
            operations = {}

            def inspect(seen):
                events.append("inspect")
                self.assertEqual(seen.snapshot_json, snapshot)
                with self.assertRaises(ValueError):
                    operations["active"].run()
                object.__setattr__(seen, "snapshot_json", other.snapshot_json)
                authority.authorize_and_consume = forbidden
                transport.submit_draft = forbidden
                return TargetObservation(ObservationStatus.OBSERVED,
                                         DraftTarget(**DraftRequest(snapshot).data["target"]),
                                         snapshot, True, True)

            def authorize(seen):
                events.append("authority")
                self.assertEqual(seen.snapshot_json, snapshot)
                object.__setattr__(seen, "snapshot_json", "{}")
                return ApprovalDecision.AUTHORIZED

            def submit(seen):
                events.append("transport")
                self.assertEqual(seen.snapshot_json, snapshot)
                return DraftTransportReply(SubmissionStatus.SUBMITTED, digest, True, 8)

            def forbidden(*args, **kwargs):
                self.fail("looked up a mutated dependency")

            class MutatingInspector:
                def __init__(self):
                    self.method = inspect

                @property
                def inspect_target(self):
                    events.append("inspector_lookup")
                    authority.authorize_and_consume = forbidden
                    transport.submit_draft = forbidden
                    return self.method

                @inspect_target.setter
                def inspect_target(self, method):
                    self.method = method

            inspector = MutatingInspector()
            authority.authorize_and_consume, transport.submit_draft = authorize, submit
            operation = _operation(fd, request, journal, "captured", (inspector, authority, transport))
            self.assertEqual(events, ["inspector_lookup"])
            self.assertIs(authority.authorize_and_consume, forbidden)
            self.assertIs(transport.submit_draft, forbidden)
            operations["active"] = operation
            object.__setattr__(request, "snapshot_json", "{}")
            inspector.inspect_target = forbidden
            authority.authorize_and_consume = forbidden
            transport.submit_draft = forbidden
            result = operation.run()
            self.assertIs(result.status, SubmissionStatus.SUBMITTED)
            self.assertEqual(result.request_sha256, digest)
            self.assertEqual(events, ["inspector_lookup", "inspect", "authority", "transport"])
            with self.assertRaises(ValueError):
                operation.run()
            self.assertEqual(events, ["inspector_lookup", "inspect", "authority", "transport"])

            lookup_events = []

            class LookupTrap:
                @property
                def inspect_target(self):
                    lookup_events.append("inspector_lookup")
                    raise RuntimeError("inspector getter failed")

                @property
                def authorize_and_consume(self):
                    lookup_events.append("authority_lookup")
                    raise AssertionError("invalid input looked up authority")

                @property
                def submit_draft(self):
                    lookup_events.append("transport_lookup")
                    raise AssertionError("invalid input looked up transport")

            trap = LookupTrap()
            _, lookup_authority, lookup_transport = _dependencies(lookup_events)
            lookup_failure = _operation(
                fd, DraftRequest(snapshot), journal, "getter-failure",
                (trap, lookup_authority, lookup_transport),
            )
            self.assertEqual(lookup_events, ["inspector_lookup"])
            with patch.object(journal, "_claim", side_effect=AssertionError("early claim")):
                unknown = lookup_failure.run()
            self.assertIs(unknown.status, SubmissionStatus.UNKNOWN)
            self.assertEqual(unknown.reason, "observation_unknown")
            self.assertFalse(unknown.dispatched)
            self.assertIs(unknown, lookup_failure.outcome)
            self.assertEqual(lookup_events, ["inspector_lookup"])
            self.assertIsNone(journal.lookup("getter-failure"))
            lookup_events.clear()
            invalid = _operation(fd, DraftRequest("{}"), journal, "invalid-getter",
                                 (trap, trap, trap))
            with patch.object(journal, "_claim", side_effect=AssertionError("early claim")):
                rejected = invalid.run()
            self.assertIs(rejected.status, SubmissionStatus.REJECTED)
            self.assertEqual(rejected.reason, "invalid_expected_request")
            self.assertFalse(rejected.dispatched)
            self.assertEqual(lookup_events, [])
            self.assertIsNone(journal.lookup("invalid-getter"))
            # Construction cannot cache a package's validity either.
            late_events = []
            late = _operation(fd, DraftRequest(snapshot), journal, "changed-package",
                              _dependencies(late_events))
            (root / "selected" / "request.json").write_text("{}", encoding="utf-8")
            self.assertIs(late.run().status, SubmissionStatus.REJECTED)
            self.assertEqual(late_events, [])
            self.assertIsNone(journal.lookup("changed-package"))
            with self.assertRaises(ValueError):
                _operation(fd, DraftRequest(snapshot), object(), "bad-journal", _dependencies([]))
            with self.assertRaises(ValueError):
                _operation(fd, DraftRequest(snapshot), journal, "", _dependencies([]))

    def test_durable_duplicate_reopen_authority_denial_and_transport_unknown(self):
        request = _request()
        with _workspace() as (root, fd, journal):
            export_review_package(request, parent_fd=fd, name="selected")
            events = []
            first = _operation(fd, request, journal, "stable", _dependencies(events)).run()
            self.assertIs(first.status, SubmissionStatus.SUBMITTED)
            for index, store in enumerate((journal, SQLiteSubmissionJournal(str(root / "journal.sqlite3")))):
                with self.subTest(replay=index):
                    replay_events = []
                    operation = _operation(fd, request, store, "stable", _dependencies(replay_events))
                    with self.assertRaises(JournalReplayError):
                        operation.run()
                    self.assertEqual(replay_events, ["inspect"])
                    self.assertIs(operation.outcome.status, SubmissionStatus.UNKNOWN)
                    self.assertFalse(operation.outcome.dispatched)
                    self.assertEqual(store.lookup("stable").outcome, first)
                    with self.assertRaises(ValueError):
                        operation.run()
            denied_events = []
            inspector, authority, transport = _dependencies(denied_events)

            def deny(seen):
                denied_events.append("authority")
                return ApprovalDecision.DENIED

            authority.authorize_and_consume = deny
            denied_request = _request("Synthetic authority denial")
            export_review_package(denied_request, parent_fd=fd, name="denied")
            denied = _operation(fd, denied_request, journal, "denied",
                                (inspector, authority, transport), name="denied").run()
            self.assertIs(denied.status, SubmissionStatus.REJECTED)
            self.assertFalse(denied.dispatched)
            self.assertEqual(denied.reason, "approval_denied")
            self.assertEqual(denied_events, ["inspect", "authority"])
            self.assertEqual(journal.lookup("denied").outcome, denied)
            unknown_events = []
            inspector, authority, transport = _dependencies(unknown_events)

            def unknown(seen):
                unknown_events.append("transport")
                return DraftTransportReply(SubmissionStatus.UNKNOWN, seen.binding_sha256, True)

            transport.submit_draft = unknown
            unknown_request = _request("Synthetic transport uncertainty")
            export_review_package(unknown_request, parent_fd=fd, name="unknown")
            uncertain = _operation(fd, unknown_request, journal, "unknown",
                                   (inspector, authority, transport), name="unknown").run()
            self.assertIs(uncertain.status, SubmissionStatus.UNKNOWN)
            self.assertTrue(uncertain.dispatched)
            self.assertEqual(unknown_events, ["inspect", "authority", "transport"])
            self.assertEqual(journal.lookup("unknown").outcome, uncertain)

    def test_terminal_journal_failure_and_cancellation_keep_unknown_and_consumed(self):
        class Cancelled(BaseException):
            pass

        with _workspace() as (root, fd, journal):
            for mode in ("terminal-error", "terminal-cancel", "transport-cancel", "inspect-cancel"):
                with self.subTest(mode=mode):
                    request = _request("Synthetic cancellation: " + mode)
                    export_review_package(request, parent_fd=fd, name=mode)
                    events = []
                    inspector, authority, transport = _dependencies(events)
                    failure = JournalError("terminal persistence failed") if mode == "terminal-error" else Cancelled()
                    advance = journal._advance

                    def persist(*args, **kwargs):
                        if args[3] == "finished" and mode.startswith("terminal-"):
                            raise failure
                        return advance(*args, **kwargs)

                    def cancel_inspect(seen):
                        events.append("inspect")
                        raise failure

                    def cancel_transport(seen):
                        events.append("transport")
                        raise failure

                    if mode == "inspect-cancel":
                        inspector.inspect_target = cancel_inspect
                    if mode == "transport-cancel":
                        transport.submit_draft = cancel_transport
                    operation = _operation(fd, request, journal, mode,
                                           (inspector, authority, transport), name=mode)
                    with patch.object(journal, "_advance", side_effect=persist):
                        with self.assertRaises(type(failure)) as caught:
                            operation.run()
                    self.assertIs(caught.exception, failure)
                    self.assertIs(operation.outcome.status, SubmissionStatus.UNKNOWN)
                    self.assertEqual(operation.outcome.dispatched, mode != "inspect-cancel")
                    expected_events = ["inspect"] if mode == "inspect-cancel" else ["inspect", "authority", "transport"]
                    self.assertEqual(events, expected_events)
                    with self.assertRaises(ValueError):
                        operation.run()
                    self.assertEqual(events, expected_events)
                    reopened = SQLiteSubmissionJournal(str(root / "journal.sqlite3"))
                    entry = reopened.lookup(mode)
                    if mode == "inspect-cancel":
                        self.assertIsNone(entry)
                    else:
                        self.assertIs(operation.outcome, operation._submission.outcome)
                        self.assertEqual(entry.stage, "transport_intent")
                        self.assertEqual(entry.outcome, operation.outcome)
                        replay_events = []
                        replay = _operation(fd, request, reopened, mode,
                                            _dependencies(replay_events), name=mode)
                        with self.assertRaises(JournalReplayError):
                            replay.run()
                        self.assertEqual(replay_events, ["inspect"])

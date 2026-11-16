"""Ten self-contained offline scenarios; all approval/transport evidence is synthetic.

Authoring does not execute these tests. One coordinator-owned targeted invocation.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier, Lock
import unittest
from unittest.mock import patch

from src.agent.core import (
    AttemptResult, CheckEvidence, LoopState as S, ModelReceipt, TestAssessment,
    TestCommand, TestOutcome as O, TestPlan,
)
from src.review.draft_submission import (
    ApprovalDecision, DraftRequest, DraftSubmission, DraftTarget, DraftTransportReply,
    SubmissionStatus as Status, prepare_draft_submission,
)
from src.review.pr_builder import build_review_report, submit_draft_pr
from src.review.submission_journal import JournalError, JournalReplayError, SQLiteSubmissionJournal


def proposal(title="Fix value"):
    def check(phase, command, outcome=O.PASS):
        failed = outcome is O.ASSERTION_FAILED
        return CheckEvidence(phase, command, int(failed), 0.01, "synthetic",
                             TestAssessment(outcome, 1, int(failed), "synthetic assertion"))
    attempt = AttemptResult(
        True, 1, diff="--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n-ONE\n+TWO\n",
        state=S.SUCCEEDED, states=[S.READY, S.SUCCEEDED],
        checks=[check(S.BASELINE, "suite"), check(S.REPRODUCING, "repro", O.ASSERTION_FAILED),
                check(S.VERIFYING_REPRODUCTION, "repro"), check(S.VERIFYING_SUITE, "suite")],
        model_calls=1, tokens_used=3, known_tokens_used=3,
        model_receipts=[ModelReceipt("synthetic", "fixture", 3)], cleanup_confirmed=True,
        sandbox_attempt_id="synthetic-attempt", elapsed_seconds=0.1,
    )
    report = build_review_report(
        attempt, {"repo": "owner/project", "number": 7, "title": "Fix value"},
        base_commit="a" * 40,
        plan=TestPlan(TestCommand("repro", 2), (TestCommand("suite", 2),)),
        execution_kind="synthetic",
    )
    request = prepare_draft_submission(
        report, title=title, target=DraftTarget(
            "https://github.com", "owner/project", "contributor/project",
            "refs/heads/fix", "b" * 40, "refs/heads/main", "a" * 40),
    )
    return report, request


class Authority:
    """Reusable synthetic decision, deliberately without its own deduplication."""
    def __init__(self, callback=None):
        self.callback, self.calls, self.lock = callback, [], Lock()

    def authorize_and_consume(self, request):
        with self.lock:
            self.calls.append(request.snapshot_json)
        return ApprovalDecision.AUTHORIZED if self.callback is None else self.callback(request)


class Transport:
    def __init__(self, callback=None):
        self.callback, self.calls = callback, []

    def submit_draft(self, request):
        self.calls.append(request.snapshot_json)
        return (DraftTransportReply(Status.SUBMITTED, request.binding_sha256, True, 42)
                if self.callback is None else self.callback(request))


class DurableDraftSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report, self.request = proposal()

    def journal(self, name="journal.sqlite"):
        return SQLiteSubmissionJournal(str(self.root / name))

    def operation(self, journal, identity="operation-1", request=None, authority=None, transport=None):
        return DraftSubmission(self.request if request is None else request,
                               authority=Authority() if authority is None else authority,
                               transport=Transport() if transport is None else transport,
                               journal=journal, operation_id=identity)

    def test_real_submit_seam_persists_intents_and_terminal_result(self):
        journal = self.journal()
        def approve(request):
            entry = self.journal().lookup("operation-1")
            self.assertEqual((entry.stage, entry.outcome.status, entry.outcome.dispatched),
                             ("authority_intent", Status.UNKNOWN, False))
            self.assertEqual(entry.snapshot_json, request.snapshot_json)
            return ApprovalDecision.AUTHORIZED
        def send(request):
            entry = self.journal().lookup("operation-1")
            self.assertEqual((entry.stage, entry.outcome.status, entry.outcome.dispatched),
                             ("transport_intent", Status.UNKNOWN, True))
            return DraftTransportReply(Status.SUBMITTED, request.binding_sha256, True, 42)
        authority, transport = Authority(approve), Transport(send)
        result = submit_draft_pr(self.report, self.request.data["body"], request=self.request,
                                 authority=authority, transport=transport,
                                 journal=journal, operation_id="operation-1")
        entry = self.journal().lookup("operation-1")
        self.assertEqual(entry.outcome, result)
        self.assertEqual((entry.stage, result.status, result.pull_request_number),
                         ("finished", Status.SUBMITTED, 42))
        self.assertEqual(authority.calls, [self.request.snapshot_json])
        self.assertEqual(transport.calls, authority.calls)

    def test_duplicate_and_misbound_identity_and_exact_request_are_consumed(self):
        journal, authority, transport = self.journal(), Authority(), Transport()
        operation = self.operation(journal, authority=authority, transport=transport)
        operation.run()
        with self.assertRaises(ValueError):
            operation.run()
        changed = proposal("Changed exact request")[1]
        for identity, request in (("operation-1", self.request), ("new-identity", self.request),
                                  ("operation-1", changed)):
            with self.assertRaises(JournalReplayError):
                self.operation(self.journal(), identity, request, authority, transport).run()
        self.assertEqual((len(authority.calls), len(transport.calls)), (1, 1))
        # An explicitly different identity AND different request is a new operation.
        self.assertEqual(self.operation(journal, "changed-operation", changed).run().status, Status.SUBMITTED)

    def test_all_terminal_results_persist_and_even_rejection_cannot_replay(self):
        cases = [(ApprovalDecision.DENIED, None, Status.REJECTED, False, "approval_denied"),
                 (ApprovalDecision.AUTHORIZED, "reject", Status.REJECTED, True, "confirmed_no_creation"),
                 (ApprovalDecision.AUTHORIZED, "malformed", Status.UNKNOWN, True, "dispatch_outcome_unknown"),
                 (True, None, Status.UNKNOWN, False, "authority_outcome_unknown")]
        for index, (decision, response, status, dispatched, reason) in enumerate(cases):
            with self.subTest(index=index):
                name = f"terminal-{index}.sqlite"
                authority = Authority(lambda r, d=decision: d)
                transport = Transport(lambda r, response=response:
                    DraftTransportReply(Status.REJECTED, r.binding_sha256, True)
                    if response == "reject" else True)
                result = self.operation(self.journal(name), authority=authority, transport=transport).run()
                self.assertEqual((result.status, result.dispatched, result.reason), (status, dispatched, reason))
                entry = self.journal(name).lookup("operation-1")
                self.assertEqual((entry.stage, entry.outcome), ("finished", result))
                with self.assertRaises(JournalReplayError):
                    self.operation(self.journal(name), "fresh-identity", authority=authority, transport=transport).run()
                self.assertEqual(len(authority.calls), 1)
                self.assertEqual(len(transport.calls), int(dispatched))
        journal, authority, transport = self.journal("invalid.sqlite"), Authority(), Transport()
        invalid = DraftRequest("{}")
        result = self.operation(journal, request=invalid, authority=authority, transport=transport).run()
        self.assertEqual((result.status, result.reason), (Status.REJECTED, "invalid_request"))
        self.assertEqual(self.journal("invalid.sqlite").lookup("operation-1").outcome, result)
        with self.assertRaises(JournalReplayError):
            self.operation(journal, "fresh-identity", invalid, authority, transport).run()
        self.assertEqual((authority.calls, transport.calls), ([], []))

    def test_exception_and_baseexception_keep_unknown_and_block_every_replay(self):
        for boundary in ("authority", "transport"):
            for exception in (TimeoutError, KeyboardInterrupt, SystemExit):
                with self.subTest(boundary=boundary, exception=exception):
                    name = f"{boundary}-{exception.__name__}.sqlite"
                    def fail(request):
                        raise exception("synthetic uncertainty")
                    authority = Authority(fail if boundary == "authority" else None)
                    transport = Transport(fail if boundary == "transport" else None)
                    operation = self.operation(self.journal(name), authority=authority, transport=transport)
                    if exception is TimeoutError:
                        self.assertEqual(operation.run().status, Status.UNKNOWN)
                    else:
                        with self.assertRaises(exception):
                            operation.run()
                    entry = self.journal(name).lookup("operation-1")
                    self.assertEqual(entry.outcome, operation.outcome)
                    self.assertEqual(entry.stage, "finished" if exception is TimeoutError else boundary + "_intent")
                    with self.assertRaises(JournalReplayError):
                        self.operation(self.journal(name), "fresh-identity").run()

    def test_process_crash_and_restart_preserve_consumed_unknown(self):
        path = str(self.root / "crash.sqlite")
        script = """
import os, sys
from src.review.draft_submission import DraftRequest, DraftSubmission
from src.review.submission_journal import SQLiteSubmissionJournal
class Authority:
    def authorize_and_consume(self, request):
        os._exit(17)
class Transport:
    def submit_draft(self, request):
        raise AssertionError('must not enter transport')
DraftSubmission(DraftRequest(sys.stdin.read()), authority=Authority(), transport=Transport(),
                journal=SQLiteSubmissionJournal(sys.argv[1]), operation_id='operation-1').run()
"""
        child = subprocess.run([sys.executable, "-c", script, path], input=self.request.snapshot_json,
                               text=True, capture_output=True, timeout=10,
                               cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(child.returncode, 17, child.stderr)
        journal = SQLiteSubmissionJournal(path)
        entry = journal.lookup("operation-1")
        self.assertEqual((entry.stage, entry.outcome.status, entry.outcome.dispatched),
                         ("authority_intent", Status.UNKNOWN, False))
        with self.assertRaises(JournalReplayError):
            self.operation(journal, "fresh-identity").run()

    def test_concurrent_independent_connections_claim_exact_request_once(self):
        journals = [self.journal(), self.journal()]
        barrier, authority, transport = Barrier(2), Authority(), Transport()
        def worker(index):
            barrier.wait(timeout=5)
            try:
                return self.operation(journals[index], f"worker-{index}",
                                      authority=authority, transport=transport).run()
            except JournalReplayError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(worker, range(2)))
        self.assertEqual(outcomes.count("blocked"), 1)
        self.assertEqual(sum(o != "blocked" and o.status is Status.SUBMITTED for o in outcomes), 1)
        self.assertEqual((len(authority.calls), len(transport.calls)), (1, 1))

    def test_persistence_failure_at_each_boundary_and_terminal_never_returns_success(self):
        for phase in (1, 2, 3):
            for committed in (False, True):
                with self.subTest(phase=phase, committed=committed):
                    name = f"failure-{phase}-{committed}.sqlite"
                    journal, authority, transport = self.journal(name), Authority(), Transport()
                    operation = self.operation(journal, authority=authority, transport=transport)
                    original, count = journal._commit, 0
                    def fail_commit(connection):
                        nonlocal count
                        count += 1
                        if count == phase:
                            if committed:
                                original(connection)
                            raise sqlite3.OperationalError("synthetic commit uncertainty")
                        original(connection)
                    with patch.object(journal, "_commit", side_effect=fail_commit):
                        with self.assertRaises(JournalError):
                            operation.run()
                    self.assertEqual(operation.outcome.status, Status.UNKNOWN)
                    self.assertEqual((len(authority.calls), len(transport.calls)),
                                     (int(phase > 1), int(phase > 2)))
                    with self.assertRaises(JournalError):
                        self.operation(journal, "another").run()
                    reopened = self.journal(name)
                    entry = reopened.lookup("operation-1")
                    if phase == 1 and not committed:
                        self.assertIsNone(entry)  # No boundary entered, no committed claim.
                    else:
                        self.assertIsNotNone(entry)
                        with self.assertRaises(JournalReplayError):
                            self.operation(reopened, "fresh-identity").run()
                    if phase == 3:
                        self.assertEqual(entry.outcome.status, Status.SUBMITTED if committed else Status.UNKNOWN)

    def test_unsafe_paths_incompatible_schema_and_stale_storage_fail_closed(self):
        for path in ("", ":memory:", "file::memory:?cache=shared", "relative.sqlite"):
            with self.assertRaises(ValueError):
                SQLiteSubmissionJournal(path)
        empty = self.root / "empty.sqlite"
        empty.touch()
        with self.assertRaises(JournalError):
            SQLiteSubmissionJournal(str(empty))
        journal = self.journal()
        self.operation(journal).run()
        with sqlite3.connect(str(self.root / "journal.sqlite")) as connection:
            connection.execute("PRAGMA user_version=99")
        authority, transport = Authority(), Transport()
        with self.assertRaises(JournalError):
            self.operation(journal, "other", authority=authority, transport=transport).run()
        with self.assertRaises(JournalError):
            self.journal()
        stale = self.journal("stale.sqlite")
        (self.root / "stale.sqlite").rename(self.root / "preserved.sqlite")
        self.journal("stale.sqlite")
        with self.assertRaises(JournalError):
            self.operation(stale, authority=authority, transport=transport).run()
        self.assertEqual((authority.calls, transport.calls), ([], []))

    def test_opt_in_pair_is_required_and_legacy_behavior_is_retained(self):
        authority, transport = Authority(), Transport()
        with self.assertRaises(NotImplementedError):
            submit_draft_pr(self.report, self.request.data["body"])
        for options in ({"journal": self.journal()}, {"operation_id": "operation-1"},
                        {"journal": self.journal(), "operation_id": ""},
                        {"journal": object(), "operation_id": "operation-1"}):
            with self.assertRaises(ValueError):
                DraftSubmission(self.request, authority=authority, transport=transport, **options)
        for _ in range(2):
            self.assertEqual(DraftSubmission(self.request, authority=authority, transport=transport).run().status,
                             Status.SUBMITTED)
        self.assertEqual((len(authority.calls), len(transport.calls)), (2, 2))

    def test_detached_request_and_captured_boundary_bindings_and_seam_mismatch(self):
        journal, transport = self.journal(), Transport()
        snapshot = self.request.snapshot_json
        def approve(request):
            object.__setattr__(request, "snapshot_json", "changed boundary copy")
            object.__setattr__(self.request, "snapshot_json", "changed caller copy")
            transport.submit_draft = lambda r: self.fail("late replacement must not be called")
            return ApprovalDecision.AUTHORIZED
        authority = Authority(approve)
        with self.assertRaises(ValueError):
            submit_draft_pr(self.report, self.request.data["body"] + "changed", request=self.request,
                            authority=authority, transport=transport, journal=journal, operation_id="operation-1")
        self.assertIsNone(journal.lookup("operation-1"))
        operation = self.operation(journal, authority=authority, transport=transport)
        self.assertEqual(operation.run().status, Status.SUBMITTED)
        self.assertEqual(transport.calls, [snapshot])
        self.assertEqual(journal.lookup("operation-1").snapshot_json, snapshot)


if __name__ == "__main__":
    unittest.main()

"""Ten offline unittest scenarios. All authority/transport/model evidence is synthetic.

Self-contained fixtures; no historical tests, external services or model backend.
"""
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import shutil
import tempfile
from threading import Lock
import unittest

from src.agent.candidate_attempt import CandidateAttempt, CandidateAttemptOutcome
from src.agent.core import (
    AttemptResult, CheckEvidence, LoopState as S, ModelReceipt, ModelReply,
    TestAssessment, TestCommand, TestOutcome as O, TestPlan,
)
from src.agent.filesystem_staging import ManifestFile
from src.agent.snapshot_workspace import SnapshotWorkspace, TextFile
from src.agent.staged_attempt import StagedAttemptAdapter
from src.agent.staged_runner_bridge import RunnerEvidence
from src.collector.digest import CandidatePoolEntry
from src.review.draft_submission import (
    MAX_REQUEST_BYTES, ApprovalDecision, DraftRequest, DraftSubmission, DraftTarget,
    DraftTransportReply, SubmissionStatus as Status, prepare_draft_submission,
)
from src.review.pr_builder import ReviewReport, build_review_report, submit_draft_pr
from src.sandbox.docker_manager import CommandResult, SandboxPolicy

BASE = "a" * 40
PLAN = TestPlan(TestCommand("reproduce", 2), (TestCommand("suite", 2),))
ISSUE = {"repo": "owner/project", "number": 7, "title": "Fix value"}
PATCH = "--- a/value.py\n+++ b/value.py\n@@ -1,1 +1,1 @@\n-VALUE = 1\n+VALUE = 2\n"
TARGET = DraftTarget("https://github.com", "owner/project", "contributor/project",
                     "refs/heads/fix", "b" * 40, "refs/heads/main", BASE)


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def successful_attempt():
    def check(phase, command, outcome=O.PASS):
        failed = outcome is O.ASSERTION_FAILED
        return CheckEvidence(phase, command, int(failed), 0.01, "synthetic tail",
                             TestAssessment(outcome, 1, int(failed), "synthetic assertion"))
    return AttemptResult(
        True, 1, diff=PATCH, state=S.SUCCEEDED, states=[S.READY, S.SUCCEEDED],
        checks=[check(S.BASELINE, "suite"), check(S.REPRODUCING, "reproduce", O.ASSERTION_FAILED),
                check(S.VERIFYING_REPRODUCTION, "reproduce"), check(S.VERIFYING_SUITE, "suite")],
        model_calls=1, tokens_used=3, known_tokens_used=3,
        model_receipts=[ModelReceipt("synthetic", "fixture", 3)], cleanup_confirmed=True,
        sandbox_attempt_id="synthetic-attempt", elapsed_seconds=0.1,
    )


def report(attempt=None, base=BASE):
    return build_review_report(successful_attempt() if attempt is None else attempt,
                               ISSUE, base_commit=base, plan=PLAN, execution_kind="synthetic")


def prepare(source=None, target=TARGET, title="Fix value", template=None):
    return prepare_draft_submission(report() if source is None else source,
                                    target=target, title=title, pr_template=template)


class SyntheticAuthority:
    """In-memory simulation of external approval of exact bytes, NOT authentication."""

    def __init__(self, approved, callback=None):
        self.approved = approved.snapshot_json
        self.consumed = False
        self.calls = []
        self.callback = callback
        self.lock = Lock()

    def authorize_and_consume(self, request):
        with self.lock:
            self.calls.append(request.snapshot_json)
            if self.consumed or request.snapshot_json != self.approved:
                return ApprovalDecision.DENIED
            self.consumed = True
        if self.callback:
            return self.callback(request)
        return ApprovalDecision.AUTHORIZED


class SyntheticTransport:
    def __init__(self, reply=None):
        self.reply, self.calls = reply, []

    def submit_draft(self, request):
        self.calls.append(request.snapshot_json)
        if self.reply is not None:
            return self.reply(request)
        return DraftTransportReply(Status.SUBMITTED, request.binding_sha256, True, 42)


class SyntheticStage:
    """Synthetic lifecycle, runner, assessor and proposer over real staged files."""

    def __init__(self, parent, fail_cleanup):
        self.parent, self.fail_cleanup = parent, fail_cleanup
        self.results, self.events = {}, []

    def create(self, attempt_id, **bounds):
        self.events.append("create")

    def start(self, attempt_id, **bounds):
        self.events.append("start")

    def remove(self, attempt_id, **bounds):
        self.events.append("remove")
        if self.fail_cleanup:
            raise OSError("synthetic cleanup uncertainty")
        for path in self.parent.iterdir():
            shutil.rmtree(path)

    def run_staged(self, ticket, **bounds):
        text = (self.parent / ticket.receipt.stage_name / "value.py").read_text()
        passed = ticket.command == "suite" or text == "VALUE = 2\n"
        result = CommandResult(ticket.command, 0 if passed else 1, stdout="synthetic")
        self.results[id(result)] = (ticket.attempt_id, result, passed)
        return RunnerEvidence(ticket.attempt_id, ticket.dispatch_id, ticket.receipt.stage_name,
                              ticket.receipt_sha256, result)

    def assess(self, attempt_id, result, **bounds):
        identity, original, passed = self.results[id(result)]
        assert identity == attempt_id and original is result
        return TestAssessment(O.PASS if passed else O.ASSERTION_FAILED, 1,
                              0 if passed else 1, "synthetic byte assertion")

    def propose(self, request, **bounds):
        self.events.append("propose")
        return ModelReply(PATCH, 3, "synthetic", "fixture")


def candidate_outcome(case, fail_cleanup=False):
    root = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, root, True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    case.addCleanup(os.close, fd)
    synthetic = SyntheticStage(root, fail_cleanup)

    def workspace(attempt_id):
        return SnapshotWorkspace(attempt_id, (TextFile("value.py", "VALUE = 1\n", "source"),),
                                 editable_paths=("value.py",), context_paths=("value.py",),
                                 assessor=synthetic)

    adapter = StagedAttemptAdapter(
        lifecycle=synthetic, workspace_factory=workspace, staging_parent_fd=fd,
        manifest=(ManifestFile("value.py", "source"),), runner=synthetic, max_output_bytes=4096)
    outcome = CandidateAttempt(
        CandidatePoolEntry("owner/project", 7, "Fix value", "https://github.com/owner/project/issues/7", 1, 0),
        base_commit=BASE, plan=PLAN, adapter=adapter, model=synthetic,
        sandbox_policy=SandboxPolicy("synthetic:test", 1, 256, max_output_bytes=4096),
        caller_declared_status="collected", execution_kind="synthetic", max_attempts=1,
        token_budget=20, timeout_seconds=30, max_model_calls=1, max_patch_bytes=4096,
    ).run()
    return outcome, synthetic


class DraftSubmissionTests(unittest.TestCase):
    def test_candidate_to_exact_approved_draft_integration(self):
        outcome, stage = candidate_outcome(self)
        self.assertEqual(outcome.status, "pending_human_review")
        request = prepare(outcome)
        authority, transport = SyntheticAuthority(request), SyntheticTransport()
        result = submit_draft_pr(outcome.report, request.data["body"], request=request,
                                 authority=authority, transport=transport)
        self.assertEqual((result.status, result.pull_request_number), (Status.SUBMITTED, 42))
        self.assertTrue(result.dispatched and authority.consumed)
        self.assertEqual(authority.calls, transport.calls)
        self.assertEqual(transport.calls, [request.snapshot_json])
        self.assertEqual(request.data["target"]["head_commit"], "b" * 40)
        self.assertEqual(request.data["report_json"], outcome.report.snapshot_json)
        self.assertEqual(stage.events, ["create", "start", "propose", "remove"])
        self.assertFalse(outcome.submitted)  # historical candidate artifact

    def test_legacy_missing_boundaries_and_confirmed_transport_rejection(self):
        source, request = report(), prepare()
        authority, transport = SyntheticAuthority(request), SyntheticTransport(
            lambda r: DraftTransportReply(Status.REJECTED, r.binding_sha256, True))
        with self.assertRaises(NotImplementedError):
            submit_draft_pr(source, request.data["body"])
        with self.assertRaises(ValueError):
            submit_draft_pr(source, request.data["body"], request=request, authority=authority)
        self.assertEqual(authority.calls, [])
        result = DraftSubmission(request, authority=authority, transport=transport).run()
        self.assertEqual((result.status, result.dispatched, result.reason),
                         (Status.REJECTED, True, "confirmed_no_creation"))

    def test_denial_and_exact_body_report_target_base_binding(self):
        request = prepare()
        authority, transport = SyntheticAuthority(request), SyntheticTransport()
        with self.assertRaises(ValueError):
            submit_draft_pr(report(), request.data["body"] + "changed", request=request,
                            authority=authority, transport=transport)
        with self.assertRaises(ValueError):
            submit_draft_pr(report(base="c" * 40), request.data["body"], request=request,
                            authority=authority, transport=transport)
        for target in (replace(TARGET, repository="other/project"),
                       replace(TARGET, base_commit="c" * 40),
                       replace(TARGET, destination="https://other.example")):
            with self.assertRaises(ValueError):
                prepare(target=target)
        # Structurally valid replacements still require approval of their exact bytes.
        changed = [prepare(title="Changed"), prepare(target=replace(TARGET, head_ref="refs/heads/other")),
                   prepare(target=replace(TARGET, head_repository="other/fork")),
                   prepare(target=replace(TARGET, head_commit="d" * 40)),
                   prepare(target=replace(TARGET, base_ref="refs/heads/other")),
                   prepare(template="changed template"),
                   prepare(report(base="c" * 40), target=replace(TARGET, base_commit="c" * 40))]
        for replacement in changed:
            result = DraftSubmission(replacement, authority=authority, transport=transport).run()
            self.assertEqual(result.status, Status.REJECTED)
        self.assertEqual(transport.calls, [])
        self.assertFalse(authority.consumed)

    def test_unknown_and_transport_exceptions_never_retry(self):
        def failure(request):
            raise TimeoutError("may have created draft")
        def cancellation(request):
            raise KeyboardInterrupt()
        for reply in (lambda r: DraftTransportReply(Status.UNKNOWN, r.binding_sha256, True),
                      failure, cancellation):
            request = prepare()
            authority, transport = SyntheticAuthority(request), SyntheticTransport(reply)
            operation = DraftSubmission(request, authority=authority, transport=transport)
            try:
                result = operation.run()
            except KeyboardInterrupt:
                result = operation.outcome
            self.assertEqual((result.status, result.dispatched), (Status.UNKNOWN, True))
            with self.assertRaises(ValueError):
                operation.run()
            self.assertEqual(len(transport.calls), 1)
            replay = DraftSubmission(request, authority=authority, transport=transport).run()
            self.assertEqual(replay.status, Status.REJECTED)
            self.assertEqual(len(transport.calls), 1)

    def test_single_use_reentry_and_authority_replay_consumption(self):
        request = prepare()
        def reenter(value):
            with self.assertRaises(ValueError):
                operation.run()
            return ApprovalDecision.AUTHORIZED
        authority, transport = SyntheticAuthority(request, reenter), SyntheticTransport()
        operation = DraftSubmission(request, authority=authority, transport=transport)
        self.assertEqual(operation.run().status, Status.SUBMITTED)
        with self.assertRaises(ValueError):
            operation.run()
        self.assertEqual(DraftSubmission(request, authority=authority, transport=transport).run().status,
                         Status.REJECTED)
        self.assertEqual(len(transport.calls), 1)

    def test_malformed_or_misbound_transport_replies_are_unknown(self):
        request = prepare()
        malformed = [True, {"status": "submitted"},
                     DraftTransportReply(Status.SUBMITTED, request.binding_sha256, True),
                     DraftTransportReply(Status.SUBMITTED, "0" * 64, True, 42),
                     DraftTransportReply(Status.SUBMITTED, request.binding_sha256, False, 42),
                     DraftTransportReply(Status.SUBMITTED, request.binding_sha256, True, True),
                     DraftTransportReply("submitted", request.binding_sha256, True, 42),
                     DraftTransportReply(Status.REJECTED, request.binding_sha256, True, 42)]
        for reply in malformed:
            transport = SyntheticTransport(lambda r, reply=reply: reply)
            operation = DraftSubmission(request, authority=SyntheticAuthority(request), transport=transport)
            self.assertEqual(operation.run().status, Status.UNKNOWN)
            with self.assertRaises(ValueError):
                operation.run()
            self.assertEqual(len(transport.calls), 1)

    def test_failed_unknown_and_malformed_evidence_cannot_prepare(self):
        failed = report(AttemptResult(False, 0, state=S.TESTS_FAILED))
        unknown = report(AttemptResult(False, 0, state=S.UNKNOWN_OUTCOME))
        forged = json.loads(report().snapshot_json)
        forged["attempt"]["checks"][-1]["assessment"]["outcome"] = "unknown"
        for source in (failed, unknown, ReviewReport("{}"), ReviewReport("not json"),
                       ReviewReport(canonical(forged))):
            with self.assertRaises(ValueError):
                prepare(source)
        request = prepare()
        data = request.data
        data["report_json"] = unknown.snapshot_json
        authority, transport = SyntheticAuthority(request), SyntheticTransport()
        operation = DraftSubmission(DraftRequest(canonical(data)), authority=authority, transport=transport)
        self.assertEqual(operation.run().status, Status.REJECTED)
        self.assertEqual((authority.calls, transport.calls), ([], []))
        with self.assertRaises(ValueError):
            operation.run()

    def test_cleanup_failure_and_stale_candidate_attempt_reject(self):
        outcome, stage = candidate_outcome(self, fail_cleanup=True)
        self.assertFalse(outcome.attempt.cleanup_confirmed)
        self.assertEqual(stage.events.count("remove"), 1)
        with self.assertRaises(ValueError):
            prepare(outcome)
        attempt = successful_attempt()
        outcome = CandidateAttemptOutcome("pending_human_review", attempt, report(attempt), "collected")
        attempt.cleanup_confirmed = False
        with self.assertRaises(ValueError):
            prepare(outcome)
        forged = json.loads(report().snapshot_json)
        forged["attempt"]["cleanup_confirmed"] = False
        with self.assertRaises(ValueError):
            prepare(ReviewReport(canonical(forged)))

    def test_bounded_immutable_request_and_separate_boundary_values(self):
        request = prepare()
        with self.assertRaises(FrozenInstanceError):
            request.snapshot_json = "changed"
        data = request.data
        data["target"]["head_ref"] = "refs/heads/changed"
        self.assertEqual(request.data["target"]["head_ref"], TARGET.head_ref)
        original = request.snapshot_json
        def mutate_boundary_copy(value):
            object.__setattr__(value, "snapshot_json", "changed boundary copy")
            return ApprovalDecision.AUTHORIZED
        authority, transport = SyntheticAuthority(request, mutate_boundary_copy), SyntheticTransport()
        operation = DraftSubmission(request, authority=authority, transport=transport)
        object.__setattr__(request, "snapshot_json", "caller changed after capture")
        self.assertEqual(operation.run().status, Status.SUBMITTED)
        self.assertEqual(transport.calls, [original])
        with self.assertRaises(ValueError):
            prepare(title="x" * 257)
        with self.assertRaises(ValueError):
            DraftSubmission(DraftRequest("x" * (MAX_REQUEST_BYTES + 1)),
                            authority=authority, transport=transport)
        tampered = json.loads(original)
        tampered["body"] += "changed"
        bad = DraftSubmission(DraftRequest(canonical(tampered)), authority=authority, transport=transport)
        self.assertEqual(bad.run().status, Status.REJECTED)
        tampered = json.loads(original)
        tampered["draft"] = False
        bad = DraftSubmission(DraftRequest(canonical(tampered)), authority=authority, transport=transport)
        self.assertEqual(bad.run().status, Status.REJECTED)
        self.assertEqual(len(transport.calls), 1)

    def test_authority_ambiguity_or_public_assertions_never_dispatch(self):
        request = prepare()
        def failure(value):
            raise OSError("consumption may have persisted")
        for callback in (failure, lambda r: True, lambda r: r.binding_sha256, lambda r: object()):
            authority, transport = SyntheticAuthority(request, callback), SyntheticTransport()
            operation = DraftSubmission(request, authority=authority, transport=transport)
            result = operation.run()
            self.assertEqual((result.status, result.dispatched), (Status.UNKNOWN, False))
            self.assertEqual(transport.calls, [])
            with self.assertRaises(ValueError):
                operation.run()


if __name__ == "__main__":
    unittest.main()

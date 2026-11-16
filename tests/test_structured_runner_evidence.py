"""Nine self-contained synthetic scenarios; fixtures only compare/hash bytes.

The real candidate/loop/attempt/bridge/snapshot/report path is used. No fixture
source is imported or executed, and no old test module is imported.
"""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from src.agent.candidate_attempt import CandidateAttempt
from src.agent.core import LoopState, ModelReply, TestCommand, TestOutcome, TestPlan
from src.agent.filesystem_staging import ManifestFile
from src.agent.snapshot_workspace import SnapshotError, SnapshotWorkspace, TextFile
from src.agent.staged_attempt import StagedAttemptAdapter
from src.agent.staged_runner_bridge import DispatchError, EvidenceRejected, receipt_digest
from src.agent.structured_runner_evidence import (
    AssertionFailure, StructuredRun, StructuredRunnerEvidenceAdapter, StructuredTestSummary,
)
from src.collector.digest import CandidatePoolEntry
from src.sandbox.docker_manager import CommandResult, SandboxPolicy

ORIGINAL = 'ANSWER = 1\n'
FIXED = 'ANSWER = 2\n'
PATCH = '--- a/answer.py\n+++ b/answer.py\n@@ -1,1 +1,1 @@\n-ANSWER = 1\n+ANSWER = 2\n'
FAILURE = AssertionFailure('expected.txt::answer_bytes', 'actual bytes differ from expected bytes')
PASS = StructuredTestSummary(1, 1, (), 0, 0, 0, 0, True, False, True)
FAIL = replace(PASS, passed=0, assertions=(FAILURE,))
POLICY = SandboxPolicy('synthetic:test', 1, 256, max_output_bytes=4096)
PLAN = TestPlan(TestCommand('reproduce', 5), (TestCommand('suite', 5),))


class ByteBackend:
    """Explicitly synthetic: reads staged bytes, returns declared structured events."""

    def __init__(self, parent, transform=None):
        self.parent, self.transform = parent, transform
        self.calls, self.replies, self.bounds = [], [], []

    def run_structured(self, ticket, *, timeout_seconds, max_output_bytes):
        self.calls.append(ticket)
        self.bounds.append((timeout_seconds, max_output_bytes))
        assert ticket.receipt_sha256 == receipt_digest(ticket.receipt)
        stage = self.parent / ticket.receipt.stage_name
        for item in ticket.receipt.files:
            data = (stage / item.path).read_bytes()
            assert len(data) == item.size_bytes and sha256(data).hexdigest() == item.sha256
        passed = (ticket.command == 'suite'
                  or (stage / 'answer.py').read_bytes() == (stage / 'expected.txt').read_bytes())
        reply = StructuredRun(ticket, CommandResult(ticket.command, 0 if passed else 1,
                                                   'synthetic bytes only', '', 0.001),
                              PASS if passed else FAIL)
        if self.transform:
            reply = self.transform(reply)
        self.replies.append(reply)
        return reply


class Lifecycle:
    def __init__(self, parent):
        self.parent, self.events = parent, []

    def create(self, attempt_id, *, policy, timeout_seconds):
        self.events.append(('create', attempt_id))

    def start(self, attempt_id, *, timeout_seconds):
        self.events.append(('start', attempt_id))

    def remove(self, attempt_id, *, timeout_seconds):
        self.events.append(('remove', attempt_id))
        for stage in self.parent.iterdir():
            shutil.rmtree(stage)


class Model:
    def __init__(self):
        self.calls = 0

    def propose(self, request, **bounds):
        self.calls += 1
        assert ORIGINAL in request.context and FAILURE.test_id in request.failing_log_tail
        return ModelReply(PATCH, 7, 'synthetic', 'fixed-reply')


class Harness:
    def __init__(self, case, transform=None, max_dispatches=32):
        self.parent = Path(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.parent, True)
        self.fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        case.addCleanup(os.close, self.fd)
        self.backend = ByteBackend(self.parent, transform)
        self.evidence = StructuredRunnerEvidenceAdapter(backend=self.backend,
                                                       max_dispatches=max_dispatches)
        self.lifecycle, self.model = Lifecycle(self.parent), Model()

        def factory(attempt_id):
            self.workspace = SnapshotWorkspace(
                attempt_id, (TextFile('answer.py', ORIGINAL, 'source'),
                             TextFile('expected.txt', FIXED, 'test')),
                editable_paths=('answer.py',), context_paths=('answer.py',),
                assessor=self.evidence,
            )
            return self.workspace

        self.adapter = StagedAttemptAdapter(
            lifecycle=self.lifecycle, workspace_factory=factory, staging_parent_fd=self.fd,
            manifest=(ManifestFile('answer.py', 'source'), ManifestFile('expected.txt', 'test')),
            runner=self.evidence, max_output_bytes=4096,
        )
        # Close the bridge even when a manual scenario rejects an assessment.
        case.addCleanup(self.close)

    def close(self):
        if self.adapter.state in ('created', 'running', 'execute_failed'):
            self.adapter.remove('manual', timeout_seconds=5)

    def start(self):
        self.adapter.create('manual', policy=POLICY, timeout_seconds=5)
        self.adapter.start('manual', timeout_seconds=5)

    def execute(self, command='suite'):
        return self.adapter.execute('manual', command, timeout_seconds=5, max_output_bytes=4096)

    def assess(self, result, **over):
        bounds = dict(timeout_seconds=5, max_bytes=4096)
        bounds.update(over)
        return self.adapter.assess('manual', result, **bounds)

    def candidate(self):
        return CandidateAttempt(
            CandidatePoolEntry('owner/project', 7, 'wrong answer',
                               'https://github.com/owner/project/issues/7', None, 1),
            base_commit='a' * 40, plan=PLAN, adapter=self.adapter, model=self.model,
            sandbox_policy=POLICY, caller_declared_status='collected', execution_kind='synthetic',
            max_attempts=1, token_budget=100, max_model_calls=1, timeout_seconds=30,
            max_text_bytes=4096, max_patch_bytes=4096,
        )


class StructuredRunnerEvidenceTests(unittest.TestCase):
    def test_complete_candidate_reproduction_fix_verify_and_report(self):
        env = Harness(self)
        outcome = env.candidate().run()
        self.assertEqual(outcome.status, 'pending_human_review')
        self.assertTrue(outcome.attempt.success and outcome.attempt.cleanup_confirmed)
        self.assertEqual(outcome.attempt.diff, PATCH)
        self.assertEqual([c.assessment.outcome for c in outcome.attempt.checks],
                         [TestOutcome.PASS, TestOutcome.ASSERTION_FAILED,
                          TestOutcome.PASS, TestOutcome.PASS])
        tickets = env.backend.calls
        self.assertEqual([t.generation for t in tickets], [1, 2, 3, 4])
        self.assertEqual([t.command for t in tickets], ['suite', 'reproduce', 'reproduce', 'suite'])
        self.assertEqual(len({t.dispatch_id for t in tickets}), 4)
        self.assertEqual(len({t.receipt.stage_name for t in tickets}), 4)
        self.assertNotEqual(tickets[1].receipt.files, tickets[2].receipt.files)
        self.assertTrue(all(t.attempt_id == outcome.attempt.sandbox_attempt_id for t in tickets))
        self.assertTrue(all(0 < seconds <= 5 and cap == 4096 for seconds, cap in env.backend.bounds))
        self.assertEqual(env.model.calls, 1)
        self.assertEqual([event for event, _ in env.lifecycle.events], ['create', 'start', 'remove'])
        self.assertEqual(list(env.parent.iterdir()), [])
        self.assertEqual(outcome.report.evidence['execution_kind'], 'synthetic')
        self.assertEqual(outcome.report.diff_preview, PATCH)
        self.assertEqual(outcome.human_approval, 'pending')
        self.assertFalse(outcome.submitted)

    def test_mixed_collection_infrastructure_and_test_errors_reject_candidate(self):
        summary = replace(FAIL, tests_run=2, test_errors=1, collection_errors=1,
                          infrastructure_errors=1)
        env = Harness(self, lambda reply: replace(reply, summary=summary))
        outcome = env.candidate().run()
        self.assertFalse(outcome.attempt.success)
        assessment = outcome.attempt.checks[0].assessment
        self.assertEqual(assessment.outcome, TestOutcome.INFRA_ERROR)
        self.assertEqual((assessment.tests_run, assessment.assertion_failures), (2, 1))
        self.assertIn(FAILURE.test_id, assessment.detail)
        self.assertEqual(env.model.calls, 0)
        self.assertEqual(len(env.backend.calls), 1)

    def test_executed_tests_with_skips_vs_empty_and_all_skipped(self):
        env = Harness(self, lambda reply: replace(reply, summary=replace(PASS, skipped=2)))
        env.start()
        self.assertEqual(env.assess(env.execute()).outcome, TestOutcome.PASS)
        env.backend.transform = lambda reply: replace(reply, summary=replace(PASS, tests_run=0,
                                                                            passed=0, skipped=2))
        verdict = env.assess(env.execute())
        self.assertEqual((verdict.outcome, verdict.tests_run), (TestOutcome.UNKNOWN, 0))
        env.backend.transform = lambda reply: replace(reply, summary=replace(PASS, tests_run=0,
                                                                            passed=0))
        self.assertEqual(env.assess(env.execute()).outcome, TestOutcome.UNKNOWN)

    def test_incomplete_truncated_unknown_and_contradictory_exit_are_unknown(self):
        env = Harness(self)
        env.start()
        variants = [(replace(PASS, complete=False), 0), (replace(FAIL, truncated=True), 1),
                    (replace(PASS, outcome_known=False), 0), (FAIL, 0), (PASS, 1), (FAIL, -9)]
        for summary, exit_code in variants:
            env.backend.transform = lambda reply, s=summary, e=exit_code: replace(
                reply, summary=s, result=CommandResult(reply.ticket.command, e,
                                                       'AssertionError: misleading stdout'))
            self.assertEqual(env.assess(env.execute()).outcome, TestOutcome.UNKNOWN)

    def test_summary_types_counts_identities_and_detail_bounds_fail_closed(self):
        variants = [replace(PASS, tests_run=True), replace(PASS, complete=1),
                    replace(PASS, passed=2), replace(PASS, skipped=-1),
                    replace(PASS, skipped=1_000_001),
                    replace(FAIL, tests_run=2, assertions=(FAILURE, FAILURE)),
                    replace(FAIL, assertions=(replace(FAILURE, detail=''),)),
                    replace(FAIL, assertions=(replace(FAILURE, detail='x' * 1025),)),
                    replace(FAIL, assertions=(FAILURE,) * 129)]
        for summary in variants:
            env = Harness(self, lambda reply, s=summary: replace(reply, summary=s))
            env.start()
            with self.assertRaises(DispatchError):
                env.execute()
            self.assertEqual(len(env.backend.calls), 1)
            with self.assertRaises(Exception):
                env.execute()
            self.assertEqual(len(env.backend.calls), 1)

    def test_full_echo_attempt_generation_receipt_command_and_digest_mismatches(self):
        changes = [lambda t: replace(t, attempt_id='other'),
                   lambda t: replace(t, dispatch_id='other'),
                   lambda t: replace(t, generation=t.generation + 1),
                   lambda t: replace(t, generation=True),
                   lambda t: replace(t, command='other'),
                   lambda t: replace(t, receipt_sha256='0' * 64),
                   lambda t: replace(t, receipt=replace(t.receipt, stage_name='other')),
                   lambda t: replace(t, receipt=replace(t.receipt, files=(replace(
                       t.receipt.files[0], sha256='0' * 64),) + t.receipt.files[1:]))]
        for change in changes:
            env = Harness(self, lambda reply, change=change: replace(reply, ticket=change(reply.ticket)))
            env.start()
            with self.assertRaises(DispatchError):
                env.execute()
            self.assertEqual(len(env.backend.calls), 1)

    def test_exact_result_shape_bounds_and_backend_result_reuse(self):
        for fields in ({'command': 'wrong'}, {'exit_code': True}, {'duration_seconds': float('nan')},
                       {'duration_seconds': 5}, {'stdout': 'x' * 4097}, {'stderr': b'bytes'}):
            env = Harness(self, lambda reply, f=fields: replace(reply, result=replace(reply.result, **f)))
            env.start()
            with self.assertRaises(DispatchError):
                env.execute()
        env = Harness(self)
        env.start()
        first = env.execute()
        env.assess(first)
        env.backend.transform = lambda reply: replace(reply, result=first)
        with self.assertRaises(DispatchError):
            env.execute()
        self.assertEqual(len(env.backend.calls), 2)

    def test_stale_copy_wrong_attempt_mutation_replay_and_snapshot_change(self):
        env = Harness(self)
        env.start()
        first = env.execute()
        second = env.execute()
        for method, args in ((env.assess, (first,)), (env.assess, (replace(second),)),
                             (env.evidence.assess, ('other', second))):
            with self.assertRaises(EvidenceRejected):
                method(*args, **({'timeout_seconds': 5, 'max_bytes': 4096}
                                if method == env.evidence.assess else {}))
        self.assertEqual(env.assess(second).outcome, TestOutcome.PASS)
        with self.assertRaises(EvidenceRejected):
            env.assess(second)
        with self.assertRaises(EvidenceRejected):
            env.evidence.assess('manual', first, timeout_seconds=5, max_bytes=4096)
        third = env.execute()
        third.exit_code = False  # equal to zero, but a different type
        with self.assertRaises(EvidenceRejected):
            env.assess(third)
        fourth = env.execute()
        fourth.stdout += ' changed'
        with self.assertRaises(EvidenceRejected):
            env.assess(fourth)
        fifth = env.execute()
        self.assertTrue(env.adapter.apply_patch('manual', PATCH, timeout_seconds=5, max_bytes=4096))
        with self.assertRaises(EvidenceRejected):
            env.assess(fifth)
        self.assertEqual(env.assess(env.execute()).outcome, TestOutcome.PASS)

    def test_uncertain_call_is_never_retried_and_assessment_bound_is_single_use(self):
        def uncertain(reply):
            raise TimeoutError('synthetic uncertain invocation')

        env = Harness(self, uncertain)
        outcome = env.candidate().run()
        self.assertFalse(outcome.attempt.success)
        self.assertEqual(len(env.backend.calls), 1)
        self.assertEqual(env.model.calls, 0)
        with self.assertRaises(EvidenceRejected):
            env.evidence.run_staged(env.backend.calls[0], timeout_seconds=5, max_output_bytes=4096)
        bounded = Harness(self, max_dispatches=1)
        bounded.start()
        result = bounded.execute()
        with self.assertRaises(SnapshotError):
            bounded.assess(result, max_bytes=1)
        with self.assertRaises(EvidenceRejected):
            bounded.assess(result)
        with self.assertRaises(DispatchError):
            bounded.execute()
        self.assertEqual(len(bounded.backend.calls), 1)


if __name__ == '__main__':
    unittest.main()

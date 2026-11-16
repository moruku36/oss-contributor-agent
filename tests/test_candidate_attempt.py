'''Nine synthetic scenarios. Real CoderLoop, StagedAttemptAdapter, SnapshotWorkspace,
staging and review builder; the lifecycle, runner and model are SYNTHETIC test doubles.
Nothing here establishes backend isolation, acquisition or model behavior.'''
from hashlib import sha256
import dataclasses
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

import src.agent.candidate_attempt as module
from src.collector.digest import CandidatePoolEntry
from src.agent.candidate_attempt import CandidateAttempt, CandidateAttemptError
from src.agent.core import LoopState, ModelReply, TestAssessment, TestCommand, TestOutcome, TestPlan
from src.agent.filesystem_staging import ManifestFile
from src.agent.snapshot_workspace import SnapshotWorkspace, TextFile
from src.agent.staged_attempt import StagedAttemptAdapter
from src.agent.staged_runner_bridge import RunnerEvidence, receipt_digest
from src.review.pr_builder import ReviewReport, submit_draft_pr
from src.sandbox.docker_manager import CommandResult, SandboxPolicy

ORIGINAL = 'ANSWER = 1\n'
FIXED = 'ANSWER = 2\n'
PATCH = '--- a/answer.py\n+++ b/answer.py\n@@ -1,1 +1,1 @@\n-ANSWER = 1\n+ANSWER = 2\n'
BASE = 'a' * 40
MANIFEST = (ManifestFile('answer.py', 'source'), ManifestFile('expected.txt', 'test'))
POLICY = SandboxPolicy('synthetic:test', 1, 256, max_output_bytes=4096)
PLAN = TestPlan(TestCommand('reproduce', 5), (TestCommand('suite', 5),))


class Lifecycle:
    def __init__(self, parent, events, fail_remove):
        self.parent, self.events, self.fail_remove = parent, events, fail_remove

    def create(self, attempt_id, *, policy, timeout_seconds):
        self.events.append('create')

    def start(self, attempt_id, *, timeout_seconds):
        self.events.append('start')

    def remove(self, attempt_id, *, timeout_seconds):
        self.events.append('remove')
        if self.fail_remove:
            raise OSError('synthetic removal unknown')
        for stage in self.parent.iterdir():
            shutil.rmtree(stage)


class Runner:
    '''Synthetic trusted stand-in: hashes staged bytes, never evaluates source.'''

    def __init__(self, parent, mode):
        self.parent, self.mode, self.tickets, self.results = parent, mode, [], []

    def run_staged(self, ticket, *, timeout_seconds, max_output_bytes):
        self.tickets.append(ticket)
        assert ticket.receipt_sha256 == receipt_digest(ticket.receipt)
        stage = self.parent / ticket.receipt.stage_name
        for item in ticket.receipt.files:
            assert sha256((stage / item.path).read_bytes()).hexdigest() == item.sha256
        answer = (stage / 'answer.py').read_bytes()
        passed = (self.mode == 'norepro' or ticket.command == 'suite'
                  or answer == (stage / 'expected.txt').read_bytes())
        result = CommandResult(ticket.command, 0 if passed else 1, stdout='synthetic evidence')
        self.results.append((ticket.attempt_id, result, passed))
        dispatch = 'wrong-dispatch' if self.mode == 'mismatch' else ticket.dispatch_id
        return RunnerEvidence(ticket.attempt_id, dispatch, ticket.receipt.stage_name,
                              ticket.receipt_sha256, result)


class Assessor:
    def __init__(self, runner):
        self.runner = runner

    def assess(self, attempt_id, result, *, timeout_seconds, max_bytes):
        passed = next(p for key, known, p in self.runner.results
                      if key == attempt_id and known is result)
        return TestAssessment(TestOutcome.PASS if passed else TestOutcome.ASSERTION_FAILED,
                              1, 0 if passed else 1, 'synthetic byte comparison')


class Model:
    def __init__(self, unknown_usage=False):
        self.calls, self.unknown_usage = 0, unknown_usage

    def propose(self, request, **bounds):
        self.calls += 1
        assert ORIGINAL in request.context
        return ModelReply(PATCH, None if self.unknown_usage else 7, 'synthetic-provider', 'synthetic-model')


class AlwaysEqual:
    '''Arbitrary equality object that must not pass as an execution kind.'''

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0


class Env:
    def __init__(self, case, mode='normal', fail_remove=False, unknown_usage=False):
        tmp = Path(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, tmp, True)
        self.parent = tmp / 'stages'
        self.parent.mkdir()
        self.fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        case.addCleanup(os.close, self.fd)
        self.events = []
        self.lifecycle = Lifecycle(self.parent, self.events, fail_remove)
        self.runner = Runner(self.parent, mode)
        assessor = Assessor(self.runner)
        self.model = Model(unknown_usage)

        def factory(attempt_id):
            return SnapshotWorkspace(
                attempt_id,
                (TextFile('answer.py', ORIGINAL, 'source'), TextFile('expected.txt', FIXED, 'test')),
                editable_paths=('answer.py',), context_paths=('answer.py',), assessor=assessor)

        self.adapter = StagedAttemptAdapter(
            lifecycle=self.lifecycle, workspace_factory=factory, staging_parent_fd=self.fd,
            manifest=MANIFEST, runner=self.runner, max_output_bytes=4096)

    def attempt(self, candidate=None, **over):
        kwargs = dict(
            base_commit=BASE, plan=PLAN, adapter=self.adapter, model=self.model,
            sandbox_policy=POLICY, caller_declared_status='collected', execution_kind='synthetic',
            max_attempts=1, token_budget=100, timeout_seconds=30, max_model_calls=1,
            max_text_bytes=4096, max_patch_bytes=4096)
        kwargs.update(over)
        return CandidateAttempt(candidate or entry(), **kwargs)

    def untouched(self):
        return self.events == [] and self.runner.tickets == [] and self.adapter.state == 'new' \
            and self.model.calls == 0


def entry():
    return CandidatePoolEntry('owner/project', 7, 'wrong answer', 'https://github.com/owner/project/issues/7', 1.5, 3)


class CandidateAttemptTests(unittest.TestCase):
    def test_success_detached_report_and_pending_approval(self):
        env = Env(self)
        candidate = entry()
        attempt = env.attempt(candidate)
        # Mutating the candidate after construction must not change the run.
        object.__setattr__(candidate, 'title', 'mutated-title')
        outcome = attempt.run()
        self.assertEqual(outcome.status, 'pending_human_review')
        self.assertTrue(outcome.attempt.success and outcome.attempt.cleanup_confirmed)
        self.assertEqual(outcome.attempt.diff, PATCH)
        self.assertEqual(outcome.human_approval, 'pending')
        self.assertFalse(outcome.submitted)
        report = outcome.report
        self.assertIsInstance(report, ReviewReport)
        data = report.evidence
        self.assertEqual((data['repo'], data['issue_number'], data['base_commit'], data['execution_kind']),
                         ('owner/project', 7, BASE, 'synthetic'))
        self.assertNotIn('mutated-title', report.snapshot_json)
        self.assertNotIn('mutated-title', report.to_markdown())
        self.assertEqual(report.diff_preview, PATCH)
        self.assertIn('not submitted', report.to_markdown())
        before = report.snapshot_json
        outcome.attempt.diff = 'tampered'
        self.assertEqual(report.snapshot_json, before)
        self.assertEqual(report.diff_preview, PATCH)
        self.assertEqual(env.events, ['create', 'start', 'remove'])
        self.assertEqual(env.adapter.state, 'removed')
        self.assertEqual(list(env.parent.iterdir()), [])
        self.assertEqual(env.model.calls, 1)

    def test_candidate_base_kind_and_report_preflight_validated_before_io(self):
        env = Env(self)
        bad_candidates = [
            SimpleNamespace(repo='owner/project', number=7, title='t', url='u', score=None, age_days=1),
            CandidatePoolEntry('not-a-repo', 7, 't', 'https://x', None, 1),
            CandidatePoolEntry('owner/project', 0, 't', 'https://x', None, 1),
            CandidatePoolEntry('owner/project', 7, '   ', 'https://x', None, 1),
            # Control character in the title is rejected by the real report validator.
            CandidatePoolEntry('owner/project', 7, 'bad\x07title', 'https://x', None, 1),
        ]
        for candidate in bad_candidates:
            with self.assertRaises(CandidateAttemptError):
                env.attempt(candidate)
        duplicate_plan = TestPlan(TestCommand('reproduce', 5),
                                  (TestCommand('suite', 5), TestCommand('suite', 5)))
        for over in ({'base_commit': 'ABC'}, {'base_commit': 'A' * 40}, {'execution_kind': 'isolated'},
                     {'execution_kind': AlwaysEqual()}, {'caller_declared_status': ''},
                     {'plan': duplicate_plan}):
            with self.assertRaises(CandidateAttemptError):
                env.attempt(**over)
        self.assertTrue(env.untouched())

    def test_missing_dependencies_and_bounds_fail_before_io(self):
        env = Env(self)
        for over in ({'model': None}, {'adapter': object()}, {'plan': None},
                     {'sandbox_policy': None}, {'max_text_bytes': 20000}):
            with self.assertRaises(CandidateAttemptError):
                env.attempt(**over)
        self.assertTrue(env.untouched())

    def test_runner_ticket_mismatch_is_not_success(self):
        env = Env(self, mode='mismatch')
        outcome = env.attempt().run()
        self.assertEqual(outcome.status, 'failed')
        self.assertFalse(outcome.attempt.success)
        self.assertEqual(env.model.calls, 0)
        self.assertEqual(len(env.runner.tickets), 1)
        self.assertEqual(outcome.human_approval, 'pending')
        self.assertTrue(outcome.report is None or not outcome.report.eligible_for_pr)

    def test_cleanup_failure_never_succeeds(self):
        env = Env(self, fail_remove=True)
        outcome = env.attempt().run()
        self.assertFalse(outcome.attempt.success)
        self.assertFalse(outcome.attempt.cleanup_confirmed)
        self.assertEqual(outcome.attempt.state, LoopState.CLEANUP_FAILED)
        self.assertEqual(outcome.status, 'failed')
        self.assertTrue(outcome.report is None or not outcome.report.eligible_for_pr)
        self.assertEqual(env.events.count('remove'), 1)
        self.assertEqual(env.adapter.state, 'remove_failed')

    def test_no_reproduction_means_no_model_call(self):
        env = Env(self, mode='norepro')
        outcome = env.attempt().run()
        self.assertEqual(outcome.attempt.state, LoopState.NOT_REPRODUCED)
        self.assertEqual(outcome.status, 'failed')
        self.assertEqual(env.model.calls, 0)
        self.assertEqual(len(env.runner.tickets), 2)

    def test_unknown_model_usage_stops_without_retry(self):
        env = Env(self, unknown_usage=True)
        outcome = env.attempt().run()
        self.assertEqual(outcome.attempt.state, LoopState.UNKNOWN_OUTCOME)
        self.assertFalse(outcome.attempt.success)
        self.assertEqual(env.model.calls, 1)
        self.assertEqual(len(env.runner.tickets), 2)  # baseline and reproduction only
        self.assertEqual(outcome.status, 'failed')
        self.assertTrue(outcome.report is None or not outcome.report.eligible_for_pr)

    def test_single_use_and_replay_prevention(self):
        env = Env(self)
        attempt = env.attempt()
        attempt.run()
        events, tickets = list(env.events), len(env.runner.tickets)
        with self.assertRaises(CandidateAttemptError):
            attempt.run()
        with self.assertRaises(CandidateAttemptError):
            env.attempt()  # adapter is no longer fresh
        self.assertEqual((env.model.calls, env.events, len(env.runner.tickets)), (1, events, tickets))

    def test_outcome_is_immutable_and_has_no_submission_path(self):
        env = Env(self)
        outcome = env.attempt().run()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            outcome.human_approval = 'approved'
        self.assertEqual(outcome.caller_declared_status, 'collected')
        self.assertFalse(any('submit' in name for name in dir(module) if not name.startswith('_')))
        with self.assertRaises(NotImplementedError):
            submit_draft_pr(outcome.report, 'body')


if __name__ == '__main__':
    unittest.main()

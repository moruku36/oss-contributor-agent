"""Six NEW offline cases. Fixture source is compared as bytes, never executed.

Real CandidateAttempt/loop/staging/snapshot/structured assessor/report/request;
only lifecycle, structured backend and patch replies are synthetic injections.
No old test modules, host runner, Docker, network or provider calls.
"""
import copy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from src.agent.candidate_attempt import CandidateAttempt
from src.agent.core import ModelReply, TestAssessment, TestCommand, TestOutcome, TestPlan
from src.agent.filesystem_staging import ManifestFile
from src.agent.snapshot_workspace import SnapshotWorkspace, TextFile
from src.agent.staged_attempt import StagedAttemptAdapter
from src.agent.staged_runner_bridge import EvidenceRejected, receipt_digest
from src.agent.structured_runner_evidence import (
    AssertionFailure, StructuredRun, StructuredRunnerEvidenceAdapter, StructuredTestSummary,
)
from src.collector.digest import CandidatePoolEntry
from src.review.draft_submission import DraftRequest, DraftTarget, prepare_draft_submission
from src.review.pr_builder import ReviewReport, build_pr_body, build_review_report
from src.sandbox.docker_manager import CommandResult, SandboxPolicy


BASE = '1bfe7cc7851033f9154d65d532b9f1ffb4b1dd5a'
BEFORE, AFTER = b'ANSWER = 1\n', b'ANSWER = 2\n'
PATCH = ('--- a/answer.py\n+++ b/answer.py\n@@ -1,1 +1,1 @@\n'
         '-ANSWER = 1\n+ANSWER = 2\n')
FAILURE = AssertionFailure('expected.txt::bytes', 'bytes differ <script> ```')
PASS = StructuredTestSummary(1, 1, (), 0, 0, 0, 0, True, False, True)
FAIL = replace(PASS, passed=0, assertions=(FAILURE,))
PLAN = TestPlan(TestCommand('reproduce', 5), (TestCommand('suite', 5),))
POLICY = SandboxPolicy('synthetic:test', 1, 256, max_output_bytes=4096)
ISSUE = {'repo': 'owner/project', 'number': 7, 'title': 'byte mismatch <script> ```'}
TARGET = DraftTarget('https://github.com', 'owner/project', 'owner/project',
                     'refs/heads/fix', 'b' * 40, 'refs/heads/main', BASE)


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def result_hash(result):
    # Independent expected digest, using all captured result bytes.
    values = [result.command, result.exit_code, result.stdout, result.stderr,
              result.duration_seconds]
    return sha256(json.dumps(values, ensure_ascii=True, allow_nan=False,
                             separators=(',', ':')).encode('ascii')).hexdigest()


def rebind(e):
    """Recompute the public hash to exercise consistency beyond hash mismatch."""
    data = {key: value for key, value in e.items() if key != 'binding_sha256'}
    e['binding_sha256'] = sha256(json.dumps(
        data, sort_keys=True, ensure_ascii=True, allow_nan=False,
        separators=(',', ':')).encode('ascii')).hexdigest()


class ByteBackend:
    def __init__(self, parent, transform):
        self.parent, self.transform = parent, transform
        self.replies, self.staged = [], []

    def run_structured(self, ticket, *, timeout_seconds, max_output_bytes):
        assert 0 < timeout_seconds <= 5 and max_output_bytes == 4096
        assert receipt_digest(ticket.receipt) == ticket.receipt_sha256
        stage = self.parent / ticket.receipt.stage_name
        captured = {}
        for item in ticket.receipt.files:
            data = (stage / item.path).read_bytes()
            assert len(data) == item.size_bytes and sha256(data).hexdigest() == item.sha256
            captured[item.path] = data
        self.staged.append(captured)
        passed = ticket.command == 'suite' or captured['answer.py'] == captured['expected.txt']
        reply = StructuredRun(ticket, CommandResult(
            ticket.command, 0 if passed else 1, 'synthetic AssertionError <script> ```', '', 0.001,
        ), replace(PASS if passed else FAIL))
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


class PatchReply:
    def __init__(self):
        self.calls = 0

    def propose(self, request, **bounds):
        self.calls += 1
        assert BEFORE.decode() in request.context and FAILURE.test_id in request.failing_log_tail
        return ModelReply(PATCH, 7, 'synthetic', 'byte-patch')


class Harness:
    def __init__(self, case, transform=None):
        self.parent = Path(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.parent, True)
        self.fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        case.addCleanup(os.close, self.fd)
        self.backend = ByteBackend(self.parent, transform)
        self.runner = StructuredRunnerEvidenceAdapter(backend=self.backend)
        self.lifecycle, self.model = Lifecycle(self.parent), PatchReply()

        def factory(attempt_id):
            self.workspace = SnapshotWorkspace(
                attempt_id, (TextFile('answer.py', BEFORE.decode(), 'source'),
                             TextFile('expected.txt', AFTER.decode(), 'test')),
                editable_paths=('answer.py',), context_paths=('answer.py',), assessor=self.runner,
            )
            return self.workspace

        self.adapter = StagedAttemptAdapter(
            lifecycle=self.lifecycle, workspace_factory=factory, staging_parent_fd=self.fd,
            manifest=(ManifestFile('answer.py', 'source'), ManifestFile('expected.txt', 'test')),
            runner=self.runner, max_output_bytes=4096,
        )
        case.addCleanup(self.close)

    def close(self):
        if self.adapter.state in ('created', 'running', 'execute_failed'):
            self.adapter.remove(self.adapter.attempt_id, timeout_seconds=5)

    def run(self):
        entry = CandidatePoolEntry(ISSUE['repo'], ISSUE['number'], ISSUE['title'],
                                   'https://github.com/owner/project/issues/7', None, 1)
        return CandidateAttempt(
            entry, base_commit=BASE, plan=PLAN, adapter=self.adapter, model=self.model,
            sandbox_policy=POLICY, caller_declared_status='collected', execution_kind='synthetic',
            max_attempts=1, max_model_calls=1, token_budget=100, timeout_seconds=30,
            max_text_bytes=4096, max_patch_bytes=4096,
        ).run()


def rebuild(attempt):
    return build_review_report(attempt, ISSUE, base_commit=BASE, plan=PLAN,
                               execution_kind='synthetic')


def prepare(source):
    return prepare_draft_submission(source, target=TARGET, title='Fix bytes')


class ReviewEvidencePropagationTests(unittest.TestCase):
    def test_actual_candidate_baseline_assertion_patch_reverify_report_request(self):
        env = Harness(self)
        outcome = env.run()
        self.assertEqual(outcome.status, 'pending_human_review')
        self.assertEqual(outcome.attempt.diff.encode(), PATCH.encode())
        self.assertTrue(outcome.attempt.success and outcome.attempt.cleanup_confirmed)
        self.assertEqual(env.model.calls, 1)
        self.assertEqual([event for event, _ in env.lifecycle.events], ['create', 'start', 'remove'])
        self.assertEqual(list(env.parent.iterdir()), [])
        self.assertEqual([s['answer.py'] for s in env.backend.staged],
                         [BEFORE, BEFORE, AFTER, AFTER])
        self.assertTrue(all(s['expected.txt'] == AFTER for s in env.backend.staged))
        data = outcome.report.evidence
        self.assertEqual((data['schema_version'], data['execution_kind']), (2, 'synthetic'))
        self.assertEqual(outcome.attempt.runner_evidence_version, 1)
        expected = ['pass', 'assertion_failed', 'pass', 'pass']
        self.assertEqual([c['assessment']['outcome'] for c in data['attempt']['checks']], expected)
        for index, (check, reply) in enumerate(zip(data['attempt']['checks'], env.backend.replies), 1):
            e = check['assessment']['runner_evidence']
            self.assertEqual((e['attempt_id'], e['dispatch_id'], e['generation'], e['stage_name'],
                              e['receipt_sha256'], e['command'], e['result_sha256']),
                             (outcome.attempt.sandbox_attempt_id, reply.ticket.dispatch_id, index,
                              reply.ticket.receipt.stage_name, receipt_digest(reply.ticket.receipt),
                              reply.ticket.command, result_hash(reply.result)))
            self.assertEqual((e['classification'], e['tests_run'], e['passed'], e['assertion_failures']),
                             (expected[index - 1], 1, 0 if index == 2 else 1, 1 if index == 2 else 0))
            copied = dict(e)
            rebind(copied)
            self.assertEqual(copied['binding_sha256'], e['binding_sha256'])
        snapshot = outcome.report.snapshot_json.encode()
        self.assertEqual(rebuild(outcome.attempt).snapshot_json.encode(), snapshot)
        self.assertEqual(ReviewReport(snapshot.decode()).snapshot_json.encode(), snapshot)
        self.assertIn(html.escape(snapshot.decode(), quote=False), outcome.report.to_markdown())
        self.assertNotIn('<script>', outcome.report.to_markdown())
        request = prepare(outcome)
        self.assertEqual(request.data['report_json'].encode(), snapshot)
        self.assertEqual(request.data['body'].encode(), build_pr_body(outcome.report).encode())
        self.assertEqual(DraftRequest(request.snapshot_json).data, request.data)
        self.assertEqual(prepare(outcome.report).snapshot_json.encode(), request.snapshot_json.encode())
        self.assertEqual(outcome.human_approval, 'pending')
        self.assertFalse(outcome.submitted)

    def test_wrong_command_stale_generation_and_reconstruction_binding(self):
        for transform in (
            lambda r: replace(r, result=replace(r.result, command='wrong')),
            lambda r: replace(r, ticket=replace(r.ticket, generation=r.ticket.generation - 1)),
        ):
            with self.subTest(transform=transform):
                env = Harness(self, transform)
                failed = env.run()
                self.assertFalse(failed.attempt.success)
                self.assertEqual((len(env.backend.replies), env.model.calls), (1, 0))
                self.assertIsNotNone(failed.report)
                self.assertFalse(failed.report.eligible_for_pr)
        outcome = Harness(self).run()
        for fields in ({'command': 'wrong'}, {'generation': 1}, {'attempt_id': 'other'},
                       {'classification': 'infra_error'}, {'tests_run': 2},
                       {'detail_sha256': '0' * 64}, {'result_sha256': 'bad'}):
            data = outcome.report.evidence
            data['attempt']['checks'][1]['assessment']['runner_evidence'].update(fields)
            rebind(data['attempt']['checks'][1]['assessment']['runner_evidence'])
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ReviewReport(canonical(data)).to_markdown()
        changed = copy.deepcopy(outcome.attempt)
        c = changed.checks[1]
        changed.checks[1] = replace(c, assessment=replace(c.assessment, runner_evidence=replace(
            c.assessment.runner_evidence, generation=1)))
        with self.assertRaises(ValueError):
            prepare(replace(outcome, attempt=changed))

    def test_assertion_suite_pass_infrastructure_and_unknown_stay_distinct(self):
        successful = Harness(self).run()
        self.assertEqual([c.assessment.outcome for c in successful.attempt.checks],
                         [TestOutcome.PASS, TestOutcome.ASSERTION_FAILED,
                          TestOutcome.PASS, TestOutcome.PASS])
        variants = [
            (FAIL, 1, TestOutcome.ASSERTION_FAILED),
            (replace(FAIL, collection_errors=1, infrastructure_errors=1), 1, TestOutcome.INFRA_ERROR),
            (replace(PASS, tests_run=2, test_errors=1), 1, TestOutcome.INFRA_ERROR),
            (replace(PASS, complete=False), 0, TestOutcome.UNKNOWN),
            (replace(PASS, tests_run=0, passed=0, skipped=1), 0, TestOutcome.UNKNOWN),
        ]
        for summary, code, expected in variants:
            with self.subTest(expected=expected, summary=summary):
                env = Harness(self, lambda r, s=summary, e=code: replace(
                    r, summary=s, result=replace(r.result, exit_code=e)))
                failed = env.run()
                assessment = failed.attempt.checks[0].assessment
                self.assertEqual(assessment.outcome, expected)
                self.assertEqual(assessment.runner_evidence.classification, expected.value)
                self.assertEqual(failed.report.evidence['attempt']['checks'][0]['assessment']
                                 ['runner_evidence']['classification'], expected.value)
                self.assertFalse(failed.report.eligible_for_pr)
                self.assertEqual(env.model.calls, 0)
                with self.assertRaises(ValueError):
                    prepare(failed.report)

    def test_mutation_cannot_change_detached_report_markdown_or_request_bytes(self):
        env = Harness(self)
        outcome = env.run()
        request = prepare(outcome)
        before = (outcome.report.snapshot_json.encode(), outcome.report.to_markdown().encode(),
                  request.snapshot_json.encode(), request.data['body'].encode())
        e = outcome.attempt.checks[0].assessment.runner_evidence
        with self.assertRaises(FrozenInstanceError):
            e.command = 'changed'
        env.backend.replies[0].result.stdout = 'mutated result'
        object.__setattr__(env.backend.replies[0].ticket, 'command', 'mutated ticket')
        object.__setattr__(env.backend.replies[0].summary, 'passed', 99)
        outcome.report.evidence['attempt']['checks'].clear()
        request.data['body'] = 'changed'
        outcome.attempt.diff = 'changed diff'
        object.__setattr__(e, 'command', 'mutated detached attempt')
        self.assertEqual((outcome.report.snapshot_json.encode(), outcome.report.to_markdown().encode(),
                          request.snapshot_json.encode(), request.data['body'].encode()), before)
        with self.assertRaises(ValueError):
            prepare(outcome)
        manual = Harness(self)
        manual.adapter.create('manual', policy=POLICY, timeout_seconds=5)
        manual.adapter.start('manual', timeout_seconds=5)
        result = manual.adapter.execute('manual', 'suite', timeout_seconds=5, max_output_bytes=4096)
        result.stdout = 'changed before assess'
        with self.assertRaises(EvidenceRejected):
            manual.adapter.assess('manual', result, timeout_seconds=5, max_bytes=4096)

    def test_missing_duplicate_out_of_order_required_evidence_and_legacy_compatibility(self):
        outcome = Harness(self).run()
        variants = []
        data = outcome.report.evidence
        del data['attempt']['checks'][0]['assessment']['runner_evidence']
        variants.append(data)
        data = outcome.report.evidence
        data['attempt']['checks'][0]['assessment']['runner_evidence'] = None
        variants.append(data)
        data = outcome.report.evidence
        del data['attempt']['runner_evidence_version']
        variants.append(data)
        data = outcome.report.evidence
        data['attempt']['checks'].pop(1)
        variants.append(data)
        data = outcome.report.evidence
        data['attempt']['checks'][1] = copy.deepcopy(data['attempt']['checks'][0])
        variants.append(data)
        data = outcome.report.evidence
        data['attempt']['checks'][2:4] = reversed(data['attempt']['checks'][2:4])
        variants.append(data)
        data = outcome.report.evidence
        data['attempt']['checks'][1]['assessment']['runner_evidence']['dispatch_id'] = (
            data['attempt']['checks'][0]['assessment']['runner_evidence']['dispatch_id'])
        rebind(data['attempt']['checks'][1]['assessment']['runner_evidence'])
        variants.append(data)
        data = outcome.report.evidence
        data['schema_version'] = 1  # no silent downgrade with strengthened fields
        variants.append(data)
        for index, data in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                prepare(ReviewReport(canonical(data)))
        legacy_attempt = copy.deepcopy(outcome.attempt)
        legacy_attempt.runner_evidence_version = 0
        legacy_attempt.checks = [replace(c, assessment=TestAssessment(
            c.assessment.outcome, c.assessment.tests_run, c.assessment.assertion_failures,
            c.assessment.detail)) for c in legacy_attempt.checks]
        legacy = rebuild(legacy_attempt)
        self.assertEqual(legacy.evidence['schema_version'], 1)
        self.assertNotIn('runner_evidence', legacy.snapshot_json)
        self.assertEqual(prepare(legacy).data['report_json'].encode(), legacy.snapshot_json.encode())
        self.assertEqual(ReviewReport(legacy.snapshot_json).to_markdown(), legacy.to_markdown())

    def test_malformed_oversized_rejection_and_bounded_failed_review(self):
        for summary in (replace(PASS, tests_run=True), replace(PASS, passed=2),
                        replace(PASS, skipped=1_000_001), replace(PASS, complete=1),
                        replace(FAIL, assertions=(replace(FAILURE, detail='x' * 1025),))):
            env = Harness(self, lambda r, s=summary: replace(r, summary=s))
            failed = env.run()
            self.assertFalse(failed.attempt.success)
            self.assertEqual(env.model.calls, 0)
            self.assertIsNotNone(failed.report)
            self.assertFalse(failed.report.eligible_for_pr)
            with self.assertRaises(ValueError):
                prepare(failed)
        outcome = Harness(self).run()
        for fields in ({'command': 'x' * 4097}, {'dispatch_id': 'x' * 129},
                       {'generation': True}, {'passed': True}, {'complete': 1},
                       {'receipt_sha256': 'z' * 64}, {'assertion_failures': 129},
                       {'result_sha256': '0' * 64}, {'receipt_sha256': '0' * 64},
                       {'duration_seconds': -1}, {'unexpected': 'field'}, {'schema_version': 99}):
            data = outcome.report.evidence
            data['attempt']['checks'][0]['assessment']['runner_evidence'].update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                ReviewReport(canonical(data)).to_markdown()
        with self.assertRaises(ValueError):
            ReviewReport(' ' * 524289).to_markdown()
        # A real failed verification retains all four accepted records and can
        # be reviewed/rebuilt, while remaining ineligible for request preparation.
        env = Harness(self, lambda r: replace(r, summary=FAIL, result=replace(r.result, exit_code=1))
                      if r.ticket.generation == 4 else r)
        failed = env.run()
        self.assertEqual(failed.status, 'failed')
        self.assertEqual(len(failed.report.evidence['attempt']['checks']), 4)
        self.assertEqual(rebuild(failed.attempt).snapshot_json.encode(), failed.report.snapshot_json.encode())
        self.assertIn('not PR eligible', failed.report.to_markdown())
        self.assertFalse(failed.report.eligible_for_pr)
        with self.assertRaises(ValueError):
            prepare(failed.report)

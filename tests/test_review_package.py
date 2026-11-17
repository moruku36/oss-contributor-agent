"""Exactly six NEW offline methods; authored for the coordinator, NOT run here.

Fixture source is inert text. Synthetic lifecycle/backend/model injections use
the actual candidate, staging, structured evidence, report and prepare path.
No old test modules or host/process/container/network runners are used.
"""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock

from src.agent.candidate_attempt import CandidateAttempt
from src.agent.core import (
    AttemptResult, CheckEvidence, LoopState as S, ModelReceipt, ModelReply,
    TestAssessment, TestCommand, TestOutcome as O, TestPlan,
)
from src.agent.filesystem_staging import ManifestFile
from src.agent.snapshot_workspace import SnapshotWorkspace, TextFile
from src.agent.staged_attempt import StagedAttemptAdapter
from src.agent.staged_runner_bridge import receipt_digest
from src.agent.structured_runner_evidence import (
    AssertionFailure, StructuredRun, StructuredRunnerEvidenceAdapter, StructuredTestSummary,
)
from src.collector.digest import CandidatePoolEntry
from src.review.draft_submission import DraftRequest, DraftTarget, prepare_draft_submission
from src.review.pr_builder import build_pr_body, build_review_report
from src.review import review_package as rp
from src.sandbox.docker_manager import CommandResult, SandboxPolicy

BASE = 'a' * 40
TARGET = DraftTarget('https://github.com', 'owner/project', 'contributor/project',
                     'refs/heads/fix', 'b' * 40, 'refs/heads/main', BASE)
PLAN = TestPlan(TestCommand('reproduce', 5), (TestCommand('suite', 5),))
ISSUE = {'repo': 'owner/project', 'number': 7, 'title': 'Fix bytes <script> ```'}
BEFORE, AFTER = 'ANSWER = 1\n', 'ANSWER = 2\n'
PATCH = '--- a/answer.py\n+++ b/answer.py\n@@ -1,1 +1,1 @@\n-ANSWER = 1\n+ANSWER = 2\n'
PAYLOAD_NAMES = ('request.json', 'report.json', 'report.md', 'body.md', 'diff.patch')
PASS = StructuredTestSummary(1, 1, (), 0, 0, 0, 0, True, False, True)
FAIL = replace(PASS, passed=0, assertions=(AssertionFailure('expected.txt::bytes',
                                                        'synthetic byte mismatch'),))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def private_root(case):
    path = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, path, True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    case.addCleanup(os.close, fd)
    return path, fd


def prepared(*, target=TARGET, title='Fix bytes', base=BASE):
    def check(phase, command, outcome=O.PASS):
        failed = outcome is O.ASSERTION_FAILED
        return CheckEvidence(phase, command, int(failed), 0.001, 'synthetic',
                             TestAssessment(outcome, 1, int(failed), 'byte assertion'))
    attempt = AttemptResult(
        True, 1, diff=PATCH, state=S.SUCCEEDED, states=[S.READY, S.SUCCEEDED],
        checks=[check(S.BASELINE, 'suite'), check(S.REPRODUCING, 'reproduce', O.ASSERTION_FAILED),
                check(S.VERIFYING_REPRODUCTION, 'reproduce'), check(S.VERIFYING_SUITE, 'suite')],
        model_calls=1, tokens_used=7, known_tokens_used=7,
        model_receipts=[ModelReceipt('synthetic', 'fixed-reply', 7)],
        cleanup_confirmed=True, sandbox_attempt_id='synthetic', elapsed_seconds=0.01,
    )
    report = build_review_report(attempt, ISSUE, base_commit=base, plan=PLAN,
                                 execution_kind='synthetic')
    return prepare_draft_submission(report, target=target, title=title, pr_template='Template 日本語')


def rebind_package(path):
    """Public hashes are consistency checks; adversarial fixtures can recompute them."""
    payloads = {name: (path / name).read_bytes() for name in PAYLOAD_NAMES}
    manifest = canonical({
        'schema_version': 1, 'request_sha256': sha256(payloads['request.json']).hexdigest(),
        'total_payload_bytes': sum(map(len, payloads.values())),
        'files': {name: {'size_bytes': len(data), 'sha256': sha256(data).hexdigest()}
                  for name, data in payloads.items()},
    })
    (path / 'manifest.json').write_bytes(manifest)
    (path / 'COMPLETE').write_bytes(canonical({
        'schema_version': 1, 'manifest_sha256': sha256(manifest).hexdigest(),
    }))


class SyntheticBackend:
    def __init__(self, parent):
        self.parent, self.replies = parent, []

    def run_structured(self, ticket, *, timeout_seconds, max_output_bytes):
        assert 0 < timeout_seconds <= 5 and max_output_bytes == 4096
        assert receipt_digest(ticket.receipt) == ticket.receipt_sha256
        stage = self.parent / ticket.receipt.stage_name
        data = {}
        for item in ticket.receipt.files:
            data[item.path] = (stage / item.path).read_bytes()
            assert len(data[item.path]) == item.size_bytes
            assert sha256(data[item.path]).hexdigest() == item.sha256
        passed = ticket.command == 'suite' or data['answer.py'] == data['expected.txt']
        reply = StructuredRun(ticket, CommandResult(ticket.command, 0 if passed else 1,
                                                   'synthetic bytes only', '', 0.001),
                              PASS if passed else FAIL)
        self.replies.append(reply)
        return reply


class SyntheticLifecycle:
    def __init__(self, parent):
        self.parent, self.events = parent, []

    def create(self, attempt_id, **bounds):
        self.events.append('create')

    def start(self, attempt_id, **bounds):
        self.events.append('start')

    def remove(self, attempt_id, **bounds):
        self.events.append('remove')
        for stage in self.parent.iterdir():
            shutil.rmtree(stage)


class SyntheticModel:
    def __init__(self):
        self.calls = 0

    def propose(self, request, **bounds):
        self.calls += 1
        assert BEFORE in request.context and 'expected.txt::bytes' in request.failing_log_tail
        return ModelReply(PATCH, 7, 'synthetic', 'fixed-reply')


def actual_candidate(case):
    parent, fd = private_root(case)
    backend = SyntheticBackend(parent)
    runner = StructuredRunnerEvidenceAdapter(backend=backend)
    lifecycle, model = SyntheticLifecycle(parent), SyntheticModel()

    def factory(attempt_id):
        return SnapshotWorkspace(
            attempt_id, (TextFile('answer.py', BEFORE, 'source'),
                         TextFile('expected.txt', AFTER, 'test')),
            editable_paths=('answer.py',), context_paths=('answer.py',), assessor=runner,
        )

    adapter = StagedAttemptAdapter(
        lifecycle=lifecycle, workspace_factory=factory, staging_parent_fd=fd,
        manifest=(ManifestFile('answer.py', 'source'), ManifestFile('expected.txt', 'test')),
        runner=runner, max_output_bytes=4096,
    )
    outcome = CandidateAttempt(
        CandidatePoolEntry(ISSUE['repo'], ISSUE['number'], ISSUE['title'],
                           'https://github.com/owner/project/issues/7', None, 1),
        base_commit=BASE, plan=PLAN, adapter=adapter, model=model,
        sandbox_policy=SandboxPolicy('synthetic:test', 1, 256, max_output_bytes=4096),
        caller_declared_status='collected', execution_kind='synthetic', max_attempts=1,
        max_model_calls=1, token_budget=100, timeout_seconds=30,
        max_text_bytes=4096, max_patch_bytes=4096,
    ).run()
    return outcome, backend, lifecycle, model


class ReviewPackageTests(unittest.TestCase):
    def test_actual_candidate_structured_report_prepare_package_readback_identical(self):
        outcome, backend, lifecycle, model = actual_candidate(self)
        self.assertEqual(outcome.status, 'pending_human_review')
        self.assertTrue(outcome.attempt.success and outcome.attempt.cleanup_confirmed)
        self.assertEqual((len(backend.replies), model.calls), (4, 1))
        self.assertEqual(lifecycle.events, ['create', 'start', 'remove'])
        report = outcome.report
        self.assertEqual(report.evidence['schema_version'], 2)
        self.assertEqual([c['assessment']['runner_evidence']['classification']
                          for c in report.evidence['attempt']['checks']],
                         ['pass', 'assertion_failed', 'pass', 'pass'])
        request = prepare_draft_submission(outcome, target=TARGET, title='Fix bytes',
                                           pr_template='日本語 template')
        root, fd = private_root(self)
        load = rp._load

        def observe(directory, name, *, complete):
            self.assertFalse(complete)
            self.assertFalse((root / name / 'COMPLETE').exists())
            return load(directory, name, complete=complete)

        with mock.patch.object(rp, '_load', side_effect=observe) as internal:
            receipt = rp.export_review_package(request, parent_fd=fd, name='candidate')
        self.assertEqual(internal.call_count, 1)
        saved = rp.read_review_package(parent_fd=fd, name='candidate')
        self.assertEqual(saved.request.snapshot_json.encode(), request.snapshot_json.encode())
        self.assertEqual(saved.request.data, request.data)
        self.assertEqual(saved.report_json, report.snapshot_json)
        self.assertEqual(saved.human_report, report.to_markdown())
        self.assertEqual(saved.body, build_pr_body(report, '日本語 template'))
        self.assertEqual(saved.diff, PATCH)
        self.assertEqual(receipt.request_sha256, request.binding_sha256)
        manifest = json.loads(saved.manifest_json)
        self.assertEqual(manifest['total_payload_bytes'], sum(
            (root / 'candidate' / name).stat().st_size for name in PAYLOAD_NAMES))
        for name, entry in manifest['files'].items():
            data = (root / 'candidate' / name).read_bytes()
            self.assertEqual(entry, {'size_bytes': len(data), 'sha256': sha256(data).hexdigest()})
        self.assertEqual(receipt.total_bytes, sum(p.stat().st_size for p in (root / 'candidate').iterdir()))
        self.assertFalse(outcome.submitted)
        self.assertEqual(outcome.human_approval, 'pending')
        os.fstat(fd)  # caller descriptor remains open

    def test_tampered_body_report_diff_and_manifest_reject(self):
        root, fd = private_root(self)
        request = prepared()

        def tamper(name, filename, data):
            rp.export_review_package(request, parent_fd=fd, name=name)
            (root / name / filename).write_bytes(data)
            rebind_package(root / name)  # exact domain checks must still reject
            with self.assertRaises(rp.ReviewPackageError):
                rp.read_review_package(parent_fd=fd, name=name)

        tamper('body', 'body.md', b'changed body')
        changed_report = json.loads(request.data['report_json'])
        changed_report['issue_title'] = 'different but valid report'
        tamper('report', 'report.json', canonical(changed_report))
        tamper('human-report', 'report.md', b'changed rendered report')
        tamper('diff', 'diff.patch', PATCH.encode() + b'changed\n')
        rp.export_review_package(request, parent_fd=fd, name='manifest')
        manifest = json.loads((root / 'manifest' / 'manifest.json').read_bytes())
        manifest['files']['body.md']['size_bytes'] = True
        data = canonical(manifest)
        (root / 'manifest' / 'manifest.json').write_bytes(data)
        (root / 'manifest' / 'COMPLETE').write_bytes(canonical({
            'schema_version': 1, 'manifest_sha256': sha256(data).hexdigest()}))
        with self.assertRaises(rp.ReviewPackageError):
            rp.read_review_package(parent_fd=fd, name='manifest')

    def test_injected_partial_write_retains_incomplete_receipt_without_overwrite(self):
        root, fd = private_root(self)
        request, write = prepared(), os.write
        calls = []

        def partial(descriptor, data):
            calls.append(len(data))
            if len(calls) == 1:
                return write(descriptor, data[:11])
            raise OSError('injected partial write failure')

        with mock.patch.object(rp.os, 'write', side_effect=partial):
            with self.assertRaises(rp.ReviewPackageExportError) as caught:
                rp.export_review_package(request, parent_fd=fd, name='partial')
        receipt = caught.exception.receipt
        self.assertEqual((receipt.name, receipt.outcome, receipt.directory_created,
                          receipt.attempted_file), ('partial', 'incomplete', True, 'request.json'))
        self.assertEqual(len(calls), 2)  # no retry after the failure
        retained = (root / 'partial' / 'request.json').read_bytes()
        self.assertEqual(retained, request.snapshot_json.encode()[:11])
        self.assertFalse((root / 'partial' / 'COMPLETE').exists())
        with self.assertRaises(rp.ReviewPackageError):
            rp.read_review_package(parent_fd=fd, name='partial')
        with self.assertRaises(rp.ReviewPackageExportError) as collision:
            rp.export_review_package(request, parent_fd=fd, name='partial')
        self.assertFalse(collision.exception.receipt.directory_created)
        self.assertEqual((root / 'partial' / 'request.json').read_bytes(), retained)
        # A failure in the LAST file must also leave an unreadable package.
        write_file = rp._write_file

        def partial_marker(directory, name, data, **options):
            if name != 'COMPLETE':
                return write_file(directory, name, data, **options)
            write_file(directory, name, data[:1])
            raise OSError('injected marker failure')

        with mock.patch.object(rp, '_write_file', side_effect=partial_marker):
            with self.assertRaises(rp.ReviewPackageExportError) as marker_error:
                rp.export_review_package(request, parent_fd=fd, name='partial-marker')
        self.assertEqual(marker_error.exception.receipt.outcome, 'incomplete')
        self.assertEqual(marker_error.exception.receipt.attempted_file, 'COMPLETE')
        self.assertEqual((root / 'partial-marker' / 'COMPLETE').read_bytes(), b'{')
        with self.assertRaises(rp.ReviewPackageError):
            rp.read_review_package(parent_fd=fd, name='partial-marker')

    def test_existing_destination_symlinks_extra_and_nonregular_leave_targets_unchanged(self):
        root, fd = private_root(self)
        request = prepared()
        (root / 'existing').mkdir(mode=0o700)
        target = root / 'existing' / 'sentinel'
        target.write_bytes(b'keep target')
        os.symlink(root / 'existing', root / 'link')
        with self.assertRaises(rp.ReviewPackageExportError):
            rp.export_review_package(request, parent_fd=fd, name='existing')
        with self.assertRaises(rp.ReviewPackageExportError):
            rp.export_review_package(request, parent_fd=fd, name='link')
        with self.assertRaises(rp.ReviewPackageError):
            rp.read_review_package(parent_fd=fd, name='link')
        rp.export_review_package(request, parent_fd=fd, name='extra')
        (root / 'extra' / 'unexpected').write_bytes(b'extra')
        with mock.patch.object(rp.os, 'read', side_effect=AssertionError('must reject before reads')) as read:
            with self.assertRaises(rp.ReviewPackageError):
                rp.read_review_package(parent_fd=fd, name='extra')
            read.assert_not_called()
        rp.export_review_package(request, parent_fd=fd, name='file-link')
        (root / 'file-link' / 'body.md').unlink()
        os.symlink(target, root / 'file-link' / 'body.md')
        with self.assertRaises(rp.ReviewPackageError):
            rp.read_review_package(parent_fd=fd, name='file-link')
        (root / 'file-link' / 'body.md').unlink()
        os.mkfifo(root / 'file-link' / 'body.md', mode=0o600)
        with self.assertRaises(rp.ReviewPackageError):
            rp.read_review_package(parent_fd=fd, name='file-link')
        with self.assertRaises(rp.ReviewPackageError):
            rp.export_review_package(request, parent_fd=fd, name='../existing')
        self.assertEqual(target.read_bytes(), b'keep target')
        self.assertEqual(list((root / 'existing').iterdir()), [target])

    def test_input_mutation_detaches_saved_bytes_and_target_head_base_binding(self):
        root, fd = private_root(self)
        request = prepared()
        original = request.snapshot_json
        rp.export_review_package(request, parent_fd=fd, name='detached')
        request.data['target']['head_commit'] = 'c' * 40
        object.__setattr__(request, 'snapshot_json', 'caller mutation')
        saved = rp.read_review_package(parent_fd=fd, name='detached')
        object.__setattr__(saved.request, 'snapshot_json', 'view mutation')
        object.__setattr__(saved.report, 'snapshot_json', 'view mutation')
        self.assertEqual(saved.request.snapshot_json, original)
        self.assertEqual(saved.request.data['target'], {k: getattr(TARGET, k)
                                                       for k in TARGET.__dataclass_fields__})
        self.assertEqual(saved.report.evidence['base_commit'], BASE)
        moved_head = prepared(target=replace(TARGET, head_commit='c' * 40))
        moved_base = prepared(target=replace(TARGET, base_commit='d' * 40), base='d' * 40)
        self.assertNotEqual(saved.request.binding_sha256, moved_head.binding_sha256)
        self.assertNotEqual(saved.request.binding_sha256, moved_base.binding_sha256)
        tampered = json.loads(original)
        tampered['target']['base_commit'] = 'e' * 40
        with self.assertRaises(ValueError):
            rp.export_review_package(DraftRequest(canonical(tampered).decode()), parent_fd=fd, name='bad-base')
        self.assertFalse((root / 'bad-base').exists())
        self.assertEqual((root / 'detached' / 'request.json').read_bytes(), original.encode())
        self.assertEqual(rp.read_review_package(parent_fd=fd, name='detached'), saved)

    def test_oversize_truncated_invalid_utf8_bounded_without_submission_effects(self):
        root, fd = private_root(self)
        request = prepared()
        journal = ModuleType('src.review.submission_journal')
        journal.SQLiteSubmissionJournal = mock.Mock(side_effect=AssertionError('no journal'))
        with mock.patch('src.review.draft_submission.DraftSubmission',
                        side_effect=AssertionError('no authority or transport')) as submission, \
                mock.patch('src.review.pr_builder.submit_draft_pr',
                           side_effect=AssertionError('no dispatch')) as dispatch, \
                mock.patch.dict(sys.modules, {'src.review.submission_journal': journal}):
            with self.assertRaises(ValueError):
                rp.export_review_package(DraftRequest('x' * (rp.MAX_REQUEST_BYTES + 1)),
                                         parent_fd=fd, name='oversize-input')
            self.assertFalse((root / 'oversize-input').exists())
            rp.export_review_package(request, parent_fd=fd, name='oversize')
            with (root / 'oversize' / 'request.json').open('wb') as stream:
                stream.truncate(rp.MAX_REQUEST_BYTES + 1)
            with mock.patch.object(rp.os, 'read', side_effect=AssertionError('no oversized reads')) as read:
                with self.assertRaises(rp.ReviewPackageError):
                    rp.read_review_package(parent_fd=fd, name='oversize')
                read.assert_not_called()
            rp.export_review_package(request, parent_fd=fd, name='total')
            for name, cap in rp._PAYLOAD_CAPS.items():
                with (root / 'total' / name).open('wb') as stream:
                    stream.truncate(cap)
            with mock.patch.object(rp.os, 'read', side_effect=AssertionError('no over-total reads')) as read:
                with self.assertRaises(rp.ReviewPackageError):
                    rp.read_review_package(parent_fd=fd, name='total')
                read.assert_not_called()
            rp.export_review_package(request, parent_fd=fd, name='truncated')
            body = root / 'truncated' / 'body.md'
            body.write_bytes(body.read_bytes()[:-1])
            with self.assertRaises(rp.ReviewPackageError):
                rp.read_review_package(parent_fd=fd, name='truncated')
            rp.export_review_package(request, parent_fd=fd, name='utf8')
            (root / 'utf8' / 'body.md').write_bytes(b'\xff')
            rebind_package(root / 'utf8')
            with self.assertRaises(rp.ReviewPackageError):
                rp.read_review_package(parent_fd=fd, name='utf8')
            submission.assert_not_called()
            dispatch.assert_not_called()
            journal.SQLiteSubmissionJournal.assert_not_called()

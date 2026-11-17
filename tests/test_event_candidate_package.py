"""Offline synthetic bytes only: NEVER execute candidate source or commands."""
from contextlib import ExitStack
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from src.agent import candidate_attempt
from src.agent.core import ModelReply, TestCommand, TestPlan
from src.agent.event_candidate_package import EventCandidatePackage, EventPackageOptions
from src.agent.filesystem_staging import ManifestFile
from src.agent.runner_event_channel import encode_runner_event_response
from src.agent.runner_events import RunnerEventCapture, RunCompleted, TestEvent
from src.agent.snapshot_workspace import SnapshotLimits, TextFile
from src.collector.digest import CandidatePoolEntry
from src.review.draft_submission import DraftTarget
from src.review import review_package as package
from src.sandbox.docker_manager import CommandResult, SandboxPolicy


BROKEN = b"value = 'broken'\n"
FIXED = b"value = 'fixed'\n"
IMMUTABLE = {
    'tests/repro.txt': b'synthetic reproduction fixture\n',
    'tests/suite.txt': b'synthetic suite fixture\n',
    'runner/events.txt': b'synthetic runner fixture; never executed\n',
}
PATCH = ("--- a/src/value.py\n+++ b/src/value.py\n@@ -1 +1 @@\n"
         "-value = 'broken'\n+value = 'fixed'\n")


class _Lifecycle:
    def __init__(self):
        self.calls = []
        self.on_create = lambda: None
        self.remove_error = None

    def create(self, attempt_id, *, policy, timeout_seconds):
        self.calls.append(('create', attempt_id))
        if policy.image != 'synthetic:declared':
            raise AssertionError('policy declaration was not detached')
        self.on_create()

    def start(self, attempt_id, **bounds):
        self.calls.append(('start', attempt_id))

    def remove(self, attempt_id, **bounds):
        self.calls.append(('remove', attempt_id))
        if self.remove_error is not None:
            raise self.remove_error


class _Model:
    def __init__(self):
        self.calls = []
        self.error = None

    def propose(self, request, **bounds):
        self.calls.append((request, bounds))
        if self.error is not None:
            raise self.error
        return ModelReply(PATCH, 12, 'synthetic-fixture', 'byte-patch-fixture')


class _Channel:
    def __init__(self, stage_path):
        self.stage_path, self.calls = stage_path, []
        self.mode = 'good'
        self.error = None

    def dispatch(self, ticket, **bounds):
        self.calls.append(ticket)
        if self.error is not None:
            raise self.error
        # Read all actual staged files and compare bytes. No eval, exec, imports
        # of fixtures, shell, subprocess, test runner or candidate commands.
        staged = self.stage_path / ticket.receipt.stage_name
        expected = {**IMMUTABLE, 'src/value.py': BROKEN if ticket.generation <= 2 else FIXED}
        actual = {p.relative_to(staged).as_posix(): p.read_bytes()
                  for p in staged.rglob('*') if p.is_file()}
        if actual != expected:
            raise AssertionError('whole staged snapshot bytes differ')
        if ticket.command not in ('synthetic-suite', 'synthetic-repro'):
            raise AssertionError('unexpected declared command')
        failed = ticket.command == 'synthetic-repro' and actual['src/value.py'] == BROKEN
        outcome = 'assertion_failed' if failed else 'passed'
        if self.mode == 'error':
            outcome = 'error'
        passed, assertions, errors = (int(outcome == k) for k in ('passed', 'assertion_failed', 'error'))
        event = TestEvent(1, 'fixture.case', outcome, '' if passed else 'synthetic failure')
        capture = RunnerEventCapture(
            (event, RunCompleted(2, 1, passed, assertions, errors, 0, 0, 0)),
            self.mode != 'unknown', False, self.mode != 'unknown',
        )
        result = CommandResult(ticket.command, 0 if passed else 1, 'synthetic log', '', 0.001)
        return encode_runner_event_response(
            ticket, result, capture, max_wire_bytes=bounds['max_wire_bytes'],
            max_output_bytes=bounds['max_output_bytes'],
        )


class _Fixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stage, self.packages = self.root / 'stages', self.root / 'packages'
        self.stage.mkdir(mode=0o700)
        self.packages.mkdir(mode=0o700)
        self.stage_fd = os.open(self.stage, os.O_RDONLY | os.O_DIRECTORY)
        self.stack.callback(os.close, self.stage_fd)
        self.package_fd = os.open(self.packages, os.O_RDONLY | os.O_DIRECTORY)
        self.stack.callback(os.close, self.package_fd)
        self.lifecycle, self.model, self.channel = _Lifecycle(), _Model(), _Channel(self.stage)
        self.candidate = CandidatePoolEntry('example/project', 7, 'Synthetic fix',
                                            'https://github.com/example/project/issues/7', 0.9, 1)
        self.files = (TextFile('src/value.py', BROKEN.decode(), 'source'),) + tuple(
            TextFile(p, b.decode(), 'runner' if p.startswith('runner/') else 'test')
            for p, b in IMMUTABLE.items()
        )
        self.plan = TestPlan(TestCommand('synthetic-repro', 5.0),
                             (TestCommand('synthetic-suite', 5.0),))
        self.target = DraftTarget('https://github.com', 'example/project', 'example/fork',
                                  'refs/heads/fix', 'b' * 40, 'refs/heads/main', 'a' * 40)
        self.policy = SandboxPolicy('synthetic:declared', 1.0, 128,
                                    attempt_timeout_seconds=60.0, operation_timeout_seconds=10.0,
                                    max_commands=8, max_output_bytes=16_384)
        return self

    def workflow(self, **changes):
        args = dict(
            base_commit='a' * 40, plan=self.plan, files=self.files,
            editable_paths=('src/value.py',), context_paths=tuple(f.path for f in self.files),
            lifecycle=self.lifecycle, channel=self.channel, model=self.model,
            staging_parent_fd=self.stage_fd, package_parent_fd=self.package_fd,
            package_name='review', target=self.target, title='Synthetic local proposal',
            sandbox_policy=self.policy, caller_declared_status='candidate', execution_kind='synthetic',
            options=EventPackageOptions(), limits=SnapshotLimits(),
        )
        args.update(changes)
        return EventCandidatePackage(self.candidate, **args)

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


class EventCandidatePackageTests(unittest.TestCase):
    def test_whole_synthetic_candidate_bytes_to_real_package_readback(self):
        with _Fixture() as f:
            workflow = f.workflow()

            def mutate_originals():
                f.candidate.repo = 'changed/project'
                f.candidate.title = 'Changed after freeze'
                object.__setattr__(f.files[0], 'text', 'changed source\n')
                object.__setattr__(f.plan, 'relevant_suite', (TestCommand('changed-command', 5.0),))
                object.__setattr__(f.target, 'head_ref', 'refs/heads/changed')
                object.__setattr__(f.policy, 'image', 'changed:policy')

            f.lifecycle.on_create = mutate_originals
            result = workflow.run()
            self.assertEqual(result.status, 'pending_human_review')
            self.assertTrue(result.candidate.attempt.success)
            self.assertTrue(result.candidate.attempt.cleanup_confirmed)
            self.assertEqual(result.candidate.attempt.model_calls, 1)
            self.assertEqual(len(f.model.calls), 1)
            self.assertEqual([t.command for t in f.channel.calls],
                             ['synthetic-suite', 'synthetic-repro', 'synthetic-repro', 'synthetic-suite'])
            self.assertEqual(len({t.receipt.stage_name for t in f.channel.calls}), 4)
            self.assertEqual([x[0] for x in f.lifecycle.calls], ['create', 'start', 'remove'])
            loaded = package.read_review_package(parent_fd=f.package_fd, name='review')
            self.assertEqual(loaded.request.snapshot_json, result.request.snapshot_json)
            self.assertEqual(loaded.report.snapshot_json, result.candidate.report.snapshot_json)
            self.assertIn("+value = 'fixed'", loaded.diff)
            self.assertEqual(loaded.request.data['target']['head_ref'], 'refs/heads/fix')
            self.assertEqual(loaded.report.evidence['execution_kind'], 'synthetic')
            self.assertEqual(result.human_approval, 'pending')
            self.assertFalse(result.submitted)
            result.candidate.attempt.notes.append('caller mutation')
            self.assertNotIn('caller mutation', workflow.outcome.candidate.attempt.notes)
            with self.assertRaises(ValueError):
                workflow.run()
            self.assertEqual(len(f.channel.calls), 4)

    def test_unknown_error_missing_model_and_report_failure_do_not_export(self):
        for mode in ('unknown', 'error', 'missing-model', 'report-failure'):
            with self.subTest(mode=mode), _Fixture() as f:
                f.channel.mode = mode
                workflow = f.workflow(**({'model': None} if mode == 'missing-model' else {}))
                original = candidate_attempt.build_review_report

                def report(attempt, *args, **kwargs):
                    if mode == 'report-failure' and attempt.success:
                        raise ValueError('synthetic report construction failure')
                    return original(attempt, *args, **kwargs)

                with mock.patch.object(candidate_attempt, 'build_review_report', side_effect=report), \
                        mock.patch.object(package, 'export_review_package') as export:
                    outcome = workflow.run()
                    export.assert_not_called()
                self.assertIsNone(outcome.receipt)
                self.assertEqual(list(f.packages.iterdir()), [])
                if mode == 'missing-model':
                    self.assertEqual(outcome.status, 'input_failed')
                    self.assertEqual(f.lifecycle.calls, [])
                elif mode == 'report-failure':
                    self.assertEqual(outcome.status, 'report_failed')
                    self.assertTrue(outcome.candidate.attempt.success)
                    self.assertIsNone(outcome.candidate.report)
                else:
                    self.assertEqual(outcome.status, 'failed')
                    self.assertFalse(outcome.candidate.attempt.success)
                    self.assertEqual(len(f.model.calls), 0)
                with self.assertRaises(ValueError):
                    workflow.run()

    def test_unconfirmed_cleanup_retains_failed_attempt_without_export(self):
        with _Fixture() as f:
            f.lifecycle.remove_error = OSError('synthetic cleanup uncertainty')
            workflow = f.workflow()
            with mock.patch.object(package, 'export_review_package') as export:
                outcome = workflow.run()
                export.assert_not_called()
            self.assertEqual(outcome.status, 'failed')
            self.assertFalse(outcome.candidate.attempt.success)
            self.assertFalse(outcome.candidate.attempt.cleanup_confirmed)
            self.assertEqual(len(f.model.calls), 1)
            self.assertEqual([x[0] for x in f.lifecycle.calls].count('remove'), 1)
            self.assertEqual(list(f.packages.iterdir()), [])
            self.assertIsNone(outcome.request)

    def test_partial_and_post_marker_package_failures_retained_without_replay(self):
        for fail_at in ('manifest.json', 'COMPLETE'):
            with self.subTest(fail_at=fail_at), _Fixture() as f:
                workflow = f.workflow()
                write = package._write_file

                def interrupted_write(root, name, data, **kwargs):
                    if name == fail_at and name != 'COMPLETE':
                        raise OSError('synthetic partial export failure')
                    write(root, name, data, **kwargs)
                    if name == fail_at:
                        raise OSError('synthetic ambiguity after completion')

                with mock.patch.object(package, '_write_file', side_effect=interrupted_write):
                    outcome = workflow.run()
                self.assertEqual(outcome.status, 'package_failed')
                self.assertTrue(outcome.candidate.attempt.cleanup_confirmed)
                self.assertIsNotNone(outcome.request)
                self.assertIsInstance(outcome.error, package.ReviewPackageExportError)
                self.assertTrue((f.packages / 'review' / 'request.json').is_file())
                if fail_at == 'manifest.json':
                    self.assertEqual(outcome.receipt.outcome, 'incomplete')
                    self.assertTrue(outcome.receipt.directory_created)
                    self.assertEqual(outcome.receipt.attempted_file, 'manifest.json')
                    self.assertFalse((f.packages / 'review' / 'COMPLETE').exists())
                    with self.assertRaises(package.ReviewPackageError):
                        package.read_review_package(parent_fd=f.package_fd, name='review')
                else:
                    self.assertEqual(outcome.receipt.outcome, 'complete')
                    self.assertEqual(package.read_review_package(parent_fd=f.package_fd, name='review')
                                     .request.snapshot_json, outcome.request.snapshot_json)
                before = {p.name: p.read_bytes() for p in (f.packages / 'review').iterdir()}
                with self.assertRaises(ValueError):
                    workflow.run()
                self.assertEqual({p.name: p.read_bytes() for p in (f.packages / 'review').iterdir()}, before)
                self.assertEqual(len(f.channel.calls), 4)
                self.assertEqual(len(f.model.calls), 1)

    def test_cancellation_propagates_at_injected_seams_and_export_consumes_once(self):
        for seam in ('channel', 'model', 'remove', 'package'):
            with self.subTest(seam=seam), _Fixture() as f:
                cancellation = KeyboardInterrupt('synthetic cancellation')
                if seam == 'channel':
                    f.channel.error = cancellation
                elif seam == 'model':
                    f.model.error = cancellation
                elif seam == 'remove':
                    f.lifecycle.remove_error = cancellation
                workflow = f.workflow()
                write = package._write_file

                def cancel_export(root, name, data, **kwargs):
                    if seam == 'package' and name == 'manifest.json':
                        raise cancellation
                    return write(root, name, data, **kwargs)

                with mock.patch.object(package, '_write_file', side_effect=cancel_export):
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        workflow.run()
                self.assertIs(raised.exception, cancellation)
                self.assertEqual(workflow.outcome.status, 'interrupted')
                self.assertEqual([x[0] for x in f.lifecycle.calls].count('remove'), 1)
                if seam == 'package':
                    self.assertEqual(workflow.outcome.receipt.outcome, 'incomplete')
                    self.assertTrue((f.packages / 'review' / 'request.json').is_file())
                else:
                    self.assertEqual(list(f.packages.iterdir()), [])
                counts = len(f.channel.calls), len(f.model.calls), len(f.lifecycle.calls)
                with self.assertRaises(ValueError):
                    workflow.run()
                self.assertEqual(counts, (len(f.channel.calls), len(f.model.calls), len(f.lifecycle.calls)))

    def test_invalid_declarations_and_destination_capabilities_fail_before_execution(self):
        for case in ('name', 'existing', 'package-fd', 'stage-fd', 'manifest',
                     'editable-test', 'base', 'options', 'files-type'):
            with self.subTest(case=case), _Fixture() as f:
                changes = {}
                if case == 'name':
                    changes['package_name'] = ''
                elif case == 'existing':
                    (f.packages / 'review').mkdir(mode=0o700)
                elif case == 'package-fd':
                    changes['package_parent_fd'] = -1
                elif case == 'stage-fd':
                    changes['staging_parent_fd'] = -1
                elif case == 'manifest':
                    changes['manifest'] = (ManifestFile('src/value.py', 'source'),)
                elif case == 'editable-test':
                    changes['editable_paths'] = ('tests/repro.txt',)
                elif case == 'base':
                    changes['target'] = replace(f.target, base_commit='c' * 40)
                elif case == 'options':
                    changes['options'] = replace(EventPackageOptions(), max_dispatches=0)
                else:
                    changes['files'] = list(f.files)
                if case == 'files-type':
                    with self.assertRaises(ValueError):
                        f.workflow(**changes)
                else:
                    workflow = f.workflow(**changes)
                    outcome = workflow.run()
                    self.assertEqual(outcome.status, 'input_failed')
                    self.assertIsNotNone(outcome.error)
                    with self.assertRaises(ValueError):
                        workflow.run()
                self.assertEqual(f.lifecycle.calls, [])
                self.assertEqual(f.channel.calls, [])
                self.assertEqual(f.model.calls, [])

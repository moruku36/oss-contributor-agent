'''Offline synthetic bytes only: NEVER execute candidate source or commands.'''
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

from src.agent.core import ModelReply, TestCommand, TestPlan
from src.agent.event_candidate_package import EventCandidatePackage, EventPackageOptions
from src.agent.filesystem_staging import StageReceipt, StagedFile
from src.agent.registered_event_channel import (
    CommandInstrumentation, RegisteredChannelError, RegisteredInstrumentedChannel,
)
from src.review import review_package as package
from src.agent.snapshot_workspace import SnapshotLimits, TextFile
from src.agent.staged_runner_bridge import DispatchTicket, receipt_digest
from src.collector.digest import CandidatePoolEntry
from src.review.draft_submission import DraftTarget
from src.sandbox.docker_manager import SandboxPolicy

NL = chr(10)
Q = chr(39)
BROKEN = ('value = ' + Q + 'broken' + Q + NL).encode()
FIXED = ('value = ' + Q + 'fixed' + Q + NL).encode()
IMMUTABLE = {
    'tests/repro.txt': b'synthetic reproduction fixture' + NL.encode(),
    'tests/suite.txt': b'synthetic suite fixture' + NL.encode(),
    'runner/events.txt': b'synthetic runner fixture; never executed' + NL.encode(),
}
PATCH = NL.join(['--- a/src/value.py', '+++ b/src/value.py', '@@ -1 +1 @@',
                 '-value = ' + Q + 'broken' + Q, '+value = ' + Q + 'fixed' + Q, ''])
IDS = ('fixture.case',)
KW = dict(timeout_seconds=5, max_output_bytes=16_384, max_wire_bytes=65_536)


class _Case:
    failureException = AssertionError

    def __init__(self, test_id='fixture.case'):
        self._id = test_id

    def id(self):
        return self._id

    def shortDescription(self):
        return None

    def __str__(self):
        return self._id


class _Cancel(BaseException):
    pass


class _Str(str):
    pass


def _exc():
    try:
        raise AssertionError('synthetic failure')
    except AssertionError:
        return sys.exc_info()


def _ticket(attempt=1, generation=1, command='synthetic-repro', stage=None, dispatch=None):
    '''Self-contained synthetic ticket built from the actual dataclass fields.'''
    attempt_id = '%032x' % attempt
    stage_name = stage or ('stage-%d-%d' % (attempt, generation))
    data = b'synthetic staged bytes ' + stage_name.encode()
    receipt = StageReceipt(
        attempt_id, stage_name,
        (StagedFile('src/value.py', 'source', hashlib.sha256(data).hexdigest(), len(data)),),
        len(data))
    dispatch_id = '%032x' % (dispatch if dispatch is not None else attempt * 1000 + generation)
    return DispatchTicket(attempt_id, dispatch_id, generation, receipt,
                          receipt_digest(receipt), command)


class _Lifecycle:
    def __init__(self):
        self.calls = []

    def create(self, attempt_id, *, policy, timeout_seconds):
        self.calls.append(('create', attempt_id))

    def start(self, attempt_id, **bounds):
        self.calls.append(('start', attempt_id))

    def remove(self, attempt_id, **bounds):
        self.calls.append(('remove', attempt_id))


class _Model:
    def __init__(self):
        self.calls = []

    def propose(self, request, **bounds):
        self.calls.append((request, bounds))
        return ModelReply(PATCH, 12, 'synthetic-fixture', 'byte-patch-fixture')


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
        self.lifecycle, self.model = _Lifecycle(), _Model()
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
            lifecycle=self.lifecycle, model=self.model,
            staging_parent_fd=self.stage_fd, package_parent_fd=self.package_fd,
            package_name='review', target=self.target, title='Synthetic local proposal',
            sandbox_policy=self.policy, caller_declared_status='candidate',
            execution_kind='synthetic', options=EventPackageOptions(), limits=SnapshotLimits(),
        )
        args.update(changes)
        return EventCandidatePackage(self.candidate, **args)

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def _stage_driver(stage_path, seen):
    '''Manually drives real callbacks from staged FILE BYTES only.'''
    def driver(ticket, result, stdout, stderr, *, timeout_seconds):
        staged = stage_path / ticket.receipt.stage_name
        actual = {p.relative_to(staged).as_posix(): p.read_bytes()
                  for p in staged.rglob('*') if p.is_file()}
        value = actual.pop('src/value.py', None)
        if value not in (BROKEN, FIXED) or actual != IMMUTABLE:
            raise AssertionError('whole staged snapshot bytes differ')
        seen.append((ticket.command, value))
        failed = ticket.command == 'synthetic-repro' and value == BROKEN
        case = _Case()
        result.startTest(case)
        if failed:
            result.addFailure(case, _exc())
        else:
            result.addSuccess(case)
        result.stopTest(case)
        stdout.write('synthetic log')
        return 1 if failed else 0
    return driver


def _simple_driver(calls, ids=IDS, skip=()):
    def driver(ticket, result, stdout, stderr, *, timeout_seconds):
        calls.append(ticket.generation)
        for test_id in ids:
            case = _Case(test_id)
            result.startTest(case)
            result.addSuccess(case)
            result.stopTest(case)
        for test_id in skip:
            case = _Case(test_id)
            result.startTest(case)
            result.addSkip(case, 'synthetic skip')
            result.stopTest(case)
        stdout.write('ok')
        return 0
    return driver


def _registry(driver, ids=IDS):
    return tuple(CommandInstrumentation(c, driver, ids)
                 for c in ('synthetic-repro', 'synthetic-suite'))


class RegisteredEventChannelTests(unittest.TestCase):
    def test_registered_callbacks_to_real_package_readback(self):
        with _Fixture() as fx:
            seen = []
            channel = RegisteredInstrumentedChannel(
                _registry(_stage_driver(fx.stage, seen)), max_dispatches=8)
            outcome = fx.workflow(channel=channel).run()
            self.assertEqual(outcome.status, 'pending_human_review')
            self.assertTrue(outcome.candidate.attempt.success)
            self.assertTrue(outcome.candidate.attempt.cleanup_confirmed)
            self.assertEqual(seen, [
                ('synthetic-suite', BROKEN), ('synthetic-repro', BROKEN),
                ('synthetic-repro', FIXED), ('synthetic-suite', FIXED)])
            self.assertEqual(channel.dispatched, 4)
            kinds = [k for k, _ in fx.lifecycle.calls]
            self.assertEqual(kinds[0], 'create')
            self.assertEqual(kinds[-1], 'remove')
            self.assertEqual(len(fx.model.calls), 1)
            loaded = package.read_review_package(parent_fd=fx.package_fd, name='review')
            self.assertEqual(loaded.diff, outcome.candidate.attempt.diff)
            self.assertEqual(loaded.request.snapshot_json, outcome.request.snapshot_json)
            self.assertEqual(loaded.report.snapshot_json, outcome.candidate.report.snapshot_json)
            self.assertEqual(outcome.human_approval, 'pending')
            self.assertFalse(outcome.submitted)

    def test_registration_validation(self):
        calls = []
        drv = _simple_driver(calls)
        ok = CommandInstrumentation('cmd', drv, IDS)
        bad_ids = [
            (), ('a', 'a'), tuple('t%d' % i for i in range(129)), ('a' + chr(0),), (1,),
            ('a' + NL,), ('a' + chr(9),), ('a' + chr(127),), (chr(0xD800),),
            (_Str('sub'),), ('',), ('x' * 100_000,),
        ]
        bad_regs = [
            [ok], (), (object(),), (ok, CommandInstrumentation('cmd', drv, IDS)),
            (CommandInstrumentation('', drv, IDS),),
            (CommandInstrumentation('  ', drv, IDS),),
            (CommandInstrumentation('a' + chr(0), drv, IDS),),
            (CommandInstrumentation('x' * 4097, drv, IDS),),
            (CommandInstrumentation(('cmd',), drv, IDS),),
            (CommandInstrumentation('cmd', None, IDS),),
            (CommandInstrumentation('cmd', drv, list(IDS)),),
        ] + [(CommandInstrumentation('cmd', drv, ids),) for ids in bad_ids]
        for regs in bad_regs:
            with self.assertRaises(RegisteredChannelError, msg=repr(regs)[:60]):
                RegisteredInstrumentedChannel(regs, max_dispatches=2)
        for bad in (0, 65, True, '1', None):
            with self.assertRaises(RegisteredChannelError):
                RegisteredInstrumentedChannel((ok,), max_dispatches=bad)
        for name, value in (('detail_limit', 0), ('detail_limit', 257),
                            ('max_subtests', 0), ('max_subtests', 129),
                            ('clock', None)):
            with self.assertRaises(RegisteredChannelError):
                RegisteredInstrumentedChannel((ok,), max_dispatches=2, **{name: value})
        self.assertEqual(calls, [])
        channel = RegisteredInstrumentedChannel((ok,), max_dispatches=2)
        self.assertEqual(channel.dispatched, 0)

    def test_coverage_must_match_declared_set(self):
        calls = []
        both_ids = ('fixture.case', 'fixture.other')
        missing = RegisteredInstrumentedChannel(
            (CommandInstrumentation('synthetic-repro', _simple_driver(calls), both_ids),),
            max_dispatches=2)
        with self.assertRaises(RegisteredChannelError):
            missing.dispatch(_ticket(generation=1), **KW)
        with self.assertRaises(RegisteredChannelError):
            missing.dispatch(_ticket(generation=2), **KW)
        self.assertEqual(calls, [1])
        extra = RegisteredInstrumentedChannel(
            (CommandInstrumentation('synthetic-repro', _simple_driver([], ids=both_ids), IDS),),
            max_dispatches=2)
        with self.assertRaises(RegisteredChannelError):
            extra.dispatch(_ticket(), **KW)
        mixed = RegisteredInstrumentedChannel(
            (CommandInstrumentation(
                'synthetic-repro', _simple_driver([], ids=('fixture.skip',), skip=('fixture.case',)),
                ('fixture.case', 'fixture.skip')),),
            max_dispatches=2)
        self.assertIs(type(mixed.dispatch(_ticket(), **KW)), bytes)

    def test_unknown_command_and_bad_bounds_never_reach_driver(self):
        calls = []
        other = RegisteredInstrumentedChannel(
            (CommandInstrumentation('other-command', _simple_driver(calls), IDS),), max_dispatches=2)
        with self.assertRaises(RegisteredChannelError):
            other.dispatch(_ticket(), **KW)
        bad = [dict(max_output_bytes=0), dict(max_wire_bytes=True), dict(max_wire_bytes=-1),
               dict(timeout_seconds=0), dict(timeout_seconds=float('nan')),
               dict(timeout_seconds='x'), dict(timeout_seconds=99_999),
               dict(max_output_bytes=524_289)]
        for change in bad:
            channel = RegisteredInstrumentedChannel(
                (CommandInstrumentation('synthetic-repro', _simple_driver(calls), IDS),),
                max_dispatches=2)
            with self.assertRaises(RegisteredChannelError, msg=repr(change)):
                channel.dispatch(_ticket(), **{**KW, **change})
            with self.assertRaises(RegisteredChannelError):
                channel.dispatch(_ticket(), **KW)  # an invalid request poisons too
        self.assertEqual(calls, [])
        malformed = RegisteredInstrumentedChannel(
            (CommandInstrumentation('synthetic-repro', _simple_driver(calls), IDS),), max_dispatches=2)
        with self.assertRaises(RegisteredChannelError):
            malformed.dispatch(object(), **KW)
        self.assertEqual(calls, [])

    def test_generation_replay_attempt_and_budget(self):
        first = _ticket(attempt=1, generation=1)
        second = _ticket(attempt=1, generation=2)
        self.assertEqual(first.attempt_id, second.attempt_id)
        self.assertNotEqual(receipt_digest(first.receipt), receipt_digest(second.receipt))
        calls = []

        def make(limit=8):
            return RegisteredInstrumentedChannel(
                _registry(_simple_driver(calls)), max_dispatches=limit)
        straight = make()
        self.assertIs(type(straight.dispatch(first, **KW)), bytes)
        self.assertIs(type(straight.dispatch(second, **KW)), bytes)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(straight.dispatched, 2)
        calls.clear()
        skipped = make()
        with self.assertRaises(RegisteredChannelError):
            skipped.dispatch(second, **KW)
        self.assertEqual(calls, [])
        replay = make()
        replay.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            replay.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            replay.dispatch(second, **KW)
        self.assertEqual(calls, [1])
        reused = make()
        reused.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            reused.dispatch(_ticket(attempt=1, generation=2, dispatch=first.dispatch_id and int(first.dispatch_id, 16)), **KW)
        calls.clear()
        budget = make(1)
        budget.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            budget.dispatch(second, **KW)
        self.assertEqual(calls, [1])
        calls.clear()
        foreign = _ticket(attempt=2, generation=2)
        mixed = make()
        mixed.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            mixed.dispatch(foreign, **KW)
        self.assertEqual(calls, [1])

    def test_cancellation_reentry_deadline_and_poison(self):
        first, second = _ticket(generation=1), _ticket(generation=2)
        calls = []

        def cancelling(ticket, result, stdout, stderr, *, timeout_seconds):
            calls.append('cancel')
            raise _Cancel()
        channel = RegisteredInstrumentedChannel(_registry(cancelling), max_dispatches=4)
        with self.assertRaises(_Cancel):
            channel.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            channel.dispatch(second, **KW)
        self.assertEqual(calls, ['cancel'])
        inner = []
        holder = []
        good = _simple_driver([])

        def reentrant(ticket, result, stdout, stderr, *, timeout_seconds):
            try:
                holder[0].dispatch(ticket, **KW)
            except RegisteredChannelError as exc:
                inner.append(exc)
            return good(ticket, result, stdout, stderr, timeout_seconds=timeout_seconds)
        reenter = RegisteredInstrumentedChannel(_registry(reentrant), max_dispatches=4)
        holder.append(reenter)
        with self.assertRaises(RegisteredChannelError):
            reenter.dispatch(first, **KW)
        self.assertEqual(len(inner), 1)
        with self.assertRaises(RegisteredChannelError):
            reenter.dispatch(second, **KW)

        def failing(ticket, result, stdout, stderr, *, timeout_seconds):
            raise RuntimeError('synthetic driver failure')
        broken = RegisteredInstrumentedChannel(_registry(failing), max_dispatches=4)
        with self.assertRaises(RegisteredChannelError):
            broken.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            broken.dispatch(second, **KW)
        now = [0.0]

        def slow(ticket, result, stdout, stderr, *, timeout_seconds):
            now[0] = 100.0
            return good(ticket, result, stdout, stderr, timeout_seconds=timeout_seconds)
        late = RegisteredInstrumentedChannel(
            _registry(slow), max_dispatches=4, clock=lambda: now[0])
        with self.assertRaises(RegisteredChannelError):
            late.dispatch(first, **KW)
        with self.assertRaises(RegisteredChannelError):
            late.dispatch(second, **KW)


if __name__ == '__main__':
    unittest.main()

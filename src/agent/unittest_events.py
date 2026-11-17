'''Collect unittest lifecycle callbacks into a bounded RunnerEventCapture.

This module never runs tests itself. A trusted runner must call the callbacks
of EventTestResult; nothing here authenticates that runner or isolates the
candidate. Any lifecycle misuse or unsupported callback poisons the result
permanently, and finish() then refuses to return a capture.

The capture reflects callback completion only. unittest's shouldStop flag is
not consulted, so an externally requested stop is not recorded in the capture.
'''
from __future__ import annotations

import unittest
from typing import NoReturn

from src.agent.runner_events import (
    RunCompleted, RunnerEventCapture, RunnerEventError, TestEvent,
    reduce_runner_events,
)

MAX_TESTS_CEILING = 128
MAX_DETAIL_CEILING = 256


def _clean(value: object, limit: int) -> str:
    '''Return control-free text, truncated on a UTF-8 boundary.

    str(value) is called on trusted callback data and may itself allocate an
    arbitrarily large string; there is no hard memory enforcement here. After
    that call, text is sliced to at most limit characters BEFORE any join or
    encoding, so every further copy is bounded. Each character occupies at
    least one byte, so limit characters always cover limit bytes.
    '''
    try:
        text = str(value)
    except Exception:
        text = 'unprintable'
    text = text[:limit]
    text = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in text)
    return text.encode('utf-8', 'replace')[:limit].decode('utf-8', 'ignore').strip()


class EventTestResult(unittest.TestResult):
    '''unittest result that records exactly one final event per started test.

    Failure details are the exception type name plus a sanitized bounded
    message; tracebacks are never read or retained. Subtests, expected
    failures and unexpected successes are unsupported and fail closed.
    '''

    def __init__(self, max_tests: int, detail_limit: int) -> None:
        super().__init__()
        for value, ceiling in ((max_tests, MAX_TESTS_CEILING),
                               (detail_limit, MAX_DETAIL_CEILING)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise RunnerEventError('bounded positive exact int required')
        self._ev_max = max_tests
        self._ev_detail = detail_limit
        self._ev_events: list[TestEvent] = []
        self._ev_ids: set[str] = set()
        self._ev_active: str | None = None
        self._ev_resulted = False
        self._ev_poison: str | None = None
        self._ev_finished = False
        self._ev_counts = {'passed': 0, 'assertion_failed': 0, 'error': 0, 'skipped': 0}

    def _fail(self, message: str) -> NoReturn:
        if self._ev_poison is None:
            self._ev_poison = message
        raise RunnerEventError(message)

    def _guard(self) -> None:
        if self._ev_poison is not None:
            raise RunnerEventError(self._ev_poison)
        if self._ev_finished:
            self._fail('callback after finish')

    def _identity(self, test: object) -> str:
        try:
            tid = test.id()  # type: ignore[attr-defined]
        except Exception:
            self._fail('test identity unavailable')
        try:  # Reuse the reducer's identity rules before retaining anything.
            reduce_runner_events(RunnerEventCapture(
                (TestEvent(1, tid, 'passed'), RunCompleted(2, 1, 1, 0, 0, 0, 0, 0)),
                True, False, True))
        except RunnerEventError:
            self._fail('invalid test identity')
        return tid

    def _detail(self, err: object) -> str:
        if type(err) is not tuple or len(err) < 2:
            self._fail('exc_info tuple required')
        name = _clean(getattr(err[0], '__name__', 'Error'), self._ev_detail) or 'Error'
        message = _clean(err[1], self._ev_detail)
        return _clean(name + ': ' + message if message else name, self._ev_detail) or 'Error'

    def _record(self, test: object, outcome: str, detail: str) -> None:
        self._guard()
        if self._ev_active is None or self._ev_resulted:
            self._fail('outcome without a single active test')
        if self._identity(test) != self._ev_active:
            self._fail('outcome for a different test')
        self._ev_events.append(TestEvent(len(self._ev_events) + 1, self._ev_active, outcome, detail))
        self._ev_counts[outcome] += 1
        self._ev_resulted = True

    def startTest(self, test) -> None:
        self._guard()
        if self._ev_active is not None:
            self._fail('nested startTest')
        if len(self._ev_ids) >= self._ev_max:
            self._fail('too many tests')
        tid = self._identity(test)
        if tid in self._ev_ids:
            self._fail('duplicate test identity')
        self._ev_ids.add(tid)
        self._ev_active, self._ev_resulted = tid, False
        self.testsRun += 1

    def stopTest(self, test) -> None:
        self._guard()
        if self._ev_active is None or self._identity(test) != self._ev_active:
            self._fail('stopTest does not match the active test')
        if not self._ev_resulted:
            self._fail('test stopped without an outcome')
        self._ev_active = None

    def addSuccess(self, test) -> None:
        self._record(test, 'passed', '')

    def addFailure(self, test, err) -> None:
        self._record(test, 'assertion_failed', self._detail(err))

    def addError(self, test, err) -> None:
        self._record(test, 'error', self._detail(err))

    def addSkip(self, test, reason) -> None:
        self._record(test, 'skipped', _clean(reason, self._ev_detail))

    def addSubTest(self, test, subtest, err) -> None:
        self._fail('subtests are unsupported')

    def addExpectedFailure(self, test, err) -> None:
        self._fail('expected failures are unsupported')

    def addUnexpectedSuccess(self, test) -> None:
        self._fail('unexpected successes are unsupported')

    def wasSuccessful(self) -> bool:
        '''Conservative success: never True after a failure, error or misuse.

        The inherited failures/errors lists are not populated by this class,
        so this method consults its own counts, poison and active-test state.
        '''
        if self._ev_poison is not None or self._ev_active is not None:
            return False
        if self._ev_counts['assertion_failed'] or self._ev_counts['error']:
            return False
        return bool(super().wasSuccessful())

    def finish(self) -> RunnerEventCapture:
        '''Return the verified capture once; any prior misuse raises.'''
        if self._ev_poison is not None:
            raise RunnerEventError(self._ev_poison)
        if self._ev_finished:
            self._fail('finish may be called once')
        if self._ev_active is not None:
            self._fail('a test is still active')
        self._ev_finished = True
        c = self._ev_counts
        done = RunCompleted(
            len(self._ev_events) + 1, c['passed'] + c['assertion_failed'] + c['error'],
            c['passed'], c['assertion_failed'], c['error'], c['skipped'], 0, 0)
        capture = RunnerEventCapture(tuple(self._ev_events) + (done,), True, False, True)
        try:
            reduce_runner_events(capture)
        except RunnerEventError as exc:
            self._fail(str(exc))
        return capture

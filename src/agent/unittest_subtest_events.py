'''Aggregate unittest subtest callbacks into one final event per parent test.

This extends the round-1 EventTestResult recorder without modifying it. It never
runs tests; a trusted runner must drive the callbacks. Nothing here authenticates
that runner or isolates the candidate. Subtests are never reported as standalone
tests: every addSubTest callback, plus any top-level outcome callbacks, is
folded into a single parent event emitted from stopTest.

Because unittest calls neither addFailure nor addError for a failed subtest, the
parent record is deferred to stopTest. Failed subtests are classified with
issubclass(err[0], parent.failureException): assertion failures aggregate as
assertion_failed, everything else as error, and error outranks assertion.
Subtest skips (addSkip called with the subtest object) fail closed, as do
expected failures and unexpected successes (inherited). A subtest callback after
a top-level success or skip, or one whose identity equals the parent identity,
fails closed. Tracebacks are never read or retained. Any misuse poisons the
result permanently.
'''
from __future__ import annotations

from src.agent.runner_events import RunnerEventError
from src.agent.unittest_events import EventTestResult, _clean

MAX_SUBTESTS_CEILING = 128
_EXTRA_TOP_LEVEL = 8


def _is_exc_class(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, BaseException)


class SubtestEventResult(EventTestResult):
    '''EventTestResult that folds subtest callbacks into one parent event.

    max_subtests bounds distinct subtest identities per parent test. A repeated
    subtest identity within one parent is rejected, which is conservative:
    unittest could legitimately produce identical subtest ids.
    '''

    def __init__(self, max_tests: int, detail_limit: int, max_subtests: int) -> None:
        if type(max_subtests) is not int or not 1 <= max_subtests <= MAX_SUBTESTS_CEILING:
            raise RunnerEventError('bounded positive exact int required')
        super().__init__(max_tests, detail_limit)
        self._st_max = max_subtests
        self._st_reset()

    def _st_reset(self) -> None:
        self._st_ids: set[str] = set()
        self._st_asserts: list[str] = []
        self._st_errors: list[str] = []
        self._st_skip: str | None = None
        self._st_success = False

    def _st_check(self, test: object) -> None:
        self._guard()
        if self._ev_active is None:
            self._fail('callback without an active test')
        if self._identity(test) != self._ev_active:
            self._fail('callback for a different test')

    def _st_add(self, bucket: list[str], detail: str) -> None:
        if len(self._st_asserts) + len(self._st_errors) >= self._st_max + _EXTRA_TOP_LEVEL:
            self._fail('too many failure records')
        bucket.append(detail)

    def startTest(self, test) -> None:
        super().startTest(test)
        self._st_reset()

    def addSubTest(self, test, subtest, err) -> None:
        self._st_check(test)
        if self._st_success or self._st_skip is not None:
            self._fail('subtest after top-level outcome')
        sid = self._identity(subtest)
        if sid == self._ev_active:
            self._fail('subtest identity equals parent identity')
        if sid in self._st_ids:
            self._fail('duplicate subtest identity')
        if len(self._st_ids) >= self._st_max:
            self._fail('too many subtests')
        self._st_ids.add(sid)
        if err is None:
            return
        detail = self._detail(err)
        kind = err[0]
        expected = getattr(test, 'failureException', None)
        if not _is_exc_class(kind) or not _is_exc_class(expected):
            self._fail('exception classes required for classification')
        bucket = self._st_asserts if issubclass(kind, expected) else self._st_errors
        self._st_add(bucket, detail)

    def addSuccess(self, test) -> None:
        self._st_check(test)
        if self._st_success or self._st_skip is not None:
            self._fail('multiple top-level outcomes')
        self._st_success = True

    def addFailure(self, test, err) -> None:
        self._st_check(test)
        self._st_add(self._st_asserts, self._detail(err))

    def addError(self, test, err) -> None:
        self._st_check(test)
        self._st_add(self._st_errors, self._detail(err))

    def addSkip(self, test, reason) -> None:
        self._st_check(test)
        if self._st_skip is not None or self._st_success:
            self._fail('multiple top-level outcomes')
        self._st_skip = _clean(reason, self._ev_detail)

    def stopTest(self, test) -> None:
        self._st_check(test)
        detail = ''
        if self._st_errors:
            outcome = 'error'
        elif self._st_asserts:
            outcome = 'assertion_failed'
        elif self._st_skip is not None:
            outcome, detail = 'skipped', self._st_skip
        elif self._st_success:
            outcome = 'passed'
        else:
            self._fail('test stopped without an outcome')
        if outcome in ('error', 'assertion_failed'):
            joined = ' | '.join(self._st_errors + self._st_asserts)
            detail = _clean(joined, self._ev_detail) or 'Error'
        self._record(test, outcome, detail)
        super().stopTest(test)

    def wasSuccessful(self) -> bool:
        '''False while any failed subtest or failure is pending, even before stop.'''
        if self._st_asserts or self._st_errors:
            return False
        return super().wasSuccessful()

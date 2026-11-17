import sys
import unittest

from src.agent.runner_events import RunnerEventCapture, RunnerEventError, reduce_runner_events
from src.agent.unittest_events import EventTestResult


class Ident:
    '''Identity-only fixture. Not a TestCase, so discovery never loads it.'''

    def __init__(self, ident):
        self._ident = ident

    def id(self):
        return self._ident


def case(name):
    return Ident('tests.fixture.Inert.' + name)


def bad_case():
    return Ident('bad' + chr(0) + 'id')


def drive(result, test, outcome=None, *args):
    result.startTest(test)
    if outcome:
        getattr(result, outcome)(test, *args)
    result.stopTest(test)


def exc(kind, message):
    try:
        raise kind(message)
    except kind:
        return sys.exc_info()


class EventTestResultTests(unittest.TestCase):
    def test_reducer_integration_for_all_outcomes(self):
        r = EventTestResult(8, 64)
        drive(r, case('test_a'), 'addSuccess')
        self.assertTrue(r.wasSuccessful())
        drive(r, case('test_b'), 'addFailure', exc(AssertionError, 'x != y' + chr(10) + 'second'))
        self.assertFalse(r.wasSuccessful())
        drive(r, case('test_c'), 'addError', exc(ValueError, 'bad'))
        drive(r, case('test_d'), 'addSkip', 'why')
        self.assertFalse(r.wasSuccessful())
        cap = r.finish()
        self.assertFalse(r.wasSuccessful())
        self.assertIs(type(cap), RunnerEventCapture)
        self.assertEqual((cap.complete, cap.truncated, cap.outcome_known), (True, False, True))
        s = reduce_runner_events(cap)
        self.assertEqual((s.tests_run, s.passed, s.test_errors, s.skipped), (3, 1, 1, 1))
        self.assertEqual(len(s.assertions), 1)
        detail = cap.events[1].detail
        self.assertTrue(detail.startswith('AssertionError: x != y second'))
        self.assertNotIn('Traceback', detail)
        self.assertNotIn('File', detail)

    def test_constructor_and_detail_bounds(self):
        for bad in ((True, 8), (0, 8), (129, 8), ('8', 8), (8, 0), (8, 257), (8, True), (8, 1.0)):
            with self.assertRaises(RunnerEventError):
                EventTestResult(*bad)
        r = EventTestResult(2, 16)
        drive(r, case('test_a'), 'addError', exc(ValueError, 'é' * 50 + chr(0)))
        detail = r.finish().events[0].detail
        self.assertLessEqual(len(detail.encode('utf-8')), 16)
        self.assertTrue(detail.startswith('ValueError'))
        self.assertNotIn(chr(0), detail)

    def test_lifecycle_misuse_poisons_finish(self):
        misuse = [
            lambda r, t: r.addSuccess(t),
            lambda r, t: (r.startTest(t), r.startTest(case('test_b'))),
            lambda r, t: (r.startTest(t), r.stopTest(t)),
            lambda r, t: (r.startTest(t), r.addSuccess(t), r.addSuccess(t)),
            lambda r, t: (r.startTest(t), r.addSuccess(case('test_b'))),
            lambda r, t: r.stopTest(t),
        ]
        for action in misuse:
            r = EventTestResult(4, 32)
            with self.assertRaises(RunnerEventError):
                action(r, case('test_a'))
            self.assertFalse(r.wasSuccessful())
            with self.assertRaises(RunnerEventError):
                r.finish()
            with self.assertRaises(RunnerEventError):
                r.startTest(case('test_c'))

    def test_duplicate_invalid_identity_and_test_limit(self):
        r = EventTestResult(4, 32)
        drive(r, case('test_a'), 'addSuccess')
        with self.assertRaises(RunnerEventError):
            r.startTest(case('test_a'))
        with self.assertRaises(RunnerEventError):
            r.finish()
        r = EventTestResult(4, 32)
        with self.assertRaises(RunnerEventError):
            r.startTest(bad_case())
        with self.assertRaises(RunnerEventError):
            r.finish()
        r = EventTestResult(1, 32)
        drive(r, case('test_a'), 'addSuccess')
        with self.assertRaises(RunnerEventError):
            r.startTest(case('test_b'))

    def test_unsupported_callbacks_fail_closed(self):
        err = exc(AssertionError, 'x')
        calls = [
            lambda r, t: r.addSubTest(t, t, err),
            lambda r, t: r.addExpectedFailure(t, err),
            lambda r, t: r.addUnexpectedSuccess(t),
        ]
        for call in calls:
            r = EventTestResult(4, 32)
            t = case('test_a')
            r.startTest(t)
            with self.assertRaises(RunnerEventError):
                call(r, t)
            self.assertFalse(r.wasSuccessful())
            with self.assertRaises(RunnerEventError):
                r.finish()

    def test_finish_once_and_active_test(self):
        cap = EventTestResult(1, 8).finish()
        self.assertEqual(len(cap.events), 1)
        self.assertEqual(reduce_runner_events(cap).tests_run, 0)
        r = EventTestResult(2, 8)
        r.startTest(case('test_a'))
        self.assertFalse(r.wasSuccessful())
        with self.assertRaises(RunnerEventError):
            r.finish()
        r = EventTestResult(2, 8)
        drive(r, case('test_a'), 'addSuccess')
        self.assertTrue(r.wasSuccessful())
        self.assertEqual(reduce_runner_events(r.finish()).passed, 1)
        with self.assertRaises(RunnerEventError):
            r.finish()
        with self.assertRaises(RunnerEventError):
            r.startTest(case('test_b'))


if __name__ == '__main__':
    unittest.main()

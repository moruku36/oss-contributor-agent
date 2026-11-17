import unittest

from src.agent.runner_events import RunnerEventError, reduce_runner_events
from src.agent.unittest_subtest_events import SubtestEventResult


class Ident:
    failureException = AssertionError

    def __init__(self, tid):
        self._tid = tid

    def id(self):
        return self._tid


class Bare:
    def __init__(self, tid):
        self._tid = tid

    def id(self):
        return self._tid


def exc(cls, msg):
    return (cls, cls(msg), None)


def make(max_subtests=4):
    return SubtestEventResult(8, 64, max_subtests)


def sub(parent, n):
    return Ident('%s.s%d' % (parent.id(), n))


class SubtestEventTests(unittest.TestCase):
    def test_passing_subtests_record_one_parent_pass(self):
        r = make()
        p = Ident('m.T.test_a')
        r.startTest(p)
        r.addSubTest(p, sub(p, 1), None)
        r.addSubTest(p, sub(p, 2), None)
        r.addSuccess(p)
        r.stopTest(p)
        cap = r.finish()
        self.assertEqual(len(cap.events), 2)
        self.assertEqual(cap.events[0].test_id, 'm.T.test_a')
        self.assertEqual(cap.events[0].outcome, 'passed')
        self.assertTrue(r.wasSuccessful())
        reduce_runner_events(cap)

    def test_multiple_assertion_subtests_aggregate_to_one_event(self):
        r = make()
        p = Ident('m.T.test_b')
        r.startTest(p)
        r.addSubTest(p, sub(p, 1), exc(AssertionError, 'a\nb'))
        r.addSubTest(p, sub(p, 2), exc(AssertionError, 'c'))
        r.addSubTest(p, sub(p, 3), None)
        self.assertFalse(r.wasSuccessful())
        r.stopTest(p)
        cap = r.finish()
        self.assertEqual(len(cap.events), 2)
        ev = cap.events[0]
        self.assertEqual(ev.outcome, 'assertion_failed')
        self.assertIn('AssertionError: a b', ev.detail)
        self.assertIn('AssertionError: c', ev.detail)
        self.assertNotIn('\n', ev.detail)
        self.assertLessEqual(len(ev.detail), 64)
        self.assertFalse(r.wasSuccessful())
        reduce_runner_events(cap)

    def test_error_precedence_and_top_level_merging(self):
        r = make()
        p1, p2, p3, p4, p5 = (Ident('m.T.t%d' % i) for i in range(1, 6))
        r.startTest(p1)
        r.addSubTest(p1, sub(p1, 1), exc(AssertionError, 'x'))
        r.addSubTest(p1, sub(p1, 2), exc(ValueError, 'y'))
        r.stopTest(p1)
        r.startTest(p2)
        r.addSubTest(p2, sub(p2, 1), exc(AssertionError, 's'))
        r.addFailure(p2, exc(AssertionError, 't'))
        r.stopTest(p2)
        r.startTest(p3)
        r.addSubTest(p3, sub(p3, 1), exc(AssertionError, 's'))
        r.addError(p3, exc(RuntimeError, 'e'))
        r.stopTest(p3)
        r.startTest(p4)
        r.addSubTest(p4, sub(p4, 1), exc(AssertionError, 'x'))
        r.addSkip(p4, 'later skip')
        r.stopTest(p4)
        # genuine teardown error after top-level success merges into the record
        r.startTest(p5)
        r.addSubTest(p5, sub(p5, 1), None)
        r.addSuccess(p5)
        r.addError(p5, exc(RuntimeError, 'teardown'))
        r.stopTest(p5)
        cap = r.finish()
        outcomes = [e.outcome for e in cap.events[:5]]
        self.assertEqual(
            outcomes, ['error', 'assertion_failed', 'error', 'assertion_failed', 'error']
        )
        self.assertTrue(cap.events[0].detail.startswith('ValueError: y'))
        self.assertIn('AssertionError: x', cap.events[0].detail)
        self.assertIn('AssertionError: t', cap.events[1].detail)
        self.assertIn('AssertionError: x', cap.events[3].detail)
        self.assertIn('RuntimeError: teardown', cap.events[4].detail)
        self.assertFalse(r.wasSuccessful())
        reduce_runner_events(cap)

    def test_duplicate_and_bound_rejection(self):
        for bad in (0, 129, True, 1.0, '1'):
            with self.assertRaises(RunnerEventError):
                SubtestEventResult(8, 64, bad)
        r = make(2)
        p = Ident('m.T.test_c')
        r.startTest(p)
        r.addSubTest(p, sub(p, 1), None)
        r.addSubTest(p, sub(p, 2), None)
        with self.assertRaises(RunnerEventError):
            r.addSubTest(p, sub(p, 3), None)
        with self.assertRaises(RunnerEventError):
            r.addSuccess(p)
        with self.assertRaises(RunnerEventError):
            r.finish()
        r = make()
        r.startTest(p)
        r.addSubTest(p, sub(p, 1), None)
        with self.assertRaises(RunnerEventError):
            r.addSubTest(p, sub(p, 1), None)
        with self.assertRaises(RunnerEventError):
            r.finish()
        # a subtest sharing the parent identity is rejected
        r = make()
        r.startTest(p)
        with self.assertRaises(RunnerEventError):
            r.addSubTest(p, Ident(p.id()), None)
        with self.assertRaises(RunnerEventError):
            r.finish()

    def test_lifecycle_and_malformed_errors_fail_closed(self):
        p = Ident('m.T.test_d')

        def fresh(started=True):
            r = make()
            if started:
                r.startTest(p)
            return r

        with self.assertRaises(RunnerEventError):
            fresh(False).addSubTest(p, sub(p, 1), None)
        with self.assertRaises(RunnerEventError):
            fresh().addSubTest(p, sub(p, 1), 'boom')
        with self.assertRaises(RunnerEventError):
            fresh().addSubTest(p, sub(p, 1), (str, 'x', None))
        bare = Bare('m.T.test_e')
        r = make()
        r.startTest(bare)
        with self.assertRaises(RunnerEventError):
            r.addSubTest(bare, Ident('m.T.test_e.s1'), exc(AssertionError, 'x'))
        r = fresh()
        r.addSubTest(p, sub(p, 1), None)
        with self.assertRaises(RunnerEventError):
            r.stopTest(p)
        r = fresh()
        with self.assertRaises(RunnerEventError):
            r.addSkip(sub(p, 1), 'subtest skip unsupported')
        r = fresh()
        with self.assertRaises(RunnerEventError):
            r.finish()
        r = fresh()
        r.addSuccess(p)
        with self.assertRaises(RunnerEventError):
            r.addSuccess(p)
        # subtest callbacks after a top-level success or skip are poison
        r = fresh()
        r.addSuccess(p)
        with self.assertRaises(RunnerEventError):
            r.addSubTest(p, sub(p, 1), None)
        with self.assertRaises(RunnerEventError):
            r.finish()
        r = fresh()
        r.addSkip(p, 'skipped')
        with self.assertRaises(RunnerEventError):
            r.addSubTest(p, sub(p, 1), exc(AssertionError, 'x'))
        with self.assertRaises(RunnerEventError):
            r.finish()

    def test_reducer_consistency_and_inherited_unsupported(self):
        r = make()
        p1, p2, p3 = (Ident('m.T.u%d' % i) for i in range(1, 4))
        r.startTest(p1)
        r.addSubTest(p1, sub(p1, 1), None)
        r.addSuccess(p1)
        r.stopTest(p1)
        r.startTest(p2)
        r.addSkip(p2, 'not today')
        r.stopTest(p2)
        r.startTest(p3)
        r.addSubTest(p3, sub(p3, 1), exc(KeyError, 'k'))
        r.stopTest(p3)
        cap = r.finish()
        self.assertEqual([e.outcome for e in cap.events[:3]], ['passed', 'skipped', 'error'])
        reduce_runner_events(cap)
        with self.assertRaises(RunnerEventError):
            r.finish()
        r = make()
        r.startTest(p1)
        with self.assertRaises(RunnerEventError):
            r.addExpectedFailure(p1, exc(AssertionError, 'x'))
        r = make()
        r.startTest(p1)
        with self.assertRaises(RunnerEventError):
            r.addUnexpectedSuccess(p1)


if __name__ == '__main__':
    unittest.main()

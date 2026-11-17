import unittest

from src.agent.instrumented_event_run import InstrumentedEventRun, InstrumentedRunError
from src.agent.runner_event_channel import decode_runner_event_response
from src.agent.runner_events import RunnerEventError
from src.agent.runner_output_buffer import RunnerOutputError
from src.agent.staged_runner_bridge import (
    DispatchTicket, StageReceipt, StagedFile, receipt_digest,
)

LIMITS = {'timeout_seconds': 10, 'max_output_bytes': 4096, 'max_wire_bytes': 65536}
COMMAND = 'python3 -m unittest example'


class _Case:
    failureException = AssertionError

    def __init__(self, ident):
        self._ident = ident

    def id(self):
        return self._ident


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class _SeqClock:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


def make_ticket():
    files = (StagedFile('src/example.py', 'source', 'a' * 64, 10),)
    receipt = StageReceipt('attempt-1', 'stage-1', files, 10)
    return DispatchTicket('attempt-1', 'dispatch-1', 1, receipt, receipt_digest(receipt),
                          COMMAND)


def one_pass(result, ident='pkg.mod.Case.test_ok'):
    case = _Case(ident)
    result.startTest(case)
    result.addSuccess(case)
    result.stopTest(case)


def good_driver(ticket, result, stdout, stderr, timeout_seconds):
    one_pass(result)
    return 0


class InstrumentedEventRunTests(unittest.TestCase):
    def test_decoded_response_matches_callback_and_output_evidence(self):
        def driver(ticket, result, stdout, stderr, timeout_seconds):
            one_pass(result, 'pkg.mod.Case.test_one')
            second = _Case('pkg.mod.Case.test_two')
            result.startTest(second)
            result.addSubTest(second, _Case('pkg.mod.Case.test_two_a'),
                              (AssertionError, AssertionError('d\u00e9'), None))
            result.addSubTest(second, _Case('pkg.mod.Case.test_two_b'),
                              (ValueError, ValueError('fail\u2713'), None))
            result.addSubTest(second, _Case('pkg.mod.Case.test_two_c'), None)
            result.stopTest(second)
            stdout.write('h\u00e9llo \u2713\n')
            stderr.write('w\u00e4rn\n')
            return 1

        ticket = make_ticket()
        payload = InstrumentedEventRun(driver).run(ticket, **LIMITS)
        self.assertIsInstance(payload, bytes)
        decoded = decode_runner_event_response(
            payload, expected_ticket=ticket,
            max_wire_bytes=LIMITS['max_wire_bytes'], max_output_bytes=LIMITS['max_output_bytes'])
        self.assertEqual(decoded.result.exit_code, 1)
        self.assertEqual(decoded.result.stdout, 'h\u00e9llo \u2713\n')
        self.assertEqual(decoded.result.stderr, 'w\u00e4rn\n')
        events = decoded.capture.events
        self.assertEqual(events[0].outcome, 'passed')
        self.assertEqual(events[1].outcome, 'error')
        self.assertIn('fail\u2713', events[1].detail)
        self.assertIn('d\u00e9', events[1].detail)

    def test_invalid_input_is_rejected_before_driver(self):
        calls = []

        def driver(*args, **kwargs):
            calls.append(1)
            return 0

        bad = [
            (make_ticket(), dict(LIMITS, timeout_seconds=0)),
            (make_ticket(), dict(LIMITS, timeout_seconds=-1)),
            (make_ticket(), dict(LIMITS, timeout_seconds=True)),
            (make_ticket(), dict(LIMITS, timeout_seconds=float('nan'))),
            (make_ticket(), dict(LIMITS, timeout_seconds=2701)),
            (make_ticket(), dict(LIMITS, timeout_seconds='5')),
            (make_ticket(), dict(LIMITS, max_output_bytes=0)),
            (make_ticket(), dict(LIMITS, max_output_bytes=True)),
            (make_ticket(), dict(LIMITS, max_output_bytes=524289)),
            (object(), LIMITS),
        ]
        for ticket, limits in bad:
            with self.assertRaises(Exception):
                InstrumentedEventRun(driver).run(ticket, **limits)
        with self.assertRaises(Exception):
            InstrumentedEventRun(driver, max_tests=0)
        with self.assertRaises(Exception):
            InstrumentedEventRun(driver, max_subtests=True)
        self.assertEqual(calls, [])

    def test_single_use_even_after_error_cancellation_or_invalid_input(self):
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            raise RuntimeError('boom')

        runner = InstrumentedEventRun(failing)
        with self.assertRaises(InstrumentedRunError):
            runner.run(make_ticket(), **LIMITS)
        with self.assertRaises(InstrumentedRunError):
            runner.run(make_ticket(), **LIMITS)

        def cancelled(*args, **kwargs):
            calls.append(1)
            raise KeyboardInterrupt()

        runner = InstrumentedEventRun(cancelled)
        with self.assertRaises(KeyboardInterrupt):
            runner.run(make_ticket(), **LIMITS)
        with self.assertRaises(InstrumentedRunError):
            runner.run(make_ticket(), **LIMITS)

        runner = InstrumentedEventRun(good_driver)
        with self.assertRaises(Exception):
            runner.run(make_ticket(), **dict(LIMITS, timeout_seconds=0))
        with self.assertRaises(InstrumentedRunError):
            runner.run(make_ticket(), **LIMITS)
        self.assertEqual(len(calls), 2)

    def test_stop_active_poison_overflow_and_bad_exit_are_refused(self):
        def stopped(ticket, result, stdout, stderr, timeout_seconds):
            one_pass(result)
            result.stop()
            return 0

        def active(ticket, result, stdout, stderr, timeout_seconds):
            result.startTest(_Case('pkg.mod.Case.test_open'))
            return 0

        def swallowed_poison(ticket, result, stdout, stderr, timeout_seconds):
            case = _Case('pkg.mod.Case.test_nested')
            result.startTest(case)
            try:
                result.startTest(case)
            except Exception:
                pass
            result.addSuccess(case)
            result.stopTest(case)
            return 0

        def swallowed_overflow(ticket, result, stdout, stderr, timeout_seconds):
            one_pass(result)
            try:
                stdout.write('x' * 20)
            except Exception:
                pass
            return 0

        for driver, limit in ((stopped, 4096), (active, 4096),
                              (swallowed_poison, 4096), (swallowed_overflow, 8)):
            with self.assertRaises((InstrumentedRunError, RunnerEventError, RunnerOutputError)):
                InstrumentedEventRun(driver).run(
                    make_ticket(), **dict(LIMITS, max_output_bytes=limit))

        for code in (True, '0', None, 0.0):
            def bad_exit(ticket, result, stdout, stderr, timeout_seconds, code=code):
                one_pass(result)
                return code

            with self.assertRaises(InstrumentedRunError):
                InstrumentedEventRun(bad_exit).run(make_ticket(), **LIMITS)

    def test_deadline_uses_mock_clock_and_refuses_after_expiry(self):
        clock = _Clock()
        seen = []

        def within(ticket, result, stdout, stderr, timeout_seconds):
            seen.append(timeout_seconds)
            clock.now += 9
            one_pass(result)
            return 0

        payload = InstrumentedEventRun(within, clock=clock).run(make_ticket(), **LIMITS)
        self.assertIsInstance(payload, bytes)
        self.assertEqual(seen, [10.0])

        def too_slow(ticket, result, stdout, stderr, timeout_seconds):
            clock.now += 11
            one_pass(result)
            return 0

        with self.assertRaises(InstrumentedRunError):
            InstrumentedEventRun(too_slow, clock=clock).run(make_ticket(), **LIMITS)

        # malformed, backwards or oversized-budget clocks never reach the driver
        calls = []

        def counting(*args, **kwargs):
            calls.append(1)
            return 0

        for values in ([float('nan')], [True], ['x'], [float('inf')],
                       [100.0, 99.0], [100.0, float('nan')], [100.0, 110.0]):
            with self.assertRaises(InstrumentedRunError):
                InstrumentedEventRun(counting, clock=_SeqClock(values)).run(
                    make_ticket(), **LIMITS)
        self.assertEqual(calls, [])

        # serialization time counts: expiry after encoding refuses the payload
        with self.assertRaises(InstrumentedRunError):
            InstrumentedEventRun(good_driver,
                                 clock=_SeqClock([100.0, 100.0, 105.0, 111.0])).run(
                make_ticket(), **LIMITS)
        payload = InstrumentedEventRun(
            good_driver, clock=_SeqClock([100.0, 100.0, 105.0, 106.0])).run(
            make_ticket(), **LIMITS)
        self.assertIsInstance(payload, bytes)

    def test_response_binds_ticket_command_and_tiny_wire_limit_precedes_driver(self):
        ticket = make_ticket()
        payload = InstrumentedEventRun(good_driver).run(ticket, **LIMITS)
        decoded = decode_runner_event_response(
            payload, expected_ticket=ticket,
            max_wire_bytes=LIMITS['max_wire_bytes'], max_output_bytes=LIMITS['max_output_bytes'])
        self.assertEqual(decoded.result.command, COMMAND)
        self.assertEqual(decoded.result.exit_code, 0)

        calls = []

        def driver(*args, **kwargs):
            calls.append(1)
            return 0

        with self.assertRaises(InstrumentedRunError):
            InstrumentedEventRun(driver).run(make_ticket(), **dict(LIMITS, max_wire_bytes=16))
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()

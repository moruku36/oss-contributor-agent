'''Run a trusted instrumentation driver and emit one verified channel response.

The driver is an injected, trusted callable. It drives a SubtestEventResult
lifecycle and writes text through BoundedRunnerOutput. This module never loads
or discovers suites, starts processes, uses the network, redirects global
streams or authenticates the driver. It cannot terminate an in-flight driver:
an expired deadline only refuses the response after the driver returns. The
clock is a trusted injection; a malformed clock is refused, never repaired.

An instance is single use. The claim happens before any validation and is
never released, so an error or cancellation can not be retried.
'''
from __future__ import annotations

import math
import threading
import time

from src.agent.runner_event_channel import (
    CommandResult, _number, encode_runner_event_response,
)
from src.agent.runner_events import RunCompleted, RunnerEventCapture, TestEvent
from src.agent.runner_output_buffer import BoundedRunnerOutput, RunnerOutputError
from src.agent.unittest_subtest_events import SubtestEventResult

MAX_TIMEOUT_SECONDS = 2700


class InstrumentedRunError(ValueError):
    '''Invalid request, misuse, or an untrustworthy instrumented run.'''


def _timeout(value: object) -> float:
    if type(value) not in (int, float):
        raise InstrumentedRunError('timeout_seconds must be an int or float')
    if not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise InstrumentedRunError('timeout_seconds out of range')
    try:
        _number(value, positive=True)
    except Exception as exc:
        raise InstrumentedRunError('timeout_seconds rejected: ' + type(exc).__name__) from None
    return float(value)


def _preflight(ticket: object, max_wire_bytes: object, max_output_bytes: object) -> None:
    '''Validate ticket and limits with the real encoder.

    The throwaway payload is never returned and is not execution evidence.
    '''
    for name, value in (('max_wire_bytes', max_wire_bytes),
                        ('max_output_bytes', max_output_bytes)):
        if type(value) is not int:
            raise InstrumentedRunError(name + ' must be an exact int')
    try:
        capture = RunnerEventCapture(
            (TestEvent(1, 'preflight.Case.test', 'passed'),
             RunCompleted(2, 1, 1, 0, 0, 0, 0, 0)),
            True, False, True)
        encode_runner_event_response(
            ticket, CommandResult(ticket.command, 0, '', '', 0), capture,
            max_wire_bytes=max_wire_bytes, max_output_bytes=max_output_bytes)
    except Exception as exc:
        raise InstrumentedRunError('request rejected: ' + type(exc).__name__) from None


class InstrumentedEventRun:
    '''Single-use transaction: driver callbacks and output -> response bytes.'''

    def __init__(self, driver, *, max_tests: int = 64, detail_limit: int = 128,
                 max_subtests: int = 32, clock=time.monotonic) -> None:
        if not callable(driver) or not callable(clock):
            raise InstrumentedRunError('driver and clock must be callable')
        SubtestEventResult(max_tests, detail_limit, max_subtests)  # validates bounds
        self._driver = driver
        self._clock = clock
        self._bounds = (max_tests, detail_limit, max_subtests)
        self._lock = threading.Lock()
        self._claimed = False

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise InstrumentedRunError('clock failed: ' + type(exc).__name__) from None
        if type(value) not in (int, float) or not math.isfinite(value):
            raise InstrumentedRunError('clock must return a finite int or float')
        return value

    def _elapsed(self, started: float, timeout: float) -> float:
        elapsed = self._now() - started
        if not math.isfinite(elapsed) or not 0 <= elapsed <= timeout:
            raise InstrumentedRunError('deadline expired or clock invalid')
        return float(elapsed)

    def run(self, ticket, *, timeout_seconds, max_output_bytes, max_wire_bytes) -> bytes:
        with self._lock:
            if self._claimed:
                raise InstrumentedRunError('run is single use')
            self._claimed = True
        started = self._now()
        timeout = _timeout(timeout_seconds)
        _preflight(ticket, max_wire_bytes, max_output_bytes)
        result = SubtestEventResult(*self._bounds)
        output = BoundedRunnerOutput(max_output_bytes)
        try:
            remaining = timeout - (self._now() - started)
            if not math.isfinite(remaining) or not 0 < remaining <= timeout:
                raise InstrumentedRunError('invalid remaining time before driver start')
            try:
                exit_code = self._driver(ticket, result, output.stdout, output.stderr,
                                         timeout_seconds=remaining)
            except Exception as exc:
                raise InstrumentedRunError('driver failed: ' + type(exc).__name__) from None
            if type(exit_code) is not int:
                raise InstrumentedRunError('driver exit code must be an exact int')
            if result.shouldStop:
                raise InstrumentedRunError('stop requested; run is incomplete')
            capture = result.finish()
            stdout, stderr = output.finish()
            elapsed = self._elapsed(started, timeout)
            command_result = CommandResult(ticket.command, exit_code, stdout, stderr, elapsed)
            payload = encode_runner_event_response(
                ticket, command_result, capture,
                max_wire_bytes=max_wire_bytes, max_output_bytes=max_output_bytes)
            # serialization can consume the remaining time
            self._elapsed(started, timeout)
            return payload
        finally:
            try:
                output.finish()
            except RunnerOutputError:
                pass

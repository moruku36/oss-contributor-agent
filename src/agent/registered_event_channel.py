'''Registered instrumented channel: literal command map to trusted drivers.

This is a TrustedEventChannel (.dispatch) for the staged candidate -> local
review-package path. Each declared command string maps to one trusted,
injected callback driver and one declared set of expected top-level test ids.
The command text is only a dictionary key. It is never interpreted, split,
executed or imported.

A channel is a single attempt. The first valid ticket binds the literal
attempt_id, generations must be contiguous from 1, and dispatch ids are
unique. Staged receipts differ between dispatches of one attempt, so the
receipt digest is deliberately not part of the binding. The generation and
dispatch id are consumed before the driver runs. Any rejection, validation
failure, clock fault, error, cancellation, malformed or late response, or
coverage mismatch poisons the whole channel, and there is no retry. The lock
is held only for state transitions, never while a driver runs.

The returned bytes are decoded again with the real codec against the full
ticket. Their top-level test identity set and count must equal the declared
expectation exactly (order may differ, skips count). That check is a
declaration check, not authenticity. The driver remains trusted, and nothing
here proves where it ran. An in-flight driver cannot be killed: an expired
deadline only refuses the response after the driver returns.
'''
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from src.agent.instrumented_event_run import InstrumentedEventRun
from src.agent.runner_event_channel import (
    _limits, _number, _ticket_document, decode_runner_event_response,
)
from src.agent.runner_events import (
    MAX_TEST_ID_BYTES, RunCompleted, RunnerEventCapture, TestEvent, reduce_runner_events,
)
from src.agent.unittest_subtest_events import SubtestEventResult

MAX_COMMAND_BYTES = 4096
MAX_EXPECTED_IDS = 128
MAX_REGISTRATIONS = 64
MAX_DISPATCHES = 64
MAX_TIMEOUT_SECONDS = 2700


class RegisteredChannelError(ValueError):
    '''Invalid registration, rejected dispatch, or a poisoned channel.'''


@dataclass(frozen=True)
class CommandInstrumentation:
    command: str
    driver: object
    expected_test_ids: tuple


def _text(value: object, limit: int, what: str) -> str:
    if type(value) is not str or not value.strip() or chr(0) in value:
        raise RegisteredChannelError(what + ' must be a nonblank NUL-free str')
    if len(value) > limit:  # characters never exceed bytes: cheap bound first
        raise RegisteredChannelError(what + ' too long')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise RegisteredChannelError(what + ' is not encodable') from None
    if size > limit:
        raise RegisteredChannelError(what + ' too long')
    return value


def _record(item: object):
    if type(item) is not CommandInstrumentation:
        raise RegisteredChannelError('registration must be a CommandInstrumentation')
    command = _text(item.command, MAX_COMMAND_BYTES, 'command')
    if not callable(item.driver):
        raise RegisteredChannelError('driver must be callable')
    ids = item.expected_test_ids
    if type(ids) is not tuple or not 1 <= len(ids) <= MAX_EXPECTED_IDS:
        raise RegisteredChannelError('expected_test_ids must be a tuple of 1..128')
    for test_id in ids:
        _text(test_id, MAX_TEST_ID_BYTES, 'expected test id')
    if len(set(ids)) != len(ids):
        raise RegisteredChannelError('expected_test_ids must be unique')
    count = len(ids)
    try:  # validation only through the real reducer, never evidence
        events = tuple(TestEvent(i, test_id, 'passed') for i, test_id in enumerate(ids, 1))
        events += (RunCompleted(count + 1, count, count, 0, 0, 0, 0, 0),)
        reduce_runner_events(RunnerEventCapture(events, True, False, True))
    except Exception as exc:
        raise RegisteredChannelError('test ids rejected: ' + type(exc).__name__) from None
    return command, item.driver, tuple(ids)


class RegisteredInstrumentedChannel:
    '''Single-attempt channel over a literal command -> driver registry.'''

    def __init__(self, registrations, *, max_dispatches: int, detail_limit: int = 128,
                 max_subtests: int = 32, clock=time.monotonic) -> None:
        if type(registrations) is not tuple or not 1 <= len(registrations) <= MAX_REGISTRATIONS:
            raise RegisteredChannelError('registrations must be a tuple of 1..64 records')
        if type(max_dispatches) is not int or not 1 <= max_dispatches <= MAX_DISPATCHES:
            raise RegisteredChannelError('max_dispatches must be an exact int 1..64')
        if type(detail_limit) is not int or not 1 <= detail_limit <= 256:
            raise RegisteredChannelError('detail_limit must be an exact int 1..256')
        if type(max_subtests) is not int or not 1 <= max_subtests <= 128:
            raise RegisteredChannelError('max_subtests must be an exact int 1..128')
        if not callable(clock):
            raise RegisteredChannelError('clock must be callable')
        registry = {}
        for item in registrations:
            command, driver, ids = _record(item)
            if command in registry:
                raise RegisteredChannelError('duplicate command registration')
            try:
                SubtestEventResult(len(ids), detail_limit, max_subtests)  # bounds only
            except Exception as exc:
                raise RegisteredChannelError('bounds rejected: ' + type(exc).__name__) from None
            registry[command] = (driver, ids)
        self._registry = registry
        self._max_dispatches = max_dispatches
        self._detail_limit = detail_limit
        self._max_subtests = max_subtests
        self._clock = clock
        self._lock = threading.Lock()
        self._state = 'idle'
        self._count = 0
        self._dispatch_ids = set()
        self._attempt = None

    @property
    def dispatched(self) -> int:
        with self._lock:
            return self._count

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise RegisteredChannelError('clock must return a finite int or float')
        return value

    def _bind(self, ticket):
        command = ticket.command
        attempt_id = ticket.attempt_id
        generation = ticket.generation
        dispatch_id = ticket.dispatch_id
        with self._lock:
            if type(command) is not str or command not in self._registry:
                raise RegisteredChannelError('command is not registered')
            if type(attempt_id) is not str or not attempt_id:
                raise RegisteredChannelError('attempt_id must be a nonblank str')
            if self._attempt is not None and attempt_id != self._attempt:
                raise RegisteredChannelError('ticket belongs to another attempt')
            if (type(generation) is not int or generation != self._count + 1
                    or generation > self._max_dispatches):
                raise RegisteredChannelError('generation not contiguous or over budget')
            if type(dispatch_id) is not str or dispatch_id in self._dispatch_ids:
                raise RegisteredChannelError('dispatch id replayed or invalid')
            self._attempt = attempt_id
            self._count = generation
            self._dispatch_ids.add(dispatch_id)
            return self._registry[command]

    def _release(self, ok: bool) -> bool:
        with self._lock:
            if ok and self._state == 'busy':
                self._state = 'idle'
                return True
            self._state = 'poisoned'
            return False

    @staticmethod
    def _check_coverage(events: tuple, expected: tuple) -> None:
        ids = [e.test_id for e in events if type(e) is TestEvent]
        if len(ids) != len(expected) or set(ids) != set(expected) or len(set(ids)) != len(ids):
            raise RegisteredChannelError('reported tests differ from the declared set')

    def dispatch(self, ticket, *, timeout_seconds, max_output_bytes, max_wire_bytes) -> bytes:
        with self._lock:
            if self._state != 'idle':
                self._state = 'poisoned'
                raise RegisteredChannelError('channel busy or poisoned')
            self._state = 'busy'
        try:
            started = self._now()
            if type(timeout_seconds) not in (int, float):
                raise RegisteredChannelError('timeout_seconds must be an int or float')
            _number(timeout_seconds, positive=True)
            if timeout_seconds > MAX_TIMEOUT_SECONDS:
                raise RegisteredChannelError('timeout_seconds out of range')
            _limits(max_wire_bytes, max_output_bytes)
            _ticket_document(ticket)
            driver, expected = self._bind(ticket)
            run = InstrumentedEventRun(
                driver, max_tests=len(expected), detail_limit=self._detail_limit,
                max_subtests=self._max_subtests, clock=self._clock)
            remaining = timeout_seconds - (self._now() - started)
            if not math.isfinite(remaining) or not 0 < remaining <= timeout_seconds:
                raise RegisteredChannelError('no time remaining or clock invalid')
            payload = run.run(ticket, timeout_seconds=remaining,
                              max_output_bytes=max_output_bytes,
                              max_wire_bytes=max_wire_bytes)
            if type(payload) is not bytes:
                raise RegisteredChannelError('run returned non-bytes')
            decoded = decode_runner_event_response(
                payload, expected_ticket=ticket, max_wire_bytes=max_wire_bytes,
                max_output_bytes=max_output_bytes)
            self._check_coverage(decoded.capture.events, expected)
            elapsed = self._now() - started
            if not math.isfinite(elapsed) or not 0 <= elapsed <= timeout_seconds:
                raise RegisteredChannelError('deadline expired or clock invalid')
        except BaseException as exc:
            self._release(False)
            if isinstance(exc, Exception) and not isinstance(exc, RegisteredChannelError):
                raise RegisteredChannelError('dispatch failed: ' + type(exc).__name__) from None
            raise
        if not self._release(True):
            raise RegisteredChannelError('channel poisoned during dispatch')
        return payload

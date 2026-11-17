'''Bounded in-memory capture of runner stdout/stderr text.

A trusted driver passes output text to this buffer. It keeps a single combined
UTF-8 byte budget across both streams, never truncates, and never retries.
The first rejected write poisons the whole capture permanently.

This module does not redirect process or global streams, start processes, use
the network, or bound execution resources. It can only account for text that a
trusted driver actually hands to its writers.
'''
from __future__ import annotations

import threading

MAX_OUTPUT_BYTES_LIMIT = 524288

_OPEN = 0
_POISONED = 1
_FINISHED = 2


class RunnerOutputError(ValueError):
    '''Invalid configuration, rejected write, or lifecycle misuse.'''


class _Sink:
    '''Minimal text writer endpoint bound to one stream of the shared state.'''

    __slots__ = ('_owner', '_index')

    def __init__(self, owner, index):
        self._owner = owner
        self._index = index

    def write(self, text):
        return self._owner._write(self._index, text)

    def flush(self):
        self._owner._require_open()

    def writable(self):
        return self._owner._is_open()


class BoundedRunnerOutput:
    '''Combined-budget stdout/stderr capture. Use finish() exactly once.'''

    __slots__ = ('_max', '_lock', '_state', '_used', '_chunks', '_stdout', '_stderr')

    def __init__(self, max_output_bytes):
        if (
            type(max_output_bytes) is not int
            or not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES_LIMIT
        ):
            raise RunnerOutputError('max_output_bytes must be an int in 1..524288')
        self._max = max_output_bytes
        self._lock = threading.Lock()
        self._state = _OPEN
        self._used = 0
        self._chunks = ([], [])
        self._stdout = _Sink(self, 0)
        self._stderr = _Sink(self, 1)

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    def write_stdout(self, text):
        return self._write(0, text)

    def write_stderr(self, text):
        return self._write(1, text)

    def finish(self):
        '''Return (stdout, stderr) once; later calls and writes are rejected.'''
        with self._lock:
            if self._state != _OPEN:
                raise RunnerOutputError('output capture is not open')
            self._state = _FINISHED
            result = (''.join(self._chunks[0]), ''.join(self._chunks[1]))
            self._chunks = ([], [])
            return result

    def _is_open(self):
        with self._lock:
            return self._state == _OPEN

    def _require_open(self):
        with self._lock:
            if self._state != _OPEN:
                raise RunnerOutputError('output capture is not open')

    def _poison_locked(self, reason):
        self._state = _POISONED
        self._chunks = ([], [])
        raise RunnerOutputError(reason)

    def _write(self, index, text):
        with self._lock:
            if self._state != _OPEN:
                raise RunnerOutputError('output capture is not open')
            if type(text) is not str:
                self._poison_locked('output must be exact str')
            if not text:
                return 0
            count = len(text)
            remaining = self._max - self._used
            # Every character needs at least one UTF-8 byte, so this cheap
            # check bounds the encode work below.
            if count > remaining:
                self._poison_locked('output exceeds byte budget')
            try:
                data = text.encode('utf-8')
            except UnicodeEncodeError:
                self._poison_locked('output is not valid UTF-8 text')
            if len(data) > remaining:
                self._poison_locked('output exceeds byte budget')
            self._chunks[index].append(text)
            self._used += len(data)
            return count

"""Fail-closed lifecycle contract; no Docker or host execution adapter is bundled.

The injected backend must enforce isolation, bounded capture and deadlines while
operations run. Python-side validation and elapsed-time checks cannot preempt a
blocked backend, undo an allocation, or prove Docker isolation. Source/dependency
acquisition is a separate future stage; this contract permits no test networking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from time import monotonic
from types import TracebackType
from typing import Protocol
from uuid import uuid4


def _positive_number(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        valid = isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a positive finite number")


def _positive_int(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class SandboxPolicy:
    """Immutable request to a trusted adapter, not evidence of enforcement."""

    image: str
    cpu_limit: float
    memory_limit_mb: int
    attempt_timeout_seconds: float = 2700.0
    operation_timeout_seconds: float = 30.0
    max_commands: int = 32
    max_command_bytes: int = 16_384
    max_output_bytes: int = 65_536  # combined UTF-8 stdout + stderr per command
    network: str = field(default="none", init=False)
    user: str = field(default="65534:65534", init=False)
    read_only_root: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or not self.image.strip() or "\0" in self.image:
            raise ValueError("image must be a nonempty string without NUL")
        for name in ("cpu_limit", "attempt_timeout_seconds", "operation_timeout_seconds"):
            _positive_number(name, getattr(self, name))
        for name in ("memory_limit_mb", "max_commands", "max_command_bytes", "max_output_bytes"):
            _positive_int(name, getattr(self, name))


@dataclass
class CommandResult:
    """Result of one command executed inside the sandbox."""

    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


class SandboxBackend(Protocol):
    """Explicit adapter boundary; never interpret commands on the host.

    create must use the caller's unique attempt_id as its removal key, including
    after partial/uncertain creation, and confirm the requested policy before
    returning. No host mounts, inherited environment, credentials, privilege or
    extra capabilities are permitted. Unsupported policy must raise.

    Every operation must enforce its timeout. The backend must enforce the total
    attempt lifetime even while the caller is idle. execute must bound combined
    output during capture, terminate on timeout/overflow and raise, rather than
    silently truncate or return success. remove must stop all attempt processes
    and confirm the container and workspace are absent (also for unknown or
    partially created attempts). An uncertain outcome must raise; no operation
    may silently retry, relax policy, or select another execution backend.
    """

    def create(self, attempt_id: str, *, policy: SandboxPolicy, timeout_seconds: float) -> None: ...

    def start(self, attempt_id: str, *, timeout_seconds: float) -> None: ...

    def execute(
        self, attempt_id: str, command: str, *, timeout_seconds: float, max_output_bytes: int
    ) -> CommandResult: ...

    def remove(self, attempt_id: str, *, timeout_seconds: float) -> None: ...


class SandboxError(RuntimeError):
    """Sandbox lifecycle or backend contract failure."""


class SandboxBackendNotConfigured(SandboxError):
    """No explicit backend was supplied; execution is unavailable."""


class SandboxCleanupError(SandboxError):
    """Removal is unconfirmed. Preserve both errors for operator reconciliation."""

    def __init__(
        self, attempt_id: str, cleanup_error: BaseException, original_error: BaseException | None
    ) -> None:
        super().__init__(f"sandbox {attempt_id}: removal unconfirmed; do not retry automatically")
        self.attempt_id = attempt_id
        self.cleanup_error = cleanup_error
        self.original_error = original_error


class Sandbox:
    """Single-use attempt. Existing positional construction and run API remain.

    A successful run result is only a command result: the context must also exit
    successfully before an attempt can be considered cleaned up. No runtime
    backend is selected or activated by default. This object is not thread-safe.
    """

    def __init__(
        self, image: str, cpu_limit: float, memory_limit_mb: int, *,
        backend: SandboxBackend | None = None,
        attempt_timeout_seconds: float = 2700.0,
        operation_timeout_seconds: float = 30.0,
        max_commands: int = 32,
        max_command_bytes: int = 16_384,
        max_output_bytes: int = 65_536,
    ) -> None:
        self._policy = SandboxPolicy(
            image, cpu_limit, memory_limit_mb, attempt_timeout_seconds,
            operation_timeout_seconds, max_commands, max_command_bytes, max_output_bytes,
        )
        self._backend = backend
        self._attempt_id = uuid4().hex
        self._state = "new"
        self._started_at = 0.0
        self._commands_used = 0

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    @property
    def image(self) -> str:
        return self.policy.image

    @property
    def cpu_limit(self) -> float:
        return self.policy.cpu_limit

    @property
    def memory_limit_mb(self) -> int:
        return self.policy.memory_limit_mb

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    @property
    def state(self) -> str:
        return self._state

    def _remaining(self) -> float:
        remaining = self.policy.attempt_timeout_seconds - (monotonic() - self._started_at)
        if remaining <= 0:
            raise TimeoutError("sandbox attempt deadline exceeded")
        return remaining

    def _check_elapsed(self, started_at: float, budget: float) -> None:
        if monotonic() - started_at >= budget:
            raise TimeoutError("sandbox backend operation exceeded its deadline")
        self._remaining()

    def __enter__(self) -> Sandbox:
        if self.state != "new":
            raise SandboxError("sandbox attempts are single-use")
        if self._backend is None:
            self._state = "failed"
            raise SandboxBackendNotConfigured("sandbox backend is not configured; refusing execution")
        self._started_at = monotonic()
        # Set before dispatch: create may allocate and then fail without a receipt.
        self._state = "creating"
        try:
            budget = min(self.policy.operation_timeout_seconds, self._remaining())
            started_at = monotonic()
            self._backend.create(self.attempt_id, policy=self.policy, timeout_seconds=budget)
            self._check_elapsed(started_at, budget)
            self._state = "starting"
            budget = min(self.policy.operation_timeout_seconds, self._remaining())
            started_at = monotonic()
            self._backend.start(self.attempt_id, timeout_seconds=budget)
            self._check_elapsed(started_at, budget)
            self._state = "running"
            return self
        except BaseException as exc:
            self._remove(exc)
            raise

    def _remove(self, original_error: BaseException | None) -> None:
        if self.state not in {"creating", "starting", "running"}:
            return  # includes unconfirmed removal: never silently retry
        self._state = "removing"
        try:
            assert self._backend is not None
            # Cleanup gets its own bounded grace period, even after attempt expiry.
            started_at = monotonic()
            self._backend.remove(
                self.attempt_id, timeout_seconds=self.policy.operation_timeout_seconds
            )
            if monotonic() - started_at >= self.policy.operation_timeout_seconds:
                raise TimeoutError("sandbox removal exceeded its deadline")
        except BaseException as exc:
            self._state = "cleanup_failed"
            raise SandboxCleanupError(self.attempt_id, exc, original_error) from exc
        self._state = "failed" if original_error is not None else "closed"

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.state == "running" and exc is None:
            try:
                self._remaining()
            except BaseException as deadline_error:
                self._remove(deadline_error)
                raise
        self._remove(exc)

    def run(self, command: str, timeout_seconds: int) -> CommandResult:
        """Dispatch a bounded command only inside a running sandbox.

        Invalid requests never dispatch. Backend failures, exhausted budgets and
        malformed results terminate the attempt and trigger one removal attempt.
        """
        if self.state != "running":
            raise SandboxError("commands require a running sandbox")
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("command must be a nonempty string without NUL")
        if len(command.encode("utf-8")) > self.policy.max_command_bytes:
            raise ValueError("command exceeds max_command_bytes")
        _positive_number("timeout_seconds", timeout_seconds)
        try:
            if self._commands_used >= self.policy.max_commands:
                raise SandboxError("sandbox command budget exhausted")
            budget = min(timeout_seconds, self._remaining())
            self._commands_used += 1
            started_at = monotonic()
            assert self._backend is not None
            result = self._backend.execute(
                self.attempt_id, command, timeout_seconds=budget,
                max_output_bytes=self.policy.max_output_bytes,
            )
            self._check_elapsed(started_at, budget)
            self._validate_result(result, command, budget)
            return result
        except BaseException as exc:
            self._remove(exc)
            raise

    def _validate_result(self, result: CommandResult, command: str, budget: float) -> None:
        if not isinstance(result, CommandResult) or result.command != command:
            raise SandboxError("backend returned an invalid command result")
        if type(result.exit_code) is not int:
            raise SandboxError("backend returned an invalid exit code")
        duration = result.duration_seconds
        if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not isfinite(duration) or not 0 <= duration < budget):
            raise SandboxError("backend returned an invalid or over-budget duration")
        if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
            raise SandboxError("backend returned non-text output")
        if len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) > self.policy.max_output_bytes:
            raise SandboxError("backend exceeded max_output_bytes")

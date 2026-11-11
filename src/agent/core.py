"""Injection-only CoderLoop; no provider, host workspace or CLI wiring.

The caller supplies prepared source, explicit commands and trusted adapters.
Exploration and reproduction-test authoring are unfinished. Adapters enforce
deadlines/capture bounds in flight; these checks cannot interrupt a blocked
call or prove isolation/model identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import json
from math import isfinite
from time import monotonic
from typing import Callable, Protocol, TypeVar

from src.sandbox.docker_manager import (
    CommandResult, Sandbox, SandboxBackendNotConfigured, SandboxCleanupError,
)

T = TypeVar("T")


def _positive_int(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _positive_number(name: str, value: object) -> None:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and isfinite(value) and value > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a positive finite number")


def _text(value: object, limit: int) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and "\0" not in value
            and len(value.encode("utf-8")) <= limit)


def _tail(value: str, limit: int) -> str:
    return value.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


class LoopState(str, Enum):
    READY = "ready"
    STARTING = "starting"
    BASELINE = "baseline"
    REPRODUCING = "reproducing"
    CONTEXT = "context"
    PROPOSING = "proposing"
    APPLYING = "applying"
    VERIFYING_REPRODUCTION = "verifying_reproduction"
    VERIFYING_SUITE = "verifying_suite"
    EXPORTING = "exporting"
    CLEANING_UP = "cleaning_up"
    SUCCEEDED = "succeeded"
    UNCONFIGURED = "unconfigured"
    INVALID_INPUT = "invalid_input"
    NOT_REPRODUCED = "not_reproduced"
    ENVIRONMENT_REJECTED = "environment_rejected"
    TESTS_FAILED = "tests_failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMED_OUT = "timed_out"
    UNKNOWN_OUTCOME = "unknown_outcome"
    CLEANUP_FAILED = "cleanup_failed"


class TestOutcome(str, Enum):
    __test__ = False
    PASS = "pass"
    ASSERTION_FAILED = "assertion_failed"
    INFRA_ERROR = "infra_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TestCommand:
    __test__ = False
    command: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not _text(self.command, 4096):
            raise ValueError("test command must be nonempty, NUL-free and <= 4096 UTF-8 bytes")
        _positive_number("command timeout", self.timeout_seconds)


@dataclass(frozen=True)
class TestPlan:
    __test__ = False
    reproduction: TestCommand
    relevant_suite: tuple[TestCommand, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.reproduction, TestCommand)
                or type(self.relevant_suite) is not tuple
                or not 1 <= len(self.relevant_suite) <= 8
                or not all(isinstance(item, TestCommand) for item in self.relevant_suite)):
            raise ValueError("supply a reproduction and 1..8 explicit relevant-suite commands")


@dataclass(frozen=True)
class TestAssessment:
    """Trusted runner evidence, never a model verdict or exit-code heuristic."""

    __test__ = False
    outcome: TestOutcome
    tests_run: int = 0  # actually executed tests; excludes skipped tests
    assertion_failures: int = 0
    detail: str = ""


@dataclass(frozen=True)
class PatchRequest:
    issue_json: str
    context: str
    failing_log_tail: str


@dataclass(frozen=True)
class ModelReply:
    patch: str
    tokens_used: int | None  # authoritative total input/output/reasoning/cache usage
    provider: str
    model: str


class PatchModel(Protocol):
    """Enforce remaining bounds in flight, including all token categories.

    No hidden retries/fallbacks. Return aggregate usage and actual provider/model
    receipt metadata. Unknown completion/usage must raise or return unknown usage;
    neither is retryable. No tools or workspace access.
    """

    def propose(
        self, request: PatchRequest, *, timeout_seconds: float,
        remaining_tokens: int, remaining_calls: int, remaining_attempts: int,
        max_patch_bytes: int,
    ) -> ModelReply: ...


class Workspace(Protocol):
    """Trusted, sandbox-bound adapter for prepared source and runner evidence.

    Enforce deadlines/capture bounds and bind attempt_id to the workspace used by
    Sandbox.run. context is read-only and caller-selected (no exploration).
    assess parses authoritative runner evidence for this exact CommandResult:
    collection/syntax/dependency/runner errors, mixed infrastructure failures,
    empty/all-skipped runs and ambiguity cannot be PASS/ASSERTION_FAILED. Never
    classify solely by exit code or model text. detail identifies failed assertions.

    apply_patch validates paths, symlinks and allowed source scope, protects the
    declared tests/runner evidence, and confirms exact bounded application. Only
    literal True confirms it; partial/uncertain/rejected application is terminal.
    Do not retry or pretend rollback. diff returns the complete bounded change
    from the prepared base. No host adapter, acquisition or Git mutation is bundled.
    """

    def context(self, attempt_id: str, *, timeout_seconds: float, max_bytes: int) -> str: ...

    def assess(
        self, attempt_id: str, result: CommandResult, *, timeout_seconds: float,
        max_bytes: int,
    ) -> TestAssessment: ...

    def apply_patch(
        self, attempt_id: str, patch: str, *, timeout_seconds: float, max_bytes: int,
    ) -> bool: ...

    def diff(self, attempt_id: str, *, timeout_seconds: float, max_bytes: int) -> str: ...


@dataclass(frozen=True)
class CheckEvidence:
    phase: LoopState
    command: str
    exit_code: int
    duration_seconds: float
    log_tail: str
    assessment: TestAssessment = field(default_factory=lambda: TestAssessment(TestOutcome.UNKNOWN))


@dataclass(frozen=True)
class ModelReceipt:
    provider: str
    model: str
    tokens_used: int


@dataclass
class AttemptResult:
    """Old positional fields are retained; evidence does not imply a report/PR."""

    success: bool
    attempts_used: int
    failure_kind: str | None = None
    test_log_tail: str = ""
    diff: str = ""
    notes: list[str] = field(default_factory=list)
    state: LoopState = LoopState.READY
    states: list[LoopState] = field(default_factory=list)
    checks: list[CheckEvidence] = field(default_factory=list)
    model_calls: int = 0
    tokens_used: int | None = 0  # None if any dispatched call has unknown usage
    known_tokens_used: int = 0  # authoritative prefix, even after unknown usage
    model_receipts: list[ModelReceipt] = field(default_factory=list)
    cleanup_confirmed: bool = False
    sandbox_attempt_id: str = ""
    elapsed_seconds: float = 0.0


class _Stop(Exception):
    def __init__(self, state: LoopState, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.state, self.kind = state, kind


class CoderLoop:
    """One injected run per fresh Sandbox; original construction still works.

    timeout_seconds covers orchestration, model/workspace calls and commands.
    Sandbox lifecycle deadlines and independent cleanup grace still apply;
    late results, including late teardown, never produce success.
    """

    def __init__(
        self, max_attempts: int, token_budget: int, *, model: PatchModel | None = None,
        timeout_seconds: float = 2700.0, max_model_calls: int | None = None,
        max_text_bytes: int = 16_384, max_patch_bytes: int = 16_384,
    ) -> None:
        _positive_int("max_attempts", max_attempts)
        _positive_int("token_budget", token_budget)
        _positive_number("timeout_seconds", timeout_seconds)
        calls = max_attempts if max_model_calls is None else max_model_calls
        for name, value in (("max_model_calls", calls), ("max_text_bytes", max_text_bytes),
                            ("max_patch_bytes", max_patch_bytes)):
            _positive_int(name, value)
        self.max_attempts, self.token_budget = max_attempts, token_budget
        self.model, self.timeout_seconds, self.max_model_calls = model, timeout_seconds, calls
        self.max_text_bytes, self.max_patch_bytes = max_text_bytes, max_patch_bytes

    def run(
        self, issue: dict, workspace: Workspace | None, sandbox: Sandbox, *,
        plan: TestPlan | None = None,
    ) -> AttemptResult:
        result = AttemptResult(False, 0)
        started = monotonic()
        sandbox_deadline: float | None = None

        def state(value: LoopState) -> None:
            result.state = value
            result.states.append(value)

        def remaining() -> float:
            now = monotonic()
            deadline = started + self.timeout_seconds
            if sandbox_deadline is not None:
                deadline = min(deadline, sandbox_deadline)
            value = deadline - now
            if value <= 0:
                raise _Stop(LoopState.TIMED_OUT, "timeout", "coder-loop deadline exhausted")
            return value

        def workspace_call(method: Callable[..., T], *args: object) -> T:
            value = method(sandbox.attempt_id, *args, timeout_seconds=remaining(),
                           max_bytes=self.max_text_bytes)
            remaining()
            return value

        def check(command: TestCommand, phase: LoopState) -> TestAssessment:
            state(phase)
            raw = sandbox.run(command.command, min(command.timeout_seconds, remaining()))
            remaining()
            log = _tail(raw.stdout + "\n" + raw.stderr, self.max_text_bytes)
            result.test_log_tail = log
            result.checks.append(CheckEvidence(phase, raw.command, raw.exit_code,
                                               raw.duration_seconds, log))
            verdict = workspace_call(workspace.assess, raw)
            if (not isinstance(verdict, TestAssessment)
                    or not isinstance(verdict.outcome, TestOutcome)
                    or type(verdict.tests_run) is not int or verdict.tests_run < 0
                    or type(verdict.assertion_failures) is not int
                    or not 0 <= verdict.assertion_failures <= verdict.tests_run
                    or not isinstance(verdict.detail, str)
                    or len(verdict.detail.encode("utf-8")) > self.max_text_bytes):
                raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "malformed runner assessment")
            result.checks[-1] = replace(result.checks[-1], assessment=verdict)
            if verdict.outcome == TestOutcome.PASS:
                valid = raw.exit_code == 0 and verdict.tests_run > 0 and verdict.assertion_failures == 0
            elif verdict.outcome == TestOutcome.ASSERTION_FAILED:
                valid = raw.exit_code > 0 and verdict.assertion_failures > 0 and bool(verdict.detail.strip())
            else:
                raise _Stop(LoopState.ENVIRONMENT_REJECTED, "infra_error", "runner infrastructure/unknown outcome")
            if not valid:
                raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "runner evidence contradicts command result")
            return verdict

        def failing_log(checks: list[CheckEvidence]) -> str:
            return _tail("\n".join(
                f"{item.command}\n{item.assessment.detail}\n{item.log_tail}"
                for item in checks if item.assessment.outcome == TestOutcome.ASSERTION_FAILED
            ), self.max_text_bytes)

        state(LoopState.READY)
        stop: _Stop | None = None
        lifecycle_attempted = False
        try:
            if self.model is None or workspace is None or plan is None:
                raise _Stop(LoopState.UNCONFIGURED, "infra_error", "explicit model, workspace and test plan required; refusing execution")
            if not isinstance(sandbox, Sandbox) or sandbox.state != "new" or not isinstance(plan, TestPlan):
                raise _Stop(LoopState.INVALID_INPUT, "infra_error", "fresh Sandbox and explicit TestPlan required")
            if not isinstance(issue, dict):
                raise _Stop(LoopState.INVALID_INPUT, "infra_error", "issue must be a bounded JSON object")
            try:
                issue_json = json.dumps(issue, ensure_ascii=False, allow_nan=False)
                if not _text(issue_json, self.max_text_bytes):
                    raise ValueError("oversized issue")
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise _Stop(LoopState.INVALID_INPUT, "infra_error", "issue must be a bounded JSON object") from None
            result.sandbox_attempt_id = sandbox.attempt_id
            state(LoopState.STARTING)
            remaining()
            lifecycle_attempted = True
            # Conservative: include entry time in the sandbox lifetime bound.
            sandbox_deadline = monotonic() + sandbox.policy.attempt_timeout_seconds
            with sandbox:
                try:
                    remaining()
                    for command in plan.relevant_suite:
                        if check(command, LoopState.BASELINE).outcome != TestOutcome.PASS:
                            raise _Stop(LoopState.ENVIRONMENT_REJECTED, "infra_error", "baseline relevant suite failed")
                    if check(plan.reproduction, LoopState.REPRODUCING).outcome != TestOutcome.ASSERTION_FAILED:
                        raise _Stop(LoopState.NOT_REPRODUCED, "not_reproduced", "reproduction passed; no model patch permitted")
                    feedback = failing_log(result.checks)
                    result.test_log_tail = feedback
                    while True:
                        if result.attempts_used >= self.max_attempts:
                            raise _Stop(LoopState.TESTS_FAILED, "tests_failed", "patch attempts exhausted")
                        if result.model_calls >= self.max_model_calls or result.known_tokens_used >= self.token_budget:
                            raise _Stop(LoopState.BUDGET_EXHAUSTED, "timeout", "model call/token budget exhausted")
                        state(LoopState.CONTEXT)
                        context = workspace_call(workspace.context)
                        if not _text(context, self.max_text_bytes):
                            raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "invalid or oversized workspace context")
                        state(LoopState.PROPOSING)
                        bounds = dict(
                            timeout_seconds=remaining(),
                            remaining_tokens=self.token_budget - result.known_tokens_used,
                            remaining_calls=self.max_model_calls - result.model_calls,
                            remaining_attempts=self.max_attempts - result.attempts_used,
                            max_patch_bytes=self.max_patch_bytes,
                        )
                        result.attempts_used += 1
                        result.model_calls += 1
                        result.tokens_used = None  # unknown until an authoritative usage receipt
                        reply = self.model.propose(PatchRequest(issue_json, context, feedback), **bounds)
                        if not isinstance(reply, ModelReply) or type(reply.tokens_used) is not int or reply.tokens_used < 0:
                            raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "model usage unknown/malformed; do not retry")
                        result.known_tokens_used += reply.tokens_used
                        result.tokens_used = result.known_tokens_used
                        if not _text(reply.provider, 256) or not _text(reply.model, 256):
                            raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "model receipt metadata missing/malformed")
                        result.model_receipts.append(ModelReceipt(reply.provider, reply.model, reply.tokens_used))
                        remaining()
                        if result.known_tokens_used > self.token_budget:
                            raise _Stop(LoopState.BUDGET_EXHAUSTED, "timeout", "model exceeded remaining token budget; no application")
                        if not _text(reply.patch, self.max_patch_bytes):
                            raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "model proposal empty/malformed/oversized")
                        state(LoopState.APPLYING)
                        applied = workspace.apply_patch(
                            sandbox.attempt_id, reply.patch, timeout_seconds=remaining(),
                            max_bytes=self.max_patch_bytes,
                        )
                        remaining()
                        if applied is not True:
                            raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "workspace application unconfirmed; do not retry")
                        first_check = len(result.checks)
                        check(plan.reproduction, LoopState.VERIFYING_REPRODUCTION)
                        for command in plan.relevant_suite:
                            check(command, LoopState.VERIFYING_SUITE)
                        verified = result.checks[first_check:]
                        if all(item.assessment.outcome == TestOutcome.PASS for item in verified):
                            state(LoopState.EXPORTING)
                            diff = workspace_call(workspace.diff)
                            if not _text(diff, self.max_text_bytes):
                                raise _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error", "complete workspace diff unconfirmed/oversized")
                            result.diff = diff
                            break
                        feedback = failing_log(verified)
                        result.test_log_tail = feedback
                finally:
                    state(LoopState.CLEANING_UP)
            remaining()  # late teardown also refuses success
        except SandboxBackendNotConfigured:
            lifecycle_attempted = False
            stop = _Stop(LoopState.UNCONFIGURED, "infra_error", "sandbox backend not configured; refusing execution")
        except SandboxCleanupError as exc:
            if isinstance(exc.original_error, _Stop):
                result.notes.append(str(exc.original_error))
            stop = _Stop(LoopState.CLEANUP_FAILED, "infra_error",
                         f"sandbox removal unconfirmed ({type(exc.cleanup_error).__name__}); original={type(exc.original_error).__name__}; do not retry")
        except _Stop as exc:
            stop = exc
        except TimeoutError:
            stop = _Stop(LoopState.TIMED_OUT, "timeout", "backend timed out; outcome may be uncertain; do not retry")
        except Exception as exc:
            stop = _Stop(LoopState.UNKNOWN_OUTCOME, "infra_error",
                         f"adapter failure ({type(exc).__name__}); do not retry")
        result.cleanup_confirmed = lifecycle_attempted and sandbox.state in {"closed", "failed"}
        result.elapsed_seconds = monotonic() - started
        if stop is None and not result.cleanup_confirmed:
            stop = _Stop(LoopState.CLEANUP_FAILED, "infra_error", "sandbox teardown not confirmed")
        if stop is not None:
            state(stop.state)
            result.failure_kind = stop.kind
            result.notes.append(str(stop))
        else:
            state(LoopState.SUCCEEDED)
            result.success = True
        return result

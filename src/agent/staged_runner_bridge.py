"""Injection-only gate from one staged snapshot generation to one runner dispatch.

Reviewed draft. For each command the bridge writes the *current* snapshot into a
fresh private generation under a caller-supplied staging-parent descriptor, then
hands a ticket (attempt, dispatch nonce, complete StageReceipt and its digest,
command) to an injected trusted runner exactly once. The runner's evidence must
echo that ticket and carry the exact CommandResult. assess forwards a result to
the snapshot's trusted assessor only while it is the latest dispatch, unassessed,
unmutated and staged from the snapshot contents that are still current.

Nothing here executes a command, starts a process, reaches Docker, acquires
source or selects a backend; the caller injects every capability. Echoed tickets
and matching digests show consistency of a trusted runner's report, not that the
runner used the staged generation or that anything was isolated. Python deadline
checks cannot preempt blocked I/O or a blocked runner/assessor.

Once a dispatch is consumed (before the generation directory is created) any
failure is terminal: partial_or_unknown, no retry, no rollback, no deletion.
Generations, complete or partial, stay under the staging parent for the owning
backend/operator. Only the duplicated parent descriptor is owned and closed here.
The directory-descriptor helpers are reused from filesystem_staging unchanged.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from hashlib import sha256
import json
from math import isfinite
import os
import stat
from time import monotonic
from typing import Protocol
from uuid import uuid4

from src.agent import filesystem_staging as _fs
from src.agent.core import TestAssessment, TestCommand
from src.agent.filesystem_staging import (
    FilesystemStaging, IncompleteStageReceipt, ManifestFile, StagedFile, StageReceipt,
)
from src.agent.snapshot_workspace import SnapshotError, SnapshotLimits, SnapshotWorkspace, TextFile
from src.sandbox.docker_manager import CommandResult

_MAX_COMMAND_SECONDS = 2700.0
_MAX_OUTPUT_BYTES = 524_288
_MAX_DISPATCHES = 64


@dataclass(frozen=True)
class DispatchTicket:
    """What the trusted runner is asked to run, and against which generation."""

    attempt_id: str
    dispatch_id: str  # fresh nonce per dispatch
    generation: int  # 1-based count of consumed dispatches on this bridge
    receipt: StageReceipt  # complete receipts only
    receipt_sha256: str
    command: str


@dataclass(frozen=True)
class RunnerEvidence:
    """Trusted runner report; every binding field must echo the ticket exactly."""

    attempt_id: str
    dispatch_id: str
    stage_name: str
    receipt_sha256: str
    result: CommandResult


@dataclass(frozen=True)
class DispatchRecord:
    ticket: DispatchTicket
    evidence: RunnerEvidence


@dataclass(frozen=True)
class IncompleteDispatch:
    attempt_id: str
    generation: int
    command: str
    phase: str  # "staging" or "runner": where the outcome became unknown
    stage: StageReceipt | IncompleteStageReceipt
    outcome: str = field(default="partial_or_unknown", init=False)


class StagedRunner(Protocol):
    """Trusted injected adapter; the only component that may execute anything.

    Run the ticket's command once against exactly the ticket's generation, with
    no retry or fallback. Enforce the timeout and combined output bound in
    flight and raise instead of truncating. Return a new CommandResult per call.
    An uncertain outcome must raise. No access to the snapshot or this bridge.
    """

    def run_staged(
        self, ticket: DispatchTicket, *, timeout_seconds: float, max_output_bytes: int,
    ) -> RunnerEvidence: ...


class EvidenceRejected(SnapshotError):
    """Stale, replayed, superseded, mutated or unrecognized result; not assessed."""


class DispatchError(RuntimeError):
    """Terminal dispatch receipt, never a success, retry or cleanup confirmation."""

    def __init__(
        self, receipt: IncompleteDispatch, original_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            f"dispatch {receipt.generation} for {receipt.attempt_id} ({receipt.phase}, "
            f"stage {receipt.stage.stage_name}): {receipt.outcome}; "
            "no automatic retry, rollback or cleanup"
        )
        self.receipt = receipt
        self.original_error = original_error
        self.close_error = close_error


def receipt_digest(receipt: StageReceipt) -> str:
    """Canonical digest of a complete receipt: attempt, stage, files, bytes."""
    if type(receipt) is not StageReceipt or receipt.outcome != "complete":
        raise SnapshotError("complete StageReceipt required")
    canonical = json.dumps(
        [receipt.attempt_id, receipt.stage_name, receipt.total_bytes, receipt.outcome,
         [[item.path, item.role, item.sha256, item.size_bytes] for item in receipt.files]],
        ensure_ascii=True, separators=(",", ":"),
    )
    return sha256(canonical.encode("ascii")).hexdigest()


def _bounded(name: str, value: object, ceiling: float, *, integer: bool) -> None:
    try:
        valid = (type(value) is int if integer
                 else not isinstance(value, bool) and isinstance(value, (int, float))
                 and isfinite(value)) and 0 < value <= ceiling
    except OverflowError:
        valid = False
    if not valid:
        raise SnapshotError(f"{name} must be positive and at most {ceiling}")


def _left(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("staged dispatch deadline exhausted")
    return remaining


def _payloads(files: tuple[TextFile, ...]) -> tuple[tuple[StagedFile, bytes], ...]:
    staged = []
    for entry in files:
        data = entry.text.encode("utf-8")
        staged.append((StagedFile(entry.path, entry.role, sha256(data).hexdigest(), len(data)), data))
    return tuple(staged)


def _result_key(result: CommandResult) -> tuple[object, ...]:
    return (result.command, result.exit_code, result.stdout, result.stderr, result.duration_seconds)


class StagedRunnerBridge:
    """Single-caller gate for one attempt; not thread-safe, not a Sandbox.

    execute and assess mirror SandboxBackend.execute and Workspace.assess so a
    trusted backend/workspace adapter can delegate to them; sandbox creation,
    start and removal stay with that backend. Construction validates the
    manifest against the snapshot namespace and the live snapshot, then
    duplicates the staging-parent descriptor. Use as a context manager or close
    explicitly; the caller's descriptor is never closed.
    """

    def __init__(
        self, attempt_id: str, *, workspace: SnapshotWorkspace, staging_parent_fd: int,
        manifest: tuple[ManifestFile, ...], runner: StagedRunner,
        limits: SnapshotLimits = SnapshotLimits(), stage_timeout_seconds: float = 30.0,
        max_command_timeout_seconds: float = _MAX_COMMAND_SECONDS,
        max_output_bytes: int = 65_536, max_dispatches: int = 32,
    ) -> None:
        if (type(workspace) is not SnapshotWorkspace or type(limits) is not SnapshotLimits
                or not callable(getattr(runner, "run_staged", None))):
            raise SnapshotError("explicit SnapshotWorkspace, limits and trusted runner required")
        if (type(manifest) is not tuple or not 1 <= len(manifest) <= limits.max_files
                or any(type(entry) is not ManifestFile for entry in manifest)):
            raise SnapshotError("bounded explicit ManifestFile tuple required")
        _bounded("stage_timeout_seconds", stage_timeout_seconds, 30, integer=False)
        _bounded("max_command_timeout_seconds", max_command_timeout_seconds,
                 _MAX_COMMAND_SECONDS, integer=False)
        _bounded("max_output_bytes", max_output_bytes, _MAX_OUTPUT_BYTES, integer=True)
        _bounded("max_dispatches", max_dispatches, _MAX_DISPATCHES, integer=True)
        if type(staging_parent_fd) is not int or staging_parent_fd < 0:
            raise SnapshotError("explicit already-open staging-parent descriptor required")
        self._attempt_id, self._workspace, self._runner = attempt_id, workspace, runner
        self._limits, self._stage_timeout = limits, stage_timeout_seconds
        self._max_command_seconds = max_command_timeout_seconds
        self._max_output_bytes, self._max_dispatches = max_output_bytes, max_dispatches
        # Empty contents validate attempt ID, namespace and roles without I/O.
        self._validate(tuple(TextFile(entry.path, "", entry.role) for entry in manifest))
        self._manifest = tuple(sorted((entry.path, entry.role) for entry in manifest))
        self._export(monotonic() + stage_timeout_seconds)  # live snapshot must match now
        _fs._platform()
        self._owned = ExitStack()
        self._parent = _fs._owned(self._owned, os.dup(staging_parent_fd))
        try:
            if not stat.S_ISDIR(os.fstat(self._parent).st_mode):
                raise SnapshotError("staging parent must be a directory")
        except BaseException:
            self._owned.close()
            raise
        self._state = "ready"
        self._generation = 0
        self._current: DispatchRecord | None = None
        self._current_key: tuple[object, ...] | None = None
        self._assessable = False
        self._results: list[CommandResult] = []  # identity guard, bounded by max_dispatches
        self._terminal: IncompleteDispatch | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def last_dispatch(self) -> DispatchRecord | None:
        """Latest accepted dispatch; whether it may still be assessed is not implied."""
        return self._current

    def __enter__(self) -> StagedRunnerBridge:
        if self._state != "ready":
            raise SnapshotError("bridge is closed, busy or failed")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the duplicated descriptor only; staged generations are not removed."""
        if self._state != "failed":
            self._state = "closed"
        self._assessable = False
        self._owned.close()

    def _usable(self, attempt_id: str) -> None:
        if type(attempt_id) is not str or attempt_id != self._attempt_id:
            raise SnapshotError("wrong bridge attempt")
        if self._terminal is not None:
            raise DispatchError(self._terminal)
        if self._state != "ready":
            raise SnapshotError("bridge is closed or busy")

    def _validate(self, files: tuple[TextFile, ...]) -> None:
        # Reuse snapshot namespace/role/bound validation; this assessor is never called.
        SnapshotWorkspace(
            self._attempt_id, files, editable_paths=(), context_paths=(),
            assessor=self._workspace, limits=self._limits,
        )

    def _export(self, deadline: float) -> tuple[tuple[StagedFile, bytes], ...]:
        files = self._workspace.export_files(
            self._attempt_id, timeout_seconds=_left(deadline),
            max_bytes=self._limits.max_total_bytes,
        )
        self._validate(files)
        if tuple((entry.path, entry.role) for entry in files) != self._manifest:
            raise SnapshotError("snapshot does not match the explicit manifest")
        return _payloads(files)

    def execute(
        self, attempt_id: str, command: str, *, timeout_seconds: float, max_output_bytes: int,
    ) -> CommandResult:
        """Stage the current snapshot into a fresh generation and dispatch once."""
        self._usable(attempt_id)
        try:
            TestCommand(command, timeout_seconds)  # existing command/timeout validation
        except ValueError as error:
            raise SnapshotError(str(error)) from None
        _bounded("timeout_seconds", timeout_seconds, self._max_command_seconds, integer=False)
        _bounded("max_output_bytes", max_output_bytes, _MAX_OUTPUT_BYTES, integer=True)
        if self._generation >= self._max_dispatches:
            raise SnapshotError("dispatch budget exhausted")
        deadline = monotonic() + timeout_seconds
        cap = min(max_output_bytes, self._max_output_bytes)
        payloads = self._export(deadline)
        stage_deadline = _fs._deadline(min(self._stage_timeout, _left(deadline)))
        name = "stage-" + uuid4().hex
        verified: list[StagedFile] = []
        attempted: str | None = None
        stage: StageReceipt | None = None
        phase = "staging"
        self._generation += 1
        self._assessable = False  # any earlier result is superseded from here on
        self._state = "dispatching"  # consumed before the first possible side effect
        try:
            with ExitStack() as handles:
                _fs._remaining(stage_deadline)
                os.mkdir(name, mode=0o700, dir_fd=self._parent)  # one name, no collision retry
                root, identity = _fs._directory(self._parent, name, handles, stage_deadline, private=True)
                created: dict[str, tuple[int, int]] = {}
                for item, data in payloads:
                    attempted = item.path
                    FilesystemStaging._stage_file(root, item.path, data, created, stage_deadline)
                    verified.append(item)
                _fs._directory_link(self._parent, name, identity, stage_deadline, private=True)
            _fs._remaining(stage_deadline)  # includes descriptor closing; no late success
            stage = StageReceipt(
                attempt_id, name, tuple(verified), sum(item.size_bytes for item in verified),
            )
            ticket = DispatchTicket(
                attempt_id, uuid4().hex, self._generation, stage, receipt_digest(stage), command,
            )
            phase = "runner"
            budget = _left(deadline)
            evidence = self._runner.run_staged(ticket, timeout_seconds=budget, max_output_bytes=cap)
            _left(deadline)  # a late report is not a result
            self._accept(evidence, ticket, budget, cap)
        except BaseException as error:
            self._state = "failed"
            self._terminal = IncompleteDispatch(
                attempt_id, self._generation, command, phase,
                stage if stage is not None else IncompleteStageReceipt(
                    attempt_id, name, tuple(verified),
                    sum(item.size_bytes for item in verified), attempted,
                ),
            )
            close_error = None
            try:
                self._owned.close()
            except BaseException as closing:
                close_error = closing
            raise DispatchError(self._terminal, error, close_error) from error
        result = evidence.result
        self._results.append(result)
        self._current, self._current_key = DispatchRecord(ticket, evidence), _result_key(result)
        self._assessable = True
        self._state = "ready"
        return result

    def _accept(
        self, evidence: RunnerEvidence, ticket: DispatchTicket, budget: float, cap: int,
    ) -> None:
        if (type(evidence) is not RunnerEvidence
                or (evidence.attempt_id, evidence.dispatch_id, evidence.stage_name,
                    evidence.receipt_sha256)
                != (ticket.attempt_id, ticket.dispatch_id, ticket.receipt.stage_name,
                    ticket.receipt_sha256)):
            raise SnapshotError("runner evidence does not echo this dispatch ticket")
        result = evidence.result
        if type(result) is not CommandResult or any(result is seen for seen in self._results):
            raise SnapshotError("runner evidence must carry a new exact CommandResult")
        # Same checks as Sandbox._validate_result, against this dispatch's bounds.
        duration = result.duration_seconds
        if (result.command != ticket.command or type(result.exit_code) is not int
                or isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not isfinite(duration) or not 0 <= duration < budget
                or not isinstance(result.stdout, str) or not isinstance(result.stderr, str)
                or len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) > cap):
            raise SnapshotError("runner returned a mismatched, malformed or over-bound result")

    def assess(
        self, attempt_id: str, result: CommandResult, *, timeout_seconds: float, max_bytes: int,
    ) -> TestAssessment:
        """Assess only the latest, unassessed, unmutated result of a current generation."""
        self._usable(attempt_id)
        record = self._current
        if record is None or not self._assessable or result is not record.evidence.result:
            raise EvidenceRejected("stale, replayed, superseded or unrecognized command result")
        if _result_key(result) != self._current_key:
            raise EvidenceRejected("command result changed after its evidence was accepted")
        started = monotonic()
        # export_files validates the timeout before any deadline arithmetic.
        files = self._workspace.export_files(
            attempt_id, timeout_seconds=timeout_seconds, max_bytes=self._limits.max_total_bytes,
        )
        deadline = started + timeout_seconds
        if tuple(item for item, _ in _payloads(files)) != record.ticket.receipt.files:
            raise EvidenceRejected("snapshot changed since this generation was staged")
        self._assessable = False  # single use, consumed before the trusted call
        self._state = "assessing"
        try:
            assessment = self._workspace.assess(
                attempt_id, result, timeout_seconds=_left(deadline), max_bytes=max_bytes,
            )
            _left(deadline)
        finally:
            if self._state == "assessing":
                self._state = "ready"
        if _result_key(result) != self._current_key:
            raise EvidenceRejected("command result changed during assessment")
        return assessment

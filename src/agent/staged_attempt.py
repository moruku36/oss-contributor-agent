"""Injection-only single-attempt lifecycle/workspace adapter; no execution backend.

The trusted lifecycle owns stage removal; the bridge only closes its descriptor.
Single-caller, not thread-safe. Bounds cannot preempt blocked trusted dependencies.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from src.agent.core import TestAssessment
from src.agent.filesystem_staging import ManifestFile
from src.agent.snapshot_workspace import SnapshotLimits, SnapshotWorkspace
from src.agent.staged_runner_bridge import StagedRunner, StagedRunnerBridge
from src.sandbox.docker_manager import (
    CommandResult, SandboxCleanupError, SandboxError, SandboxPolicy,
)


class LifecycleBackend(Protocol):
    """Trusted lifecycle dependency, deliberately without execute."""

    def create(self, attempt_id: str, *, policy: SandboxPolicy, timeout_seconds: float) -> None: ...
    def start(self, attempt_id: str, *, timeout_seconds: float) -> None: ...
    def remove(self, attempt_id: str, *, timeout_seconds: float) -> None: ...


class StagedAttemptAdapter:
    """One SandboxBackend and Workspace sharing a lazily constructed snapshot.

    No default runner, filesystem acquisition, Docker, provider or host execution.
    A failed create/start remains removable once using the same attempt ID.
    """

    def __init__(
        self, *, lifecycle: LifecycleBackend,
        workspace_factory: Callable[[str], SnapshotWorkspace],
        staging_parent_fd: int, manifest: tuple[ManifestFile, ...],
        runner: StagedRunner, limits: SnapshotLimits = SnapshotLimits(),
        stage_timeout_seconds: float = 30.0,
        max_command_timeout_seconds: float = 2700.0,
        max_output_bytes: int = 65536, max_dispatches: int = 32,
    ) -> None:
        if (not callable(workspace_factory)
                or any(not callable(getattr(lifecycle, name, None))
                       for name in ("create", "start", "remove"))):
            raise SandboxError("explicit trusted lifecycle and workspace factory required")
        if type(manifest) is not tuple:
            raise SandboxError("explicit manifest tuple required")
        self._lifecycle, self._factory = lifecycle, workspace_factory
        self._parent, self._manifest, self._runner = staging_parent_fd, manifest, runner
        self._bounds = dict(
            limits=limits, stage_timeout_seconds=stage_timeout_seconds,
            max_command_timeout_seconds=max_command_timeout_seconds,
            max_output_bytes=max_output_bytes, max_dispatches=max_dispatches,
        )
        self._state = "new"
        self._attempt_id: str | None = None
        self._workspace: SnapshotWorkspace | None = None
        self._bridge: StagedRunnerBridge | None = None
        self._remove_consumed = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def attempt_id(self) -> str | None:
        return self._attempt_id

    def _check_id(self, attempt_id: str) -> None:
        if type(attempt_id) is not str or self._attempt_id is None or attempt_id != self._attempt_id:
            raise SandboxError("mismatched attempt_id")

    def _require(self, attempt_id: str, state: str) -> None:
        self._check_id(attempt_id)
        if self._state != state:
            raise SandboxError(f"operation not allowed in state {self._state!r}")

    def create(self, attempt_id: str, *, policy: SandboxPolicy, timeout_seconds: float) -> None:
        if self._state != "new":
            raise SandboxError("attempt is single-use")
        if type(attempt_id) is not str or not attempt_id:
            raise SandboxError("explicit attempt ID required")
        # Consume before the first potentially allocating operation.
        self._attempt_id, self._state = attempt_id, "creating"
        try:
            self._lifecycle.create(attempt_id, policy=policy, timeout_seconds=timeout_seconds)
            self._workspace = self._factory(attempt_id)
            self._bridge = StagedRunnerBridge(
                attempt_id, workspace=self._workspace, staging_parent_fd=self._parent,
                manifest=self._manifest, runner=self._runner, **self._bounds,
            )
        except BaseException:
            self._state = "create_failed"
            raise
        self._state = "created"

    def start(self, attempt_id: str, *, timeout_seconds: float) -> None:
        self._require(attempt_id, "created")
        self._state = "starting"
        try:
            self._lifecycle.start(attempt_id, timeout_seconds=timeout_seconds)
        except BaseException:
            self._state = "start_failed"
            raise
        self._state = "running"

    def execute(
        self, attempt_id: str, command: str, *, timeout_seconds: float, max_output_bytes: int,
    ) -> CommandResult:
        self._require(attempt_id, "running")
        assert self._bridge is not None
        try:
            return self._bridge.execute(
                attempt_id, command, timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        except BaseException:
            self._state = "execute_failed"
            raise

    def remove(self, attempt_id: str, *, timeout_seconds: float) -> None:
        self._check_id(attempt_id)
        if self._remove_consumed or self._state not in {
            "created", "running", "create_failed", "start_failed", "execute_failed",
        }:
            raise SandboxError(f"remove not allowed in state {self._state!r}")
        self._remove_consumed, self._state = True, "removing"
        close_error = lifecycle_error = None
        if self._bridge is not None:
            try:
                self._bridge.close()
            except BaseException as error:
                close_error = error
        try:
            self._lifecycle.remove(attempt_id, timeout_seconds=timeout_seconds)
        except BaseException as error:
            lifecycle_error = error
        if close_error is not None or lifecycle_error is not None:
            self._state = "remove_failed"
            if close_error is not None and lifecycle_error is not None:
                raise SandboxCleanupError(attempt_id, lifecycle_error, close_error)
            if close_error is not None:
                raise close_error
            assert lifecycle_error is not None
            raise lifecycle_error
        self._state = "removed"

    def context(self, attempt_id: str, *, timeout_seconds: float, max_bytes: int) -> str:
        self._require(attempt_id, "running")
        assert self._workspace is not None
        return self._workspace.context(attempt_id, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    def apply_patch(
        self, attempt_id: str, patch: str, *, timeout_seconds: float, max_bytes: int,
    ) -> bool:
        self._require(attempt_id, "running")
        assert self._workspace is not None
        return self._workspace.apply_patch(
            attempt_id, patch, timeout_seconds=timeout_seconds, max_bytes=max_bytes,
        )

    def diff(self, attempt_id: str, *, timeout_seconds: float, max_bytes: int) -> str:
        self._require(attempt_id, "running")
        assert self._workspace is not None
        return self._workspace.diff(attempt_id, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    def assess(
        self, attempt_id: str, result: CommandResult, *, timeout_seconds: float, max_bytes: int,
    ) -> TestAssessment:
        self._require(attempt_id, "running")
        assert self._bridge is not None
        return self._bridge.assess(
            attempt_id, result, timeout_seconds=timeout_seconds, max_bytes=max_bytes,
        )

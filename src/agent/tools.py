"""Explicit memory-only tools; legacy Path APIs remain unimplemented.

Snapshot wrappers require the attempt ID and finite call bounds. They do not
open host files, run commands, or prove real sandbox binding/isolation.
"""

from __future__ import annotations

from pathlib import Path

from src.agent.snapshot_workspace import SnapshotWorkspace


def read_file(workspace: Path, relative_path: str) -> str:
    """Read a file inside the workspace."""
    raise NotImplementedError("Phase 2: file tools")


def write_file(workspace: Path, relative_path: str, content: str) -> None:
    """Write a file inside the workspace."""
    raise NotImplementedError("Phase 2: file tools")


def search_code(workspace: Path, pattern: str) -> list[str]:
    """Search the workspace with ripgrep; return matching locations."""
    raise NotImplementedError("Phase 2: search tool")


def apply_patch(workspace: Path, unified_diff: str) -> None:
    """Apply a unified diff inside the workspace."""
    raise NotImplementedError("Phase 2: patch tool")


def snapshot_read_file(
    workspace: SnapshotWorkspace, attempt_id: str, relative_path: str, *,
    timeout_seconds: float, max_bytes: int,
) -> str:
    """Read complete bounded text from the validated in-memory namespace."""
    return workspace.read_file(attempt_id, relative_path, timeout_seconds=timeout_seconds, max_bytes=max_bytes)


def snapshot_search_code(
    workspace: SnapshotWorkspace, attempt_id: str, literal: str, *,
    timeout_seconds: float, max_bytes: int, paths: tuple[str, ...] | None = None,
) -> str:
    """Search a literal substring; refuse oversized results instead of truncating."""
    return workspace.search_code(
        attempt_id, literal, paths=paths, timeout_seconds=timeout_seconds, max_bytes=max_bytes,
    )


def snapshot_apply_patch(
    workspace: SnapshotWorkspace, attempt_id: str, unified_diff: str, *,
    timeout_seconds: float, max_bytes: int,
) -> bool:
    """Apply the strict existing-file diff subset atomically, or raise."""
    return workspace.apply_patch(attempt_id, unified_diff, timeout_seconds=timeout_seconds, max_bytes=max_bytes)


def snapshot_diff(
    workspace: SnapshotWorkspace, attempt_id: str, *, timeout_seconds: float, max_bytes: int,
) -> str:
    """Return the complete deterministic prepared-base diff, or refuse."""
    return workspace.diff(attempt_id, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

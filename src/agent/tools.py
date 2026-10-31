"""Tools the coder agent may use inside the sandbox.

The tool surface is deliberately small and file-oriented:

- ``read_file`` / ``write_file`` / ``apply_patch`` for source files
- ``search_code`` (ripgrep) for locating relevant code and tests
- ``run_tests`` delegating to the sandbox (never a host shell)

Tools operate only within the sandbox workspace path. Any attempt to
escape the workspace (absolute paths, ``..``) is rejected.
"""

from __future__ import annotations

from pathlib import Path


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

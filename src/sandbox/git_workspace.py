"""Git workspace preparation inside the sandbox.

Responsibilities:
- Clone the target repository (shallow) into the sandbox workspace.
- Create a working branch from the project's default branch using
  the per-target branch prefix from configs/targets.yaml.
- Produce the final diff for the review report.
- Create commits that satisfy the project's conventions, including
  a DCO ``Signed-off-by`` line when the target requires one.
- Clean the workspace up after the attempt.
"""

from __future__ import annotations

from pathlib import Path


def prepare_workspace(repo: str, default_branch: str, branch_prefix: str) -> Path:
    """Clone ``repo`` and create the working branch; return its path."""
    raise NotImplementedError("Phase 2: workspace preparation")


def export_diff(workspace: Path) -> str:
    """Return the full diff of the working branch vs. the base branch."""
    raise NotImplementedError("Phase 2: diff export")


def make_commit(workspace: Path, message: str, sign_off: bool) -> str:
    """Create a commit; append a DCO sign-off line when ``sign_off``."""
    raise NotImplementedError("Phase 3: commit creation")


def cleanup_workspace(workspace: Path) -> None:
    """Delete the workspace directory completely."""
    raise NotImplementedError("Phase 2: workspace cleanup")

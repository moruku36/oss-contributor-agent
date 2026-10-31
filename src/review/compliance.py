"""Compliance guard: verify a patch obeys the project's rules.

Checks, all of which must pass before a review report is produced:

- The project's lint and format commands exit cleanly.
- Commit messages follow the project's convention
  (e.g. Conventional Commits) from configs/targets.yaml.
- A DCO ``Signed-off-by`` line is present when the target requires it.
- The diff touches only files relevant to the issue (no drive-by
  changes, no edits to CI or license files).

Any failed check fails the whole attempt (fail-closed): the patch is
discarded or returned to the coder loop, never shipped as-is.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ComplianceResult:
    """Outcome of the compliance checks for one patch."""

    passed: bool
    failures: list[str] = field(default_factory=list)


def check_commit_message(message: str, convention: str, sign_off_required: bool) -> list[str]:
    """Validate one commit message; return a list of violations."""
    raise NotImplementedError("Phase 3: commit message check")


def run_compliance(workspace: object, target: dict, commit_messages: list[str]) -> ComplianceResult:
    """Run lint/format/convention checks for a finished patch."""
    raise NotImplementedError("Phase 3: compliance guard")

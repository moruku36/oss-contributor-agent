"""GitHub API client for the Collector stage.

Responsibilities:
- List open issues for a target repository, filtered by watch labels.
- Detect whether an issue already has a pull request (human or bot)
  that addresses it, so the agent never duplicates existing work.
- Fetch repository metadata the pipeline needs (default branch,
  PR template presence).

All calls go through the GitHub REST/GraphQL API with a token held
*outside* the sandbox. Rate-limit responses are treated as a stop
signal for the current run (fail-closed), never retried aggressively.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IssueRef:
    """Minimal issue record passed between pipeline stages."""

    repo: str
    number: int
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    url: str = ""


class GitHubClient:
    """Thin wrapper over the GitHub API (implementation: Phase 1)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def list_open_issues(self, repo: str, labels: list[str]) -> list[IssueRef]:
        """Return open issues in ``repo`` carrying any of ``labels``."""
        raise NotImplementedError("Phase 1: GitHub issue listing")

    def find_existing_prs(self, repo: str, issue_number: int) -> list[dict]:
        """Return PRs that reference or claim to close ``issue_number``.

        A non-empty result means the issue must be skipped: someone is
        already working on it.
        """
        raise NotImplementedError("Phase 1: duplicate PR check")

    def get_default_branch(self, repo: str) -> str:
        """Return the repository's default branch name."""
        raise NotImplementedError("Phase 1: repository metadata")

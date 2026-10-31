"""Draft PR builder and the human review report.

Two outputs are produced for every successful attempt:

1. A review report for the human operator: issue summary, what was
   changed and why, the test commands that ran and their results,
   and a diff preview. The human approves or rejects from this.
2. After approval, a *draft* pull request built on the target
   project's PR template. The agent never creates a non-draft PR
   and never merges anything.

Every PR body includes the AI-assistance disclosure line below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Included verbatim in every PR body (see configs/agent_rules.yaml:
# ai_disclosure_in_pr). Hiding AI involvement would be a breach of
# the anti-spam ethics in docs/architecture.md.
AI_DISCLOSURE_LINE = (
    "Disclosure: this draft PR was prepared by an autonomous AI agent "
    "and reviewed by a human before submission. All tests were run "
    "locally in an isolated sandbox; logs are available on request."
)


@dataclass
class ReviewReport:
    """Human-facing summary of one successful patch attempt."""

    repo: str
    issue_number: int
    summary: str
    tests_run: list[str] = field(default_factory=list)
    diff_preview: str = ""


def build_review_report(attempt: object, issue: dict) -> ReviewReport:
    """Build the human review report for a successful attempt."""
    raise NotImplementedError("Phase 3: review report")


def build_pr_body(report: ReviewReport, pr_template: str | None) -> str:
    """Render the draft PR body from the report and project template."""
    raise NotImplementedError("Phase 3: PR body builder")


def submit_draft_pr(report: ReviewReport, body: str) -> str:
    """Create the draft PR on GitHub; return its URL. Human-gated."""
    raise NotImplementedError("Phase 3: draft PR submission")

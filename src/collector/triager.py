"""Deterministic, rule-based issue triage scoring.

This is the first triage stage described in docs/architecture.md
(section 6, "Triage scoring rubric"). It is pure and deterministic:
no network access and no LLM calls. Its job is to cheaply drop issues
that are clearly unsuitable and to pass plausible candidates on.

Scores land in one of three bands:

- ``score >= ACCEPT_THRESHOLD``  -> candidate for the sandbox stage
- ``SKIP_THRESHOLD <= score < ACCEPT_THRESHOLD`` -> borderline; the
  LLM triage stage makes the final call
- ``score < SKIP_THRESHOLD``     -> skip and record in the ledger

The rubric is intentionally explainable: every point is returned as a
human-readable reason so the daily report can show *why* an issue was
accepted or skipped.
"""

from __future__ import annotations

import re

# Thresholds mirror configs/agent_rules.yaml.
ACCEPT_THRESHOLD = 0.60
SKIP_THRESHOLD = 0.40

_LABEL_POINTS: dict[str, float] = {
    "good first issue": 0.25,
    "help wanted": 0.20,
    "bug": 0.15,
    "documentation": 0.20,
    "docs": 0.20,
}
_LABEL_CAP = 0.35

_FEATURE_REQUEST_LABELS = {"feature request", "feature-request"}

# Title words that signal a narrow, well-bounded change.
_SCOPE_KEYWORDS = ("typo", "docs", "documentation", "readme", "spelling", "broken link")
# Words that signal a broad, multi-part effort an agent should not attempt.
_BROAD_KEYWORDS = ("epic", "umbrella", "roadmap", "redesign", "overhaul")


def score_issue(issue: dict) -> tuple[float, list[str]]:
    """Score an issue's suitability for an autonomous fix attempt.

    Expected keys in ``issue`` (all optional; missing keys score zero):

    - ``labels``: list of label names (str)
    - ``title``: issue title (str)
    - ``body``: issue body (str)
    - ``has_reproduction``: True if the issue contains reproduction steps
    - ``mentions_tests``: True if the issue references tests or test files

    Returns ``(score, reasons)`` where score is clamped to [0.0, 1.0]
    and reasons explains every contribution to the score.
    """
    score = 0.0
    reasons: list[str] = []

    labels = [str(label).strip().lower() for label in issue.get("labels") or []]

    # --- Labels ------------------------------------------------------
    label_points = 0.0
    for label in labels:
        points = _LABEL_POINTS.get(label)
        if points:
            label_points += points
            reasons.append(f"label '{label}' (+{points:.2f})")
    if label_points > 0:
        capped = min(label_points, _LABEL_CAP)
        if capped < label_points:
            reasons.append(f"label points capped at {_LABEL_CAP:.2f}")
        score += capped
    if _FEATURE_REQUEST_LABELS.intersection(labels):
        score -= 0.15
        reasons.append("feature request label (-0.15): scope is usually too broad")

    # --- Body signals --------------------------------------------------
    body = issue.get("body") or ""
    if issue.get("has_reproduction"):
        score += 0.20
        reasons.append("reproduction steps present (+0.20)")
    else:
        reasons.append("no reproduction steps (+0.00)")

    if issue.get("mentions_tests"):
        score += 0.15
        reasons.append("tests mentioned in the issue (+0.15)")

    body_len = len(body)
    if body_len >= 400:
        score += 0.10
        reasons.append("detailed body, >= 400 chars (+0.10)")
    elif body_len >= 150:
        score += 0.05
        reasons.append("body >= 150 chars (+0.05)")
    else:
        reasons.append("body is thin, < 150 chars (+0.00)")

    # --- Scope signals -------------------------------------------------
    title = (issue.get("title") or "").lower()
    if any(keyword in title for keyword in _SCOPE_KEYWORDS):
        score += 0.15
        reasons.append("narrow-scope keyword in title (+0.15)")

    haystack = f"{title}\n{body.lower()}"
    if any(keyword in haystack for keyword in _BROAD_KEYWORDS):
        score -= 0.10
        reasons.append("broad-scope keyword such as epic/umbrella/roadmap (-0.10)")

    score = max(0.0, min(1.0, score))
    return round(score, 3), reasons


def verdict(
    score: float,
    accept_threshold: float = ACCEPT_THRESHOLD,
    skip_threshold: float = SKIP_THRESHOLD,
) -> str:
    """Map a numeric score to a triage band: accept / borderline / skip."""
    if score >= accept_threshold:
        return "accept"
    if score >= skip_threshold:
        return "borderline"
    return "skip"


# --- Signal derivation -------------------------------------------------
# score_issue() consumes two boolean signals that a human reader would
# spot in the issue body. The collector derives them with deliberately
# simple, explainable heuristics; they are weak signals by design and
# never decide anything on their own.

_REPRO_MARKERS = (
    "steps to reproduce",
    "how to reproduce",
    "reproduction steps",
    "repro steps",
    "to reproduce",
)

_TEST_MENTION = re.compile(
    r"\btests?\b|\btesting\b|_test\.go\b|\.test\.[a-z]+$|test file",
    re.IGNORECASE | re.MULTILINE,
)


def derive_signals(body: str | None) -> dict[str, bool]:
    """Derive ``has_reproduction`` / ``mentions_tests`` from an issue body."""
    text = (body or "").lower()
    return {
        "has_reproduction": any(marker in text for marker in _REPRO_MARKERS),
        "mentions_tests": bool(_TEST_MENTION.search(body or "")),
    }


def score_issue_ref(issue: object) -> tuple[float, list[str]]:
    """Score an ``IssueRef``-shaped object (repo/number/title/body/labels).

    Convenience wrapper used by the pipeline: derives the body signals,
    then delegates to :func:`score_issue`.
    """
    body = getattr(issue, "body", "") or ""
    payload = {
        "labels": list(getattr(issue, "labels", []) or []),
        "title": getattr(issue, "title", "") or "",
        "body": body,
        **derive_signals(body),
    }
    return score_issue(payload)

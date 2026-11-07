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

Body signals are kind-aware. Reproduction steps predict whether the
TDD loop can start, so they are scored for bug reports only. Every
other kind of issue (feature requests, enhancements, tasks) is scored
instead on *detail signals* — a stated use case, a concrete example,
an expected outcome — because "no reproduction steps" says nothing
about a request's quality. (The 2026-11-07 digest showed concrete
feature requests being skipped almost solely for lacking reproduction
steps.) An issue counts as a bug report when it carries the ``bug``
label or its body actually contains reproduction steps; everything
else is treated as a request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Thresholds mirror configs/agent_rules.yaml.
ACCEPT_THRESHOLD = 0.60
SKIP_THRESHOLD = 0.40


@dataclass(frozen=True)
class ScoringWeights:
    """Body-signal weights for :func:`score_issue`.

    The defaults mirror ``rules.triage_scoring`` in
    configs/agent_rules.yaml; the pipeline passes the configured
    values in, and a test keeps the two in sync (the same arrangement
    as the accept/skip thresholds above).
    """

    reproduction_points: float = 0.20  # bug report with reproduction steps
    detail_signal_points: float = 0.10  # per detail signal on a request
    detail_points_cap: float = 0.20  # cap on the detail-signal component

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


def _issue_kind(labels: list[str], *, has_reproduction: bool) -> str:
    """Classify an issue as ``"bug"`` or ``"request"``.

    Maintainer intent wins: a ``bug`` label settles it. A body that
    actually contains reproduction steps also reads as a bug report,
    whatever its labels. Everything else — feature requests,
    enhancements, documentation tasks, unlabelled requests — is a
    request, and is scored on detail signals instead of reproduction.
    """
    if "bug" in labels or has_reproduction:
        return "bug"
    return "request"


def score_issue(
    issue: dict, weights: ScoringWeights | None = None
) -> tuple[float, list[str]]:
    """Score an issue's suitability for an autonomous fix attempt.

    Expected keys in ``issue`` (all optional; missing keys score zero):

    - ``labels``: list of label names (str)
    - ``title``: issue title (str)
    - ``body``: issue body (str)
    - ``has_reproduction``: True if the issue contains reproduction steps
    - ``mentions_tests``: True if the issue references tests or test files
    - ``detail_signals``: names of detail signals found in the body
      (see :func:`derive_detail_signals`); scored for requests only

    ``weights`` carries the body-signal weights from
    configs/agent_rules.yaml; the defaults apply when it is omitted.

    Returns ``(score, reasons)`` where score is clamped to [0.0, 1.0]
    and reasons explains every contribution to the score.
    """
    weights = weights or ScoringWeights()
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

    # --- Body signals (kind-aware) -------------------------------------
    body = issue.get("body") or ""
    has_reproduction = bool(issue.get("has_reproduction"))
    if _issue_kind(labels, has_reproduction=has_reproduction) == "bug":
        if has_reproduction:
            score += weights.reproduction_points
            reasons.append(
                f"reproduction steps present (+{weights.reproduction_points:.2f})"
            )
        else:
            reasons.append("no reproduction steps (+0.00)")
    else:
        detail_signals = [str(name) for name in issue.get("detail_signals") or []]
        if detail_signals:
            raw = len(detail_signals) * weights.detail_signal_points
            detail_points = min(raw, weights.detail_points_cap)
            score += detail_points
            reasons.append(
                "request detail signals ("
                + ", ".join(detail_signals)
                + f") (+{detail_points:.2f})"
            )
            if raw > weights.detail_points_cap:
                reasons.append(
                    f"request detail points capped at {weights.detail_points_cap:.2f}"
                )
        else:
            reasons.append(
                "request with no detail signals "
                "(use case / example / expected outcome) (+0.00)"
            )

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
        "has_reproduction": any(
            re.search(re.escape(marker) + r"[ \t:#]*\s*[^\s:#]", text)
            for marker in _REPRO_MARKERS
        ),
        "mentions_tests": bool(_TEST_MENTION.search(body or "")),
    }


# Detail signals: evidence that a *request* (anything that is not a
# bug report) is concrete enough to act on. Each name is worth
# ScoringWeights.detail_signal_points, capped like the label points.
_USE_CASE_MARKERS = (
    "use case",
    "use-case",
    "as a user",
    "ユースケース",
    "利用シーン",
)

_EXPECTED_OUTCOME_MARKERS = (
    "expected behavior",
    "expected behaviour",
    "expected outcome",
    "acceptance criteria",
    "describe the solution",
    "proposed solution",
    "proposal:",
    "期待する動作",
    "期待結果",
)


def derive_detail_signals(body: str | None) -> list[str]:
    """Return the detail-signal names found in an issue body.

    The result feeds the request branch of :func:`score_issue`: a use
    case (why this is wanted), a concrete example (a code block or an
    explicit "example:"), and an expected outcome (what should happen
    when the request is done). Order is fixed so reason strings are
    deterministic.
    """
    text = body or ""
    lowered = text.lower()
    signals: list[str] = []
    if any(marker in lowered for marker in _USE_CASE_MARKERS):
        signals.append("use case")
    if "```" in text or "example:" in lowered or "例えば" in text:
        signals.append("example")
    if any(marker in lowered for marker in _EXPECTED_OUTCOME_MARKERS):
        signals.append("expected outcome")
    return signals


def score_issue_ref(
    issue: object, weights: ScoringWeights | None = None
) -> tuple[float, list[str]]:
    """Score an ``IssueRef``-shaped object (repo/number/title/body/labels).

    Convenience wrapper used by the pipeline: derives the body signals,
    then delegates to :func:`score_issue`.
    """
    body = getattr(issue, "body", "") or ""
    payload = {
        "labels": list(getattr(issue, "labels", []) or []),
        "title": getattr(issue, "title", "") or "",
        "body": body,
        "detail_signals": derive_detail_signals(body),
        **derive_signals(body),
    }
    return score_issue(payload, weights)

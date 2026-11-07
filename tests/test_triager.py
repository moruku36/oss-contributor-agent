"""Tests for the deterministic triage scorer (src/collector/triager.py)."""

from src.collector.triager import ScoringWeights, score_issue, score_issue_ref, verdict

FEATURE_BODY = (
    "Use case: our on-call team rotates weekly and each person wants "
    "the dashboard to open on their own team's panels. Today everyone "
    "has to re-apply the same filters by hand every morning. "
    "Expected behavior: Grafana remembers the last panel filter per "
    "user and restores it on load, with a reset button in the panel "
    "menu. This is a small, self-contained change to the dashboard "
    "page state handling and does not affect shared dashboards."
)

LONG_BODY = (
    "Steps to reproduce: 1. Start the server with an empty config file. "
    "2. Open the status page. Expected: the page loads. Actual: the server "
    "panics with a nil pointer error. The relevant code is in the config "
    "loader, and there is an existing test file covering that package, so a "
    "regression test can be added next to it. Environment: Linux, Go 1.22. "
    "This has been confirmed on the latest main branch by two reporters."
)


def test_good_first_issue_with_repro_scores_high() -> None:
    issue = {
        "labels": ["good first issue", "bug"],
        "title": "Server panics when the config file is empty",
        "body": LONG_BODY,
        "has_reproduction": True,
        "mentions_tests": True,
    }
    score, reasons = score_issue(issue)
    assert score >= 0.60
    assert verdict(score) == "accept"
    assert reasons  # every score must be explainable


def test_vague_feature_request_scores_low() -> None:
    issue = {
        "labels": ["feature request"],
        "title": "Add a plugin system",
        "body": "It would be nice to have plugins someday.",
        "has_reproduction": False,
        "mentions_tests": False,
    }
    score, _ = score_issue(issue)
    assert score < 0.40
    assert verdict(score) == "skip"


def test_documentation_typo_survives_triage() -> None:
    issue = {
        "labels": ["documentation"],
        "title": "Typo in README installation section",
        "body": (
            "The installation section of the README says 'promethues' "
            "instead of 'prometheus' in the third paragraph, and the "
            "linked example config file name is also misspelled. The "
            "exact location is the 'Getting started' heading, first "
            "code block. Fixing this only requires editing README.md."
        ),
        "has_reproduction": False,
        "mentions_tests": False,
    }
    score, _ = score_issue(issue)
    # Not an automatic accept, but must not be skipped outright.
    assert score >= 0.40
    assert verdict(score) in {"borderline", "accept"}


def test_unlabeled_bug_with_repro_is_borderline() -> None:
    issue = {
        "labels": ["bug"],
        "title": "Crash on startup with an empty configuration",
        "body": LONG_BODY,
        "has_reproduction": True,
        "mentions_tests": False,
    }
    score, _ = score_issue(issue)
    assert 0.40 <= score < 0.60
    assert verdict(score) == "borderline"


def test_empty_issue_scores_zero() -> None:
    score, reasons = score_issue({"labels": [], "title": "", "body": ""})
    assert score == 0.0
    assert verdict(score) == "skip"
    assert reasons


# --- Kind-aware body signals -------------------------------------------
# Bug reports are scored on reproduction steps; requests (feature
# requests, enhancements, unlabelled tasks) are scored on detail
# signals instead, because "no reproduction steps" says nothing
# about a request's quality.


def test_concrete_feature_request_reaches_the_llm_band() -> None:
    issue = {
        "labels": ["good first issue", "feature request"],
        "title": "Remember the last panel filter per user",
        "body": FEATURE_BODY,
        "has_reproduction": False,
        "mentions_tests": False,
        "detail_signals": ["use case", "expected outcome"],
    }
    score, reasons = score_issue(issue)
    # labels 0.25 - 0.15, detail 2 x 0.10, body >= 400 chars +0.10
    assert score == 0.40
    assert verdict(score) == "borderline"
    assert any("detail signals" in reason for reason in reasons)
    assert not any("reproduction" in reason for reason in reasons)


def test_unlabelled_request_is_scored_on_detail_not_reproduction() -> None:
    # Mirrors the 2026-11-07 Grafana digest entries: 'help wanted'
    # only, no kind label, no reproduction steps, concrete body. The
    # old rubric skipped these at 0.30 for lacking reproduction steps.
    issue = {
        "labels": ["help wanted"],
        "title": "Gauge: setting neutral/min/max from query",
        "body": FEATURE_BODY,
        "has_reproduction": False,
        "mentions_tests": False,
        "detail_signals": ["use case", "expected outcome"],
    }
    score, _ = score_issue(issue)
    assert score == 0.50  # 0.20 label + 0.20 detail + 0.10 body
    assert verdict(score) == "borderline"


def test_feature_request_detail_points_are_capped() -> None:
    issue = {
        "labels": ["feature request"],
        "title": "Add export presets",
        "body": FEATURE_BODY,
        "has_reproduction": False,
        "mentions_tests": False,
        "detail_signals": ["use case", "example", "expected outcome"],
    }
    score, reasons = score_issue(issue)
    # 3 signals would be 0.30 uncapped; the component caps at 0.20.
    # labels -0.15, detail +0.20, body +0.10
    assert score == 0.15
    assert any("capped" in reason for reason in reasons)


def test_request_without_detail_signals_gets_no_detail_points() -> None:
    issue = {
        "labels": ["enhancement"],
        "title": "Make the thing better",
        "body": "It should just be better than it is now, somehow.",
        "has_reproduction": False,
        "mentions_tests": False,
        "detail_signals": [],
    }
    score, reasons = score_issue(issue)
    assert score == 0.0
    assert verdict(score) == "skip"
    assert any("no detail signals" in reason for reason in reasons)


def test_bug_label_keeps_reproduction_scoring() -> None:
    issue = {
        "labels": ["bug"],
        "title": "Something broke",
        "body": "It broke after the update and nobody knows why yet.",
        "has_reproduction": False,
        "mentions_tests": False,
        "detail_signals": ["use case"],  # detail signals do not apply to bugs
    }
    score, reasons = score_issue(issue)
    assert score == 0.15  # the bug label only, exactly as before
    assert "no reproduction steps (+0.00)" in reasons


def test_scoring_weights_are_configurable() -> None:
    weights = ScoringWeights(
        reproduction_points=0.30,
        detail_signal_points=0.05,
        detail_points_cap=0.05,
    )
    bug = {
        "labels": ["bug"],
        "title": "Crash on startup with an empty configuration",
        "body": LONG_BODY,
        "has_reproduction": True,
        "mentions_tests": False,
    }
    score, _ = score_issue(bug, weights)
    assert score == 0.55  # 0.15 label + 0.30 reproduction + 0.10 body

    request = {
        "labels": ["help wanted"],
        "title": "Gauge: setting neutral/min/max from query",
        "body": FEATURE_BODY,
        "has_reproduction": False,
        "mentions_tests": False,
        "detail_signals": ["use case", "expected outcome"],
    }
    score, _ = score_issue(request, weights)
    assert score == 0.35  # 0.20 label + 0.05 detail (capped) + 0.10 body


class _Ref:
    """Minimal IssueRef-shaped stub for score_issue_ref."""

    def __init__(self, labels: list[str], title: str, body: str) -> None:
        self.repo = "grafana/grafana"
        self.number = 1
        self.labels = labels
        self.title = title
        self.body = body
        self.url = "https://github.com/grafana/grafana/issues/1"


def test_score_issue_ref_derives_detail_signals_for_requests() -> None:
    ref = _Ref(
        ["help wanted"],
        "Gauge: setting neutral/min/max from query",
        FEATURE_BODY + "\n\nExample:\n```\nneutral: 0\n```",
    )
    score, reasons = score_issue_ref(ref)
    assert score == 0.50  # 0.20 label + 0.20 detail (capped) + 0.10 body
    assert verdict(score) == "borderline"
    assert any(
        "use case" in reason and "example" in reason for reason in reasons
    )



def test_reproduction_without_bug_label_uses_bug_scoring() -> None:
    ref = _Ref(["enhancement"], "Failure", "Steps to reproduce: run it. Use case: daily work")
    score, reasons = score_issue_ref(ref)
    assert score == 0.20
    assert "reproduction steps present (+0.20)" in reasons
    assert not any("detail signals" in reason for reason in reasons)


def test_empty_reproduction_template_keeps_request_scoring() -> None:
    ref = _Ref([], "Add export", "Use case: daily work\n### Steps to reproduce\n\n### Expected behavior\nsave a file")
    score, reasons = score_issue_ref(ref)
    assert score == 0.20
    assert any("request detail signals" in reason for reason in reasons)

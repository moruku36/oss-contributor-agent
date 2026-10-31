"""Tests for the deterministic triage scorer (src/collector/triager.py)."""

from src.collector.triager import score_issue, verdict

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

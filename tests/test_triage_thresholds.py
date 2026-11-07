"""Boundary tests for triage bands and collector signal derivation."""

import pytest

from src.collector.triager import derive_detail_signals, derive_signals, verdict


@pytest.mark.parametrize(
    "score, expected",
    [
        (1.0, "accept"),
        (0.60, "accept"),     # exactly at the accept threshold -> candidate
        (0.599, "borderline"),
        (0.40, "borderline"),  # exactly at the skip threshold -> LLM band
        (0.399, "skip"),
        (0.0, "skip"),
    ],
)
def test_verdict_band_boundaries(score: float, expected: str) -> None:
    assert verdict(score) == expected


def test_verdict_honours_configured_thresholds() -> None:
    # The pipeline passes agent_rules.yaml values through; the band
    # edges must move with them.
    assert verdict(0.55, accept_threshold=0.50, skip_threshold=0.30) == "accept"
    assert verdict(0.35, accept_threshold=0.50, skip_threshold=0.30) == "borderline"
    assert verdict(0.25, accept_threshold=0.50, skip_threshold=0.30) == "skip"


def test_derive_signals_detects_reproduction_steps() -> None:
    signals = derive_signals("Steps to reproduce:\n1. run it\n2. watch it fail")
    assert signals["has_reproduction"] is True
    assert signals["mentions_tests"] is False


def test_derive_signals_detects_test_mentions() -> None:
    signals = derive_signals("The existing unit test for this parser fails.")
    assert signals["mentions_tests"] is True
    assert signals["has_reproduction"] is False


def test_derive_signals_handles_empty_body() -> None:
    assert derive_signals("") == {
        "has_reproduction": False,
        "mentions_tests": False,
    }
    assert derive_signals(None) == {
        "has_reproduction": False,
        "mentions_tests": False,
    }


@pytest.mark.parametrize("body, expected", [
    (None, []), ("", []), ("a vague request", []),
    ("USE CASE: daily work", ["use case"]),
    ("Example: foo", ["example"]),
    ("```python\nprint(1)\n```", ["example"]),
    ("Expected behaviour: done", ["expected outcome"]),
    ("ユースケース: 集計。例えば CSV。期待結果: 保存。",
     ["use case", "example", "expected outcome"]),
    ("Use case: a use case. Example: x. Expected outcome: y",
     ["use case", "example", "expected outcome"]),
    ("Steps to reproduce: run it", []),
])
def test_detail_signal_derivation(body, expected) -> None:
    assert derive_detail_signals(body) == expected


@pytest.mark.parametrize("body", ["Steps to reproduce:", "### Steps to reproduce\n\n", "Steps to reproduce:\n\n### Expected behavior\nok"])
def test_empty_reproduction_heading_is_not_a_reproduction(body) -> None:
    assert derive_signals(body)["has_reproduction"] is False

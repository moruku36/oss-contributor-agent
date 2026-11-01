"""Boundary tests for triage bands and collector signal derivation."""

import pytest

from src.collector.triager import derive_signals, verdict


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

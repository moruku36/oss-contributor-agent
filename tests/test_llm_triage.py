"""Tests for the LLM triage stage (src/collector/llm_triage.py)."""

import json

import pytest

from src.collector.github_client import IssueRef
from src.collector.llm_triage import (
    LLMTriageError,
    build_llm_triager,
    parse_verdict,
)

ISSUE = IssueRef(repo="o/r", number=1, title="Fix typo", body="details")


def test_no_provider_configured_means_no_triager() -> None:
    # The pipeline relies on this: None => borderline issues stay in
    # the explicit pending_llm state instead of being guessed at.
    assert build_llm_triager({}) is None
    assert build_llm_triager({"LLM_TRIAGE_PROVIDER": ""}) is None


def test_unknown_provider_is_a_configuration_error() -> None:
    with pytest.raises(LLMTriageError):
        build_llm_triager({"LLM_TRIAGE_PROVIDER": "carrier-pigeon"})


def test_provider_without_api_key_is_a_configuration_error() -> None:
    with pytest.raises(LLMTriageError):
        build_llm_triager({"LLM_TRIAGE_PROVIDER": "openai"})


def test_parse_verdict_accepts_clean_json() -> None:
    verdict = parse_verdict('{"accept": true, "rationale": "narrow and clear"}')
    assert verdict.accepted is True
    assert verdict.rationale == "narrow and clear"


def test_parse_verdict_tolerates_surrounding_prose() -> None:
    verdict = parse_verdict(
        'Here is my decision: {"accept": false, "rationale": "too broad"} — done'
    )
    assert verdict.accepted is False


@pytest.mark.parametrize(
    "bad",
    [
        "I think this one is fine, probably accept.",   # no JSON at all
        '{"verdict": "accept"}',                        # wrong shape
        '{"accept": "yes", "rationale": "sure"}',       # accept not boolean
        '{"accept": true}',                             # rationale missing
        "{not json",                                    # malformed JSON
    ],
)
def test_parse_verdict_rejects_unusable_output(bad: str) -> None:
    # Fail-closed: an unparseable verdict must raise, so the caller
    # leaves the issue pending instead of acting on a guess.
    with pytest.raises(LLMTriageError):
        parse_verdict(bad)


def test_openai_provider_round_trip_with_fake_transport() -> None:
    captured: dict = {}

    def transport(url: str, headers: dict, payload: dict) -> tuple[int, bytes]:
        captured["url"] = url
        captured["payload"] = payload
        body = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"accept": true, "rationale": "small docs fix"}'
                        )
                    }
                }
            ]
        }
        return 200, json.dumps(body).encode()

    triager = build_llm_triager(
        {"LLM_TRIAGE_PROVIDER": "openai", "OPENAI_API_KEY": "k"},
        transport=transport,
    )
    assert triager is not None
    verdict = triager.judge(ISSUE, score=0.5, reasons=["label 'bug' (+0.15)"])
    assert verdict.accepted is True
    assert verdict.rationale == "small docs fix"
    assert captured["url"].endswith("/chat/completions")
    prompt_text = captured["payload"]["messages"][0]["content"]
    assert "Fix typo" in prompt_text


def test_provider_http_error_becomes_triage_error() -> None:
    def transport(url: str, headers: dict, payload: dict) -> tuple[int, bytes]:
        return 500, b"internal error"

    triager = build_llm_triager(
        {"LLM_TRIAGE_PROVIDER": "openai", "OPENAI_API_KEY": "k"},
        transport=transport,
    )
    assert triager is not None
    with pytest.raises(LLMTriageError):
        triager.judge(ISSUE, score=0.5, reasons=[])

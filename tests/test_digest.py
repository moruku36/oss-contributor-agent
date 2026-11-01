"""Tests for the daily digest (src/collector/digest.py)."""

from datetime import date
from pathlib import Path

from src.collector import ledger as ledger_mod
from src.collector.digest import (
    RepoRun,
    RunSummary,
    TriageDecision,
    render_digest,
    write_digest,
)


def _summary() -> RunSummary:
    return RunSummary(
        run_date=date(2026, 11, 1),
        repos=[
            RepoRun(repo="grafana/grafana", mode="collect-only", seen=3, new=2),
            RepoRun(repo="o/limited", mode="collect-only",
                    error="GitHub rate limit exhausted"),
        ],
        decisions=[
            TriageDecision(
                repo="grafana/grafana", number=101, title="Typo in docs",
                url="https://github.com/grafana/grafana/issues/101",
                score=0.80, outcome=ledger_mod.OUTCOME_CANDIDATE,
                reasons=["label 'documentation' (+0.20)"], is_new=True,
            ),
            TriageDecision(
                repo="grafana/grafana", number=102, title="Vague idea",
                url="https://github.com/grafana/grafana/issues/102",
                score=0.10, outcome=ledger_mod.OUTCOME_SKIPPED_LOW_SCORE,
                reasons=["body is thin, < 150 chars (+0.00)"], is_new=True,
            ),
            TriageDecision(
                repo="grafana/grafana", number=103, title="Borderline bug",
                url="https://github.com/grafana/grafana/issues/103",
                score=0.45, outcome=ledger_mod.OUTCOME_PENDING_LLM,
                reasons=["label 'bug' (+0.15)"],
            ),
        ],
        still_pending_llm=1,
        notes=["LLM トリアージは未設定です。"],
    )


def test_render_digest_contains_outcomes_and_reasons() -> None:
    text = render_digest(_summary())
    assert "2026-11-01" in text
    assert "grafana/grafana" in text
    assert "Typo in docs" in text
    assert "候補" in text
    assert "スコア不足" in text
    assert "LLM 判定待ち" in text
    assert "label 'documentation' (+0.20)" in text  # reasons are shown
    assert "rate limit" in text                    # repo failure is honest
    assert "LLM 判定待ちの残件: 1 件" in text


def test_render_digest_handles_an_empty_day() -> None:
    summary = RunSummary(run_date=date(2026, 11, 2))
    text = render_digest(summary)
    assert "新規 Issue はありませんでした" in text
    assert "判定対象の Issue はありませんでした" in text


def test_write_digest_uses_dated_filename(tmp_path: Path) -> None:
    path = write_digest(tmp_path / "reports", _summary())
    assert path.name == "digest-2026-11-01.md"
    assert path.exists()
    assert "デイリーダイジェスト" in path.read_text(encoding="utf-8")

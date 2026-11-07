"""Tests for the SQLite ledger (src/collector/ledger.py)."""

from pathlib import Path

from src.collector.github_client import IssueRef
from src.collector.ledger import (
    OUTCOME_CANDIDATE,
    OUTCOME_PENDING_LLM,
    OUTCOME_SKIPPED_LOW_SCORE,
    TERMINAL_OUTCOMES,
    Ledger,
)


def _issue(number: int, title: str = "title") -> IssueRef:
    return IssueRef(
        repo="o/r",
        number=number,
        title=title,
        url=f"https://github.com/o/r/issues/{number}",
    )


def test_upsert_seen_reports_new_then_known(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite3") as store:
        assert store.upsert_seen(_issue(1)) is True
        assert store.upsert_seen(_issue(1)) is False
        assert store.upsert_seen(_issue(2)) is True


def test_upsert_seen_refreshes_title_on_later_sightings(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite3") as store:
        store.upsert_seen(_issue(1, "old title"), now="2026-01-01T00:00:00+00:00")
        store.upsert_seen(_issue(1, "new title"), now="2026-01-02T00:00:00+00:00")
        row = store.get("o/r", 1)
        assert row is not None
        assert row.title == "new title"
        assert row.first_seen_at == "2026-01-01T00:00:00+00:00"
        assert row.last_seen_at == "2026-01-02T00:00:00+00:00"


def test_record_triage_round_trips(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite3") as store:
        store.upsert_seen(_issue(5))
        store.record_triage(
            "o/r",
            5,
            score=0.45,
            reasons=["label 'bug' (+0.15)"],
            outcome=OUTCOME_PENDING_LLM,
        )
        row = store.get("o/r", 5)
        assert row is not None
        assert row.triage_score == 0.45
        assert row.triage_reasons == ["label 'bug' (+0.15)"]
        assert row.outcome == OUTCOME_PENDING_LLM
        assert row.llm_verdict is None


def test_record_triage_stores_llm_verdict(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite3") as store:
        store.upsert_seen(_issue(6))
        store.record_triage(
            "o/r",
            6,
            score=0.5,
            reasons=[],
            outcome=OUTCOME_CANDIDATE,
            llm_verdict={"accepted": True, "rationale": "narrow", "model": "m"},
            pr_url=None,
        )
        row = store.get("o/r", 6)
        assert row is not None
        assert row.llm_verdict == {
            "accepted": True,
            "rationale": "narrow",
            "model": "m",
        }


def test_pending_llm_listing_and_outcome_counts(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.sqlite3") as store:
        for number in (1, 2, 3):
            store.upsert_seen(_issue(number))
        store.record_triage("o/r", 1, score=0.5, reasons=[],
                            outcome=OUTCOME_PENDING_LLM)
        store.record_triage("o/r", 2, score=0.1, reasons=[],
                            outcome=OUTCOME_SKIPPED_LOW_SCORE)
        store.record_triage("o/r", 3, score=0.5, reasons=[],
                            outcome=OUTCOME_PENDING_LLM)
        pending = store.pending_llm()
        assert [row.issue_number for row in pending] == [1, 3]
        counts = store.count_by_outcome()
        assert counts[OUTCOME_PENDING_LLM] == 2
        assert counts[OUTCOME_SKIPPED_LOW_SCORE] == 1


def test_pending_and_infra_outcomes_are_not_terminal() -> None:
    # These two states must be retried by later runs, never treated as
    # final decisions.
    assert OUTCOME_PENDING_LLM not in TERMINAL_OUTCOMES
    assert "infra_error" not in TERMINAL_OUTCOMES
    assert OUTCOME_CANDIDATE in TERMINAL_OUTCOMES


def test_candidates_filter_order_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    with Ledger(path) as store:
        assert store.candidates() == []
        for number, day, outcome in [(3, 2, OUTCOME_CANDIDATE), (2, 1, OUTCOME_CANDIDATE),
                                     (1, 1, OUTCOME_CANDIDATE), (4, 1, OUTCOME_PENDING_LLM),
                                     (5, 1, OUTCOME_SKIPPED_LOW_SCORE)]:
            store.upsert_seen(_issue(number), now=f"2026-11-0{day}T00:00:00+00:00")
            store.record_triage("o/r", number, score=0.65, reasons=["reason"], outcome=outcome)
        other = IssueRef(repo="a/r", number=9, title="other", url="")
        store.upsert_seen(other)
        store.record_triage("a/r", 9, score=0.7, reasons=[], outcome=OUTCOME_CANDIDATE)
    with Ledger(path) as store:
        rows = store.candidates()
        assert [(r.repo, r.issue_number) for r in rows] == [("a/r", 9), ("o/r", 1), ("o/r", 2), ("o/r", 3)]
        assert rows[1].triage_score == 0.65
        assert rows[1].triage_reasons == ["reason"]
        store.set_outcome("o/r", 1, "attempted")
        assert len(store.candidates()) == 3

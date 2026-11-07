"""The ledger: the agent's persistent memory (architecture section 8).

A single SQLite database (``state/ledger.sqlite3`` by default, git-
ignored) with one row per (repo, issue_number) ever seen. It records
the rule score, the reasons, the LLM verdict for borderline issues and
the outcome, so the same issue is never triaged twice and every
decision stays auditable.

Phase 1 outcome values (architecture section 14 taxonomy, plus the two
states Phase 1 needs before any attempt exists):

- ``candidate``           rule/LLM triage accepted; awaits a later phase
- ``pending_llm``         borderline band, no LLM verdict yet (fail-closed)
- ``skipped_low_score``   rule score below the skip threshold
- ``skipped_llm_reject``  LLM triage rejected a borderline issue
- ``skipped_duplicate``   an open PR already addresses the issue
- ``infra_error``         a check failed; the issue is retried next run
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

OUTCOME_CANDIDATE = "candidate"
OUTCOME_PENDING_LLM = "pending_llm"
OUTCOME_SKIPPED_LOW_SCORE = "skipped_low_score"
OUTCOME_SKIPPED_LLM_REJECT = "skipped_llm_reject"
OUTCOME_SKIPPED_DUPLICATE = "skipped_duplicate"
OUTCOME_INFRA_ERROR = "infra_error"

# Outcomes after which an issue is never automatically re-triaged.
# ``pending_llm`` and ``infra_error`` are deliberately absent: both are
# retried on a later run.
TERMINAL_OUTCOMES = frozenset(
    {
        OUTCOME_CANDIDATE,
        OUTCOME_SKIPPED_LOW_SCORE,
        OUTCOME_SKIPPED_LLM_REJECT,
        OUTCOME_SKIPPED_DUPLICATE,
        "attempted",
        "tests_failed",
        "compliance_failed",
        "report_ready",
        "draft_created",
        "human_rejected",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    repo           TEXT NOT NULL,
    issue_number   INTEGER NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    triage_score   REAL,
    triage_reasons TEXT,
    llm_verdict    TEXT,
    outcome        TEXT,
    attempts_used  INTEGER NOT NULL DEFAULT 0,
    pr_url         TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (repo, issue_number)
)
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class LedgerRow:
    repo: str
    issue_number: int
    title: str
    url: str
    first_seen_at: str
    last_seen_at: str
    triage_score: float | None
    triage_reasons: list[str]
    llm_verdict: dict | None
    outcome: str | None
    attempts_used: int
    pr_url: str | None
    updated_at: str


class Ledger:
    """SQLite-backed issue ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------

    def upsert_seen(self, issue: object, *, now: str | None = None) -> bool:
        """Record that ``issue`` was observed. Returns True if it is new."""
        now = now or utcnow_iso()
        repo = str(getattr(issue, "repo"))
        number = int(getattr(issue, "number"))
        existing = self.get(repo, number)
        if existing is None:
            self._conn.execute(
                "INSERT INTO issues (repo, issue_number, title, url,"
                " first_seen_at, last_seen_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    repo,
                    number,
                    str(getattr(issue, "title", "") or ""),
                    str(getattr(issue, "url", "") or ""),
                    now,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return True
        self._conn.execute(
            "UPDATE issues SET title = ?, url = ?, last_seen_at = ?,"
            " updated_at = ? WHERE repo = ? AND issue_number = ?",
            (
                str(getattr(issue, "title", "") or ""),
                str(getattr(issue, "url", "") or ""),
                now,
                now,
                repo,
                number,
            ),
        )
        self._conn.commit()
        return False

    def record_triage(
        self,
        repo: str,
        issue_number: int,
        *,
        score: float,
        reasons: list[str],
        outcome: str,
        llm_verdict: dict | None = None,
        pr_url: str | None = None,
        now: str | None = None,
    ) -> None:
        """Store a triage decision for an issue already in the ledger."""
        now = now or utcnow_iso()
        self._conn.execute(
            "UPDATE issues SET triage_score = ?, triage_reasons = ?,"
            " llm_verdict = ?, outcome = ?, pr_url = ?, updated_at = ?"
            " WHERE repo = ? AND issue_number = ?",
            (
                score,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(llm_verdict, ensure_ascii=False)
                if llm_verdict is not None
                else None,
                outcome,
                pr_url,
                now,
                repo,
                issue_number,
            ),
        )
        self._conn.commit()

    def set_outcome(
        self, repo: str, issue_number: int, outcome: str, *, now: str | None = None
    ) -> None:
        """Change only the outcome (e.g. an LLM verdict for a pending issue)."""
        now = now or utcnow_iso()
        self._conn.execute(
            "UPDATE issues SET outcome = ?, updated_at = ?"
            " WHERE repo = ? AND issue_number = ?",
            (outcome, now, repo, issue_number),
        )
        self._conn.commit()

    # -- reads ----------------------------------------------------------

    def get(self, repo: str, issue_number: int) -> LedgerRow | None:
        row = self._conn.execute(
            "SELECT * FROM issues WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        ).fetchone()
        return self._to_row(row) if row else None

    def pending_llm(self, repo: str | None = None) -> list[LedgerRow]:
        """Issues still waiting for an LLM triage verdict."""
        if repo is None:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE outcome = ? ORDER BY repo, issue_number",
                (OUTCOME_PENDING_LLM,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE outcome = ? AND repo = ?"
                " ORDER BY issue_number",
                (OUTCOME_PENDING_LLM, repo),
            ).fetchall()
        return [self._to_row(row) for row in rows]

    def candidates(self) -> list[LedgerRow]:
        """Issues currently sitting in the candidate pool.

        These passed triage (by rules or by LLM) and stay candidates
        until a later phase acts on them; the daily digest lists them
        so the pool never becomes invisible. Ordered by repo, then by
        when each issue was first seen.
        """
        rows = self._conn.execute(
            "SELECT * FROM issues WHERE outcome = ?"
            " ORDER BY repo, first_seen_at, issue_number",
            (OUTCOME_CANDIDATE,),
        ).fetchall()
        return [self._to_row(row) for row in rows]

    def count_by_outcome(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM issues"
            " GROUP BY outcome ORDER BY outcome"
        ).fetchall()
        return {str(row["outcome"]): int(row["n"]) for row in rows}

    @staticmethod
    def _to_row(row: sqlite3.Row) -> LedgerRow:
        reasons_raw = row["triage_reasons"]
        verdict_raw = row["llm_verdict"]
        return LedgerRow(
            repo=str(row["repo"]),
            issue_number=int(row["issue_number"]),
            title=str(row["title"]),
            url=str(row["url"]),
            first_seen_at=str(row["first_seen_at"]),
            last_seen_at=str(row["last_seen_at"]),
            triage_score=(
                float(row["triage_score"]) if row["triage_score"] is not None else None
            ),
            triage_reasons=json.loads(reasons_raw) if reasons_raw else [],
            llm_verdict=json.loads(verdict_raw) if verdict_raw else None,
            outcome=row["outcome"],
            attempts_used=int(row["attempts_used"]),
            pr_url=row["pr_url"],
            updated_at=str(row["updated_at"]),
        )

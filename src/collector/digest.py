"""Daily digest: the operator-facing report of one collection pass.

Written as Markdown under ``reports/`` (git-ignored; scheduled runs
upload it as a CI artifact instead). The digest is deliberately
honest: it lists what was seen, what triage decided and why, what is
still waiting for an LLM verdict, which issues are sitting in the
candidate pool, and what went wrong — including the "nothing worth
doing today" case, which is a legitimate result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.collector import ledger as ledger_mod


@dataclass
class TriageDecision:
    """One triage outcome produced (or revisited) during a run."""

    repo: str
    number: int
    title: str
    url: str
    score: float | None
    outcome: str
    reasons: list[str] = field(default_factory=list)
    llm_rationale: str = ""
    is_new: bool = False


@dataclass
class RepoRun:
    """Per-target results of one collection pass."""

    repo: str
    mode: str
    seen: int = 0
    new: int = 0
    error: str = ""  # non-empty when collection for this repo failed


@dataclass
class CandidatePoolEntry:
    """One issue currently waiting in the candidate pool."""

    repo: str
    number: int
    title: str
    url: str
    score: float | None
    age_days: int  # days since the issue was first seen by the collector


@dataclass
class RunSummary:
    """Everything the digest needs about one collection pass."""

    run_date: date
    repos: list[RepoRun] = field(default_factory=list)
    decisions: list[TriageDecision] = field(default_factory=list)
    still_pending_llm: int = 0
    candidate_pool: list[CandidatePoolEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def decisions_with(self, *outcomes: str) -> list[TriageDecision]:
        return [d for d in self.decisions if d.outcome in outcomes]


_OUTCOME_HEADINGS: list[tuple[str, tuple[str, ...]]] = [
    ("候補（candidate）", (ledger_mod.OUTCOME_CANDIDATE,)),
    ("スキップ：スコア不足", (ledger_mod.OUTCOME_SKIPPED_LOW_SCORE,)),
    ("スキップ：LLM 却下", (ledger_mod.OUTCOME_SKIPPED_LLM_REJECT,)),
    ("スキップ：既存 PR あり", (ledger_mod.OUTCOME_SKIPPED_DUPLICATE,)),
    ("LLM 判定待ち（pending）", (ledger_mod.OUTCOME_PENDING_LLM,)),
    ("インフラエラー（次回再試行）", (ledger_mod.OUTCOME_INFRA_ERROR,)),
]


def render_digest(summary: RunSummary) -> str:
    """Render a RunSummary as a Markdown digest."""
    lines: list[str] = [
        f"# デイリーダイジェスト — {summary.run_date.isoformat()}",
        "",
        "OSS コントリビューターエージェントの収集＋トリアージ結果"
        "（Phase 1：収集のみ。パッチ試行と PR 作成は行いません）。",
        "",
        "## サマリー",
        "",
        "| 対象 | モード | 収集 | 新規 | 状態 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for repo in summary.repos:
        status = f"⚠️ {repo.error}" if repo.error else "OK"
        lines.append(
            f"| {repo.repo} | {repo.mode} | {repo.seen} | {repo.new} | {status} |"
        )
    counts: dict[str, int] = {}
    for decision in summary.decisions:
        counts[decision.outcome] = counts.get(decision.outcome, 0) + 1
    lines += [
        "",
        f"- 今回の判定: {len(summary.decisions)} 件"
        + (
            "（"
            + "、".join(f"{name} {count}" for name, count in sorted(counts.items()))
            + "）"
            if counts
            else ""
        ),
        f"- LLM 判定待ちの残件: {summary.still_pending_llm} 件",
        "",
    ]

    new_decisions = [d for d in summary.decisions if d.is_new]
    lines += ["## 新規に収集した Issue", ""]
    if new_decisions:
        for decision in new_decisions:
            score = f"{decision.score:.2f}" if decision.score is not None else "—"
            title = decision.title or "(no title)"
            link = f"[{title}]({decision.url})" if decision.url else title
            lines.append(
                f"- {decision.repo}#{decision.number} {link}"
                f" — スコア {score} → {decision.outcome}"
            )
    else:
        lines.append("新規 Issue はありませんでした。")
    lines.append("")

    lines += ["## トリアージ結果", ""]
    any_section = False
    for heading, outcomes in _OUTCOME_HEADINGS:
        group = summary.decisions_with(*outcomes)
        if not group:
            continue
        any_section = True
        lines += [f"### {heading}", ""]
        for decision in group:
            score = f"{decision.score:.2f}" if decision.score is not None else "—"
            title = decision.title or "(no title)"
            link = f"[{title}]({decision.url})" if decision.url else title
            lines.append(f"- {decision.repo}#{decision.number} {link}（スコア {score}）")
            for reason in decision.reasons:
                lines.append(f"  - {reason}")
            if decision.llm_rationale:
                lines.append(f"  - LLM 判定理由: {decision.llm_rationale}")
        lines.append("")
    if not any_section:
        lines += ["今回、判定対象の Issue はありませんでした。", ""]

    lines += ["## 現在の候補プール", ""]
    if summary.candidate_pool:
        lines += [
            "トリアージを通過し、候補のまま残っている Issue"
            f"（全 {len(summary.candidate_pool)} 件）。",
            "",
        ]
        for entry in summary.candidate_pool:
            score = f"{entry.score:.2f}" if entry.score is not None else "—"
            title = entry.title or "(no title)"
            link = f"[{title}]({entry.url})" if entry.url else title
            lines.append(
                f"- {entry.repo}#{entry.number} {link}"
                f"（スコア {score}、経過 {entry.age_days} 日）"
            )
        lines.append("")
    else:
        lines += ["候補プールは空です。", ""]

    if summary.notes:
        lines += ["## 備考", ""]
        lines += [f"- {note}" for note in summary.notes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_digest(reports_dir: str | Path, summary: RunSummary) -> Path:
    """Write the digest to ``reports/digest-YYYY-MM-DD.md``."""
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"digest-{summary.run_date.isoformat()}.md"
    path.write_text(render_digest(summary), encoding="utf-8")
    return path

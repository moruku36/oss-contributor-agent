"""One collection + triage pass (Phase 1 pipeline).

For every enabled target:

1. Collect open issues carrying the target's watch labels.
2. Record them in the ledger (already-decided issues are not
   re-triaged; ``pending_llm`` issues are retried when an LLM triage
   provider is configured).
3. Score new issues with the deterministic triager:
   - accept band -> existing-PR check, then ``candidate``
     (or ``skipped_duplicate`` when an open PR already covers it)
   - skip band -> ``skipped_low_score``
   - borderline band -> LLM triage when configured, otherwise the
     explicit ``pending_llm`` state (fail-closed, never guessed)
4. Produce the data for the daily digest.

Failure policy: a rate limit or API error on one target is logged and
that target is skipped; the rest of the run continues. A failing
existing-PR check or LLM call leaves the issue undecided so a later
run can retry — filters are never loosened to keep moving.

This module never imports the sandbox or the coder loop: in Phase 1
there is no code path from collection to a patch attempt, by
construction as well as by the collect-only config gate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.collector import ledger as ledger_mod
from src.collector import triager
from src.collector.digest import (
    CandidatePoolEntry,
    RepoRun,
    RunSummary,
    TriageDecision,
)
from src.collector.github_client import GitHubAPIError, GitHubClient, IssueRef
from src.collector.llm_triage import LLMTriageError, LLMTriager
from src.config import Rules, Target

log = logging.getLogger(__name__)


def _judge_with_llm(
    llm: LLMTriager,
    issue: IssueRef,
    *,
    score: float,
    reasons: list[str],
    project_notes: str,
) -> tuple[str, dict | None, str]:
    """Run the LLM stage. Returns (outcome, verdict_dict, rationale).

    The returned outcome is candidate / skipped_llm_reject; the caller
    still runs the existing-PR check for candidates. LLM failures
    propagate as LLMTriageError and are handled by the caller.
    """
    verdict = llm.judge(
        issue, score=score, reasons=reasons, project_notes=project_notes
    )
    outcome = (
        ledger_mod.OUTCOME_CANDIDATE
        if verdict.accepted
        else ledger_mod.OUTCOME_SKIPPED_LLM_REJECT
    )
    return outcome, verdict.as_dict(), verdict.rationale


def _check_existing_prs(
    client: GitHubClient, issue: IssueRef
) -> tuple[str, str | None]:
    """Map the duplicate check to (outcome, pr_url).

    Raises GitHubAPIError when the check itself fails; the caller
    treats that as "undecided", never as "no PR exists".
    """
    prs = client.find_existing_prs(issue.repo, issue.number)
    if prs:
        return ledger_mod.OUTCOME_SKIPPED_DUPLICATE, str(prs[0].get("url") or "") or None
    return ledger_mod.OUTCOME_CANDIDATE, None


def _triage_new_issue(
    *,
    client: GitHubClient,
    store: ledger_mod.Ledger,
    llm: LLMTriager | None,
    target: Target,
    rules: Rules,
    issue: IssueRef,
    is_new: bool,
) -> TriageDecision:
    """Full triage for an issue with no recorded decision yet."""
    score, reasons = triager.score_issue_ref(issue, weights=rules.triage_scoring)
    band = triager.verdict(
        score,
        accept_threshold=rules.triage_accept_threshold,
        skip_threshold=rules.triage_skip_threshold,
    )
    llm_verdict: dict | None = None
    llm_rationale = ""

    if band == "skip":
        outcome = ledger_mod.OUTCOME_SKIPPED_LOW_SCORE
    elif band == "borderline" and llm is None:
        outcome = ledger_mod.OUTCOME_PENDING_LLM
    else:
        outcome = ledger_mod.OUTCOME_CANDIDATE  # provisional until PR check
        if band == "borderline":
            try:
                outcome, llm_verdict, llm_rationale = _judge_with_llm(
                    llm,  # type: ignore[arg-type]
                    issue,
                    score=score,
                    reasons=reasons,
                    project_notes=target.notes,
                )
            except LLMTriageError as exc:
                log.warning("LLM triage failed for %s#%s: %s", issue.repo, issue.number, exc)
                outcome = ledger_mod.OUTCOME_PENDING_LLM
        if outcome == ledger_mod.OUTCOME_CANDIDATE:
            try:
                outcome, pr_url = _check_existing_prs(client, issue)
            except GitHubAPIError as exc:
                log.warning(
                    "existing-PR check failed for %s#%s: %s",
                    issue.repo,
                    issue.number,
                    exc,
                )
                outcome = ledger_mod.OUTCOME_INFRA_ERROR
                pr_url = None
            store.record_triage(
                issue.repo,
                issue.number,
                score=score,
                reasons=reasons,
                outcome=outcome,
                llm_verdict=llm_verdict,
                pr_url=pr_url,
            )
            return TriageDecision(
                repo=issue.repo,
                number=issue.number,
                title=issue.title,
                url=issue.url,
                score=score,
                outcome=outcome,
                reasons=reasons,
                llm_rationale=llm_rationale,
                is_new=is_new,
            )

    store.record_triage(
        issue.repo,
        issue.number,
        score=score,
        reasons=reasons,
        outcome=outcome,
        llm_verdict=llm_verdict,
    )
    return TriageDecision(
        repo=issue.repo,
        number=issue.number,
        title=issue.title,
        url=issue.url,
        score=score,
        outcome=outcome,
        reasons=reasons,
        llm_rationale=llm_rationale,
        is_new=is_new,
    )


def _resolve_pending_issue(
    *,
    client: GitHubClient,
    store: ledger_mod.Ledger,
    llm: LLMTriager,
    target: Target,
    issue: IssueRef,
    row: ledger_mod.LedgerRow,
) -> TriageDecision:
    """Retry the LLM stage for an issue parked in ``pending_llm``."""
    score = row.triage_score if row.triage_score is not None else 0.0
    reasons = row.triage_reasons
    try:
        outcome, llm_verdict, llm_rationale = _judge_with_llm(
            llm, issue, score=score, reasons=reasons, project_notes=target.notes
        )
    except LLMTriageError as exc:
        log.warning("LLM triage failed for %s#%s: %s", issue.repo, issue.number, exc)
        return TriageDecision(
            repo=issue.repo,
            number=issue.number,
            title=issue.title,
            url=issue.url,
            score=score,
            outcome=ledger_mod.OUTCOME_PENDING_LLM,
            reasons=reasons,
            is_new=False,
        )
    pr_url = None
    if outcome == ledger_mod.OUTCOME_CANDIDATE:
        try:
            outcome, pr_url = _check_existing_prs(client, issue)
        except GitHubAPIError as exc:
            log.warning(
                "existing-PR check failed for %s#%s: %s", issue.repo, issue.number, exc
            )
            outcome = ledger_mod.OUTCOME_INFRA_ERROR
    store.record_triage(
        issue.repo,
        issue.number,
        score=score,
        reasons=reasons,
        outcome=outcome,
        llm_verdict=llm_verdict,
        pr_url=pr_url,
    )
    return TriageDecision(
        repo=issue.repo,
        number=issue.number,
        title=issue.title,
        url=issue.url,
        score=score,
        outcome=outcome,
        reasons=reasons,
        llm_rationale=llm_rationale,
        is_new=False,
    )


def run_collection_pass(
    *,
    targets: list[Target],
    rules: Rules,
    client: GitHubClient,
    store: ledger_mod.Ledger,
    llm: LLMTriager | None,
    now: datetime | None = None,
) -> RunSummary:
    """Run one collection + triage pass over all enabled targets."""
    now = now or datetime.now(timezone.utc)
    summary = RunSummary(run_date=now.date())
    if llm is None:
        summary.notes.append(
            "LLM トリアージは未設定です。境界帯の Issue は pending_llm として"
            "保持され、推測で判定されることはありません。"
        )

    for target in targets:
        if not target.enabled:
            continue
        repo_run = RepoRun(repo=target.repo, mode=target.mode)
        summary.repos.append(repo_run)
        try:
            issues = client.list_open_issues(target.repo, target.watch_labels)
        except GitHubAPIError as exc:
            # Includes RateLimitError: log + skip this target, keep going.
            log.warning("collection failed for %s: %s", target.repo, exc)
            repo_run.error = str(exc)
            summary.notes.append(f"{target.repo}: 収集をスキップしました（{exc}）")
            continue

        repo_run.seen = len(issues)
        for issue in issues:
            row_before = store.get(issue.repo, issue.number)
            is_new = store.upsert_seen(issue)
            if is_new:
                repo_run.new += 1
            previous_outcome = row_before.outcome if row_before else None

            if previous_outcome in ledger_mod.TERMINAL_OUTCOMES:
                continue  # already decided; never re-triage automatically
            if previous_outcome == ledger_mod.OUTCOME_PENDING_LLM:
                if llm is None:
                    continue  # stays pending; counted in the digest totals
                assert row_before is not None
                summary.decisions.append(
                    _resolve_pending_issue(
                        client=client,
                        store=store,
                        llm=llm,
                        target=target,
                        issue=issue,
                        row=row_before,
                    )
                )
                continue
            summary.decisions.append(
                _triage_new_issue(
                    client=client,
                    store=store,
                    llm=llm,
                    target=target,
                    rules=rules,
                    issue=issue,
                    is_new=is_new,
                )
            )

    summary.still_pending_llm = len(store.pending_llm())
    summary.candidate_pool = [
        CandidatePoolEntry(
            repo=row.repo,
            number=row.issue_number,
            title=row.title,
            url=row.url,
            score=row.triage_score,
            age_days=max(
                0,
                (
                    summary.run_date
                    - datetime.fromisoformat(row.first_seen_at).date()
                ).days,
            ),
        )
        for row in store.candidates()
    ]
    return summary

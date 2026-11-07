"""End-to-end tests for one collection pass (src/collector/pipeline.py).

The GitHub client is stubbed and the LLM triager is a mock: no network,
no real model. The point is the state machine — bands, ledger outcomes,
fail-closed retries — not the transports (covered by their own tests).
"""

import os
import subprocess
import sys
from pathlib import Path

from src.collector import ledger as ledger_mod
from src.collector.github_client import IssueRef, RateLimitError
from src.collector.llm_triage import LLMTriageError, LLMVerdict
from src.collector.pipeline import run_collection_pass
from src.config import Rules, Target

LONG_BODY_WITH_REPRO_AND_TESTS = (
    "Steps to reproduce: start the server with an empty config file and "
    "open the status page. Expected: the page loads. Actual: the server "
    "panics with a nil pointer error in the config loader. There is an "
    "existing test file for that package, so a regression test can be "
    "added next to it. Confirmed on the latest main branch, Linux, "
    "Go 1.22, by two independent reporters with the same stack trace."
)

LONG_BODY_WITH_REPRO_ONLY = (
    "Steps to reproduce: start the server with an empty config file and "
    "open the status page. Expected: the page loads normally. Actual: "
    "the server panics with a nil pointer error somewhere inside the "
    "configuration loading path during startup. Confirmed on the "
    "latest main branch on Linux by two independent reporters who "
    "both attached the same stack trace and their config files."
)


def _issue(repo: str, number: int, labels: list[str], body: str) -> IssueRef:
    return IssueRef(
        repo=repo,
        number=number,
        title=f"issue {number}",
        body=body,
        labels=labels,
        url=f"https://github.com/{repo}/issues/{number}",
    )


ACCEPT_ISSUE = _issue("o/r", 1, ["good first issue", "bug"],
                      LONG_BODY_WITH_REPRO_AND_TESTS)   # score 0.80
BORDERLINE_ISSUE = _issue("o/r", 2, ["bug"], LONG_BODY_WITH_REPRO_ONLY)  # 0.45
SKIP_ISSUE = _issue("o/r", 3, [], "too short")          # score 0.00


class FakeClient:
    """Stands in for GitHubClient with canned data / failures."""

    def __init__(self, issues_by_repo: dict, prs: dict | None = None,
                 errors: dict | None = None, pr_error_repos: set | None = None):
        self._issues = issues_by_repo
        self._prs = prs or {}
        self._errors = errors or {}
        self._pr_error_repos = pr_error_repos or set()

    def list_open_issues(self, repo: str, labels: list[str]) -> list[IssueRef]:
        if repo in self._errors:
            raise self._errors[repo]
        return list(self._issues.get(repo, []))

    def find_existing_prs(self, repo: str, issue_number: int) -> list[dict]:
        if repo in self._pr_error_repos:
            raise RateLimitError("rate limited during PR check")
        return list(self._prs.get((repo, issue_number), []))


class MockLLM:
    def __init__(self, verdicts: dict[int, bool] | None = None,
                 error_numbers: set[int] | None = None):
        self._verdicts = verdicts or {}
        self._errors = error_numbers or set()
        self.judged: list[int] = []

    def judge(self, issue: IssueRef, *, score: float, reasons: list[str],
              project_notes: str = "") -> LLMVerdict:
        self.judged.append(issue.number)
        if issue.number in self._errors:
            raise LLMTriageError("mock provider failure")
        accepted = self._verdicts.get(issue.number, True)
        return LLMVerdict(accepted=accepted, rationale="mock rationale",
                          model="mock-1")


def _target(repo: str = "o/r") -> Target:
    return Target(repo=repo, enabled=True, mode="collect-only",
                  watch_labels=["good first issue", "bug"])


def _run(tmp_path: Path, client: FakeClient, llm=None, targets=None):
    store = ledger_mod.Ledger(tmp_path / "ledger.sqlite3")
    summary = run_collection_pass(
        targets=targets or [_target()],
        rules=Rules(),
        client=client,  # type: ignore[arg-type]
        store=store,
        llm=llm,
    )
    return store, summary


def test_pass_without_llm_bands_and_pending(tmp_path: Path) -> None:
    client = FakeClient({"o/r": [ACCEPT_ISSUE, BORDERLINE_ISSUE, SKIP_ISSUE]})
    store, summary = _run(tmp_path, client, llm=None)
    outcomes = {d.number: d.outcome for d in summary.decisions}
    assert outcomes == {
        1: ledger_mod.OUTCOME_CANDIDATE,
        2: ledger_mod.OUTCOME_PENDING_LLM,   # never guessed without an LLM
        3: ledger_mod.OUTCOME_SKIPPED_LOW_SCORE,
    }
    assert store.get("o/r", 2).outcome == ledger_mod.OUTCOME_PENDING_LLM
    assert summary.still_pending_llm == 1
    store.close()


def test_candidate_with_existing_pr_is_skipped_as_duplicate(
    tmp_path: Path,
) -> None:
    prs = {("o/r", 1): [{"number": 99, "url": "https://github.com/o/r/pull/99",
                         "title": "fix"}]}
    client = FakeClient({"o/r": [ACCEPT_ISSUE]}, prs=prs)
    store, summary = _run(tmp_path, client)
    (decision,) = summary.decisions
    assert decision.outcome == ledger_mod.OUTCOME_SKIPPED_DUPLICATE
    row = store.get("o/r", 1)
    assert row.pr_url == "https://github.com/o/r/pull/99"
    store.close()


def test_llm_resolves_borderline_band(tmp_path: Path) -> None:
    client = FakeClient({"o/r": [BORDERLINE_ISSUE]})
    llm = MockLLM(verdicts={2: False})
    store, summary = _run(tmp_path, client, llm=llm)
    (decision,) = summary.decisions
    assert decision.outcome == ledger_mod.OUTCOME_SKIPPED_LLM_REJECT
    row = store.get("o/r", 2)
    assert row.llm_verdict["accepted"] is False
    assert summary.still_pending_llm == 0
    store.close()


def test_llm_failure_leaves_issue_pending(tmp_path: Path) -> None:
    client = FakeClient({"o/r": [BORDERLINE_ISSUE]})
    llm = MockLLM(error_numbers={2})
    store, summary = _run(tmp_path, client, llm=llm)
    (decision,) = summary.decisions
    assert decision.outcome == ledger_mod.OUTCOME_PENDING_LLM
    assert store.get("o/r", 2).outcome == ledger_mod.OUTCOME_PENDING_LLM
    store.close()


def test_pending_issue_is_resolved_on_a_later_run(tmp_path: Path) -> None:
    client = FakeClient({"o/r": [BORDERLINE_ISSUE]})
    store, _ = _run(tmp_path, client, llm=None)
    assert store.get("o/r", 2).outcome == ledger_mod.OUTCOME_PENDING_LLM

    llm = MockLLM(verdicts={2: True})
    summary = run_collection_pass(
        targets=[_target()], rules=Rules(), client=client,  # type: ignore[arg-type]
        store=store, llm=llm,
    )
    (decision,) = summary.decisions
    assert decision.outcome == ledger_mod.OUTCOME_CANDIDATE
    assert llm.judged == [2]
    assert summary.still_pending_llm == 0
    store.close()


def test_decided_issues_are_not_retriaged(tmp_path: Path) -> None:
    client = FakeClient({"o/r": [BORDERLINE_ISSUE, SKIP_ISSUE]})
    llm = MockLLM(verdicts={2: True})
    store, _ = _run(tmp_path, client, llm=llm)
    assert llm.judged == [2]

    summary = run_collection_pass(
        targets=[_target()], rules=Rules(), client=client,  # type: ignore[arg-type]
        store=store, llm=llm,
    )
    assert summary.decisions == []       # nothing decided twice
    assert llm.judged == [2]             # and the LLM was not re-asked
    store.close()


def test_rate_limit_on_one_repo_does_not_stop_the_run(tmp_path: Path) -> None:
    limited = Target(repo="o/limited", enabled=True, mode="collect-only",
                     watch_labels=["bug"])
    healthy = _target("o/healthy")
    healthy_issue = _issue("o/healthy", 9, [], "too short")
    client = FakeClient(
        {"o/healthy": [healthy_issue]},
        errors={"o/limited": RateLimitError("rate limit exhausted")},
    )
    store, summary = _run(tmp_path, client, targets=[limited, healthy])
    by_repo = {repo.repo: repo for repo in summary.repos}
    assert "rate limit" in by_repo["o/limited"].error
    assert by_repo["o/healthy"].error == ""
    assert by_repo["o/healthy"].seen == 1
    assert store.get("o/healthy", 9).outcome == ledger_mod.OUTCOME_SKIPPED_LOW_SCORE
    store.close()


def test_failed_pr_check_is_infra_error_and_retried(tmp_path: Path) -> None:
    client = FakeClient({"o/r": [ACCEPT_ISSUE]}, pr_error_repos={"o/r"})
    store, summary = _run(tmp_path, client)
    (decision,) = summary.decisions
    assert decision.outcome == ledger_mod.OUTCOME_INFRA_ERROR

    fixed = FakeClient({"o/r": [ACCEPT_ISSUE]})
    summary = run_collection_pass(
        targets=[_target()], rules=Rules(), client=fixed,  # type: ignore[arg-type]
        store=store, llm=None,
    )
    (decision,) = summary.decisions
    assert decision.outcome == ledger_mod.OUTCOME_CANDIDATE
    store.close()


def test_pipeline_imports_no_patch_machinery() -> None:
    # Structural collect-only guarantee: importing and running the
    # Phase 1 pipeline must not pull in the sandbox or the coder loop.
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=str(root))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import src.collector.pipeline, sys; "
            "bad = [m for m in sys.modules "
            "if m.startswith('src.sandbox') or m == 'src.agent.core']; "
            "assert not bad, bad",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_pool_includes_old_and_new_candidates_without_retriage(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timezone
    from src.collector.digest import render_digest

    monkeypatch.setattr(ledger_mod, "utcnow_iso", lambda: "2026-11-07T00:00:00+00:00")
    with ledger_mod.Ledger(tmp_path / "ledger.sqlite3") as store:
        store.upsert_seen(ACCEPT_ISSUE, now="2026-11-01T00:00:00+00:00")
        store.record_triage("o/r", 1, score=0.8, reasons=[], outcome=ledger_mod.OUTCOME_CANDIDATE)
        client = FakeClient({"o/r": [BORDERLINE_ISSUE, SKIP_ISSUE]})
        summary = run_collection_pass(
            targets=[_target()], rules=Rules(), client=client, store=store,
            llm=MockLLM(), now=datetime(2026, 11, 7, tzinfo=timezone.utc),
        )
        assert [d.number for d in summary.decisions] == [2, 3]
        entries = {e.number: e for e in summary.candidate_pool}
        assert set(entries) == {1, 2}
        assert entries[1].age_days == 6
        assert entries[1].title == ACCEPT_ISSUE.title
        assert entries[1].url == ACCEPT_ISSUE.url
        assert entries[1].score == 0.8
        assert entries[2].age_days == 0
        assert "経過 6 日" in render_digest(summary)
        # The pool remains visible even when this run sees no issues.
        later = run_collection_pass(
            targets=[_target()], rules=Rules(), client=FakeClient({}), store=store,
            llm=None, now=datetime(2026, 11, 8, tzinfo=timezone.utc),
        )
        assert later.decisions == []
        assert {e.number for e in later.candidate_pool} == {1, 2}
        assert next(e for e in later.candidate_pool if e.number == 1).age_days == 7


def test_pipeline_uses_configured_body_weights(tmp_path: Path) -> None:
    from src.config import load_rules

    config = tmp_path / "rules.yaml"
    config.write_text("rules:\n  triage_scoring:\n    reproduction_points: 0\n    detail_signal_points: 0.3\n    detail_points_cap: 0.3\n")
    request = _issue("o/r", 9, ["help wanted"], "Use case: daily work")
    with ledger_mod.Ledger(tmp_path / "ledger.sqlite3") as store:
        summary = run_collection_pass(
            targets=[_target()], rules=load_rules(config),
            client=FakeClient({"o/r": [BORDERLINE_ISSUE, request]}), store=store, llm=None,
        )
        assert [(d.number, d.score, d.outcome) for d in summary.decisions] == [
            (2, 0.2, ledger_mod.OUTCOME_SKIPPED_LOW_SCORE),
            (9, 0.5, ledger_mod.OUTCOME_PENDING_LLM),
        ]
        assert summary.candidate_pool == []

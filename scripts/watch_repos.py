#!/usr/bin/env python3
"""Scheduled collection + triage across all enabled targets.

This is the entry point used by the daily batch
(.github/workflows/daily-triage.yml, 06:30 JST) and, optionally, by a
long-running watch daemon.

Usage:
    python3 scripts/watch_repos.py --dry-run          # print the plan
    python3 scripts/watch_repos.py --once             # one real pass
    python3 scripts/watch_repos.py --daemon           # watch mode

One pass (Phase 1: collection + triage only — no patch attempts, no
PRs are ever created by this script):
    1. For each enabled target in configs/targets.yaml, list open
       issues with the watch labels (GitHub token from $GITHUB_TOKEN
       when set; unauthenticated works for public repos).
    2. Record issues in the ledger (state/ledger.sqlite3); issues with
       a recorded decision are not re-triaged.
    3. Score new issues with the deterministic triager; only the
       borderline band goes to the LLM triage stage, and only when one
       is configured ($LLM_TRIAGE_PROVIDER etc.). Without an LLM,
       borderline issues wait in the explicit `pending_llm` state.
    4. Accepted issues are checked for an existing open PR before
       being recorded as candidates.
    5. Write the daily digest report (reports/digest-YYYY-MM-DD.md).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collector import ledger as ledger_mod  # noqa: E402
from src.collector.digest import write_digest  # noqa: E402
from src.collector.github_client import GitHubClient  # noqa: E402
from src.collector.llm_triage import LLMTriageError, build_llm_triager  # noqa: E402
from src.collector.pipeline import run_collection_pass  # noqa: E402
from src.config import ConfigError, load_rules, load_targets  # noqa: E402

PLANNED_FLOW = [
    "load targets + global rules",
    "collect open issues per enabled target",
    "ledger + existing-PR dedupe",
    "rule-based triage, then LLM triage for the borderline band",
    "write the daily digest report",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="configs",
                        help="Directory holding targets.yaml / agent_rules.yaml")
    parser.add_argument("--ledger", default="state/ledger.sqlite3",
                        help="Path to the SQLite ledger")
    parser.add_argument("--reports-dir", default="reports",
                        help="Directory the daily digest is written to")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--daemon", action="store_true", help="Run as a watch daemon")
    parser.add_argument("--interval-minutes", type=int, default=60,
                        help="Polling interval for --daemon (default: 60)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the planned flow without doing any work")
    return parser.parse_args(argv)


def run_once(args: argparse.Namespace) -> int:
    """Run a single collection + triage pass. Returns a process exit code."""
    config_dir = Path(args.config_dir)
    try:
        targets = load_targets(config_dir / "targets.yaml")
        rules = load_rules(config_dir / "agent_rules.yaml")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    enabled = [target for target in targets if target.enabled]
    if not enabled:
        print("No enabled targets in configs/targets.yaml; nothing to do.")
        return 0

    try:
        llm = build_llm_triager()
    except LLMTriageError as exc:
        print(f"LLM triage configuration error: {exc}", file=sys.stderr)
        return 2

    client = GitHubClient(token=os.environ.get("GITHUB_TOKEN") or None)
    with ledger_mod.Ledger(args.ledger) as store:
        summary = run_collection_pass(
            targets=targets, rules=rules, client=client, store=store, llm=llm
        )
    digest_path = write_digest(args.reports_dir, summary)

    seen = sum(repo.seen for repo in summary.repos)
    new = sum(repo.new for repo in summary.repos)
    print(
        f"Collection pass complete: {seen} issues seen, {new} new, "
        f"{len(summary.decisions)} triage decisions, "
        f"{summary.still_pending_llm} still pending LLM."
    )
    print(f"Digest written to {digest_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)
    if args.dry_run:
        print("Planned flow for one collection pass:")
        for step, name in enumerate(PLANNED_FLOW, start=1):
            print(f"  {step}. {name}")
        return 0
    if args.daemon:
        print(f"Watch daemon: one pass every {args.interval_minutes} minutes.")
        try:
            while True:
                run_once(args)
                time.sleep(args.interval_minutes * 60)
        except KeyboardInterrupt:
            print("Daemon stopped.")
            return 0
    if args.once:
        return run_once(args)
    print("Nothing to do: pass --once, --daemon or --dry-run.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Scheduled collection + triage across all enabled targets.

This is the entry point used by the daily batch
(.github/workflows/daily-triage.yml, 06:30 JST) and, optionally, by a
long-running watch daemon.

Usage:
    python3 scripts/watch_repos.py --dry-run          # print the plan
    python3 scripts/watch_repos.py --once             # one real pass
    python3 scripts/watch_repos.py --daemon           # watch mode

One pass:
    1. For each enabled target in configs/targets.yaml, list open
       issues with the watch labels.
    2. Drop issues already in the ledger or with existing PRs.
    3. Score the rest with the deterministic triager; send only the
       borderline band to the LLM triage stage.
    4. Attempt at most `max_issues_attempted_per_day` issues
       (configs/agent_rules.yaml), respecting per-project cooldowns.
    5. Write the daily digest report (issues seen / triaged out /
       attempted / drafts prepared) for the human operator.
"""

from __future__ import annotations

import argparse
import sys

PLANNED_FLOW = [
    "load targets + global rules",
    "collect open issues per enabled target",
    "ledger + existing-PR dedupe",
    "rule-based triage, then LLM triage for the borderline band",
    "attempt up to the daily cap in sandboxes",
    "write the daily digest report",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="configs",
                        help="Directory holding targets.yaml / agent_rules.yaml")
    parser.add_argument("--once", action="store_true", help="Run one pass and exit")
    parser.add_argument("--daemon", action="store_true", help="Run as a watch daemon")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the planned flow without doing any work")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        print("Planned flow for one collection pass:")
        for step, name in enumerate(PLANNED_FLOW, start=1):
            print(f"  {step}. {name}")
        return 0
    raise NotImplementedError(
        "Collection runs are implemented in Phase 1+; use --dry-run for now."
    )


if __name__ == "__main__":
    sys.exit(main())

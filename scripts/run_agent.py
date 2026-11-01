#!/usr/bin/env python3
"""Run the agent end-to-end for a single issue.

Usage:
    python3 scripts/run_agent.py --repo grafana/grafana --issue 12345
    python3 scripts/run_agent.py --repo grafana/grafana --issue 12345 --dry-run

Planned flow (see docs/architecture.md):
    1. Load target rules from configs/targets.yaml and global limits
       from configs/agent_rules.yaml.
    2. Check the ledger: skip if this issue was already attempted.
    3. Check for existing PRs addressing the issue: skip if any.
    4. Start the sandbox, clone, create the working branch.
    5. Run the coder loop (TDD, max attempts from agent_rules.yaml).
    6. Run the compliance guard.
    7. Write the review report and STOP. A draft PR is created only
       after a human approves the report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    CollectOnlyViolation,
    ConfigError,
    find_target,
    load_targets,
)

PLANNED_FLOW = [
    "load target + global rules",
    "ledger dedupe check",
    "existing-PR check",
    "sandbox start + clone + branch",
    "coder loop (TDD patch attempts)",
    "compliance guard",
    "review report -> human approval gate",
    "draft PR submission (only after approval)",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Target repo, e.g. grafana/grafana")
    parser.add_argument("--issue", required=True, type=int, help="Issue number")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the planned flow without doing any work")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"Target: {args.repo} issue #{args.issue}")
    if args.dry_run:
        print("Planned flow:")
        for step, name in enumerate(PLANNED_FLOW, start=1):
            print(f"  {step}. {name}")
        return 0

    # Safety gate, checked before anything else: in Phase 1 every
    # enabled target is collect-only, so a patch attempt is refused
    # here regardless of any other setting. Only a target a human has
    # explicitly onboarded as mode=full in configs/targets.yaml could
    # pass this gate — and the loop itself still lands in Phase 2/3.
    targets_path = Path(__file__).resolve().parent.parent / "configs" / "targets.yaml"
    try:
        target = find_target(load_targets(targets_path), args.repo)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if target is None:
        print(f"Refused: {args.repo} is not a configured target.", file=sys.stderr)
        return 2
    try:
        target.ensure_attempt_allowed()
    except CollectOnlyViolation as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    raise NotImplementedError(
        "End-to-end run is implemented in Phase 2/3; use --dry-run for now."
    )


if __name__ == "__main__":
    sys.exit(main())

"""List saved candidate issues from the local ledger."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .ledger import OUTCOME_CANDIDATE


def read_saved_candidates(path: Path) -> list[dict[str, str | int]]:
    uri = path.resolve().as_uri() + "?mode=ro"
    candidates = []
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            """
            SELECT repo, issue_number
            FROM issues
            WHERE outcome = ?
            ORDER BY repo, issue_number
            """,
            (OUTCOME_CANDIDATE,),
        )
        for repo, issue_number in rows:
            if not isinstance(repo, str) or not repo.strip():
                raise ValueError("invalid candidate row: repo must be nonblank text")
            if not isinstance(issue_number, int) or issue_number <= 0:
                raise ValueError("invalid candidate row: issue_number must be a positive integer")
            candidates.append({"repo": repo, "issue_number": issue_number})
    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List saved candidate issue identities from a local ledger. "
            "Does not check current GitHub status."
        ),
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("state/ledger.sqlite3"),
        help="Existing SQLite ledger (default: state/ledger.sqlite3).",
    )
    args = parser.parse_args(argv)

    try:
        candidates = read_saved_candidates(args.ledger)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"Cannot read ledger {str(args.ledger)!r}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(candidates, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

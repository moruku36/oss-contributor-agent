"""Offline operator tests using an ordinary collection ledger."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from src.collector.ledger import OUTCOME_CANDIDATE, OUTCOME_PENDING_LLM, Ledger


def run_lookup(cwd, *args):
    return subprocess.run(
        [sys.executable, "-B", "-m", "src.collector.saved_candidates", *args],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def save_issue(ledger, repo, number, outcome):
    ledger.upsert_seen(SimpleNamespace(repo=repo, number=number))
    ledger.record_triage(repo, number, score=1, reasons=[], outcome=outcome)


def test_saved_candidates_only_in_stable_order_and_no_database_write(tmp_path):
    path = tmp_path / "desk ? # %.sqlite3"
    with Ledger(path) as ledger:
        save_issue(ledger, "org/repo", 10, OUTCOME_CANDIDATE)
        save_issue(ledger, "org/repo", 2, OUTCOME_CANDIDATE)
        save_issue(ledger, "Org/Repo", 7, OUTCOME_CANDIDATE)
        save_issue(ledger, "org/repo", 3, OUTCOME_PENDING_LLM)

    before = path.read_bytes()
    result = run_lookup(tmp_path, "--ledger", str(path))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == [
        {"repo": "Org/Repo", "issue_number": 7},
        {"repo": "org/repo", "issue_number": 2},
        {"repo": "org/repo", "issue_number": 10},
    ]
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_default_path_distinguishes_absent_ledger_from_empty_candidate_pool(tmp_path):
    path = tmp_path / "state" / "ledger.sqlite3"
    missing = run_lookup(tmp_path)
    assert missing.returncode == 1
    assert missing.stdout == ""
    assert missing.stderr
    assert not path.parent.exists()

    with Ledger(path):
        pass

    empty = run_lookup(tmp_path)
    assert empty.returncode == 0, empty.stderr
    assert empty.stderr == ""
    assert json.loads(empty.stdout) == []


def test_invalid_candidate_row_or_ledger_does_not_print_partial_json(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with Ledger(path) as ledger:
        save_issue(ledger, "ok/repo", 1, OUTCOME_CANDIDATE)
        save_issue(ledger, "z/bad", -1, OUTCOME_CANDIDATE)

    invalid_db = tmp_path / "wrong-schema.sqlite3"
    with closing(sqlite3.connect(invalid_db)) as conn:
        conn.execute("CREATE TABLE not_issues (name TEXT)")
        conn.commit()

    for bad in (path, invalid_db):
        before = bad.read_bytes()
        result = run_lookup(tmp_path, "--ledger", str(bad))
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr
        assert bad.read_bytes() == before

"""Approval-bindable entry point for exactly three commit-compliance cases.

No discovery or user-supplied selection. This binds the entry file, not the
transitive import tree. Use the same normal native execution approval policy.
"""
from pathlib import Path
import os
import sys


CASES = (
    "tests/test_commit_compliance.py::test_conventional_subjects_and_final_dco",
    "tests/test_commit_compliance.py::test_body_mentions_do_not_satisfy_dco",
    "tests/test_commit_compliance.py::test_unknown_policy_and_malformed_inputs_fail_closed",
)


def main() -> int:
    if len(sys.argv) != 1:
        raise SystemExit("This entry point accepts no arguments; selection is fixed at 3 cases.")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTEST_ADDOPTS"] = ""
    import pytest

    return int(pytest.main(["-q", "-o", "addopts=", *CASES]))


if __name__ == "__main__":
    raise SystemExit(main())

# Declared-path compliance check

`src/review/path_compliance.py` adds `check_changed_paths(changed_paths, allowed_paths) -> list[str]`,
a pure, standalone helper for the "no drive-by changes, no CI or license edits" rule
in `compliance.py`. It does not change `run_compliance`, the report schema, config or workflows.

**Status: eight targeted offline scenarios passed in one invocation.**
No legacy suites, CI or live executions were run.

## Contract

- The caller declares both lists. Entries are exact repo-relative POSIX paths in builtin
  `list` / `str` types. There is no globbing and no path resolution. The helper does no git,
  filesystem, subprocess or network access.
- Limits: the changed list must be non-empty; the allow list may be empty. At most 128
  paths per list, 1024 UTF-8 bytes per path, 32768 bytes across both lists.
- Rejected paths: empty, absolute, containing a backslash or colon, containing empty, `.` or
  `..` segments (so also a trailing slash), containing control, format, surrogate,
  unassigned or line/paragraph-separator characters, or duplicated within a list.
- The whole input is validated first. A malformed input returns exactly one validation code
  (`invalid-collection`, `invalid-path`, `limit-exceeded` or `duplicate-path`).
- Otherwise the result is a sorted list of unique codes: `protected-path` and/or
  `out-of-scope`. Codes never contain raw input.
- Scope is exact membership in the allow list. Every changed path must be listed.
- Protected paths are always flagged, even if explicitly allowed (case-insensitive):
  any `.git` component; top-level `.github`, `.gitlab`, `.circleci`, `.buildkite`,
  `.gitlab-ci.yml`, `azure-pipelines.yml`; and any basename `LICENSE`, `LICENCE`, `COPYING`,
  `NOTICE`, `COPYRIGHT`, `AUTHORS`, alone or followed by a dot suffix (`LICENSE-MIT` is not matched).

## Non-goals

An empty result means only that the declared lists are lexically in scope. It does not
show that the declaration matches the real diff, that symlinks or file types are safe, that
anything is legally cleared, or that policy is approved. It is not `run_compliance`
completion and gives no permission to execute or submit.

## Tests

Executed once from the repository root:

    env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_path_compliance.py -v

There are eight tests, `test_P01` to `test_P08`. They use only the new module and the stdlib.

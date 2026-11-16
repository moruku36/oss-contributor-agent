"""Declared-path scope check (pure, lexical).

The caller declares exact repo-relative POSIX paths. Nothing here globs,
resolves paths, or touches git, the filesystem, subprocesses or the network.
An empty result means only that the declared lists are lexically in scope.
It is not a real-diff, symlink/type, legal or policy verdict, and it is not
run_compliance completion or permission to execute or submit.
"""

from __future__ import annotations

import unicodedata

MAX_PATHS = 128
MAX_PATH_BYTES = 1024
MAX_TOTAL_BYTES = 32768

INVALID_COLLECTION = "invalid-collection"
INVALID_PATH = "invalid-path"
LIMIT_EXCEEDED = "limit-exceeded"
DUPLICATE_PATH = "duplicate-path"
PROTECTED_PATH = "protected-path"
OUT_OF_SCOPE = "out-of-scope"

_CI_DIRS = frozenset({".github", ".gitlab", ".circleci", ".buildkite"})
_CI_FILES = frozenset({".gitlab-ci.yml", "azure-pipelines.yml"})
_LEGAL = ("license", "licence", "copying", "notice", "copyright", "authors")


def _path_problem(path: object) -> str | None:
    if type(path) is not str:
        return INVALID_PATH
    try:
        size = len(path.encode("utf-8"))
    except UnicodeError:
        return INVALID_PATH
    if size > MAX_PATH_BYTES:
        return LIMIT_EXCEEDED
    if not path or path.startswith("/") or chr(92) in path or ":" in path:
        return INVALID_PATH
    for ch in path:
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zl", "Zp"):
            return INVALID_PATH
    if any(seg in ("", ".", "..") for seg in path.split("/")):
        return INVALID_PATH
    return None


def _collection_problem(paths: object) -> str | None:
    if type(paths) is not list:
        return INVALID_COLLECTION
    if len(paths) > MAX_PATHS:
        return LIMIT_EXCEEDED
    for path in paths:
        problem = _path_problem(path)
        if problem:
            return problem
    if len(set(paths)) != len(paths):
        return DUPLICATE_PATH
    return None


def _is_protected(path: str) -> bool:
    parts = path.casefold().split("/")
    if ".git" in parts or parts[0] in _CI_DIRS:
        return True
    if len(parts) == 1 and parts[0] in _CI_FILES:
        return True
    base = parts[-1]
    return any(base == n or base.startswith(n + ".") for n in _LEGAL)


def check_changed_paths(changed_paths: list[str], allowed_paths: list[str]) -> list[str]:
    """Return sorted unique stable codes; [] means declared scope only.

    Structural problems yield exactly one validation code and nothing else.
    Protected paths are flagged even if explicitly allowed. Codes never
    contain raw input.
    """
    for paths in (changed_paths, allowed_paths):
        problem = _collection_problem(paths)
        if problem:
            return [problem]
    if not changed_paths:
        return [INVALID_COLLECTION]
    total = sum(len(p.encode("utf-8")) for p in changed_paths + allowed_paths)
    if total > MAX_TOTAL_BYTES:
        return [LIMIT_EXCEEDED]
    allowed = frozenset(allowed_paths)
    codes: set[str] = set()
    for path in changed_paths:
        if _is_protected(path):
            codes.add(PROTECTED_PATH)
        elif path not in allowed:
            codes.add(OUT_OF_SCOPE)
    return sorted(codes)

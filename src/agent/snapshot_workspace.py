"""Bounded, injection-only text snapshots, never host files or isolation evidence.

Inputs are explicit TextFile records with trusted source/test/runner roles. Only
caller-selected source paths can change. Paths use a conservative portable ASCII
namespace (no hidden components, case aliases, device names or file/dir aliases).
The caller must identify runner/test evidence accurately; this is not discovery.

Patch subset: existing files only, exact `--- a/path` / `+++ b/path` headers,
ordered nonoverlapping `@@ -start[,count] +start[,count] @@` hunks, LF records,
literal context/removal lines and standard end-of-file no-newline markers. Every
nonempty old span must match uniquely at its declared position. Insertions into
nonempty files need old context. Counts and new positions must agree exactly.
No preamble, timestamps, fuzz, binary, rename, mode, link, add/delete-file or
context-only hunks. A proposal commits once after all validation; errors raise
SnapshotError without changing the snapshot. Complete sorted base-to-current
diffs use whole-file replacement hunks, keeping generation linear and bounded.

Attempt IDs prevent accidental cross-attempt use; they do not establish Sandbox
binding. A trusted injected assessor must recognize the exact result object and
attempt, using authoritative runner evidence. No exit/log heuristics are supplied.
This single-caller object has no filesystem adapter or production CLI activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from time import monotonic
from typing import Protocol

from src.agent.core import TestAssessment, TestOutcome
from src.sandbox.docker_manager import CommandResult


class SnapshotError(ValueError):
    """Rejected input, bounds or patch; no partial snapshot mutation."""


@dataclass(frozen=True)
class TextFile:
    """One regular UTF-8 text file, with no mode, links or external references."""

    path: str
    text: str
    role: str  # explicitly source, test or runner; only source can be editable


@dataclass(frozen=True)
class SnapshotLimits:
    max_total_bytes: int = 1_048_576
    max_file_bytes: int = 131_072
    max_files: int = 128
    max_path_bytes: int = 240
    max_output_bytes: int = 65_536
    max_patch_bytes: int = 65_536
    max_hunks: int = 128
    max_matches: int = 256

    def __post_init__(self) -> None:
        ceilings = (4_194_304, 262_144, 256, 240, 524_288, 262_144, 256, 4096)
        for name, ceiling in zip(self.__dataclass_fields__, ceilings):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise SnapshotError(f"invalid finite limit: {name}")


class RunnerAssessor(Protocol):
    """Trusted, bounded evidence adapter, not model output or text classification.

    Reject unrecognized/replayed results and wrong attempts. Parse only evidence
    tied to this exact CommandResult object; ambiguity/infrastructure/empty runs
    cannot become PASS or ASSERTION_FAILED. Enforce bounds during the call.
    """

    def assess(
        self, attempt_id: str, result: CommandResult, *, timeout_seconds: float,
        max_bytes: int,
    ) -> TestAssessment: ...


_COMPONENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", re.ASCII)
_HUNK = re.compile(r"@@ -([0-9]{1,7})(?:,([0-9]{1,7}))? \+([0-9]{1,7})(?:,([0-9]{1,7}))? @@\n", re.ASCII)
_DEVICES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
_NO_NEWLINE = "\\ No newline at end of file\n"


def _utf8(value: object, cap: int) -> int:
    if type(value) is not str or len(value) > cap:
        raise SnapshotError("expected bounded UTF-8 text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise SnapshotError("invalid UTF-8 text") from None
    if size > cap or any(ord(char) < 32 and char not in "\n\t" for char in value) or "\x7f" in value:
        raise SnapshotError("oversized or unsupported text (LF/tab only controls)")
    return size


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _positive_int(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise SnapshotError("byte bound must be a positive integer")


def _time_left(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("snapshot operation deadline exhausted")
    return remaining


class SnapshotWorkspace:
    def __init__(
        self, attempt_id: str, files: tuple[TextFile, ...], *,
        editable_paths: tuple[str, ...], context_paths: tuple[str, ...],
        assessor: RunnerAssessor, limits: SnapshotLimits = SnapshotLimits(),
    ) -> None:
        if type(attempt_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", attempt_id):
            raise SnapshotError("explicit bounded attempt ID required")
        if type(limits) is not SnapshotLimits or not callable(getattr(assessor, "assess", None)):
            raise SnapshotError("explicit limits and trusted runner assessor required")
        self._attempt_id, self._limits, self._assessor = attempt_id, limits, assessor
        if type(files) is not tuple or not 1 <= len(files) <= limits.max_files:
            raise SnapshotError("supply a bounded tuple of explicit regular text files")
        source: dict[str, str] = {}
        roles: dict[str, str] = {}
        prefixes: dict[str, str] = {}
        for entry in files:
            if type(entry) is not TextFile:
                raise SnapshotError("unsupported entry; only TextFile is accepted")
            path = self._path(entry.path)
            if path in source or type(entry.role) is not str or entry.role not in ("source", "test", "runner"):
                raise SnapshotError("duplicate path or unsupported file role")
            parts = path.split("/")
            for length in range(1, len(parts) + 1):
                prefix = "/".join(parts[:length])
                key = prefix.lower()
                if key in prefixes and prefixes[key] != prefix:
                    raise SnapshotError("ambiguous case alias")
                prefixes[key] = prefix
            source[path], roles[path] = entry.text, entry.role
        for path in source:
            parts = path.split("/")
            if any("/".join(parts[:length]) in source for length in range(1, len(parts))):
                raise SnapshotError("file/directory conflict")
        self._validate_contents(source)
        self._base = source.copy()
        self._current = source
        self._editable = frozenset(self._selection(editable_paths))
        self._context_paths = self._selection(context_paths)
        for path in self._editable:
            parts = path.lower().split("/")
            name = parts[-1]
            if (roles[path] != "source" or any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts)
                    or name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".spec.js"))
                    or name in {"conftest.py", "pytest.ini", "tox.ini"}):
                raise SnapshotError("tests and runner evidence cannot be editable")

    def _path(self, path: object) -> str:
        _utf8(path, self._limits.max_path_bytes)
        if not path:
            raise SnapshotError("empty path")
        for part in path.split("/"):
            if (not _COMPONENT.fullmatch(part) or part.endswith(".")
                    or part.split(".")[0].lower() in _DEVICES):
                raise SnapshotError("noncanonical, metadata or ambiguous path")
        return path

    def _selection(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        if type(paths) is not tuple or len(paths) > self._limits.max_files:
            raise SnapshotError("path selection must be a bounded tuple")
        selected = tuple(self._path(path) for path in paths)
        if len(set(selected)) != len(selected) or any(path not in self._current for path in selected):
            raise SnapshotError("duplicate or missing selected path")
        return tuple(sorted(selected))

    def _validate_contents(self, files: dict[str, str]) -> None:
        if len(files) > self._limits.max_files:
            raise SnapshotError("file count exceeded")
        total = sum(_utf8(text, self._limits.max_file_bytes) for text in files.values())
        if total > self._limits.max_total_bytes:
            raise SnapshotError("total snapshot size exceeded")

    def _operation(self, attempt_id: str, timeout_seconds: float, max_bytes: int, cap: int) -> tuple[float, int]:
        if type(attempt_id) is not str or attempt_id != self._attempt_id:
            raise SnapshotError("wrong workspace attempt")
        try:
            valid = (not isinstance(timeout_seconds, bool) and isinstance(timeout_seconds, (int, float))
                     and isfinite(timeout_seconds) and timeout_seconds > 0)
        except OverflowError:
            valid = False
        if not valid:
            raise SnapshotError("timeout must be positive and finite")
        _positive_int(max_bytes)
        # All local work is finite; these guards do not preempt a blocked assessor.
        return monotonic() + timeout_seconds, min(max_bytes, cap)

    @staticmethod
    def _output(parts, cap: int, deadline: float) -> str:
        output: list[str] = []
        total = 0
        for part in parts:
            _time_left(deadline)
            total += _utf8(part, cap)
            if total > cap:
                raise SnapshotError("complete output exceeds bound")
            output.append(part)
        _time_left(deadline)
        return "".join(output)

    def read_file(self, attempt_id: str, path: str, *, timeout_seconds: float, max_bytes: int) -> str:
        deadline, cap = self._operation(attempt_id, timeout_seconds, max_bytes, self._limits.max_output_bytes)
        path = self._selection((path,))[0]
        return self._output((self._current[path],), cap, deadline)

    def search_code(
        self, attempt_id: str, literal: str, *, timeout_seconds: float, max_bytes: int,
        paths: tuple[str, ...] | None = None,
    ) -> str:
        """Literal, case-sensitive line search; complete results or refusal, no regex."""
        deadline, cap = self._operation(attempt_id, timeout_seconds, max_bytes, self._limits.max_output_bytes)
        _utf8(literal, cap)
        if not literal or "\n" in literal:
            raise SnapshotError("search requires a nonempty single-line literal")
        selected = self._selection(paths) if paths is not None else tuple(sorted(self._current))

        def matches():
            count = 0
            for path in selected:
                for number, line in enumerate(_lines(self._current[path]), 1):
                    _time_left(deadline)
                    if literal in line:
                        count += 1
                        if count > self._limits.max_matches:
                            raise SnapshotError("search match count exceeded")
                        yield f"{path}:{number}:{line}" + ("" if line.endswith("\n") else "\n")

        return self._output(matches(), cap, deadline)

    def context(self, attempt_id: str, *, timeout_seconds: float, max_bytes: int) -> str:
        """Length-delimited, caller-selected read-only context in the same namespace."""
        deadline, cap = self._operation(attempt_id, timeout_seconds, max_bytes, self._limits.max_output_bytes)

        def records():
            for path in self._context_paths:
                text = self._current[path]
                yield f"--- {path} ({len(text.encode('utf-8'))} UTF-8 bytes) ---\n"
                yield text
                yield "\n"

        return self._output(records(), cap, deadline)

    def assess(
        self, attempt_id: str, result: CommandResult, *, timeout_seconds: float, max_bytes: int,
    ) -> TestAssessment:
        deadline, cap = self._operation(attempt_id, timeout_seconds, max_bytes, self._limits.max_output_bytes)
        if not isinstance(result, CommandResult):
            raise SnapshotError("exact runner CommandResult required")
        assessment = self._assessor.assess(
            attempt_id, result, timeout_seconds=_time_left(deadline), max_bytes=cap,
        )
        _time_left(deadline)
        if (not isinstance(assessment, TestAssessment) or not isinstance(assessment.outcome, TestOutcome)
                or type(assessment.tests_run) is not int or assessment.tests_run < 0
                or type(assessment.assertion_failures) is not int
                or not 0 <= assessment.assertion_failures <= assessment.tests_run):
            raise SnapshotError("malformed trusted runner assessment")
        _utf8(assessment.detail, cap)
        return assessment

    def apply_patch(self, attempt_id: str, patch: str, *, timeout_seconds: float, max_bytes: int) -> bool:
        deadline, cap = self._operation(attempt_id, timeout_seconds, max_bytes, self._limits.max_patch_bytes)
        _utf8(patch, cap)
        if not patch or not patch.endswith("\n"):
            raise SnapshotError("patch must contain complete LF records")
        records = _lines(patch)
        candidate = self._current.copy()
        seen: set[str] = set()
        index = hunks = 0
        while index < len(records):
            _time_left(deadline)
            if not records[index].startswith("--- a/") or index + 1 >= len(records):
                raise SnapshotError("exact file headers required")
            path = self._path(records[index][6:-1])
            if records[index + 1] != f"+++ b/{path}\n":
                raise SnapshotError("old/new paths must agree exactly")
            if path in seen or path not in self._editable:
                raise SnapshotError("duplicate, missing or protected patch path")
            seen.add(path)
            index += 2
            original = self._current[path]
            old_lines = _lines(original)
            output: list[str] = []
            cursor = 0
            file_hunks = 0
            while index < len(records) and not records[index].startswith("--- a/"):
                _time_left(deadline)
                match = _HUNK.fullmatch(records[index])
                if match is None:
                    raise SnapshotError("unsupported hunk header")
                old_start, old_count, new_start, new_count = (
                    int(value) if value is not None else 1 for value in match.groups()
                )
                hunks += 1
                file_hunks += 1
                if hunks > self._limits.max_hunks:
                    raise SnapshotError("patch hunk count exceeded")
                position = old_start - 1 if old_count else old_start
                if (position < cursor or position > len(old_lines) or position + old_count > len(old_lines)
                        or (old_count and old_start == 0)
                        or (not old_count and (old_lines or file_hunks > 1))):
                    raise SnapshotError("overlapping/out-of-range hunk or missing old context")
                output.extend(old_lines[cursor:position])
                expected_new = len(output) + 1 if new_count else len(output)
                if new_start != expected_new:
                    raise SnapshotError("new hunk position disagrees with preceding edits")
                index += 1
                before: list[str] = []
                after: list[str] = []
                changed = False
                while index < len(records) and records[index][:1] in {" ", "+", "-"}:
                    # A file header is legal only after this hunk's counts are met.
                    if len(before) == old_count and len(after) == new_count:
                        break
                    record = records[index]
                    tag, text = record[0], record[1:]
                    index += 1
                    if index < len(records) and records[index] == _NO_NEWLINE:
                        if text == "\n":
                            raise SnapshotError("an empty line cannot have a no-newline marker")
                        text = text[:-1]
                        index += 1
                    if tag != "+":
                        before.append(text)
                    if tag != "-":
                        after.append(text)
                    changed = changed or tag != " "
                    if len(before) > old_count or len(after) > new_count:
                        raise SnapshotError("hunk line counts exceeded")
                if (len(before) != old_count or len(after) != new_count or not changed
                        or before != old_lines[position:position + old_count]):
                    raise SnapshotError("old text or hunk counts do not match exactly")
                if before:
                    needle = "\n" + "".join(before)
                    haystack = "\n" + original
                    first = haystack.find(needle)
                    if needle.endswith("\n") and haystack.find(needle, first + 1) != -1:
                        raise SnapshotError("ambiguous old context")
                output.extend(after)
                cursor = position + old_count
            if not file_hunks:
                raise SnapshotError("file patch has no hunks")
            output.extend(old_lines[cursor:])
            if any(not line.endswith("\n") for line in output[:-1]):
                raise SnapshotError("no-newline marker is only valid at end of file")
            candidate[path] = "".join(output)
            if candidate[path] == original:
                raise SnapshotError("file patch makes no change")
        self._validate_contents(candidate)
        _time_left(deadline)
        self._current = candidate
        return True

    def diff(self, attempt_id: str, *, timeout_seconds: float, max_bytes: int) -> str:
        deadline, cap = self._operation(attempt_id, timeout_seconds, max_bytes, self._limits.max_output_bytes)

        def records():
            for path in sorted(self._base):
                if self._base[path] == self._current[path]:
                    continue
                before, after = _lines(self._base[path]), _lines(self._current[path])
                yield f"--- a/{path}\n+++ b/{path}\n"
                yield f"@@ -{1 if before else 0},{len(before)} +{1 if after else 0},{len(after)} @@\n"
                for tag, lines in (("-", before), ("+", after)):
                    for line in lines:
                        yield tag + line + ("" if line.endswith("\n") else "\n" + _NO_NEWLINE)

        return self._output(records(), cap, deadline)

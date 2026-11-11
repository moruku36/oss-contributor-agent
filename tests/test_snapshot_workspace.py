"""Exactly three new cases; all model, backend and runner behavior is synthetic.

The integration uses the real CoderLoop, Sandbox and SnapshotWorkspace. Fixture
source is read as constant data, never executed/evaluated. No provider, host
workspace, container or actual isolation evidence is involved.
"""

from pathlib import Path

import pytest

from src.agent.core import (
    CoderLoop, LoopState, ModelReply, TestAssessment, TestCommand, TestOutcome, TestPlan,
)
from src.agent.snapshot_workspace import SnapshotError, SnapshotLimits, SnapshotWorkspace, TextFile
from src.agent.tools import (
    apply_patch, read_file, search_code, snapshot_apply_patch, snapshot_diff,
    snapshot_read_file, snapshot_search_code, write_file,
)
from src.sandbox.docker_manager import CommandResult, Sandbox
from src.sandbox.git_workspace import cleanup_workspace, export_diff, make_commit, prepare_workspace


ATTEMPT = "snapshot-fixture"
BOUNDS = {"timeout_seconds": 5, "max_bytes": 8192}


class _NoRunner:
    def assess(self, attempt_id, result, **bounds):
        raise SnapshotError("no runner evidence was registered")


def _workspace(files, *, editable, context=None, limits=SnapshotLimits()):
    return SnapshotWorkspace(
        ATTEMPT, files, editable_paths=editable,
        context_paths=editable if context is None else context, assessor=_NoRunner(), limits=limits,
    )


def _replacement(path, before, after):
    """Small exact one-line fixture patch; no generated historical test wrappers."""
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-{before}\n+{after}\n"


def test_multifile_edit_read_search_diff_and_atomic_rejection() -> None:
    files = (
        TextFile("src/a.py", "VALUE = 1\nkeep = 'é'\n", "source"),
        TextFile("src/b.py", "other = 3\n", "source"),
        TextFile("src/untouched.py", "immutable = True\n", "source"),
        TextFile("tests/evidence.txt", "expected=2\n", "test"),
    )
    editable = ("src/a.py", "src/b.py")
    workspace = _workspace(files, editable=editable, context=("src/a.py", "tests/evidence.txt"))
    patch_a = _replacement("src/a.py", "VALUE = 1", "VALUE = 2")
    patch_b = _replacement("src/b.py", "other = 3", "other = 4")
    assert snapshot_read_file(workspace, ATTEMPT, "src/a.py", **BOUNDS) == files[0].text
    assert snapshot_search_code(workspace, ATTEMPT, "é", **BOUNDS) == "src/a.py:2:keep = 'é'\n"
    assert snapshot_search_code(workspace, ATTEMPT, "(a+)+$", **BOUNDS) == ""  # literal, never regex
    context = workspace.context(ATTEMPT, **BOUNDS)
    assert "tests/evidence.txt" in context and "expected=2" in context and "src/b.py" not in context
    assert snapshot_apply_patch(workspace, ATTEMPT, patch_b + patch_a, **BOUNDS) is True
    assert snapshot_read_file(workspace, ATTEMPT, "src/a.py", **BOUNDS) == "VALUE = 2\nkeep = 'é'\n"
    assert snapshot_search_code(workspace, ATTEMPT, " = ", paths=editable, **BOUNDS) == (
        "src/a.py:1:VALUE = 2\nsrc/a.py:2:keep = 'é'\nsrc/b.py:1:other = 4\n"
    )
    expected = (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n"
        "-VALUE = 1\n-keep = 'é'\n+VALUE = 2\n+keep = 'é'\n"
        + patch_b
    )
    assert snapshot_diff(workspace, ATTEMPT, **BOUNDS) == expected
    assert snapshot_diff(workspace, ATTEMPT, **BOUNDS) == expected
    replay = _workspace(files, editable=editable)
    assert replay.apply_patch(ATTEMPT, expected, **BOUNDS) is True
    assert replay.diff(ATTEMPT, **BOUNDS) == expected
    with pytest.raises(SnapshotError):
        snapshot_diff(workspace, ATTEMPT, timeout_seconds=5, max_bytes=len(expected.encode("utf-8")) - 1)

    # A valid first file must never survive a rejected later file or hunk.
    valid_first = _replacement("src/a.py", "VALUE = 2", "VALUE = 9")
    with pytest.raises(SnapshotError):
        workspace.apply_patch(ATTEMPT, valid_first + _replacement("src/b.py", "missing", "other = 9"), **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.apply_patch(ATTEMPT, valid_first + valid_first, **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.apply_patch(ATTEMPT, valid_first + _replacement("tests/evidence.txt", "expected=2", "expected=9"), **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.apply_patch(ATTEMPT, valid_first + "@@ -2,1 +2,1 @@\n-wrong\n+changed\n", **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.apply_patch(ATTEMPT, valid_first.replace("+1,1", "+2,1"), **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.apply_patch(ATTEMPT, valid_first.replace("-1,1", "-1,2"), **BOUNDS)
    assert workspace.diff(ATTEMPT, **BOUNDS) == expected
    assert workspace.read_file(ATTEMPT, "tests/evidence.txt", **BOUNDS) == "expected=2\n"

    ambiguous = _workspace((TextFile("src/repeat.py", "same\nsame\n", "source"),), editable=("src/repeat.py",))
    with pytest.raises(SnapshotError, match="ambiguous"):
        ambiguous.apply_patch(ATTEMPT, _replacement("src/repeat.py", "same", "new"), **BOUNDS)
    assert ambiguous.diff(ATTEMPT, **BOUNDS) == ""

    # Empty files, multiple positioned hunks and missing terminal newlines round-trip.
    edges = _workspace((TextFile("src/edge.py", "one\nmiddle\nthree", "source"),
                        TextFile("src/empty.py", "", "source")), editable=("src/edge.py", "src/empty.py"))
    edge_patch = (
        "--- a/src/edge.py\n+++ b/src/edge.py\n@@ -1,1 +1,2 @@\n-one\n+ONE\n+extra\n"
        "@@ -3,1 +4,1 @@\n-three\n\\ No newline at end of file\n+THREE\n\\ No newline at end of file\n"
        "--- a/src/empty.py\n+++ b/src/empty.py\n@@ -0,0 +1,1 @@\n+filled\n"
    )
    duplicate_empty = ("--- a/src/empty.py\n+++ b/src/empty.py\n"
                       "@@ -0,0 +1,1 @@\n+first\n@@ -0,0 +2,1 @@\n+second\n")
    with pytest.raises(SnapshotError):
        edges.apply_patch(ATTEMPT, duplicate_empty, **BOUNDS)
    assert edges.diff(ATTEMPT, **BOUNDS) == ""
    assert edges.apply_patch(ATTEMPT, edge_patch, **BOUNDS) is True
    assert edges.read_file(ATTEMPT, "src/edge.py", **BOUNDS) == "ONE\nextra\nmiddle\nTHREE"
    exported = edges.diff(ATTEMPT, **BOUNDS)
    edge_replay = _workspace((TextFile("src/edge.py", "one\nmiddle\nthree", "source"),
                             TextFile("src/empty.py", "", "source")), editable=("src/edge.py", "src/empty.py"))
    assert edge_replay.apply_patch(ATTEMPT, exported, **BOUNDS) is True
    assert edge_replay.diff(ATTEMPT, **BOUNDS) == exported


def test_path_protected_unsupported_entry_and_size_boundaries_preserve_snapshot() -> None:
    files = (TextFile("src/a.py", "é\n", "source"), TextFile("src/b.py", "x\n", "source"),
             TextFile("tests/repro.txt", "2\n", "test"), TextFile("runner/data.txt", "ok\n", "runner"))
    limits = SnapshotLimits(max_total_bytes=10, max_file_bytes=3, max_files=4, max_path_bytes=16,
                            max_output_bytes=256, max_patch_bytes=256, max_hunks=2, max_matches=1)
    workspace = _workspace(files, editable=("src/a.py",), context=("src/a.py",), limits=limits)
    assert workspace.read_file(ATTEMPT, "src/a.py", timeout_seconds=5, max_bytes=3) == "é\n"
    before = tuple(workspace.read_file(ATTEMPT, entry.path, **BOUNDS) for entry in files)
    bad_paths = (
        "/src/a.py", "C:/src/a.py", "C:a.py", "src\\a.py", "./src/a.py", "src/../a.py",
        "src/./a.py", "src//a.py", "src/a.py/", "src/a.py\0", "src/a.py ", "src/a.py.",
        ".git/config", "src/.hg/x", "src/.svn/x", "src/.git/x", "src/A.py", "src/missing.py",
        "src/CON.txt", "src/a.py:stream", "src/é.py", "src/a\ud800", "src/longlonglong.py",
    )
    for path in bad_paths:
        with pytest.raises(SnapshotError):
            workspace.read_file(ATTEMPT, path, **BOUNDS)
        with pytest.raises(SnapshotError):
            workspace.search_code(ATTEMPT, "x", paths=(path,), **BOUNDS)
        with pytest.raises(SnapshotError):
            workspace.apply_patch(ATTEMPT, _replacement(path, "é", "z"), **BOUNDS)
    for path in ("src/b.py", "tests/repro.txt", "runner/data.txt"):
        with pytest.raises(SnapshotError):
            workspace.apply_patch(ATTEMPT, _replacement(path, "x", "z"), **BOUNDS)
    for path in ("tests/repro.txt", "runner/data.txt"):
        with pytest.raises(SnapshotError):
            _workspace(files, editable=(path,), limits=limits)
    for entries in (
        ({"path": "src/link", "kind": "symlink", "target": "src/a.py"},),
        ({"path": "src/link", "kind": "hardlink", "target": "src/a.py"},),
        ({"path": "src/dir", "kind": "directory"},),
        ({"path": "src/raw", "kind": "regular", "text": b"text"},),
        (TextFile("src/a.py", b"\xff", "source"),),
        (TextFile("src/a.py", "\ud800", "source"),),
        (TextFile("src/a.py", "a\0b", "source"),),
        (TextFile("src/a.py", "a\r\n", "source"),),
        (TextFile("src/a.py", "a\n", "symlink"),),
        (TextFile("src/a.py", "éé", "source"),),
        (files[0], files[0]),
        (files[0], TextFile("src/A.py", "a\n", "source")),
        (files[0], TextFile("SRC/b.py", "a\n", "source")),
        (TextFile("src", "a\n", "source"), files[0]),
        files + (TextFile("extra", "a\n", "source"),),
        (files[0], files[1], files[2], TextFile("runner/data.txt", "é\n", "runner")),
    ):
        # Last entry equals ten total bytes; use a smaller total to reject it.
        entry_limits = SnapshotLimits(max_total_bytes=9, max_file_bytes=3, max_files=4)
        with pytest.raises(SnapshotError):
            _workspace(entries, editable=("src/a.py",), limits=entry_limits)

    for kwargs in ({"max_files": True}, {"max_total_bytes": float("inf")}, {"max_patch_bytes": 0}):
        with pytest.raises(SnapshotError):
            SnapshotLimits(**kwargs)
    for paths in (("src/a.py", "src/a.py"), ("src/missing.py",), ("../src/a.py",)):
        with pytest.raises(SnapshotError):
            _workspace(files, editable=paths, limits=limits)
    with pytest.raises(SnapshotError):
        _workspace(files, editable=("src/a.py",), context=(".git/config",), limits=limits)
    for operation in (workspace.context, workspace.diff):
        with pytest.raises(SnapshotError, match="attempt"):
            operation("different-attempt", **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.apply_patch("different-attempt", _replacement("src/a.py", "é", "z"), **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.read_file("different-attempt", "src/a.py", **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.search_code("different-attempt", "é", **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.assess("different-attempt", CommandResult("repro", 1, "AssertionError"), **BOUNDS)
    with pytest.raises(SnapshotError, match="no runner evidence"):
        workspace.assess(ATTEMPT, CommandResult("repro", 1, "AssertionError"), **BOUNDS)

    # Independent file, aggregate, patch, hunk, match and complete-output bounds.
    for patch in (
        _replacement("src/a.py", "é", "éé"),
        _replacement("src/a.py", "é", "\ud800"),
        "diff --git a/src/a.py b/src/a.py\n" + _replacement("src/a.py", "é", "z"),
        _replacement("src/a.py", "é", "z").replace("b/src/a.py", "b/src/b.py"),
        _replacement("src/a.py", "é", "z").replace("@@ -1,1 +1,1 @@", "@@ -2,1 +1,1 @@"),
        _replacement("src/a.py", "é", "z") + "GIT binary patch\n",
        "x" * 257 + "\n",
    ):
        with pytest.raises(SnapshotError):
            workspace.apply_patch(ATTEMPT, patch, **BOUNDS)
    with pytest.raises(SnapshotError):
        workspace.read_file(ATTEMPT, "src/a.py", timeout_seconds=5, max_bytes=2)
    with pytest.raises(SnapshotError):
        workspace.context(ATTEMPT, timeout_seconds=5, max_bytes=3)
    with pytest.raises(SnapshotError):
        workspace.search_code(ATTEMPT, "é", timeout_seconds=5, max_bytes=3)
    with pytest.raises(SnapshotError):
        workspace.apply_patch(ATTEMPT, _replacement("src/a.py", "é", "z"), timeout_seconds=5, max_bytes=8)
    with pytest.raises(SnapshotError):
        workspace.read_file(ATTEMPT, "src/a.py", timeout_seconds=float("inf"), max_bytes=3)

    total = _workspace(files, editable=("src/b.py",), limits=SnapshotLimits(max_total_bytes=10, max_file_bytes=4))
    with pytest.raises(SnapshotError, match="total"):
        total.apply_patch(ATTEMPT, _replacement("src/b.py", "x", "xxx"), **BOUNDS)
    assert total.diff(ATTEMPT, **BOUNDS) == ""
    counts = _workspace((TextFile("a", "one\nmiddle\nthree\n", "source"),), editable=("a",),
                        limits=SnapshotLimits(max_hunks=1, max_matches=1))
    with pytest.raises(SnapshotError, match="hunk count"):
        counts.apply_patch(ATTEMPT, _replacement("a", "one", "ONE") + "@@ -3,1 +3,1 @@\n-three\n+THREE\n", **BOUNDS)
    with pytest.raises(SnapshotError, match="match count"):
        counts.search_code(ATTEMPT, "e", **BOUNDS)
    assert counts.diff(ATTEMPT, **BOUNDS) == ""
    assert tuple(workspace.read_file(ATTEMPT, entry.path, **BOUNDS) for entry in files) == before
    assert workspace.diff(ATTEMPT, **BOUNDS) == ""

    # Path/Git APIs still refuse; these calls never touch the constructed path.
    host_path = Path("unopened-host-path")
    for function, args in (
        (read_file, (host_path, "src/a.py")), (write_file, (host_path, "src/a.py", "z")),
        (search_code, (host_path, "x")), (apply_patch, (host_path, "patch")),
        (prepare_workspace, ("https://invalid.example/repo", "main", "fix")),
        (export_diff, (host_path,)), (make_commit, (host_path, "message", False)),
        (cleanup_workspace, (host_path,)),
    ):
        with pytest.raises(NotImplementedError):
            function(*args)


def test_real_coder_loop_and_sandbox_share_snapshot_with_trusted_synthetic_runner() -> None:
    class FixtureRunner:
        """Synthetic constant-data runner; no Python source execution or isolation."""

        def __init__(self):
            self.workspace = None
            self.attempt = None
            self.active = False
            self.removed = []
            self.executions = []
            self.receipts = []
            self.assessed = []

        def create(self, attempt_id, *, policy, timeout_seconds):
            assert self.attempt is None and timeout_seconds > 0
            assert policy.network == "none"
            self.attempt = attempt_id

        def start(self, attempt_id, *, timeout_seconds):
            assert attempt_id == self.attempt and not self.active and timeout_seconds > 0
            self.active = True

        def execute(self, attempt_id, command, *, timeout_seconds, max_output_bytes):
            assert self.active and attempt_id == self.attempt
            bounds = {"timeout_seconds": timeout_seconds, "max_bytes": max_output_bytes}
            source = self.workspace.read_file(attempt_id, "src/answer.py", **bounds)
            runner = self.workspace.read_file(attempt_id, "runner/fixture.txt", **bounds)
            assert runner == "constant-data-runner-v1\n"
            expected = self.workspace.read_file(attempt_id, f"tests/{command}.txt", **bounds)
            if command == "reproduce":
                assert expected == "ANSWER = 2\n"
                passed = source == expected
            elif command == "suite-shape":
                assert expected == "single ANSWER assignment\n"
                passed = source in {"ANSWER = 1\n", "ANSWER = 2\n"}
            elif command == "suite-range":
                assert expected == "allowed answers: 1,2\n"
                passed = source.removeprefix("ANSWER = ").strip() in {"1", "2"}
            else:
                raise AssertionError("undeclared fixture command")
            assessment = TestAssessment(TestOutcome.PASS, 1) if passed else TestAssessment(
                TestOutcome.ASSERTION_FAILED, 1, 1, "reproduce: expected ANSWER = 2, got ANSWER = 1",
            )
            # Text deliberately provides no verdict. Receipt identity is authoritative.
            result = CommandResult(command, 0 if passed else 1, "fixture receipt only", duration_seconds=0)
            self.receipts.append((attempt_id, result, assessment))
            self.executions.append((command, source))
            return result

        def assess(self, attempt_id, result, *, timeout_seconds, max_bytes):
            assert timeout_seconds > 0 and max_bytes > 0
            for number, (key, receipt, assessment) in enumerate(self.receipts):
                if attempt_id == key and result is receipt and number not in self.assessed:
                    self.assessed.append(number)
                    return assessment
            raise SnapshotError("unrecognized, wrong-attempt or replayed runner result")

        def remove(self, attempt_id, *, timeout_seconds):
            assert attempt_id == self.attempt and self.active and timeout_seconds > 0
            self.active = False
            self.removed.append(attempt_id)  # synthetic resource cleanup only

    class FixtureModel:
        """One explicitly synthetic proposal, with no provider or model evidence."""

        def __init__(self):
            self.calls = 0

        def propose(self, request, **bounds):
            self.calls += 1
            assert "ANSWER = 1" in request.context and "expected ANSWER = 2" in request.failing_log_tail
            assert "runner/fixture.txt" not in request.context
            assert bounds["remaining_attempts"] == bounds["remaining_calls"] == 1
            return ModelReply(_replacement("src/answer.py", "ANSWER = 1", "ANSWER = 2"),
                              7, "synthetic-fixture", "synthetic-fixture")

    runner = FixtureRunner()
    sandbox = Sandbox("synthetic-memory-fixture", 1, 64, backend=runner, max_commands=6)
    workspace = SnapshotWorkspace(
        sandbox.attempt_id,
        (TextFile("src/answer.py", "ANSWER = 1\n", "source"),
         TextFile("tests/reproduce.txt", "ANSWER = 2\n", "test"),
         TextFile("tests/suite-shape.txt", "single ANSWER assignment\n", "test"),
         TextFile("tests/suite-range.txt", "allowed answers: 1,2\n", "test"),
         TextFile("runner/fixture.txt", "constant-data-runner-v1\n", "runner")),
        editable_paths=("src/answer.py",), context_paths=("src/answer.py",), assessor=runner,
    )
    runner.workspace = workspace
    model = FixtureModel()
    plan = TestPlan(TestCommand("reproduce", 2), (TestCommand("suite-shape", 2), TestCommand("suite-range", 2)))
    result = CoderLoop(1, 20, model=model, timeout_seconds=20, max_text_bytes=8192).run(
        {"number": 1, "title": "constant should be 2"}, workspace, sandbox, plan=plan,
    )
    assert result.success and result.state == LoopState.SUCCEEDED and result.failure_kind is None
    assert model.calls == result.model_calls == result.attempts_used == 1
    assert result.tokens_used == 7
    assert result.diff == _replacement("src/answer.py", "ANSWER = 1", "ANSWER = 2")
    assert runner.executions == [
        ("suite-shape", "ANSWER = 1\n"), ("suite-range", "ANSWER = 1\n"), ("reproduce", "ANSWER = 1\n"),
        ("reproduce", "ANSWER = 2\n"), ("suite-shape", "ANSWER = 2\n"), ("suite-range", "ANSWER = 2\n"),
    ]
    assert [check.phase for check in result.checks] == [
        LoopState.BASELINE, LoopState.BASELINE, LoopState.REPRODUCING,
        LoopState.VERIFYING_REPRODUCTION, LoopState.VERIFYING_SUITE, LoopState.VERIFYING_SUITE,
    ]
    assert [check.assessment.outcome for check in result.checks] == [
        TestOutcome.PASS, TestOutcome.PASS, TestOutcome.ASSERTION_FAILED,
        TestOutcome.PASS, TestOutcome.PASS, TestOutcome.PASS,
    ]
    assert all(check.assessment.tests_run == 1 for check in result.checks)
    assert runner.assessed == list(range(6))
    assert result.cleanup_confirmed and sandbox.state == "closed"
    assert runner.removed == [sandbox.attempt_id] and not runner.active
    assert result.sandbox_attempt_id == sandbox.attempt_id
    assert workspace.read_file(sandbox.attempt_id, "src/answer.py", **BOUNDS) == "ANSWER = 2\n"
    assert workspace.read_file(sandbox.attempt_id, "tests/reproduce.txt", **BOUNDS) == "ANSWER = 2\n"
    assert workspace.read_file(sandbox.attempt_id, "runner/fixture.txt", **BOUNDS) == "constant-data-runner-v1\n"
    # Equal-looking exit/log results are not evidence; consumed receipts cannot replay.
    with pytest.raises(SnapshotError, match="unrecognized"):
        workspace.assess(sandbox.attempt_id, CommandResult("reproduce", 1, "AssertionError"), **BOUNDS)
    with pytest.raises(SnapshotError, match="unrecognized"):
        workspace.assess(sandbox.attempt_id, runner.receipts[2][1], **BOUNDS)

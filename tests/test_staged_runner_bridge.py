"""Exactly three bridge cases on a real temporary filesystem; nothing is executed."""

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat

import pytest

from src.agent import filesystem_staging as staging
from src.agent.core import TestAssessment, TestOutcome
from src.agent.filesystem_staging import IncompleteStageReceipt, ManifestFile, StageReceipt
from src.agent.snapshot_workspace import SnapshotError, SnapshotWorkspace, TextFile
from src.agent.staged_runner_bridge import (
    DispatchError, EvidenceRejected, RunnerEvidence, StagedRunnerBridge, receipt_digest,
)
from src.sandbox.docker_manager import CommandResult


ATTEMPT = "bridge-fixture"
COMMAND = "pytest tests/expected.txt"
RUN = {"timeout_seconds": 5, "max_output_bytes": 4096}
ASSESS = {"timeout_seconds": 5, "max_bytes": 4096}
FILES = {
    "src/answer.py": ("ANSWER = 1\n", "source"),
    "tests/expected.txt": ("ANSWER = 2\n", "test"),
    "runner/fixture.txt": ("fixture\n", "runner"),
}
MANIFEST = tuple(ManifestFile(path, role) for path, (_, role) in FILES.items())
PATCH = "--- a/src/answer.py\n+++ b/src/answer.py\n@@ -1,1 +1,1 @@\n-ANSWER = 1\n+ANSWER = 2\n"


class _Runner:
    """Fake trusted runner: reads the staged generation, never runs the command."""

    def __init__(self, parent: Path) -> None:
        self.parent, self.tickets, self.bounds, self.verdicts = parent, [], [], []
        self.fail: BaseException | None = None
        self.replay: RunnerEvidence | None = None

    def run_staged(self, ticket, *, timeout_seconds, max_output_bytes):
        self.tickets.append(ticket)
        self.bounds.append((timeout_seconds, max_output_bytes))
        if self.fail is not None:
            raise self.fail
        if self.replay is not None:
            return self.replay
        stage = self.parent / ticket.receipt.stage_name
        for item in ticket.receipt.files:
            assert sha256((stage / item.path).read_bytes()).hexdigest() == item.sha256
        answer = (stage / "src/answer.py").read_text()
        passed = answer == (stage / "tests/expected.txt").read_text()
        result = CommandResult(ticket.command, 0 if passed else 1, stdout=answer, duration_seconds=0.01)
        self.verdicts.append((result, passed))
        return RunnerEvidence(
            ticket.attempt_id, ticket.dispatch_id, ticket.receipt.stage_name,
            ticket.receipt_sha256, result,
        )


class _Assessor:
    def __init__(self, runner: _Runner) -> None:
        self.runner, self.calls = runner, []

    def assess(self, attempt_id, result, *, timeout_seconds, max_bytes):
        assert attempt_id == ATTEMPT
        self.calls.append(result)
        passed = next(verdict for known, verdict in self.runner.verdicts if known is result)
        if passed:
            return TestAssessment(TestOutcome.PASS, 1, 0, "")
        return TestAssessment(TestOutcome.ASSERTION_FAILED, 1, 1, "answer mismatch")


@contextmanager
def _parent_fd(parent: Path):
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def _setup(tmp_path: Path):
    parent = tmp_path / "stages"
    parent.mkdir(exist_ok=True)
    runner = _Runner(parent)
    assessor = _Assessor(runner)
    workspace = SnapshotWorkspace(
        ATTEMPT, tuple(TextFile(path, text, role) for path, (text, role) in FILES.items()),
        editable_paths=("src/answer.py",), context_paths=(), assessor=assessor,
    )
    return parent, runner, assessor, workspace


def _bridge(workspace, parent_fd, runner):
    return StagedRunnerBridge(
        ATTEMPT, workspace=workspace, staging_parent_fd=parent_fd, manifest=MANIFEST, runner=runner,
    )


def test_each_command_hands_off_a_fresh_generation_of_the_current_snapshot(tmp_path: Path) -> None:
    parent, runner, assessor, workspace = _setup(tmp_path)
    with _parent_fd(parent) as parent_fd:
        with _bridge(workspace, parent_fd, runner) as bridge:
            first = bridge.execute(ATTEMPT, COMMAND, **RUN)
            assert first.exit_code == 1 and bridge.generation == 1
            assert bridge.assess(ATTEMPT, first, **ASSESS).outcome is TestOutcome.ASSERTION_FAILED
            assert workspace.apply_patch(ATTEMPT, PATCH, timeout_seconds=5, max_bytes=4096)
            second = bridge.execute(ATTEMPT, COMMAND, **RUN)
            assert second is not first and second.exit_code == 0 and bridge.generation == 2
            assert bridge.assess(ATTEMPT, second, **ASSESS).outcome is TestOutcome.PASS
            record = bridge.last_dispatch
            assert record.evidence.result is second and record.ticket is runner.tickets[1]
        assert bridge.state == "closed"
        assert stat.S_ISDIR(os.fstat(parent_fd).st_mode)  # caller descriptor stays open
    old, new = runner.tickets
    assert (old.generation, new.generation) == (1, 2) and old.command == new.command == COMMAND
    assert old.dispatch_id != new.dispatch_id
    assert old.receipt.stage_name != new.receipt.stage_name
    assert old.receipt_sha256 != new.receipt_sha256
    for ticket, answer in ((old, b"ANSWER = 1\n"), (new, b"ANSWER = 2\n")):
        receipt = ticket.receipt
        assert type(receipt) is StageReceipt and receipt.attempt_id == ATTEMPT
        assert ticket.receipt_sha256 == receipt_digest(receipt)
        stage = parent / receipt.stage_name
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
        assert (stage / "src/answer.py").read_bytes() == answer  # old generation is retained as staged
        assert tuple(item.path for item in receipt.files) == tuple(sorted(FILES))
        for item in receipt.files:
            assert stat.S_IMODE((stage / item.path).stat().st_mode) == 0o600
    assert sorted(path.name for path in parent.iterdir()) == sorted(
        ticket.receipt.stage_name for ticket in runner.tickets
    )
    assert all(0 < timeout <= 5 and cap == 4096 for timeout, cap in runner.bounds)
    assert len(assessor.calls) == 2


def test_stale_replayed_or_mismatched_evidence_is_never_assessed(tmp_path: Path) -> None:
    parent, runner, assessor, workspace = _setup(tmp_path)
    with _parent_fd(parent) as parent_fd, _bridge(workspace, parent_fd, runner) as bridge:
        first = bridge.execute(ATTEMPT, COMMAND, **RUN)
        assert workspace.apply_patch(ATTEMPT, PATCH, timeout_seconds=5, max_bytes=4096)
        with pytest.raises(EvidenceRejected, match="snapshot changed"):
            bridge.assess(ATTEMPT, first, **ASSESS)  # staged before the patch
        second = bridge.execute(ATTEMPT, COMMAND, **RUN)
        twin = CommandResult(second.command, second.exit_code, second.stdout,
                             second.stderr, second.duration_seconds)
        for other in (first, twin):  # superseded generation; equal but not the exact object
            with pytest.raises(EvidenceRejected, match="stale"):
                bridge.assess(ATTEMPT, other, **ASSESS)
        with pytest.raises(SnapshotError, match="attempt"):
            bridge.assess("other-attempt", second, **ASSESS)
        assert assessor.calls == []
        assert bridge.assess(ATTEMPT, second, **ASSESS).outcome is TestOutcome.PASS
        with pytest.raises(EvidenceRejected, match="replayed"):
            bridge.assess(ATTEMPT, second, **ASSESS)
        third = bridge.execute(ATTEMPT, COMMAND, **RUN)
        third.exit_code = 1  # mutated after its evidence was accepted
        with pytest.raises(EvidenceRejected, match="changed after"):
            bridge.assess(ATTEMPT, third, **ASSESS)
        assert len(assessor.calls) == 1 and assessor.calls[0] is second

        # A runner replaying evidence of an earlier generation is a terminal mismatch.
        runner.replay = bridge.last_dispatch.evidence
        with pytest.raises(DispatchError) as mismatched:
            bridge.execute(ATTEMPT, COMMAND, **RUN)
        receipt = mismatched.value.receipt
        assert receipt.phase == "runner" and receipt.outcome == "partial_or_unknown"
        assert receipt.generation == 4 and type(receipt.stage) is StageReceipt
        assert "echo" in str(mismatched.value.original_error)
        assert bridge.state == "failed" and len(runner.tickets) == 4
        with pytest.raises(DispatchError) as refused:
            bridge.assess(ATTEMPT, third, **ASSESS)
        assert refused.value.receipt is receipt
    assert len(assessor.calls) == 1 and len(list(parent.iterdir())) == 4


def test_partial_or_unknown_dispatch_is_terminal_and_owns_only_its_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned: set[int] = set()
    real_owned, real_write = staging._owned, os.write

    def track_owned(stack, fd):
        owned.add(fd)
        return real_owned(stack, fd)

    def assert_released(parent_fd: int) -> None:
        assert owned
        for fd in owned:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert stat.S_ISDIR(os.fstat(parent_fd).st_mode)  # never the caller's descriptor
        owned.clear()

    monkeypatch.setattr(staging, "_owned", track_owned)

    # Partial staging: a real short write, then failure. The runner is never called.
    parent, runner, assessor, workspace = _setup(tmp_path)
    with _parent_fd(parent) as parent_fd:
        bridge = _bridge(workspace, parent_fd, runner)

        def fail_source_write(fd, data):
            if bytes(data) == b"ANSWER = 1\n":
                real_write(fd, data[:3])
                raise OSError("injected failure after partial write")
            return real_write(fd, data)

        with monkeypatch.context() as failing:
            failing.setattr(staging.os, "write", fail_source_write)
            with pytest.raises(DispatchError) as failed:
                bridge.execute(ATTEMPT, COMMAND, **RUN)
        receipt = failed.value.receipt
        assert receipt.phase == "staging" and receipt.outcome == "partial_or_unknown"
        assert type(receipt.stage) is IncompleteStageReceipt
        assert receipt.stage.attempted_path == "src/answer.py"
        assert tuple(item.path for item in receipt.stage.verified_files) == ("runner/fixture.txt",)
        assert "injected failure" in str(failed.value.original_error)
        assert failed.value.close_error is None
        assert bridge.state == "failed" and bridge.generation == 1 and runner.tickets == []
        assert_released(parent_fd)
        with pytest.raises(DispatchError) as repeated:  # no retry, no new generation
            bridge.execute(ATTEMPT, COMMAND, **RUN)
        assert repeated.value.receipt is receipt and bridge.generation == 1
        bridge.close()
        assert bridge.state == "failed" and runner.tickets == [] and not owned
        # No rollback or deletion: the partial generation stays for its owner.
        stage = parent / receipt.stage.stage_name
        assert [path.name for path in parent.iterdir()] == [stage.name]
        assert (stage / "runner/fixture.txt").read_bytes() == b"fixture\n"
        assert (stage / "src/answer.py").read_bytes() == b"ANS"
        assert not (stage / "tests/expected.txt").exists()

        # Unknown runner outcome after a complete stage: equally terminal, dispatched once.
        _, runner, assessor, workspace = _setup(tmp_path)
        runner.fail = TimeoutError("runner outcome unknown")
        bridge = _bridge(workspace, parent_fd, runner)
        with pytest.raises(DispatchError) as unknown:
            bridge.execute(ATTEMPT, COMMAND, **RUN)
        receipt = unknown.value.receipt
        assert receipt.phase == "runner" and receipt.outcome == "partial_or_unknown"
        assert type(receipt.stage) is StageReceipt and receipt.stage is runner.tickets[0].receipt
        assert isinstance(unknown.value.original_error, TimeoutError)
        assert_released(parent_fd)
        for call in (
            lambda: bridge.execute(ATTEMPT, COMMAND, **RUN),
            lambda: bridge.assess(ATTEMPT, CommandResult(COMMAND, 0), **ASSESS),
        ):
            with pytest.raises(DispatchError) as again:
                call()
            assert again.value.receipt is receipt
        assert len(runner.tickets) == 1 and assessor.calls == [] and bridge.last_dispatch is None
        complete = parent / receipt.stage.stage_name
        assert (complete / "src/answer.py").read_bytes() == b"ANSWER = 1\n"
        assert len(list(parent.iterdir())) == 2

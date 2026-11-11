"""Three synthetic cases; real staging bytes, no fixture execution or live model."""
from contextlib import contextmanager
from hashlib import sha256
import os
import shutil

import pytest

from src.agent.core import (
    CoderLoop, ModelReply, TestAssessment, TestCommand, TestOutcome, TestPlan,
)
from src.agent.filesystem_staging import ManifestFile
from src.agent.snapshot_workspace import SnapshotWorkspace, TextFile
from src.agent.staged_attempt import StagedAttemptAdapter
from src.agent.staged_runner_bridge import RunnerEvidence, StagedRunnerBridge, receipt_digest
from src.sandbox.docker_manager import (
    CommandResult, Sandbox, SandboxCleanupError, SandboxError, SandboxPolicy,
)

ORIGINAL = "ANSWER = 1\n"
FIXED = "ANSWER = 2\n"
PATCH = "--- a/answer.py\n+++ b/answer.py\n@@ -1,1 +1,1 @@\n-ANSWER = 1\n+ANSWER = 2\n"
MANIFEST = (ManifestFile("answer.py", "source"), ManifestFile("expected.txt", "test"))
POLICY = SandboxPolicy("synthetic:test", 1, 256)
WORK = dict(timeout_seconds=5, max_bytes=4096)
RUN = dict(timeout_seconds=5, max_output_bytes=4096)


class Lifecycle:
    def __init__(self, parent, events, fail_create=False, fail_remove=False):
        self.parent, self.events = parent, events
        self.fail_create, self.fail_remove = fail_create, fail_remove
        self.calls = []

    def create(self, attempt_id, *, policy, timeout_seconds):
        self.events.append("create")
        self.calls.append(("create", attempt_id, policy, timeout_seconds))
        if self.fail_create:
            raise OSError("partial lifecycle create")

    def start(self, attempt_id, *, timeout_seconds):
        self.events.append("start")
        self.calls.append(("start", attempt_id, timeout_seconds))

    def remove(self, attempt_id, *, timeout_seconds):
        self.events.append("remove")
        self.calls.append(("remove", attempt_id, timeout_seconds))
        if self.fail_remove:
            raise OSError("lifecycle removal unknown")
        # This synthetic lifecycle owns this private temporary parent only.
        for stage in self.parent.iterdir():
            shutil.rmtree(stage)


class Runner:
    """Trusted test stand-in reads and hashes bytes; never evaluates source."""

    def __init__(self, parent, mode="normal"):
        self.parent, self.mode = parent, mode
        self.tickets, self.results, self.bounds = [], [], []

    def run_staged(self, ticket, *, timeout_seconds, max_output_bytes):
        self.tickets.append(ticket)
        self.bounds.append((timeout_seconds, max_output_bytes))
        assert ticket.receipt_sha256 == receipt_digest(ticket.receipt)
        stage = self.parent / ticket.receipt.stage_name
        for item in ticket.receipt.files:
            data = (stage / item.path).read_bytes()
            assert sha256(data).hexdigest() == item.sha256
            assert len(data) == item.size_bytes
        if self.mode == "raise":
            raise TimeoutError("runner outcome unknown")
        answer = (stage / "answer.py").read_bytes()
        passed = ticket.command == "suite" or answer == (stage / "expected.txt").read_bytes()
        result = CommandResult(ticket.command, 0 if passed else 1, stdout="synthetic evidence")
        self.results.append((ticket.attempt_id, result, passed))
        return RunnerEvidence(
            ticket.attempt_id, "wrong-dispatch" if self.mode == "mismatch" else ticket.dispatch_id,
            ticket.receipt.stage_name, ticket.receipt_sha256, result,
        )


class Assessor:
    def __init__(self, runner):
        self.runner, self.calls = runner, []

    def assess(self, attempt_id, result, *, timeout_seconds, max_bytes):
        # Recognize the exact trusted runner object, not exit-code/log heuristics.
        assert not any(known is result for known in self.calls)
        passed = next(passed for key, known, passed in self.runner.results
                      if key == attempt_id and known is result)
        self.calls.append(result)
        return TestAssessment(
            TestOutcome.PASS if passed else TestOutcome.ASSERTION_FAILED,
            1, 0 if passed else 1, "synthetic byte comparison",
        )


class Model:
    def __init__(self):
        self.calls = []

    def propose(self, request, **bounds):
        self.calls.append(bounds)
        assert ORIGINAL in request.context
        assert bounds["remaining_calls"] == bounds["remaining_attempts"] == 1
        assert bounds["remaining_tokens"] == 100 and bounds["max_patch_bytes"] == 4096
        assert 0 < bounds["timeout_seconds"] <= 30
        return ModelReply(PATCH, 7, "synthetic-provider", "synthetic-model")


@contextmanager
def setup_attempt(tmp_path, *, mode="normal", fail_create=False, fail_remove=False, factory_fail=False):
    parent = tmp_path / "stages"
    parent.mkdir(parents=True)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    events = []
    lifecycle = Lifecycle(parent, events, fail_create, fail_remove)
    runner = Runner(parent, mode)
    assessor = Assessor(runner)

    def factory(attempt_id):
        events.append("factory")
        if factory_fail:
            raise RuntimeError("factory failure")
        return SnapshotWorkspace(
            attempt_id, (TextFile("answer.py", ORIGINAL, "source"),
                         TextFile("expected.txt", FIXED, "test")),
            editable_paths=("answer.py",), context_paths=("answer.py",),
            assessor=assessor,
        )

    adapter = StagedAttemptAdapter(
        lifecycle=lifecycle, workspace_factory=factory,
        staging_parent_fd=fd, manifest=MANIFEST, runner=runner,
        max_output_bytes=4096,
    )
    assert events == [] and adapter.state == "new" and runner.tickets == []
    try:
        yield adapter, lifecycle, runner, assessor, parent, fd, events
    finally:
        os.close(fd)


def test_real_loop_with_staged_adapter_end_to_end(tmp_path):
    with setup_attempt(tmp_path) as (adapter, lifecycle, runner, assessor, parent, fd, events):
        model = Model()
        sandbox = Sandbox("synthetic:test", 1, 256, backend=adapter, max_output_bytes=4096)
        result = CoderLoop(
            1, 100, model=model, max_model_calls=1, timeout_seconds=30,
            max_text_bytes=4096, max_patch_bytes=4096,
        ).run(
            {"title": "synthetic wrong answer"}, adapter, sandbox,
            plan=TestPlan(TestCommand("reproduce", 5), (TestCommand("suite", 5),)),
        )
        assert result.success and result.cleanup_confirmed and result.diff == PATCH
        assert result.model_calls == result.attempts_used == len(model.calls) == 1
        assert result.tokens_used == 7
        assert len(assessor.calls) == 4
        assert [ticket.command for ticket in runner.tickets] == ["suite", "reproduce", "reproduce", "suite"]
        assert [known.exit_code for known in assessor.calls] == [0, 1, 0, 0]
        assert [ticket.generation for ticket in runner.tickets] == [1, 2, 3, 4]
        assert len({ticket.dispatch_id for ticket in runner.tickets}) == 4
        assert len({ticket.receipt.stage_name for ticket in runner.tickets}) == 4
        for index, ticket in enumerate(runner.tickets):
            source = next(item for item in ticket.receipt.files if item.path == "answer.py")
            assert source.sha256 == sha256((ORIGINAL if index < 2 else FIXED).encode()).hexdigest()
        assert all(0 < timeout <= 5 and cap == 4096 for timeout, cap in runner.bounds)
        assert lifecycle.calls[0][2] is sandbox.policy
        assert events == ["create", "factory", "start", "remove"]
        assert adapter.state == "removed" and list(parent.iterdir()) == []
        assert os.fstat(fd)  # caller descriptor is not owned by the bridge


def test_lifecycle_order_ids_single_use_and_partial_create_cleanup(tmp_path, monkeypatch):
    with setup_attempt(tmp_path / "normal") as (adapter, lifecycle, runner, assessor, parent, fd, events):
        with pytest.raises(SandboxError):
            adapter.start("attempt", timeout_seconds=5)
        adapter.create("attempt", policy=POLICY, timeout_seconds=5)
        assert events == ["create", "factory"] and lifecycle.calls[0][2] is POLICY
        with pytest.raises(SandboxError):
            adapter.execute("attempt", "suite", **RUN)
        adapter.start("attempt", timeout_seconds=5)
        for operation in (
            lambda key: adapter.create(key, policy=POLICY, timeout_seconds=5),
            lambda key: adapter.start(key, timeout_seconds=5),
            lambda key: adapter.execute(key, "suite", **RUN),
            lambda key: adapter.context(key, **WORK),
            lambda key: adapter.apply_patch(key, PATCH, **WORK),
            lambda key: adapter.diff(key, **WORK),
            lambda key: adapter.assess(key, CommandResult("suite", 0), **WORK),
            lambda key: adapter.remove(key, timeout_seconds=5),
        ):
            with pytest.raises(SandboxError):
                operation("wrong")
        assert runner.tickets == [] and assessor.calls == []
        adapter.remove("attempt", timeout_seconds=5)
        for operation in (
            lambda: adapter.context("attempt", **WORK),
            lambda: adapter.apply_patch("attempt", PATCH, **WORK),
            lambda: adapter.diff("attempt", **WORK),
            lambda: adapter.assess("attempt", CommandResult("suite", 0), **WORK),
            lambda: adapter.execute("attempt", "suite", **RUN),
            lambda: adapter.start("attempt", timeout_seconds=5),
            lambda: adapter.create("attempt", policy=POLICY, timeout_seconds=5),
            lambda: adapter.remove("attempt", timeout_seconds=5),
        ):
            with pytest.raises(SandboxError):
                operation()
        assert [call[0] for call in lifecycle.calls] == ["create", "start", "remove"]
        assert lifecycle.calls[-1][-1] == 5 and os.fstat(fd)

    # Both lifecycle partial allocation and workspace-factory failure stay removable.
    for failure in ("lifecycle", "factory"):
        with setup_attempt(tmp_path / failure, fail_create=failure == "lifecycle",
                           factory_fail=failure == "factory") as fixture:
            adapter, lifecycle, runner, _, _, _, events = fixture
            with pytest.raises((OSError, RuntimeError)):
                adapter.create("partial", policy=POLICY, timeout_seconds=5)
            assert adapter.state == "create_failed"
            adapter.remove("partial", timeout_seconds=5)
            with pytest.raises(SandboxError):
                adapter.remove("partial", timeout_seconds=5)
            with pytest.raises(SandboxError):
                adapter.create("other", policy=POLICY, timeout_seconds=5)
            assert [call[0] for call in lifecycle.calls] == ["create", "remove"]
            assert runner.tickets == []


def test_runner_failure_and_cleanup_failure_are_not_confirmed_or_retried(tmp_path, monkeypatch):
    # Two distinct unknown-dispatch modes, not hidden test-suite invocations.
    for mode in ("raise", "mismatch"):
        with setup_attempt(tmp_path / mode, mode=mode, fail_remove=True) as fixture:
            adapter, lifecycle, runner, assessor, parent, fd, _ = fixture
            sandbox = Sandbox("synthetic:test", 1, 256, backend=adapter, max_output_bytes=4096)
            result = CoderLoop(1, 100, model=Model()).run(
                {"title": "unknown baseline"}, adapter, sandbox,
                plan=TestPlan(TestCommand("reproduce", 5), (TestCommand("suite", 5),)),
            )
            assert not result.success and not result.cleanup_confirmed
            assert result.model_calls == 0 and len(runner.tickets) == 1
            assert assessor.calls == [] and adapter.state == "remove_failed"
            stage = parent / runner.tickets[0].receipt.stage_name
            assert (stage / "answer.py").read_text() == ORIGINAL
            with pytest.raises(SandboxError):
                adapter.remove(sandbox.attempt_id, timeout_seconds=5)
            with pytest.raises(SandboxError):
                adapter.execute(sandbox.attempt_id, "suite", **RUN)
            assert len(runner.tickets) == 1
            assert [call[0] for call in lifecycle.calls] == ["create", "start", "remove"]
            assert os.fstat(fd)

    # Descriptor-close error cannot suppress lifecycle removal or its error.
    for fail_remove in (False, True):
        with setup_attempt(tmp_path / f"closing-{fail_remove}", fail_remove=fail_remove) as fixture:
            adapter, lifecycle, _, _, parent, fd, events = fixture
            adapter.create("closing", policy=POLICY, timeout_seconds=5)
            original_close = StagedRunnerBridge.close
            close_error = OSError("descriptor close report unknown")

            def close_then_raise(bridge):
                original_close(bridge)
                raise close_error

            with monkeypatch.context() as scoped:
                scoped.setattr(StagedRunnerBridge, "close", close_then_raise)
                with pytest.raises((OSError, SandboxCleanupError)) as caught:
                    adapter.remove("closing", timeout_seconds=5)
            if fail_remove:
                assert isinstance(caught.value, SandboxCleanupError)
                assert caught.value.original_error is close_error
                assert isinstance(caught.value.cleanup_error, OSError)
            else:
                assert caught.value is close_error
            assert events == ["create", "factory", "remove"]
            assert adapter.state == "remove_failed" and os.fstat(fd)
            with pytest.raises(SandboxError):
                adapter.remove("closing", timeout_seconds=5)

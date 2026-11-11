"""Exactly three mock-only CoderLoop cases; no tests/commands actually dispatched.

Sandbox is the existing lifecycle implementation with a mocked backend. Runner
receipts, model replies, source and time are synthetic; no provider or host-file
workspace exists here. These cases do not wrap or re-run historical tests.
"""

from unittest.mock import Mock

import pytest

from src.agent import core
from src.agent.core import (
    AttemptResult, CoderLoop, LoopState, ModelReply, PatchModel, TestAssessment,
    TestCommand, TestOutcome, TestPlan, Workspace,
)
from src.sandbox import docker_manager
from src.sandbox.docker_manager import CommandResult, Sandbox, SandboxBackend


PASS = TestAssessment(TestOutcome.PASS, tests_run=4)
FAIL = TestAssessment(TestOutcome.ASSERTION_FAILED, 1, 1, "test_bug: expected 2, got 1")
PLAN = TestPlan(TestCommand("reproduce", 2), (TestCommand("suite-a", 3), TestCommand("suite-b", 3)))
PATCH = "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-return 1\n+return 2\n"


def _rig(monkeypatch: pytest.MonkeyPatch, script: list[tuple[str, int, TestAssessment, str]]):
    now = [0.0]
    monkeypatch.setattr(core, "monotonic", lambda: now[0])
    monkeypatch.setattr(docker_manager, "monotonic", lambda: now[0])
    backend = Mock(spec=SandboxBackend)
    backend.create.return_value = backend.start.return_value = backend.remove.return_value = None
    backend.execute.side_effect = [CommandResult(cmd, code, log, duration_seconds=0.1)
                                   for cmd, code, _, log in script]
    workspace = Mock(spec=Workspace)
    workspace.assess.side_effect = [assessment for _, _, assessment, _ in script]
    workspace.context.return_value = "source.py: return 1; tests are caller-supplied and protected"
    workspace.apply_patch.return_value = True
    workspace.diff.return_value = PATCH
    model = Mock(spec=PatchModel)
    model.propose.return_value = ModelReply(PATCH, 7, "mock-provider", "mock-model")
    sandbox = Sandbox("fixture:local", 1, 64, backend=backend, attempt_timeout_seconds=10,
                      operation_timeout_seconds=1, max_commands=16)
    return now, backend, workspace, model, sandbox


def test_complete_lifecycle_verifies_entire_suite_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    script = [
        ("suite-a", 0, PASS, "baseline a"), ("suite-b", 0, PASS, "baseline b"),
        ("reproduce", 1, FAIL, "AssertionError: expected 2, got 1"),
        ("reproduce", 0, PASS, "reproduction fixed"),
        ("suite-a", 0, PASS, "verified a"), ("suite-b", 0, PASS, "verified b"),
    ]
    now, backend, workspace, model, sandbox = _rig(monkeypatch, script)
    events = Mock()
    events.attach_mock(backend, "sandbox")
    events.attach_mock(workspace, "workspace")
    events.attach_mock(model, "model")

    def propose(request, **bounds):
        assert "test_bug" in request.failing_log_tail and "AssertionError" in request.failing_log_tail
        assert request.issue_json == '{"number": 1}'
        assert bounds == dict(timeout_seconds=10, remaining_tokens=20, remaining_calls=2,
                              remaining_attempts=2, max_patch_bytes=16384)
        now[0] = 1
        return ModelReply(PATCH, 7, "mock-provider", "mock-model")

    model.propose.side_effect = propose
    result = CoderLoop(2, 20, model=model, timeout_seconds=20).run(
        {"number": 1}, workspace, sandbox, plan=PLAN,
    )
    assert result.success and result.state == LoopState.SUCCEEDED
    assert result.failure_kind is None and result.diff == PATCH
    assert result.attempts_used == result.model_calls == 1
    assert result.tokens_used == result.known_tokens_used == 7
    assert [(r.provider, r.model, r.tokens_used) for r in result.model_receipts] == [
        ("mock-provider", "mock-model", 7),
    ]
    assert result.cleanup_confirmed and sandbox.state == "closed" and result.elapsed_seconds == 1
    assert result.states[-3:] == [LoopState.EXPORTING, LoopState.CLEANING_UP, LoopState.SUCCEEDED]
    assert [c.command for c in result.checks] == [item[0] for item in script]
    assert [c.assessment for c in result.checks] == [item[2] for item in script]
    assert [c.phase for c in result.checks] == [
        LoopState.BASELINE, LoopState.BASELINE, LoopState.REPRODUCING,
        LoopState.VERIFYING_REPRODUCTION, LoopState.VERIFYING_SUITE, LoopState.VERIFYING_SUITE,
    ]
    names = [event[0] for event in events.mock_calls]
    assert names[:2] == ["sandbox.create", "sandbox.start"]
    assert names.index("model.propose") > names.index("workspace.assess")
    assert names.index("workspace.apply_patch") > names.index("model.propose")
    assert names[-2:] == ["workspace.diff", "sandbox.remove"]
    backend.remove.assert_called_once_with(sandbox.attempt_id, timeout_seconds=1)
    for operation in workspace.mock_calls:
        assert operation.args[0] == sandbox.attempt_id
        assert 0 < operation.kwargs["timeout_seconds"] <= 10
    # Old constructor positions remain usable, without dispatch or invented evidence.
    legacy = AttemptResult(False, 1, "tests_failed", "tail", "diff", ["note"])
    assert legacy.test_log_tail == "tail" and legacy.notes == ["note"]

    # Passing verification is insufficient when removal has no confirmed receipt.
    _, broken, ws, mdl, sb = _rig(monkeypatch, script)
    broken.remove.side_effect = OSError("removal receipt lost")
    failed = CoderLoop(1, 20, model=mdl).run({}, ws, sb, plan=PLAN)
    assert not failed.success and failed.state == LoopState.CLEANUP_FAILED
    assert not failed.cleanup_confirmed and sb.state == "cleanup_failed"
    assert len(failed.checks) == 6 and failed.diff == PATCH
    broken.remove.assert_called_once()


def test_reproduction_and_environment_reject_without_model_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = [("suite-a", 0, PASS, "ok"), ("suite-b", 0, PASS, "ok")]
    # An already passing reproduction is not a reproduced assertion failure.
    _, backend, workspace, model, sandbox = _rig(monkeypatch, baseline + [("reproduce", 0, PASS, "ok")])
    result = CoderLoop(2, 20, model=model).run({}, workspace, sandbox, plan=PLAN)
    assert result.state == LoopState.NOT_REPRODUCED and result.failure_kind == "not_reproduced"
    assert not result.success and result.attempts_used == result.model_calls == result.tokens_used == 0
    assert result.cleanup_confirmed
    model.propose.assert_not_called()
    workspace.context.assert_not_called()
    workspace.apply_patch.assert_not_called()
    backend.remove.assert_called_once()

    # A nonzero runner exit with no assertion evidence cannot authorize a patch.
    _, backend, workspace, model, sandbox = _rig(monkeypatch, baseline + [
        ("reproduce", 2, TestAssessment(TestOutcome.INFRA_ERROR, detail="collection/import error"), "ImportError"),
    ])
    result = CoderLoop(2, 20, model=model).run({}, workspace, sandbox, plan=PLAN)
    assert result.state == LoopState.ENVIRONMENT_REJECTED and not result.success
    assert result.checks[-1].assessment.outcome == TestOutcome.INFRA_ERROR
    assert "ImportError" in result.test_log_tail and result.cleanup_confirmed
    model.propose.assert_not_called()
    workspace.apply_patch.assert_not_called()
    backend.remove.assert_called_once()

    # Baseline red stops before reproduction as well as before any model call.
    _, backend, workspace, model, sandbox = _rig(monkeypatch, [("suite-a", 1, FAIL, "baseline red")])
    result = CoderLoop(2, 20, model=model).run({}, workspace, sandbox, plan=PLAN)
    assert result.state == LoopState.ENVIRONMENT_REJECTED and len(result.checks) == 1
    assert result.cleanup_confirmed and result.attempts_used == 0
    model.propose.assert_not_called()
    workspace.apply_patch.assert_not_called()
    backend.execute.assert_called_once()

    # Legacy unconfigured construction refuses before touching even a fresh sandbox.
    _, backend, workspace, _, sandbox = _rig(monkeypatch, [])
    result = CoderLoop(2, 20).run({}, workspace, sandbox)
    assert result.state == LoopState.UNCONFIGURED and not result.cleanup_confirmed
    assert backend.mock_calls == workspace.mock_calls == [] and sandbox.state == "new"
    with pytest.raises(ValueError):
        CoderLoop(True, 20)
    with pytest.raises(ValueError):
        CoderLoop(2, 20, timeout_seconds=float("inf"))
    with pytest.raises(ValueError):
        TestPlan(TestCommand("reproduce", 1), ())


def test_bounded_retry_feedback_and_unknown_or_excess_usage_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = [("suite-a", 0, PASS, "ok"), ("suite-b", 0, PASS, "ok"),
               ("reproduce", 1, FAIL, "initial assertion")]
    retry = [("reproduce", 1, FAIL, "x" * 256 + "retry assertion tail"),
             ("suite-a", 0, PASS, "ok"), ("suite-b", 1, FAIL, "suite regression")]
    now, backend, workspace, model, sandbox = _rig(monkeypatch, initial + retry)
    workspace.context.return_value = "bounded source"

    def first_reply(request, **bounds):
        assert bounds["remaining_tokens"] == 20 and bounds["remaining_attempts"] == 3
        now[0] += 1
        return ModelReply(PATCH, 7, "mock-provider", "mock-model")

    def unknown_reply(request, **bounds):
        assert bounds["remaining_tokens"] == 13
        assert bounds["remaining_attempts"] == 2 and bounds["remaining_calls"] == 1
        assert bounds["timeout_seconds"] == 9
        assert len(request.failing_log_tail.encode("utf-8")) <= 128
        assert "retry assertion tail" in request.failing_log_tail and "suite regression" in request.failing_log_tail
        assert "initial assertion" not in request.failing_log_tail
        return ModelReply(PATCH, None, "mock-provider", "mock-model")

    model.propose.side_effect = [first_reply, unknown_reply]  # dispatch callables explicitly below
    replies = iter((first_reply, unknown_reply))
    model.propose.side_effect = lambda request, **bounds: next(replies)(request, **bounds)
    result = CoderLoop(3, 20, model=model, max_model_calls=2, max_text_bytes=128).run(
        {}, workspace, sandbox, plan=PLAN,
    )
    assert not result.success and result.state == LoopState.UNKNOWN_OUTCOME
    assert result.attempts_used == result.model_calls == 2
    assert result.tokens_used is None and result.known_tokens_used == 7
    assert len(result.model_receipts) == 1 and len(result.checks) == 6
    assert result.cleanup_confirmed and len(result.test_log_tail.encode("utf-8")) <= 128
    workspace.apply_patch.assert_called_once()
    workspace.diff.assert_not_called()
    backend.remove.assert_called_once()

    # Known over-budget usage is recorded honestly and rejected before application.
    _, backend, workspace, model, sandbox = _rig(monkeypatch, initial)
    model.propose.return_value = ModelReply(PATCH, 21, "mock-provider", "mock-model")
    result = CoderLoop(3, 20, model=model).run({}, workspace, sandbox, plan=PLAN)
    assert result.state == LoopState.BUDGET_EXHAUSTED and not result.success
    assert result.tokens_used == result.known_tokens_used == 21 and result.model_calls == 1
    assert result.cleanup_confirmed
    workspace.apply_patch.assert_not_called()
    model.propose.assert_called_once()
    backend.remove.assert_called_once()

    # A failed verification can consume the final permitted call, never a third.
    _, backend, workspace, model, sandbox = _rig(monkeypatch, initial + retry)
    result = CoderLoop(3, 20, model=model, max_model_calls=1).run({}, workspace, sandbox, plan=PLAN)
    assert result.state == LoopState.BUDGET_EXHAUSTED and result.model_calls == 1
    assert result.tokens_used == 7 and result.cleanup_confirmed
    assert "suite regression" in result.test_log_tail
    model.propose.assert_called_once()
    workspace.apply_patch.assert_called_once()
    backend.remove.assert_called_once()

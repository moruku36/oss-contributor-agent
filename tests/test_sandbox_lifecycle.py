"""Exactly three mock-only lifecycle cases; no Docker, shell, network or model.

Failure branches are explicit synthetic attempts within the cleanup case, not
parametrized cases or wrappers around any historical tests.
"""

from dataclasses import FrozenInstanceError
from unittest.mock import Mock, call

import pytest

from src.sandbox import docker_manager as manager
from src.sandbox.docker_manager import (
    CommandResult,
    Sandbox,
    SandboxBackend,
    SandboxBackendNotConfigured,
    SandboxCleanupError,
    SandboxError,
)


def _backend() -> Mock:
    backend = Mock(spec=SandboxBackend)
    backend.create.return_value = None
    backend.start.return_value = None
    backend.remove.return_value = None
    backend.execute.return_value = CommandResult("test", 0, "ok", duration_seconds=0.25)
    return backend


def test_normal_lifecycle_and_resource_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(manager, "monotonic", lambda: now[0])
    backend = _backend()
    sandbox = Sandbox(
        "fixture:local", 1.5, 512, backend=backend,
        attempt_timeout_seconds=10, operation_timeout_seconds=2,
        max_commands=2, max_command_bytes=64, max_output_bytes=8,
    )
    backend.execute.side_effect = [
        CommandResult("reproduce", 1, "fail", duration_seconds=0.5),
        CommandResult("verify", 0, "ok", duration_seconds=0.5),
    ]
    with sandbox as active:
        assert active is sandbox and sandbox.state == "running"
        assert (sandbox.image, sandbox.cpu_limit, sandbox.memory_limit_mb) == (
            "fixture:local", 1.5, 512,
        )
        assert sandbox.policy.network == "none"
        assert sandbox.policy.user == "65534:65534"
        assert sandbox.policy.read_only_root is True
        assert sandbox.policy.attempt_timeout_seconds == 10
        assert sandbox.policy.max_commands == 2
        assert sandbox.policy.max_command_bytes == 64
        with pytest.raises(FrozenInstanceError):
            sandbox.policy.cpu_limit = 99  # type: ignore[misc]
        assert active.run("reproduce", 1).exit_code == 1
        now[0] = 9.0
        assert active.run("verify", 20).exit_code == 0
    key = sandbox.attempt_id
    assert backend.mock_calls == [
        call.create(key, policy=sandbox.policy, timeout_seconds=2),
        call.start(key, timeout_seconds=2),
        call.execute(key, "reproduce", timeout_seconds=1, max_output_bytes=8),
        call.execute(key, "verify", timeout_seconds=1, max_output_bytes=8),
        call.remove(key, timeout_seconds=2),
    ]
    assert sandbox.state == "closed"
    with pytest.raises(SandboxError, match="running"):
        sandbox.run("test", 1)
    with pytest.raises(SandboxError, match="single-use"):
        sandbox.__enter__()
    assert len(backend.mock_calls) == 5
    assert Sandbox("fixture:local", 1, 64).attempt_id != key


def test_failure_cleanup_is_terminal_and_preserves_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager, "monotonic", lambda: 0.0)

    # No creation receipt: cleanup still addresses the preselected attempt key.
    uncertain = _backend()
    create_error = OSError("create receipt lost after allocation")
    uncertain.create.side_effect = create_error
    creation = Sandbox("fixture:local", 1, 64, backend=uncertain)
    with pytest.raises(OSError) as caught:
        creation.__enter__()
    assert caught.value is create_error
    assert uncertain.mock_calls == [
        call.create(creation.attempt_id, policy=creation.policy, timeout_seconds=30),
        call.remove(creation.attempt_id, timeout_seconds=30),
    ]
    with pytest.raises(SandboxError, match="single-use"):
        creation.__enter__()
    assert creation.state == "failed" and len(uncertain.mock_calls) == 2

    partial = _backend()
    start_error = RuntimeError("start partially completed")
    partial.start.side_effect = start_error
    starting = Sandbox("fixture:local", 1, 64, backend=partial)
    with pytest.raises(RuntimeError) as caught:
        starting.__enter__()
    assert caught.value is start_error
    assert [event[0] for event in partial.mock_calls] == ["create", "start", "remove"]
    partial.remove.assert_called_once_with(starting.attempt_id, timeout_seconds=30)
    assert starting.state == "failed"

    timed = _backend()
    timeout = TimeoutError("execute deadline")
    timed.execute.side_effect = timeout
    timing = Sandbox("fixture:local", 1, 64, backend=timed)
    with pytest.raises(TimeoutError) as caught:
        with timing:
            timing.run("test", 1)
    assert caught.value is timeout and timing.state == "failed"
    timed.remove.assert_called_once_with(timing.attempt_id, timeout_seconds=30)
    assert [event[0] for event in timed.mock_calls] == ["create", "start", "execute", "remove"]

    broken = _backend()
    execution_error = RuntimeError("execution failed")
    removal_error = OSError("removal receipt unknown")
    broken.execute.side_effect = execution_error
    broken.remove.side_effect = removal_error
    failing = Sandbox("fixture:local", 1, 64, backend=broken)
    with pytest.raises(SandboxCleanupError) as caught:
        with failing:
            failing.run("test", 1)
    assert caught.value.original_error is execution_error
    assert caught.value.cleanup_error is removal_error
    assert caught.value.attempt_id == failing.attempt_id
    assert failing.state == "cleanup_failed"
    with pytest.raises(SandboxError):
        failing.__enter__()
    failing.__exit__(None, None, None)
    broken.remove.assert_called_once_with(failing.attempt_id, timeout_seconds=30)

    # Even a zero exit code cannot turn unconfirmed context teardown into success.
    teardown = _backend()
    teardown.remove.side_effect = removal_error
    ending = Sandbox("fixture:local", 1, 64, backend=teardown)
    with pytest.raises(SandboxCleanupError) as caught:
        with ending:
            assert ending.run("test", 1).exit_code == 0
    assert caught.value.original_error is None
    assert caught.value.cleanup_error is removal_error
    assert ending.state == "cleanup_failed"
    teardown.remove.assert_called_once()


def test_validation_and_no_dispatch_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(manager, "monotonic", lambda: now[0])
    backend = _backend()
    invalid_options = [
        {"image": ""}, {"image": " \t"}, {"image": None}, {"image": "x\0"},
        {"cpu_limit": 0}, {"cpu_limit": -1}, {"cpu_limit": True},
        {"cpu_limit": "1"}, {"cpu_limit": float("nan")}, {"cpu_limit": float("inf")},
        {"memory_limit_mb": 0}, {"memory_limit_mb": 1.5}, {"memory_limit_mb": True},
        {"attempt_timeout_seconds": float("nan")}, {"attempt_timeout_seconds": 0},
        {"operation_timeout_seconds": float("inf")}, {"operation_timeout_seconds": False},
        {"max_commands": 0}, {"max_command_bytes": -1}, {"max_output_bytes": 1.5},
    ]
    for invalid in invalid_options:
        options = dict(image="fixture:local", cpu_limit=1, memory_limit_mb=64, backend=backend)
        options.update(invalid)
        with pytest.raises(ValueError):
            Sandbox(**options)
    backend.assert_not_called()
    assert backend.mock_calls == []
    unconfigured = Sandbox("fixture:local", 1, 64)
    with pytest.raises(SandboxBackendNotConfigured, match="not configured"):
        unconfigured.__enter__()
    assert unconfigured.state == "failed"

    sandbox = Sandbox(
        "fixture:local", 1, 64, backend=backend,
        max_commands=1, max_command_bytes=4, max_output_bytes=4,
    )
    with pytest.raises(SandboxError, match="running"):
        sandbox.run("test", 1)
    assert backend.mock_calls == []
    with sandbox:
        for command in ("", " \n", "x\0", "ééé", None):
            with pytest.raises(ValueError):
                sandbox.run(command, 1)
        for timeout in (0, -1, True, "1", float("nan"), float("inf")):
            with pytest.raises(ValueError):
                sandbox.run("test", timeout)
        backend.execute.assert_not_called()
        backend.execute.return_value = CommandResult("éé", 0, "é", "é", 0.25)
        assert sandbox.run("éé", 1).stdout == "é"  # exact UTF-8 byte boundaries
        with pytest.raises(SandboxError, match="command budget"):
            sandbox.run("test", 1)
    backend.execute.assert_called_once()
    backend.remove.assert_called_once()
    assert sandbox.state == "failed"

    oversized = _backend()
    oversized.execute.return_value = CommandResult("test", 0, "éé", "x")
    capped = Sandbox("fixture:local", 1, 64, backend=oversized, max_output_bytes=4)
    with pytest.raises(SandboxError, match="max_output_bytes"):
        with capped:
            capped.run("test", 1)
    assert capped.state == "failed"
    oversized.remove.assert_called_once()

    # Elapsed-time checks also refuse late success and expiry while caller is idle.
    late = _backend()
    def late_result(*args: object, **kwargs: object) -> CommandResult:
        now[0] += 2
        return CommandResult("test", 0)
    late.execute.side_effect = late_result
    delayed = Sandbox("fixture:local", 1, 64, backend=late)
    with pytest.raises(TimeoutError):
        with delayed:
            delayed.run("test", 1)
    late.remove.assert_called_once()
    idle_backend = _backend()
    idle = Sandbox("fixture:local", 1, 64, backend=idle_backend, attempt_timeout_seconds=1)
    with pytest.raises(TimeoutError):
        with idle:
            now[0] += 1
    idle_backend.execute.assert_not_called()
    idle_backend.remove.assert_called_once()

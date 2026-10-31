"""Docker sandbox lifecycle management.

Responsibilities:
- Start a throwaway container per issue attempt, with CPU, memory
  and wall-clock limits taken from configs/agent_rules.yaml.
- Run build/test/lint commands inside the container and capture logs.
- Guarantee teardown: the container and its workspace are removed
  after every attempt, success or failure.

Security invariants (see docs/architecture.md, section 9):
- No credentials are ever mounted into the container.
- Network egress is restricted to the source clone and package
  registries; everything else is denied.
- The container runs as a non-root user with a read-only root
  filesystem where the target project allows it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommandResult:
    """Result of one command executed inside the sandbox."""

    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


class Sandbox:
    """One isolated container for one issue attempt (Phase 2)."""

    def __init__(self, image: str, cpu_limit: float, memory_limit_mb: int) -> None:
        self.image = image
        self.cpu_limit = cpu_limit
        self.memory_limit_mb = memory_limit_mb

    def __enter__(self) -> "Sandbox":
        raise NotImplementedError("Phase 2: container start")

    def __exit__(self, *exc_info: object) -> None:
        raise NotImplementedError("Phase 2: container teardown")

    def run(self, command: str, timeout_seconds: int) -> CommandResult:
        """Run ``command`` inside the container with a hard timeout."""
        raise NotImplementedError("Phase 2: command execution")

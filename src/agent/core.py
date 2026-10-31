"""The coder loop: explore -> failing test -> patch -> verify.

One run of the loop handles exactly one issue inside one sandbox:

1. Explore: locate the relevant code (search, existing tests).
2. Reproduce: write or identify a test that fails because of the bug
   (TDD). If the bug cannot be reproduced, the attempt ends here —
   fail-closed, no patch is attempted on a guess.
3. Patch: apply the smallest change that addresses the issue.
4. Verify: run the project's test suite in the sandbox.
5. Self-heal: on failure, feed the test output back to the model and
   retry, up to ``max_patch_attempts`` from configs/agent_rules.yaml.

The loop succeeds only when the *full* relevant test suite passes.
Partial success is recorded as a failure with its logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AttemptResult:
    """Outcome of one coder-loop run."""

    success: bool
    attempts_used: int
    failure_kind: str | None = None  # see failure taxonomy in architecture.md
    test_log_tail: str = ""
    diff: str = ""
    notes: list[str] = field(default_factory=list)


class CoderLoop:
    """Iterative patch loop for a single issue (implementation: Phase 2)."""

    def __init__(self, max_attempts: int, token_budget: int) -> None:
        self.max_attempts = max_attempts
        self.token_budget = token_budget

    def run(self, issue: dict, workspace: object, sandbox: object) -> AttemptResult:
        """Run the TDD patch loop for ``issue`` inside ``sandbox``."""
        raise NotImplementedError("Phase 2: coder loop")

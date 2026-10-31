"""Prompt templates for the triage and coder models.

Two families of prompts live here:

- Triage prompts: judge borderline issues (rule score between the
  skip and accept thresholds) for scope, reproducibility and risk.
- Coder prompts: per-language (Go / TypeScript / ...) instructions
  for the patch loop, always combined with the target project's
  contribution rules (CONTRIBUTING.md summary, commit convention,
  lint/format commands) loaded from configs/targets.yaml.

Every coder prompt carries the standing constraints: smallest
possible diff, no unrelated refactoring, tests first, and stop
(fail-closed) when the issue cannot be reproduced.
"""

from __future__ import annotations

# Standing constraints appended to every coder prompt.
STANDING_CONSTRAINTS = """\
Constraints:
- Make the smallest change that fixes the issue. No unrelated refactoring.
- Write or identify a failing test before changing production code.
- If you cannot reproduce the problem, stop and report that instead of guessing.
- Follow the project's CONTRIBUTING.md, linter and commit conventions.
"""

PROMPT_KINDS = ("triage", "explore", "patch", "repair")


def get_prompt(kind: str, language: str, project_rules: str) -> str:
    """Assemble a prompt of ``kind`` for ``language`` + project rules."""
    raise NotImplementedError("Phase 1/2: prompt assembly")

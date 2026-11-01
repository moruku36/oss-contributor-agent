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
    raise NotImplementedError("Phase 2: coder prompt assembly")


def triage_prompt(
    *,
    repo: str,
    title: str,
    body: str,
    score: float,
    reasons: list[str],
    project_notes: str = "",
) -> str:
    """Prompt for the LLM triage stage (borderline band only).

    The model must answer with a single JSON object:
    ``{"accept": true|false, "rationale": "..."}``. Anything else is
    treated as no verdict at all (fail-closed) by the caller.
    """
    body_excerpt = (body or "")[:4000]
    reasons_text = "\n".join(f"- {reason}" for reason in reasons) or "- (none)"
    notes = project_notes.strip()
    notes_block = f"\nProject notes:\n{notes}\n" if notes else ""
    return f"""\
You are the triage stage of an autonomous OSS contributor agent.
Decide whether the issue below is a suitable candidate for a small,
fully test-verified automated fix attempt, or should be skipped.

Repository: {repo}
Issue title: {title}
Rule-based score: {score:.2f} (borderline band)
Rule score reasons:
{reasons_text}
{notes_block}
Issue body:
\"\"\"
{body_excerpt}
\"\"\"

Accept only if the issue is narrow in scope, describes a concrete
problem or a small well-specified change, and looks reproducible or
verifiable by tests. Reject broad features, design discussions,
vague reports, and anything needing maintainer decisions first.

Respond with exactly one JSON object and nothing else:
{{"accept": true or false, "rationale": "one or two sentences"}}
"""

"""Compliance guard: verify a patch obeys the project's rules.

Checks, all of which must pass before a review report is produced:

- The project's lint and format commands exit cleanly.
- Commit messages follow the project's convention
  (e.g. Conventional Commits) from configs/targets.yaml.
- A DCO ``Signed-off-by`` line is present when the target requires it.
- The diff touches only files relevant to the issue (no drive-by
  changes, no edits to CI or license files).

Any failed check fails the whole attempt (fail-closed): the patch is
discarded or returned to the coder loop, never shipped as-is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass
class ComplianceResult:
    """Outcome of the compliance checks for one patch."""

    passed: bool
    failures: list[str] = field(default_factory=list)


def check_commit_message(message: str, convention: str, sign_off_required: bool) -> list[str]:
    """Pure syntax check, not DCO identity, CLA or project-policy approval.

    Only conventional-commits is supported. Required sign-offs must be in a
    final blank-line-separated trailer block. Body examples do not count.
    """
    if type(convention) is not str or convention != "conventional-commits":
        return ["unsupported or unverified commit convention"]
    if type(sign_off_required) is not bool:
        return ["sign-off policy must be an explicit boolean"]
    if type(message) is not str:
        return ["commit message must be text"]
    if not 1 <= len(message) <= 8192:
        return ["invalid or oversized commit message"]
    try:
        size = len(message.encode("utf-8"))
    except UnicodeError:
        return ["commit message must be valid UTF-8"]
    if (not 1 <= size <= 8192
            or any(ord(c) < 32 and c not in "\n\r\t" for c in message)
            or "\x7f" in message):
        return ["invalid or oversized commit message"]
    normalized = message.replace("\r\n", "\n")
    if "\r" in normalized:
        return ["invalid commit message line endings"]
    lines = normalized.rstrip("\n").split("\n")
    subject = lines[0]
    failures = []
    # Conservative subset; custom lowercase types are supported.
    if (len(subject.encode("utf-8")) > 256
            or not re.fullmatch(
                r"[a-z]+(?:\([A-Za-z0-9_./-]+\))?!?: [^\s].*", subject)
            or subject != subject.rstrip()
            or "\t" in subject):
        failures.append("invalid Conventional Commits subject")
    if len(lines) > 1 and lines[1] != "":
        failures.append("commit body must be separated by a blank line")
    if sign_off_required:
        start = len(lines) - 1
        while start > 0 and lines[start - 1] != "":
            start -= 1
        trailers = lines[start:]
        trailer_line = r"[A-Za-z][A-Za-z0-9-]*: [^\s].*"
        signoff_line = (
            r"Signed-off-by: [^<>\s]+(?: [^<>\s]+)* "
            r"<[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+@"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+>"
        )
        if (start < 2
                or not all(re.fullmatch(trailer_line, line) for line in trailers)
                or not any(re.fullmatch(signoff_line, line) for line in trailers)):
            failures.append("required final DCO Signed-off-by trailer is missing or malformed")
    return failures


def run_compliance(workspace: object, target: dict, commit_messages: list[str]) -> ComplianceResult:
    """Run lint/format/convention checks for a finished patch."""
    raise NotImplementedError("Phase 3: compliance guard")

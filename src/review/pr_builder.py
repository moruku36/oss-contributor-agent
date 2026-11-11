"""Pure bounded review artifacts; no execution, approval or submission authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import html
import json
from math import isfinite
import re

from src.agent.core import (
    AttemptResult, CheckEvidence, LoopState, ModelReceipt, TestAssessment,
    TestOutcome, TestPlan,
)

AI_DISCLOSURE_LINE = (
    "Disclosure: prepared with AI assistance. Local draft only; not submitted. "
    "Human review, approval and project-compliance review are pending. "
    "Execution provenance is caller-declared; real isolation is not verified. "
    "Report generation does not grant approval."
)
MAX_DIFF_BYTES = 131072
MAX_SNAPSHOT_BYTES = 524288
MAX_RENDER_BYTES = 1048576


def _text(value: object, limit: int, *, required: bool = False) -> str:
    if type(value) is not str:
        raise ValueError("expected text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("invalid UTF-8 text") from exc
    if (size > limit or (required and not value.strip())
            or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
            or "\x7f" in value):
        raise ValueError("invalid or oversized text")
    return value


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ValueError("invalid integer")
    return value


def _number(value: object, *, positive: bool = False) -> None:
    try:
        valid = (type(value) in (int, float) and isfinite(value)
                 and (value > 0 if positive else value >= 0))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid finite resource value")


def _items(value: object, limit: int) -> list:
    if type(value) is not list or len(value) > limit:
        raise ValueError("invalid or oversized collection")
    return value


def _canonical(data: dict) -> str:
    result = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                        allow_nan=False)
    _text(result, MAX_SNAPSHOT_BYTES)
    return result


def _validate(data: dict) -> None:
    """Validate detached evidence, including each complete verification round."""
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported schema")
    repo = _text(data["repo"], 201, required=True)
    segment = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?"
    if not re.fullmatch(segment + "/" + segment, repo):
        raise ValueError("invalid repository identity")
    _integer(data["issue_number"], 1)
    _text(data["issue_title"], 1024, required=True)
    if not re.fullmatch(r"[0-9a-f]{40}", _text(data["base_commit"], 40)):
        raise ValueError("expected explicit lowercase base SHA")
    if data["execution_kind"] not in ("synthetic", "unverified"):
        raise ValueError("execution provenance cannot claim isolation")
    plan = data["plan"]
    commands = [plan["reproduction"]] + _items(plan["relevant_suite"], 8)
    if len(commands) < 2:
        raise ValueError("explicit suite required")
    for cmd in commands:
        _text(cmd["command"], 4096, required=True)
        _number(cmd["timeout_seconds"], positive=True)
    suite = [cmd["command"] for cmd in commands[1:]]
    if len(set(suite)) != len(suite):
        raise ValueError("duplicate suite command")
    a = data["attempt"]
    if type(a["success"]) is not bool or type(a["cleanup_confirmed"]) is not bool:
        raise ValueError("invalid boolean")
    state = LoopState(a["state"])
    states = _items(a["states"], 512)
    for value in states:
        LoopState(value)
    if states and states[-1] != state:
        raise ValueError("terminal state history mismatch")
    for name in ("attempts_used", "model_calls", "known_tokens_used"):
        _integer(a[name])
    if a["tokens_used"] is not None:
        _integer(a["tokens_used"])
    _number(a["elapsed_seconds"])
    _text(a["sandbox_attempt_id"], 256)
    if a["failure_kind"] is not None:
        _text(a["failure_kind"], 1024, required=True)
    for note in _items(a["notes"], 128):
        _text(note, 4096)
    _text(a["test_log_tail"], 16384)
    diff = _text(a["diff"], MAX_DIFF_BYTES)
    if (data["diff_sha256"] != hashlib.sha256(diff.encode("utf-8")).hexdigest()
            or data["diff_bytes"] != len(diff.encode("utf-8"))):
        raise ValueError("diff integrity mismatch")
    receipts = _items(a["model_receipts"], 128)
    for receipt in receipts:
        _text(receipt["provider"], 256, required=True)
        _text(receipt["model"], 256, required=True)
        _integer(receipt["tokens_used"])
    total = sum(r["tokens_used"] for r in receipts)
    if a["model_calls"] < len(receipts) or a["known_tokens_used"] < total:
        raise ValueError("model receipt count/known usage mismatch")
    if a["tokens_used"] is not None and a["tokens_used"] != a["known_tokens_used"]:
        raise ValueError("aggregate usage mismatch")
    checks = _items(a["checks"], 256)
    for c in checks:
        LoopState(c["phase"])
        _text(c["command"], 4096, required=True)
        _integer(c["exit_code"], -(2**31))
        _number(c["duration_seconds"])
        _text(c["log_tail"], 16384)
        assessment = c["assessment"]
        outcome = TestOutcome(assessment["outcome"])
        count = _integer(assessment["tests_run"])
        failures = _integer(assessment["assertion_failures"])
        _text(assessment["detail"], 4096)
        if failures > count:
            raise ValueError("impossible assertion counts")
        if outcome == TestOutcome.PASS and (c["exit_code"] != 0 or not count or failures):
            raise ValueError("contradictory PASS")
        if outcome == TestOutcome.ASSERTION_FAILED and (
            c["exit_code"] <= 0 or not failures or not assessment["detail"].strip()
        ):
            raise ValueError("missing assertion failure evidence")
    if not a["success"]:
        if state == LoopState.SUCCEEDED:
            raise ValueError("false success/state mismatch")
        return
    if (state != LoopState.SUCCEEDED or not states or a["failure_kind"] is not None
            or not a["cleanup_confirmed"] or not diff.strip()
            or not a["sandbox_attempt_id"].strip()
            or not 1 <= a["attempts_used"] <= a["model_calls"]
            or a["model_calls"] != len(receipts) or a["tokens_used"] is None
            or a["known_tokens_used"] != total or a["attempts_used"] != a["model_calls"]):
        raise ValueError("incomplete successful evidence")

    def match(index: int, phase: LoopState, command: str, outcome: TestOutcome) -> None:
        if index >= len(checks):
            raise ValueError("missing required check")
        c = checks[index]
        if (c["phase"], c["command"], c["assessment"]["outcome"]) != (phase, command, outcome):
            raise ValueError("check order/command/outcome mismatch")

    for i, command in enumerate(suite):
        match(i, LoopState.BASELINE, command, TestOutcome.PASS)
    i = len(suite)
    repro = plan["reproduction"]["command"]
    match(i, LoopState.REPRODUCING, repro, TestOutcome.ASSERTION_FAILED)
    i += 1
    rounds = 0
    final_pass = False
    while i < len(checks):
        # CoderLoop records reproduction and ALL suite commands each round,
        # even if an earlier command has an assertion failure.
        c = checks[i]
        outcome = c["assessment"]["outcome"]
        if outcome not in (TestOutcome.PASS, TestOutcome.ASSERTION_FAILED):
            raise ValueError("unknown verification cannot precede success")
        match(i, LoopState.VERIFYING_REPRODUCTION, repro, outcome)
        i += 1
        rounds += 1
        final_pass = outcome == TestOutcome.PASS
        for command in suite:
            if i >= len(checks):
                raise ValueError("incomplete verification suite")
            outcome = checks[i]["assessment"]["outcome"]
            if outcome not in (TestOutcome.PASS, TestOutcome.ASSERTION_FAILED):
                raise ValueError("invalid verification outcome")
            match(i, LoopState.VERIFYING_SUITE, command, outcome)
            i += 1
            if outcome != TestOutcome.PASS:
                final_pass = False
    if not final_pass or rounds != a["attempts_used"]:
        raise ValueError("missing successful final round or attempt count mismatch")


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """Immutable canonical JSON is the single detached source of report data."""

    snapshot_json: str

    @property
    def evidence(self) -> dict:
        return _report_data(self)

    @property
    def repo(self) -> str:
        return self.evidence["repo"]

    @property
    def issue_number(self) -> int:
        return self.evidence["issue_number"]

    @property
    def summary(self) -> str:
        return self.evidence["issue_title"]

    @property
    def tests_run(self) -> tuple[str, ...]:
        return tuple(c["command"] for c in self.evidence["attempt"]["checks"])

    @property
    def diff_preview(self) -> str:
        return self.evidence["attempt"]["diff"]

    @property
    def status(self) -> str:
        a = self.evidence["attempt"]
        return "success" if a["success"] else (
            "unknown" if a["state"] == LoopState.UNKNOWN_OUTCOME else "failed"
        )

    @property
    def eligible_for_pr(self) -> bool:
        return self.evidence["attempt"]["success"]

    def to_markdown(self) -> str:
        return _render(self.evidence)


def _report_data(report: ReviewReport) -> dict:
    if type(report) is not ReviewReport:
        raise ValueError("expected ReviewReport")
    _text(report.snapshot_json, MAX_SNAPSHOT_BYTES)
    try:
        data = json.loads(report.snapshot_json)
        _validate(data)
        if _canonical(data) != report.snapshot_json:
            raise ValueError("noncanonical evidence")
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise ValueError("malformed report") from exc
    return data


def build_review_report(
    attempt: AttemptResult, issue: dict, *, base_commit: str, plan: TestPlan,
    execution_kind: str = "unverified",
) -> ReviewReport:
    if type(attempt) is not AttemptResult or type(plan) is not TestPlan or type(issue) is not dict:
        raise ValueError("expected actual attempt, issue and explicit test plan")
    # Bound collections before recursive dataclass copying.
    for values, limit in ((attempt.checks, 256), (attempt.notes, 128),
                          (attempt.states, 512), (attempt.model_receipts, 128)):
        _items(values, limit)
    if (not all(type(c) is CheckEvidence and type(c.assessment) is TestAssessment
                and type(c.phase) is LoopState and type(c.assessment.outcome) is TestOutcome
                for c in attempt.checks)
            or not all(type(r) is ModelReceipt for r in attempt.model_receipts)
            or type(attempt.state) is not LoopState
            or not all(type(s) is LoopState for s in attempt.states)):
        raise ValueError("invalid evidence types")
    _text(attempt.diff, MAX_DIFF_BYTES)
    data = {
        "schema_version": 1, "repo": issue.get("repo"),
        "issue_number": issue.get("number"), "issue_title": issue.get("title"),
        "base_commit": base_commit, "execution_kind": execution_kind,
        "plan": asdict(plan), "attempt": asdict(attempt),
        "diff_sha256": hashlib.sha256(attempt.diff.encode("utf-8")).hexdigest(),
        "diff_bytes": len(attempt.diff.encode("utf-8")),
    }
    _validate(data)
    return ReviewReport(_canonical(data))


def _literal(value: str) -> str:
    # Escaping HTML plus a longer fence keeps all untrusted content inert.
    escaped = html.escape(value, quote=False)
    fence = "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", escaped)), default=0))
    return fence + "\n" + escaped + "\n" + fence


def _render(data: dict, template: str | None = None) -> str:
    a = data["attempt"]
    status = "SUCCESS (caller-declared)" if a["success"] else "FAILED / UNKNOWN — not PR eligible"
    body = (
        "# Local review artifact\n\n" + AI_DISCLOSURE_LINE
        + "\n\n## Status\n\n" + status
        + "\n\nExecution kind: " + data["execution_kind"]
        + "\n\nCaptured log tails are not complete logs. CPU/memory usage is not measured."
        + "\n\n## Issue\n\n" + _literal(data["issue_title"])
        + "\n\nIssue: https://github.com/" + data["repo"] + "/issues/" + str(data["issue_number"])
        + "\n\nBase SHA (caller-declared): " + data["base_commit"]
        + "\n\n## Resource summary\n\n"
        + _literal(json.dumps({key: a[key] for key in (
            "attempts_used", "model_calls", "tokens_used", "known_tokens_used",
            "elapsed_seconds", "cleanup_confirmed")}, sort_keys=True))
        + "\n\n## Ordered check summary (captured evidence)\n\n"
        + _literal("\n".join(
            c["phase"] + " | " + c["command"] + " | " + c["assessment"]["outcome"]
            + " | tests=" + str(c["assessment"]["tests_run"])
            for c in a["checks"]))
        + "\n\n## Complete evidence (canonical JSON; literal data)\n\n"
        + _literal(_canonical(data))
    )
    if template is not None:
        body += "\n\n## Untrusted PR template (quoted reference)\n\n" + _literal(template)
    body += "\n"
    _text(body, MAX_RENDER_BYTES)
    return body


def build_pr_body(report: ReviewReport, pr_template: str | None = None) -> str:
    data = _report_data(report)
    if not data["attempt"]["success"]:
        raise ValueError("failed/unknown report cannot generate a contribution body")
    if pr_template is not None:
        _text(pr_template, 32768)
    return _render(data, pr_template)


def submit_draft_pr(report: ReviewReport, body: str) -> str:
    """Submission remains unimplemented; generation is not approval."""
    raise NotImplementedError("Phase 3: approval-bound draft PR submission")

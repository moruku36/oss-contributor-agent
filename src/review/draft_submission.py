"""Injection-only preparation and single-use dispatch; no approval credentials.

Consistency checks do not authenticate evidence. Applications must inject an
independently trusted authority and transport; see docs/draft-submission.md.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import re
from threading import Lock
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from src.review.submission_journal import SQLiteSubmissionJournal

from src.agent.core import TestCommand, TestPlan
from src.review.pr_builder import (
    MAX_RENDER_BYTES, MAX_SNAPSHOT_BYTES, ReviewReport, _report_data, _text,
    build_pr_body, build_review_report,
)

MAX_REQUEST_BYTES = 4_194_304
_SEG = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?"


def _bounded(value: object, limit: int) -> str:
    if type(value) is not str or len(value) > limit:
        raise ValueError("expected bounded text")
    return _text(value, limit, required=True)


@dataclass(frozen=True, slots=True)
class DraftTarget:
    destination: str  # currently the literal HTTPS origin https://github.com
    repository: str
    head_repository: str
    head_ref: str  # explicit refs/heads/...; no implicit current branch
    head_commit: str
    base_ref: str
    base_commit: str


def _target(data: object) -> dict:
    names = tuple(DraftTarget.__dataclass_fields__)
    if type(data) is not dict or set(data) != set(names):
        raise ValueError("invalid target fields")
    if _bounded(data["destination"], 256) != "https://github.com":
        raise ValueError("unsupported explicit destination")
    for name in ("repository", "head_repository"):
        if not re.fullmatch(_SEG + "/" + _SEG, _bounded(data[name], 201)):
            raise ValueError("invalid repository")
    for name in ("head_commit", "base_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", _bounded(data[name], 40)):
            raise ValueError("explicit lowercase commit SHA required")
    for name in ("head_ref", "base_ref"):
        ref = _bounded(data[name], 240)
        if (not re.fullmatch(r"refs/heads/[A-Za-z0-9_/-][A-Za-z0-9_./-]*", ref)
                or ".." in ref or any(not part or part.startswith(".")
                    or part.endswith((".", ".lock")) for part in ref.split("/"))):
            raise ValueError("unsupported full branch ref")
    return data


def _canonical(data: dict) -> str:
    result = json.dumps(data, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False)
    return _bounded(result, MAX_REQUEST_BYTES)


@dataclass(frozen=True, slots=True)
class DraftRequest:
    """One canonical string is the source; data returns a detached copy.

    The public digest binds content. It is never an approval credential.
    """

    snapshot_json: str

    @property
    def data(self) -> dict:
        return _request_data(self.snapshot_json)

    @property
    def binding_sha256(self) -> str:
        return sha256(_bounded(self.snapshot_json, MAX_REQUEST_BYTES).encode("utf-8")).hexdigest()


def _request_data(snapshot: str) -> dict:
    _bounded(snapshot, MAX_REQUEST_BYTES)
    try:
        data = json.loads(snapshot)
        if type(data) is not dict or set(data) != {
            "schema_version", "report_json", "body", "title", "target", "draft", "pr_template",
        }:
            raise ValueError("invalid request fields")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1 or data["draft"] is not True:
            raise ValueError("only schema 1 draft requests are supported")
        title = _bounded(data["title"], 256)
        if any(c in title for c in "\n\r\t"):
            raise ValueError("single-line title required")
        report = ReviewReport(_bounded(data["report_json"], MAX_SNAPSHOT_BYTES))
        evidence = _report_data(report)
        target = _target(data["target"])
        if (target["repository"] != evidence["repo"]
                or target["base_commit"] != evidence["base_commit"]):
            raise ValueError("report/target/base mismatch")
        if _bounded(data["body"], MAX_RENDER_BYTES) != build_pr_body(report, data["pr_template"]):
            raise ValueError("body does not match the exact report/template")
        if _canonical(data) != snapshot:
            raise ValueError("noncanonical request")
        return data
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise ValueError("malformed draft request") from exc


def _source_report(source: object) -> ReviewReport:
    from src.agent.candidate_attempt import CandidateAttemptOutcome

    if type(source) is ReviewReport:
        return ReviewReport(source.snapshot_json)
    if type(source) is not CandidateAttemptOutcome:
        raise ValueError("expected CandidateAttemptOutcome or ReviewReport")
    if (type(source.status) is not str or source.status != "pending_human_review"
            or type(source.human_approval) is not str or source.human_approval != "pending"
            or source.submitted is not False or type(source.report) is not ReviewReport):
        raise ValueError("candidate is not pending human review")
    report = ReviewReport(source.report.snapshot_json)
    data = _report_data(report)
    plan = data["plan"]
    rebuilt = build_review_report(
        copy.deepcopy(source.attempt),
        {"repo": data["repo"], "number": data["issue_number"], "title": data["issue_title"]},
        base_commit=data["base_commit"], execution_kind=data["execution_kind"],
        plan=TestPlan(TestCommand(**plan["reproduction"]),
                      tuple(TestCommand(**c) for c in plan["relevant_suite"])),
    )
    if rebuilt.snapshot_json != report.snapshot_json:
        raise ValueError("candidate attempt/report mismatch")
    return report


def prepare_draft_submission(
    source: object, *, target: DraftTarget, title: str, pr_template: str | None = None,
) -> DraftRequest:
    """Detach the exact proposal, with no approval, I/O or external calls."""
    report = _source_report(source)
    _bounded(title, 256)
    if type(target) is not DraftTarget:
        raise ValueError("explicit DraftTarget required")
    target_data = _target({name: getattr(target, name) for name in DraftTarget.__dataclass_fields__})
    snapshot = _canonical({
        "schema_version": 1, "report_json": report.snapshot_json,
        "body": build_pr_body(report, pr_template), "title": title,
        "target": target_data, "draft": True, "pr_template": pr_template,
    })
    _request_data(snapshot)
    return DraftRequest(snapshot)


class ApprovalDecision(Enum):
    AUTHORIZED = "authorized"
    DENIED = "denied"


class HumanApprovalAuthority(Protocol):
    """Trusted external authority, not a caller-supplied bearer-token checker.

    Independently authenticate human approval, authentic evidence, compliance,
    upstream identity and head/base contents. Verify the exact entire request,
    then atomically persist/consume authorization once BEFORE AUTHORIZED. Deny
    replays across objects/processes/restarts. Never authorize on a public hash
    or caller assertion alone. Exceptions/ambiguity require reconciliation.
    """

    def authorize_and_consume(self, request: DraftRequest) -> ApprovalDecision: ...


class SubmissionStatus(Enum):
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DraftTransportReply:
    status: SubmissionStatus
    request_sha256: str
    draft: bool
    pull_request_number: int | None = None


class DraftOnlyTransport(Protocol):
    """Trusted dispatch of exactly this request, once, as a DRAFT only.

    No retries, fallback, non-draft creation, push, promotion or other mutation.
    Independently verify destination/repository and exact head/base identities
    (including ref movement) before dispatch; unsupported atomic preconditions
    must reject. SUBMITTED confirms the exact draft and positive PR number;
    REJECTED confirms no creation. Ambiguity/partial effects must be UNKNOWN or
    raise. Enforce bounds/deadlines in flight and retain reconciliation evidence.
    Echoed hashes are consistency checks, not independent upstream verification.
    """

    def submit_draft(self, request: DraftRequest) -> DraftTransportReply: ...


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    status: SubmissionStatus
    request_sha256: str
    dispatched: bool  # entered transport; does not prove a network dispatch
    reason: str
    pull_request_number: int | None = None


class DraftSubmission:
    """Single-use locally, with an optional trusted durable consumption journal.

    Each boundary receives a separate frozen value from one captured string,
    never a shared mutable dictionary or request. A lock also rejects reentry.
    """

    def __init__(
        self, request: DraftRequest, *, authority: HumanApprovalAuthority,
        transport: DraftOnlyTransport,
        journal: SQLiteSubmissionJournal | None = None, operation_id: str | None = None,
    ) -> None:
        if type(request) is not DraftRequest:
            raise ValueError("exact DraftRequest required")
        self._snapshot = _bounded(request.snapshot_json, MAX_REQUEST_BYTES)
        if (not callable(getattr(authority, "authorize_and_consume", None))
                or not callable(getattr(transport, "submit_draft", None))):
            raise ValueError("explicit trusted authority and draft-only transport required")
        if journal is not None or operation_id is not None:
            from src.review.submission_journal import SQLiteSubmissionJournal, _operation_id
            if type(journal) is not SQLiteSubmissionJournal:
                raise ValueError("journal and explicit stable operation_id are required together")
            _operation_id(operation_id)
        self._authorize = authority.authorize_and_consume
        self._submit = transport.submit_draft
        self._journal, self._operation_id = journal, operation_id
        self._lock, self._used = Lock(), False
        self._outcome: SubmissionOutcome | None = None

    @property
    def outcome(self) -> SubmissionOutcome | None:
        return self._outcome

    def run(self) -> SubmissionOutcome:
        with self._lock:
            if self._used:
                raise ValueError("submission is single-use, including errors; reconcile, do not replay")
            self._used = True
        # Capture every binding before either injected boundary runs.
        snapshot, journal, identity = self._snapshot, self._journal, self._operation_id
        authorize, submit = self._authorize, self._submit
        digest = sha256(snapshot.encode("utf-8")).hexdigest()
        stage = "authority_intent"
        self._outcome = SubmissionOutcome(SubmissionStatus.UNKNOWN, digest, False,
                                          "authority_outcome_unknown")
        if journal is not None:
            journal._claim(identity, snapshot, self._outcome)

        def result(
            status: SubmissionStatus, dispatched: bool, reason: str, number: int | None = None,
        ) -> SubmissionOutcome:
            candidate = SubmissionOutcome(status, digest, dispatched, reason, number)
            if journal is not None:
                # Keep the prior UNKNOWN if writing the terminal result fails.
                journal._advance(identity, snapshot, stage, "finished", candidate)
            self._outcome = candidate
            return self._outcome

        try:
            _request_data(snapshot)
        except ValueError:
            return result(SubmissionStatus.REJECTED, False, "invalid_request")
        try:
            decision = authorize(DraftRequest(snapshot))
        except Exception:
            return result(SubmissionStatus.UNKNOWN, False, "authority_outcome_unknown")
        if decision is ApprovalDecision.DENIED:
            return result(SubmissionStatus.REJECTED, False, "approval_denied")
        if decision is not ApprovalDecision.AUTHORIZED:
            return result(SubmissionStatus.UNKNOWN, False, "authority_outcome_unknown")
        intent = SubmissionOutcome(SubmissionStatus.UNKNOWN, digest, True, "dispatch_outcome_unknown")
        if journal is not None:
            journal._advance(identity, snapshot, stage, "transport_intent", intent)
        stage = "transport_intent"
        self._outcome = intent
        try:
            reply = submit(DraftRequest(snapshot))
            if (type(reply) is not DraftTransportReply or type(reply.status) is not SubmissionStatus
                    or type(reply.request_sha256) is not str or reply.request_sha256 != digest
                    or reply.draft is not True):
                reply = None
            if reply is None:
                pass
            elif reply.status is SubmissionStatus.SUBMITTED:
                if type(reply.pull_request_number) is not int or not 1 <= reply.pull_request_number <= 2**63 - 1:
                    reply = None
        except Exception:
            reply = None
        # Persistence errors must escape, never be swallowed as transport errors.
        if reply is not None:
            if reply.status is SubmissionStatus.SUBMITTED:
                return result(SubmissionStatus.SUBMITTED, True, "confirmed_draft", reply.pull_request_number)
            if reply.status is SubmissionStatus.REJECTED and reply.pull_request_number is None:
                return result(SubmissionStatus.REJECTED, True, "confirmed_no_creation")
        return result(SubmissionStatus.UNKNOWN, True, "dispatch_outcome_unknown")

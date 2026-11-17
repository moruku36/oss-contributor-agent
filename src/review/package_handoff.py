"""Single-use local package selection, observation, and durable draft submission."""
from __future__ import annotations

from hashlib import sha256
from threading import Lock

from src.review.draft_submission import (
    MAX_REQUEST_BYTES, DraftOnlyTransport, DraftRequest, DraftSubmission,
    HumanApprovalAuthority, SubmissionOutcome, SubmissionStatus, _bounded,
)
from src.review.package_selection import load_selected_review_request
from src.review.review_package import ReviewPackageError
from src.review.submission_journal import SQLiteSubmissionJournal, _operation_id
from src.review.target_preflight import (
    TargetInspector, TargetPreflightStatus, preflight_target,
)

__all__ = ["ReviewPackageSubmission"]


class _BoundInspector:
    """Give preflight only the method captured before any package I/O."""

    __slots__ = ("inspect_target",)

    def __init__(self, method: object) -> None:
        self.inspect_target = method


class ReviewPackageSubmission:
    """Compose the existing boundaries without creating approval or transport.

    The caller supplies an exclusively controlled private parent directory fd,
    one child name, independently retained expected proposal bytes, separately
    trusted dependencies, and a durable journal with a stable operation identity.
    Construction performs no package I/O or inspect/authorize/submit invocation.
    Method binding may run trusted dependency property getters.
    Invalid expected proposal bytes are retained as a local rejection, not fixed.
    """

    def __init__(
        self, parent_fd: int, name: str, expected_request: DraftRequest, *,
        inspector: TargetInspector, authority: HumanApprovalAuthority,
        transport: DraftOnlyTransport, journal: SQLiteSubmissionJournal,
        operation_id: str,
    ) -> None:
        if type(journal) is not SQLiteSubmissionJournal:
            raise ValueError("exact SQLiteSubmissionJournal required")
        _operation_id(operation_id)
        snapshot = ""
        valid = False
        try:
            if type(expected_request) is not DraftRequest:
                raise ValueError("exact independently expected DraftRequest required")
            snapshot = _bounded(expected_request.snapshot_json, MAX_REQUEST_BYTES)
            _ = DraftRequest(snapshot).data
            valid = True
        except (ValueError, TypeError, AttributeError, RecursionError):
            pass

        # Capture authority/transport before an inspector property getter can
        # mutate them. Invalid expected bytes must not look up any dependency.
        self._submission = (
            DraftSubmission(DraftRequest(snapshot), authority=authority,
                            transport=transport, journal=journal,
                            operation_id=operation_id)
            if valid else None
        )
        # A failed inspector lookup becomes UNKNOWN through the real preflight,
        # after strict package selection has succeeded.
        inspect = None
        if valid:
            try:
                inspect = getattr(inspector, "inspect_target", None)
            except Exception:
                pass
        self._inspector = _BoundInspector(inspect)
        self._parent_fd, self._name = parent_fd, name
        self._snapshot, self._valid = snapshot, valid
        self._digest = sha256(snapshot.encode("utf-8")).hexdigest()
        self._lock, self._used = Lock(), False
        self._outcome: SubmissionOutcome | None = None

    @property
    def outcome(self) -> SubmissionOutcome | None:
        # DraftSubmission publishes UNKNOWN before journal/authority/transport
        # boundaries. Never mask it with a locally inferred terminal result.
        submission = self._submission
        if submission is not None and submission.outcome is not None:
            return submission.outcome
        return self._outcome

    def run(self) -> SubmissionOutcome:
        with self._lock:
            if self._used:
                raise ValueError("package handoff is single-use, including errors and cancellation")
            self._used = True
            parent_fd, name, snapshot = self._parent_fd, self._name, self._snapshot
            valid, inspector = self._valid, self._inspector
            submission, digest = self._submission, self._digest
        self._outcome = SubmissionOutcome(
            SubmissionStatus.UNKNOWN, digest, False, "local_handoff_unknown",
        )

        def local(status: SubmissionStatus, reason: str) -> SubmissionOutcome:
            self._outcome = SubmissionOutcome(status, digest, False, reason)
            return self._outcome

        if not valid:
            return local(SubmissionStatus.REJECTED, "invalid_expected_request")
        try:
            selected = load_selected_review_request(parent_fd, name, DraftRequest(snapshot))
        except (ReviewPackageError, OSError, ValueError, TypeError, AttributeError, RecursionError):
            return local(SubmissionStatus.REJECTED, "package_rejected")
        result = preflight_target(selected, inspector=inspector)
        if result.status is TargetPreflightStatus.REJECTED:
            return local(SubmissionStatus.REJECTED, result.reason)
        if result.status is not TargetPreflightStatus.READY:
            return local(SubmissionStatus.UNKNOWN, result.reason)
        # READY is local consistency only. The preconstructed existing operation
        # still requires its independent authority and atomic transport recheck.
        # Its exceptions, cancellation, durable replay rules and outcome survive.
        return submission.run()

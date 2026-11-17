"""Injected read-only target observation; READY means local consistency only.

No authority, transport, journal, network or configuration is invoked here.
Observations are untrusted assertions, not authentication or TOCTOU protection.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from src.review.draft_submission import DraftRequest, DraftTarget, _bounded, _target

__all__ = [
    "ObservationStatus", "TargetObservation", "TargetInspector",
    "TargetPreflightStatus", "TargetPreflightResult", "preflight_target",
]


class ObservationStatus(Enum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TargetObservation:
    """Entire observed identity and exact request string, never a hash credential.

    Constructors do not validate. Both capabilities must be actual bools;
    atomic_head_base_preconditions means support for enforcing BOTH identities
    atomically with creation, including their repositories and full refs.
    """

    status: ObservationStatus
    target: DraftTarget
    request_snapshot_json: str
    draft_only: bool
    atomic_head_base_preconditions: bool


class TargetInspector(Protocol):
    """Read-only injected observation. Must not submit or consume authorization.

    The embedding supplies any implementation and its trust policy separately.
    No implementation, discovery, credentials, deadlines or retries live here.
    """

    def inspect_target(self, request: DraftRequest) -> TargetObservation: ...


class TargetPreflightStatus(Enum):
    READY = "ready"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TargetPreflightResult:
    """Local consistency result only; never permission to dispatch."""

    status: TargetPreflightStatus
    reason: str
    observation: TargetObservation | None = None


def _detach_observation(reply: object) -> TargetObservation:
    if type(reply) is not TargetObservation:
        raise ValueError("exact TargetObservation required")
    # Capture fields once before validation; retain no injected mutable objects.
    status, target, snapshot, draft, atomic = (
        reply.status, reply.target, reply.request_snapshot_json,
        reply.draft_only, reply.atomic_head_base_preconditions,
    )
    if (type(status) is not ObservationStatus or type(target) is not DraftTarget
            or type(snapshot) is not str or type(draft) is not bool
            or type(atomic) is not bool):
        raise ValueError("malformed observation")
    fields = {name: getattr(target, name) for name in DraftTarget.__dataclass_fields__}
    # A different bounded destination is a definite identity mismatch. Validate
    # the other fields with the actual target grammar, whose only supported
    # request destination is GitHub; never normalize the observed destination.
    _bounded(fields["destination"], 256)
    _target({**fields, "destination": "https://github.com"})
    _ = DraftRequest(snapshot).data
    return TargetObservation(status, DraftTarget(**fields), snapshot, draft, atomic)


def preflight_target(request: DraftRequest, *, inspector: TargetInspector) -> TargetPreflightResult:
    """Validate, capture, observe once, detach and compare exact identities.

    Feed the result of load_selected_review_request here. Invalid input rejects
    before even looking up the inspector. Exceptions at the injected boundary
    become UNKNOWN (process cancellation/BaseException still propagates).
    Re-observation cannot authenticate, authorize, close TOCTOU, or replace the
    transport's independent atomic head/base recheck. Unsupported support rejects.
    """
    try:
        if type(request) is not DraftRequest:
            raise ValueError("exact DraftRequest required")
        snapshot = request.snapshot_json
        data = DraftRequest(snapshot).data
        expected = DraftTarget(**data["target"])
    except (ValueError, TypeError, AttributeError, RecursionError):
        return TargetPreflightResult(TargetPreflightStatus.REJECTED, "invalid_request")

    try:
        inspect = getattr(inspector, "inspect_target", None)  # bind exactly once
        if not callable(inspect):
            raise ValueError("explicit inspector method required")
        observation = _detach_observation(inspect(DraftRequest(snapshot)))
    except Exception:
        return TargetPreflightResult(TargetPreflightStatus.UNKNOWN, "observation_unknown")

    if observation.status is ObservationStatus.UNKNOWN:
        return TargetPreflightResult(TargetPreflightStatus.UNKNOWN, "observation_unknown", observation)
    if observation.status is not ObservationStatus.OBSERVED:
        return TargetPreflightResult(TargetPreflightStatus.UNKNOWN, "observation_unknown")
    if observation.request_snapshot_json != snapshot:
        return TargetPreflightResult(TargetPreflightStatus.REJECTED, "request_mismatch", observation)
    if any(getattr(observation.target, name) != getattr(expected, name)
           for name in DraftTarget.__dataclass_fields__):
        return TargetPreflightResult(TargetPreflightStatus.REJECTED, "target_mismatch", observation)
    if observation.draft_only is not True or observation.atomic_head_base_preconditions is not True:
        return TargetPreflightResult(TargetPreflightStatus.REJECTED, "unsupported_capability", observation)
    return TargetPreflightResult(TargetPreflightStatus.READY, "local_consistency_only", observation)

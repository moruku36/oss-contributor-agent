"""Bind one persisted review package to independently retained proposal bytes.

Selection is local preflight, never approval, authentication or dispatch. The
strict reader's trusted-process/private-directory/exclusive-control prerequisites
and all existing package bounds apply unchanged.
"""
from __future__ import annotations

from src.review.draft_submission import DraftRequest
from src.review.review_package import ReviewPackageError, read_review_package

__all__ = ["load_selected_review_request"]


def load_selected_review_request(
    parent_fd: int, name: str, expected_request: DraftRequest,
) -> DraftRequest:
    """Read a complete package and require the exact expected canonical snapshot.

    Capture and revalidate the expected request BEFORE filesystem I/O. A package
    name, constructor, receipt or public digest cannot establish this binding.
    Later caller mutation cannot change the captured proposal. No submission
    object, journal, authority or transport is created or invoked here.
    """
    if type(expected_request) is not DraftRequest:
        raise ReviewPackageError("actual expected prepared DraftRequest required")
    try:
        snapshot = expected_request.snapshot_json  # capture caller state once
        detached = DraftRequest(snapshot)
        # Constructors alone do not validate. Use the actual domain validator,
        # including report, target/head/base, title, template and rendered body.
        _ = detached.data
        expected_bytes = snapshot.encode("utf-8")
    except (ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise ReviewPackageError("invalid expected prepared DraftRequest") from exc

    package = read_review_package(parent_fd=parent_fd, name=name)
    if package.snapshot_json.encode("utf-8") != expected_bytes:
        raise ReviewPackageError("selected package differs from the exact expected request")
    return detached

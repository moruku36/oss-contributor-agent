'''Trusted host delivery; the builder authenticates execution, report, source and cleanup.
Historical staged bytes, hashes and exit status never grant approval.
'''
from dataclasses import dataclass

from ..sandbox.seatbelt_runner import Result
from ..sandbox.seatbelt_stage import StagedRun
from .draft_submission import (
    DraftRequest, DraftSubmission, SubmissionOutcome,
)
from .submission_journal import SQLiteSubmissionJournal, _operation_id
from .target_preflight import (
    TargetPreflightResult, TargetPreflightStatus, preflight_target,
)


@dataclass(frozen=True)
class DeliveryResult:
    preflight: TargetPreflightResult | None = None
    submission: SubmissionOutcome | None = None


def deliver_staged(staged, *, request_builder, inspector, authority, transport,
                   journal, operation_id) -> DeliveryResult:
    if type(staged) is not StagedRun:
        raise ValueError('staged must be an exact StagedRun')
    result = getattr(staged, 'result', None)
    if type(result) is not Result:
        raise ValueError('staged.result must be an exact Result')
    if not (
        type(getattr(result, 'returncode', None)) is int
        and type(getattr(result, 'timed_out', None)) is bool
        and type(getattr(result, 'stdout', None)) is bytes
        and type(getattr(result, 'stderr', None)) is bytes
        and result.success
    ):
        raise ValueError('staged result must be well formed and successful')
    if type(journal) is not SQLiteSubmissionJournal:
        raise ValueError('journal must be an exact SQLiteSubmissionJournal')
    if type(operation_id) is not str or not operation_id:
        raise ValueError('operation_id must be an explicit stable string')
    _operation_id(operation_id)
    try:
        detached = StagedRun(
            staged.stage_path, staged.candidate_sha256, staged.driver_sha256,
            Result(result.returncode, result.timed_out, result.stdout, result.stderr),
        )
    except AttributeError as exc:
        raise ValueError('malformed staged history') from exc
    request = request_builder(detached)
    if (type(request) is not DraftRequest
            or type(getattr(request, 'snapshot_json', None)) is not str):
        raise ValueError('builder must return an exact DraftRequest')
    snapshot = DraftRequest(request.snapshot_json)
    snapshot.data
    preflight = preflight_target(snapshot, inspector=inspector)
    if preflight.status is not TargetPreflightStatus.READY:
        return DeliveryResult(preflight, None)
    submission = DraftSubmission(
        snapshot, authority=authority, transport=transport,
        journal=journal, operation_id=operation_id,
    ).run()
    return DeliveryResult(preflight, submission)

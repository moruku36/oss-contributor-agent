'''Single-use candidate -> staged attempt -> detached review report orchestration.

Injection-only and offline. The caller supplies an already collected
CandidatePoolEntry, a fresh StagedAttemptAdapter (trusted lifecycle, prepared
snapshot workspace, runner) and a patch model (any object structurally
providing the PatchModel protocol, i.e. a callable propose method). This module
opens no files, network or processes, selects no backend, acquires no source
and never submits anything. Candidate status and base SHA are caller-declared,
not verified. The report only records caller-declared execution provenance
(synthetic or unverified); it does not prove isolation, source authenticity or
the base. Human approval is always pending. Draft PR submission stays
unimplemented.
'''
from __future__ import annotations

import copy
from dataclasses import dataclass
import re

from src.collector.digest import CandidatePoolEntry
from src.agent.core import AttemptResult, CoderLoop, TestPlan
from src.agent.staged_attempt import StagedAttemptAdapter
from src.review.pr_builder import ReviewReport, build_review_report
from src.sandbox.docker_manager import Sandbox, SandboxPolicy

_SEG = r'[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?'
_REPO = re.compile(_SEG + '/' + _SEG)
_SHA = re.compile(r'[0-9a-f]{40}')
_KINDS = ('synthetic', 'unverified')


class CandidateAttemptError(ValueError):
    '''Rejected before any lifecycle, workspace or model action.'''


def _text(name: str, value: object, limit: int) -> str:
    try:
        ok = (type(value) is str and bool(value.strip()) and '\0' not in value
              and len(value.encode('utf-8')) <= limit)
    except UnicodeError:
        ok = False
    if not ok:
        raise CandidateAttemptError(name + ' must be bounded nonempty text')
    return value


@dataclass(frozen=True)
class CandidateAttemptOutcome:
    '''Local review artifact only. Approval is pending and nothing is submitted.'''

    status: str  # pending_human_review | failed | report_failed
    attempt: AttemptResult
    report: ReviewReport | None
    caller_declared_status: str
    human_approval: str = 'pending'
    submitted: bool = False


class CandidateAttempt:
    '''One candidate, one fresh adapter, one run. Replays are refused.'''

    def __init__(
        self, candidate: CandidatePoolEntry, *, base_commit: str, plan: TestPlan,
        adapter: StagedAttemptAdapter, model: object, sandbox_policy: SandboxPolicy,
        caller_declared_status: str, execution_kind: str = 'unverified',
        max_attempts: int = 3, token_budget: int = 100_000,
        timeout_seconds: float = 2700.0, max_model_calls: int | None = None,
        max_text_bytes: int = 16_384, max_patch_bytes: int = 16_384,
    ) -> None:
        if type(candidate) is not CandidatePoolEntry:
            raise CandidateAttemptError('exact CandidatePoolEntry required')
        repo = _text('repo', candidate.repo, 201)
        if not _REPO.fullmatch(repo):
            raise CandidateAttemptError('invalid repository identity')
        if type(candidate.number) is not int or not 1 <= candidate.number <= 2**31:
            raise CandidateAttemptError('invalid issue number')
        title = _text('title', candidate.title, 1024)
        url = _text('url', candidate.url, 2048)
        if type(base_commit) is not str or not _SHA.fullmatch(base_commit):
            raise CandidateAttemptError('explicit lowercase 40-hex base SHA required')
        if type(execution_kind) is not str or execution_kind not in _KINDS:
            raise CandidateAttemptError('execution kind must be synthetic or unverified')
        _text('caller_declared_status', caller_declared_status, 64)
        if type(plan) is not TestPlan:
            raise CandidateAttemptError('explicit TestPlan required')
        if type(sandbox_policy) is not SandboxPolicy:
            raise CandidateAttemptError('explicit SandboxPolicy required')
        if type(adapter) is not StagedAttemptAdapter or adapter.state != 'new':
            raise CandidateAttemptError('fresh StagedAttemptAdapter required')
        if model is None or not callable(getattr(model, 'propose', None)):
            raise CandidateAttemptError('explicit patch model required')
        # The report schema bounds log tails at 16384 bytes.
        if type(max_text_bytes) is not int or max_text_bytes > 16_384:
            raise CandidateAttemptError('max_text_bytes must be <= 16384 for report bounds')
        # Detached copies: later mutation of the candidate cannot change the run.
        issue = {'repo': repo, 'number': candidate.number, 'title': title, 'url': url}
        # No-action preflight through the real report validator (plan shape,
        # duplicate suite commands, title control characters, ...). It reuses the
        # existing schema checks instead of duplicating them here.
        try:
            build_review_report(
                AttemptResult(False, 0), dict(issue), base_commit=base_commit,
                plan=plan, execution_kind=execution_kind,
            )
        except ValueError as exc:
            raise CandidateAttemptError('report preflight rejected input: ' + str(exc)) from None
        try:
            self._loop = CoderLoop(
                max_attempts, token_budget, model=model, timeout_seconds=timeout_seconds,
                max_model_calls=max_model_calls, max_text_bytes=max_text_bytes,
                max_patch_bytes=max_patch_bytes,
            )
        except ValueError as exc:
            raise CandidateAttemptError(str(exc)) from None
        self._issue = issue
        self._base, self._plan, self._adapter = base_commit, plan, adapter
        self._policy, self._kind = sandbox_policy, execution_kind
        self._declared = caller_declared_status
        self._used = False

    def run(self) -> CandidateAttemptOutcome:
        if self._used:
            raise CandidateAttemptError('attempt is single-use; construct a new one')
        if self._adapter.state != 'new':
            raise CandidateAttemptError('adapter is no longer fresh')
        self._used = True
        p = self._policy
        sandbox = Sandbox(
            p.image, p.cpu_limit, p.memory_limit_mb, backend=self._adapter,
            attempt_timeout_seconds=p.attempt_timeout_seconds,
            operation_timeout_seconds=p.operation_timeout_seconds,
            max_commands=p.max_commands, max_command_bytes=p.max_command_bytes,
            max_output_bytes=p.max_output_bytes,
        )
        attempt = self._loop.run(dict(self._issue), self._adapter, sandbox, plan=self._plan)
        detached = copy.deepcopy(attempt)
        if attempt.success and not attempt.cleanup_confirmed:
            return CandidateAttemptOutcome('failed', detached, None, self._declared)
        try:
            report = build_review_report(
                attempt, dict(self._issue), base_commit=self._base, plan=self._plan,
                execution_kind=self._kind,
            )
        except ValueError:
            # No retry. The generated diff may exceed the report bounds even when
            # the patch bound was respected, so this outcome is explicitly allowed.
            return CandidateAttemptOutcome('report_failed', detached, None, self._declared)
        status = 'pending_human_review' if attempt.success else 'failed'
        return CandidateAttemptOutcome(status, detached, report, self._declared)


def run_candidate_attempt(candidate: CandidatePoolEntry, **kwargs: object) -> CandidateAttemptOutcome:
    '''Validate everything first, then run exactly once.'''
    return CandidateAttempt(candidate, **kwargs).run()

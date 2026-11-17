"""Single-use, injection-only event-channel candidate to LOCAL review package.

No acquisition, dependency selection, approval, submission or remote preflight.
Declared provenance is synthetic/unverified, never proof of isolation or origin.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
from dataclasses import dataclass, replace
import os
from threading import Lock
from types import SimpleNamespace

from src.agent.candidate_attempt import CandidateAttempt, CandidateAttemptOutcome
from src.agent.core import TestPlan
from src.agent.filesystem_staging import ManifestFile
from src.agent.runner_event_channel import EventChannelBackend, TrustedEventChannel
from src.agent.snapshot_workspace import SnapshotLimits, SnapshotWorkspace, TextFile
from src.agent.staged_attempt import LifecycleBackend, StagedAttemptAdapter
from src.agent.staged_runner_bridge import StagedRunnerBridge
from src.agent.structured_runner_evidence import StructuredRunnerEvidenceAdapter
from src.collector.digest import CandidatePoolEntry
from src.review import draft_submission as draft
from src.review import review_package as package
from src.sandbox.docker_manager import SandboxPolicy


@dataclass(frozen=True)
class EventPackageOptions:
    max_attempts: int = 1
    token_budget: int = 4096
    timeout_seconds: float = 120.0
    max_model_calls: int = 1
    max_text_bytes: int = 16_384
    max_patch_bytes: int = 16_384
    stage_timeout_seconds: float = 10.0
    max_command_timeout_seconds: float = 30.0
    max_output_bytes: int = 16_384
    max_dispatches: int = 8
    max_wire_bytes: int = 131_072


@dataclass(frozen=True)
class EventPackageOutcome:
    status: str
    candidate: CandidateAttemptOutcome | None = None
    request: draft.DraftRequest | None = None
    receipt: package.PackageReceipt | package.IncompletePackageReceipt | None = None
    error: BaseException | None = None
    human_approval: str = 'pending'
    submitted: bool = False


class EventCandidatePackage:
    """Freeze declarations now; consume once before any capability/dependency I/O.

    Caller retains its FDs. run() owns temporary duplicates, never deletes staged
    files or package artifacts. Only the supplied lifecycle can confirm cleanup.
    Ordinary failures return an outcome; BaseException cancellation propagates.
    The retained outcome never turns uncertain persistence into success.
    """

    def __init__(
        self, candidate: CandidatePoolEntry, *, base_commit: str, plan: TestPlan,
        files: tuple[TextFile, ...], editable_paths: tuple[str, ...],
        context_paths: tuple[str, ...], lifecycle: LifecycleBackend,
        channel: TrustedEventChannel, model: object, staging_parent_fd: int,
        package_parent_fd: int, package_name: str, target: draft.DraftTarget,
        title: str, sandbox_policy: SandboxPolicy, caller_declared_status: str,
        execution_kind: str, options: EventPackageOptions,
        limits: SnapshotLimits, manifest: tuple[ManifestFile, ...] | None = None,
        pr_template: str | None = None,
    ) -> None:
        # Do not inspect injected dependencies until ALL declarations are detached.
        for value, kind in ((candidate, CandidatePoolEntry), (plan, TestPlan),
                            (sandbox_policy, SandboxPolicy), (target, draft.DraftTarget),
                            (options, EventPackageOptions), (limits, SnapshotLimits)):
            if type(value) is not kind:
                raise ValueError('exact declared input type required: ' + kind.__name__)
        if (type(files) is not tuple or any(type(f) is not TextFile for f in files)
                or type(editable_paths) is not tuple or type(context_paths) is not tuple
                or (manifest is not None and (type(manifest) is not tuple
                    or any(type(f) is not ManifestFile for f in manifest)))):
            raise ValueError('explicit file/selection/manifest tuples required')
        self._declared = copy.deepcopy(dict(
            candidate=candidate, base_commit=base_commit, plan=plan, files=files,
            editable_paths=editable_paths, context_paths=context_paths,
            policy=sandbox_policy, target=target, title=title, template=pr_template,
            declared_status=caller_declared_status, kind=execution_kind,
            options=options, limits=limits, manifest=manifest, name=package_name,
        ))
        self._lifecycle, self._channel, self._model = lifecycle, channel, model
        self._stage_fd, self._package_fd = staging_parent_fd, package_parent_fd
        self._used, self._gate = False, Lock()
        self._outcome: EventPackageOutcome | None = None

    @property
    def outcome(self) -> EventPackageOutcome | None:
        # AttemptResult contains mutable lists; callers cannot rewrite retention.
        if self._outcome is None:
            return None
        return replace(self._outcome, candidate=copy.deepcopy(self._outcome.candidate))

    def run(self) -> EventPackageOutcome:
        with self._gate:
            if self._used:
                raise ValueError('local package workflow is single-use; no replay')
            self._used = True
        d = self._declared
        o = d['options']
        candidate = request = receipt = None
        phase = 'input'
        try:
            # These are the exporter's own guards, not an imitation of its rules.
            # No public validation-only API exists. This is a limited destination
            # check, not a reservation or a full filesystem writability preflight.
            package._name(d['name'])
            draft._target({k: getattr(d['target'], k)
                           for k in draft.DraftTarget.__dataclass_fields__})
            draft._bounded(d['title'], 256)
            if (d['target'].repository != d['candidate'].repo
                    or d['target'].base_commit != d['base_commit']):
                raise ValueError('target must match declared candidate repository and base')
            with ExitStack() as handles:
                parent = package._parent(self._package_fd, handles)
                try:
                    os.stat(d['name'], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise package.ReviewPackageError('package child already exists')
                if type(self._stage_fd) is not int or self._stage_fd < 0:
                    raise ValueError('already-open staging-parent FD required')
                stage = os.dup(self._stage_fd)
                handles.callback(os.close, stage)
                interruptions: list[BaseException] = []

                def observed(call):
                    if not callable(call):
                        return call

                    def invoke(*args, **kwargs):
                        try:
                            return call(*args, **kwargs)
                        except BaseException as exc:
                            if not isinstance(exc, Exception):
                                interruptions.append(exc)
                            raise
                    return invoke

                # Dispatch/staging and sandbox cleanup deliberately wrap errors.
                # Observe cancellation at the injected seams so even a wrapped
                # cancellation cannot be reduced to an ordinary failed attempt.
                channel = SimpleNamespace(dispatch=observed(getattr(self._channel, 'dispatch', None)))
                lifecycle = SimpleNamespace(**{
                    name: observed(getattr(self._lifecycle, name, None))
                    for name in ('create', 'start', 'remove')
                })
                model = SimpleNamespace(propose=observed(getattr(self._model, 'propose', None)))
                backend = EventChannelBackend(
                    channel=channel, max_dispatches=o.max_dispatches,
                    max_wire_bytes=o.max_wire_bytes,
                )
                evidence = StructuredRunnerEvidenceAdapter(
                    backend=backend, max_dispatches=o.max_dispatches,
                )

                def workspace(attempt_id: str) -> SnapshotWorkspace:
                    return SnapshotWorkspace(
                        attempt_id, d['files'], editable_paths=d['editable_paths'],
                        context_paths=d['context_paths'], assessor=evidence, limits=d['limits'],
                    )

                manifest = d['manifest']
                if manifest is None:
                    manifest = tuple(ManifestFile(f.path, f.role) for f in d['files'])
                bounds = dict(
                    limits=d['limits'], stage_timeout_seconds=o.stage_timeout_seconds,
                    max_command_timeout_seconds=o.max_command_timeout_seconds,
                    max_output_bytes=o.max_output_bytes, max_dispatches=o.max_dispatches,
                )
                # Real bridge validation checks exact manifest, snapshot namespace,
                # bounds and directory capability before any lifecycle allocation.
                with StagedRunnerBridge(
                    'validation', workspace=workspace('validation'), staging_parent_fd=stage,
                    manifest=manifest, runner=evidence, **bounds,
                ):
                    pass
                adapter = StagedAttemptAdapter(
                    lifecycle=lifecycle, workspace_factory=workspace,
                    staging_parent_fd=stage, manifest=manifest, runner=evidence, **bounds,
                )
                attempt = CandidateAttempt(
                    d['candidate'], base_commit=d['base_commit'], plan=d['plan'],
                    adapter=adapter, model=model, sandbox_policy=d['policy'],
                    caller_declared_status=d['declared_status'], execution_kind=d['kind'],
                    max_attempts=o.max_attempts, token_budget=o.token_budget,
                    timeout_seconds=o.timeout_seconds, max_model_calls=o.max_model_calls,
                    max_text_bytes=o.max_text_bytes, max_patch_bytes=o.max_patch_bytes,
                )
                phase = 'attempt'
                candidate = attempt.run()
                if interruptions:
                    raise interruptions[0]
                if (candidate.status != 'pending_human_review'
                        or not candidate.attempt.success
                        or not candidate.attempt.cleanup_confirmed
                        or candidate.report is None or not candidate.report.eligible_for_pr):
                    self._outcome = EventPackageOutcome(candidate.status, candidate)
                else:
                    phase = 'prepare'
                    request = draft.prepare_draft_submission(
                        candidate, target=d['target'], title=d['title'],
                        pr_template=d['template'],
                    )
                    phase = 'package'
                    try:
                        receipt = package.export_review_package(request, parent_fd=parent, name=d['name'])
                    except package.ReviewPackageExportError as exc:
                        receipt = exc.receipt
                        # The exporter wraps cancellation too; restore its semantics.
                        if not isinstance(exc.original_error, Exception):
                            raise exc.original_error
                        raise
            # FD-close failure above also refuses a success outcome, retaining receipt.
            if self._outcome is None:
                self._outcome = EventPackageOutcome('pending_human_review', candidate, request, receipt)
        except BaseException as exc:
            status = phase + '_failed' if isinstance(exc, Exception) else 'interrupted'
            self._outcome = EventPackageOutcome(status, candidate, request, receipt, exc)
            if not isinstance(exc, Exception):
                raise
        assert self.outcome is not None
        return self.outcome

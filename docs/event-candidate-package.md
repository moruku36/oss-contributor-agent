# Event candidate to local review package

`src.agent.event_candidate_package.EventCandidatePackage` connects an explicitly
supplied candidate and snapshot to the existing local package exporter. It does
not acquire source, choose dependencies, inspect a remote target, invoke an
approval authority, access a submission journal, or submit anything. Human
approval remains `pending` even after successful export.

The internal composition is:

```text
explicit trusted channel -> EventChannelBackend
                         -> ONE StructuredRunnerEvidenceAdapter
                            | staged runner AND SnapshotWorkspace assessor
SnapshotWorkspace -> StagedAttemptAdapter -> CandidateAttempt
 -> successful cleanup-confirmed ReviewReport
 -> prepare_draft_submission -> export_review_package
```

The caller supplies exact `CandidatePoolEntry`, `TestPlan`, `SandboxPolicy`,
`DraftTarget`, `SnapshotLimits`, and `EventPackageOptions` values, a tuple of
`TextFile` declarations, editable/context path tuples, trusted lifecycle,
channel and patch model, already-open staging/package parent directory FDs,
and a fresh simple package child name. An optional exact `ManifestFile` tuple
must match all declared file paths and roles. Omitting it derives precisely
those declarations; it does not discover files. Only declared source paths may
be edited. Test and runner files remain protected by the existing workspace.

This callable example uses the actual API. All dependencies and declarations
are arguments supplied by the caller; it contains no automatic acquisition or
backend selection. The caller keeps both FDs open through the call. The package
parent must satisfy the exporter's caller-owned private-directory requirements
(mode `0700`); both parents must support descriptor-relative staging/export.

```python
from src.agent.event_candidate_package import EventCandidatePackage, EventPackageOptions
from src.agent.snapshot_workspace import SnapshotLimits


def create_local_package(
    *, candidate, base_commit, plan, files, editable_paths, context_paths,
    lifecycle, channel, model, staging_parent_fd, package_parent_fd,
    package_name, target, title, policy, caller_declared_status,
    execution_kind, manifest=None,
):
    workflow = EventCandidatePackage(
        candidate,
        base_commit=base_commit,
        plan=plan,
        files=files,
        editable_paths=editable_paths,
        context_paths=context_paths,
        manifest=manifest,
        lifecycle=lifecycle,
        channel=channel,
        model=model,
        staging_parent_fd=staging_parent_fd,
        package_parent_fd=package_parent_fd,
        package_name=package_name,
        target=target,
        title=title,
        sandbox_policy=policy,
        caller_declared_status=caller_declared_status,
        execution_kind=execution_kind,  # explicitly "synthetic" or "unverified"
        options=EventPackageOptions(
            max_attempts=1, token_budget=4096, max_model_calls=1,
            timeout_seconds=120.0, stage_timeout_seconds=10.0,
            max_command_timeout_seconds=30.0, max_dispatches=8,
            max_output_bytes=16384, max_wire_bytes=131072,
            max_text_bytes=16384, max_patch_bytes=16384,
        ),
        limits=SnapshotLimits(),
    )
    outcome = workflow.run()  # once only; no retry on this object
    return workflow, outcome
```

Choose reproduction/suite timeouts within the supplied command bounds and a
policy/dispatch budget that covers baseline suite, reproduction and verification.
`target.repository` and `target.base_commit` must match the declarations. Every
target ref and commit is explicit; the workflow does not verify that the patch
was committed to the declared head, or that any declared SHA exists remotely.

All declared inputs are detached before injected dependency attributes are
inspected. A run consumes the instance before capability access or any candidate,
lifecycle, model, channel or package execution. Existing snapshot/bridge,
candidate, target and package validators enforce their respective contracts.
The workflow uses the existing package module's internal name/parent guards
because there is no public validation-only API. It checks parent capabilities,
platform support and absence of the destination child before lifecycle execution.
An empty-name rejection alone is not filesystem preflight. These checks do not
reserve the name, guarantee write permissions/free space, or eliminate later
races; exclusive export and its readback remain authoritative.

`pending_human_review` requires actual successful attempt evidence, confirmed
lifecycle cleanup, a valid report, preparation, export, and successful closure of
owned descriptors. Failed/unknown attempts and unconfirmed cleanup cannot export.
Ordinary failures retain an outcome, including the actual candidate outcome when
available, prepared request and export receipt. Package persistence uncertainty
stays `package_failed` even if the exporter retained a complete receipt after a
completion-marker/close error. Partial artifacts are retained without deletion
or resend. A receipt is evidence, never permission to replay.

Cancellation and other non-`Exception` failures propagate while the object stays
consumed. The workflow observes injected calls to restore cancellation that the
existing dispatch or cleanup adapters may wrap. `workflow.outcome` retains an
`interrupted` outcome and any available candidate/receipt. If execution did not
return an attempt result, the workflow does not manufacture one or invent
cleanup confirmation. Its temporary FD closure is not lifecycle cleanup.

Package selection and submission are separate decisions. After independent
human review of a successful local result, existing selection can bind the exact
prepared request to readback without submitting:

```python
from src.review.package_selection import load_selected_review_request


def select_local_package(*, outcome, package_parent_fd, package_name):
    if outcome.status != 'pending_human_review' or outcome.request is None:
        raise ValueError('successful local package required')
    return load_selected_review_request(
        package_parent_fd, package_name, outcome.request,
    )
```

The existing [package handoff](package-handoff.md) is a subsequent, explicitly
authorized workflow requiring its own exact expected request, target inspector,
human authority, draft transport, durable journal and operation ID. This module
does not construct or invoke that workflow. A package name, public digest or
readable package alone grants no approval or submission authority.

The six self-contained methods in `tests/test_event_candidate_package.py` use
whole staged synthetic snapshots and encoded event-channel bytes. Their nominal
flow is suite baseline pass, reproduction assertion failure, one byte patch,
reproduction and suite pass, confirmed synthetic lifecycle removal, real local
export and package readback. Fixtures compare bytes only and never execute
candidate source or commands. Failure cases cover uncertain/error evidence,
missing model, report rejection, cleanup uncertainty, retained partial and
post-marker failures, cancellation, input and destination rejection, and replay.
These tests were authored without running them in this file-only increment.

Synthetic event producers, patch models and lifecycle fixtures do not prove
real test execution or isolation. `unverified` is also caller-declared provenance,
not an attestation. Remaining work includes independently trusted real runner
instrumentation, an enforced isolated lifecycle/runner, authenticated channel
origin and transport with in-flight deadlines, real source/base/head provenance,
and independently authorized target/approval/submission integration. Local
composition and export do not close those gaps.

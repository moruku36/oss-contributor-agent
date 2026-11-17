# Increment 35: isolated staged delivery

`deliver_staged` provides a small host-side handoff from retained `StagedRun` history to the existing draft-submission path. It is independent of the missing execution-journal34 implementation. Increment34 timed out without source; this increment neither implements nor relabels it. Existing sources31–34, packages, dependencies and submission defaults remain untouched.

The entry point requires exact `StagedRun` and `Result` instances, correctly typed result fields, and `result.success` before invoking injected capabilities. A failed or malformed result raises `ValueError`. It also requires an exact existing `SQLiteSubmissionJournal` and an explicit stable operation identifier validated by the existing `_operation_id` helper.

The builder receives separate frozen staged and result records. Their path, digests, output bytes and successful exit status describe historical controlled execution; they authenticate neither a report nor approval. The independently trusted HOST builder must authenticate execution, report authenticity, source binding and completed cleanup before preparing an exact canonical `DraftRequest`. This module does not supply that authentication capability.

Delivery credentials remain exclusively in the trusted host builder, authority and transport. They must never be placed in candidate code, staged inputs, reports, output streams or subprocess environments. This module performs no subprocess execution, staging invocation, environment access or stage deletion. Historical stage records are retained; authenticated cleanup remains a prerequisite enforced by the trusted builder and existing guards.

The returned request is copied into a separate `DraftRequest`, whose `data` getter validates the snapshot. Preflight runs once. Any status other than `READY` returns `DeliveryResult(preflight, None)` without constructing a submission or calling authority or transport. `READY` delegates once to `DraftSubmission(...).run()` with the same mandatory journal and operation identifier. Existing authority, atomic-source and cleanup guards remain in force. Exceptions propagate without fallback or retry.

`READY` establishes local consistency only. Creation still requires supported atomic expected-head and expected-base source predicates in the trusted inspector/transport. Ordinary GitHub REST/GraphQL CREATE provides no supported expected head/base OID predicates for this requirement and must remain unsupported and fail closed. This increment provides no actual transport, invented authentication, boolean/token approval or product PR acceptance.

Four offline unittest methods cover failed execution, unsupported capability, READY delegation with detached records and journal/operation binding, and builder failure. Each invokes `deliver_staged` exactly once. Preflight and submission are mocked, request parsing uses a mocked data getter, and the journal fixture bypasses database initialization through `object.__new__`. These tests demonstrate handoff behavior only; they do not establish authentic execution, supported remote atomic creation or live acceptance.

No commands, tests, processes or network requests were run for this handoff. Runtime behavior remains unverified. Execution crash recovery34 is absent and is not provided by the existing submission journal or this increment.

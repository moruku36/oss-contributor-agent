# Local review report — proposed next increment

Status: locally implemented, 2026-11-11; untested. The three-case local test invocation was denied before dispatch by native approval binding. The owner subsequently requested feature-branch publication as Ready for review. Publication uses `[skip ci]`; it does not claim test success, acceptance, approval-binding repair, or merge authorization.

## Verified starting point

PR #8 was owner-merged as `f337504bf971aa46e5073561f21cd1d440aa3228`.
Its main Actions run `42956366045` completed successfully: Python 3.12.16,
three lifecycle contract tests, one invocation, `3 passed in 0.04s`.
This was an existing workflow triggered by the owner's merge, not a new test
execution requested by this increment. The lifecycle trial now has three
actual case executions (local 0, PR 0, main 3). Do not rerun it here.
The local interpreter/approval-binding problem remains unresolved. CI success
is neither proof that binding was repaired nor proof of real Docker isolation.

## Purpose and boundary

Implement the existing pure report and PR-body stubs in
`src/review/pr_builder.py`. Do not enable runtime inference, filesystem writes,
Git operations, HTTP, actual Docker access, or submission from that module.
Keep `submit_draft_pr` explicitly unimplemented and the production CLI's
collect-only gate unchanged. Development PR delivery for this project is
separate from approval to submit generated contributions to upstream projects.

A review artifact records caller-supplied evidence. It is not authentication,
proof of isolation, compliance approval, or permission to publish.

## Proposed input contract

`build_review_report(attempt, issue, *, base_commit, plan,
execution_kind="unverified")` takes:

- An actual `AttemptResult` with its checks and model receipts.
- Explicit issue identity: bounded `owner/repo`, a strictly positive integer
  issue number (not bool), and bounded title. Derive the GitHub issue link from
  validated identity; do not accept arbitrary link schemes.
- An immutable base SHA (40 lowercase hex characters for the current repository).
  This is caller-declared source identity, not evidence that checkout was verified.
- The exact `TestPlan`, including reproduction and all declared suite commands.
- An execution label `synthetic` or `unverified`. There is no `isolated` label
  in this increment: current evidence cannot independently substantiate it.

Missing identity, malformed evidence, invalid enums, nonfinite/negative
resources, oversized text/collections, invalid UTF-8 encodings, and truncated
complete diff must reject before rendering. Limits must apply to input counts
and bytes, canonical snapshot bytes, template bytes, and final rendered bytes.
Overflow must reject, never silently truncate evidence.

## Immutable evidence snapshot

Capture a detached immutable snapshot; later caller mutation must not change
an existing report or its rendered body. Preserve:

- Issue identity/title, base SHA, execution label and sandbox attempt ID.
- Success/state/failure kind, cleanup confirmation, notes and state history.
- Full diff and SHA-256 of its exact UTF-8 bytes, not only a preview.
- Exact ordered command records: phase, command, exit code, duration, bounded
  captured log tail, assessment outcome, tests run, assertion failures, detail.
- Declared reproduction/suite commands and their timeouts.
- Patch attempt count, model call count, authoritative known token prefix,
  nullable total usage, and each provider/model/token receipt.
- Elapsed time. Do not invent CPU/memory usage measurements.

Explicitly call log fields *captured tails*, not complete logs. Preserve unknown
usage as unknown; do not coerce it to zero. Canonical serialization should use
stable key order, explicit enum values and strict JSON (`allow_nan=False`).

## Consistency versus authenticity

Even an internally consistent successful snapshot is not independently trusted.
Before allowing local PR-body generation, at minimum check:

1. `success is True`, terminal state is `SUCCEEDED`, failure kind is absent,
   cleanup is confirmed and diff/attempt identity are nonempty.
2. Initial baseline contains every declared suite command, in order, with
   PASS, exit 0, positive executed test count and no assertion failures.
3. The declared reproduction was observed as ASSERTION_FAILED before any
   verification round, with positive exit and assertion-failure evidence.
4. Every subsequent verification round has the reproduction followed by all
   declared suite commands, in order. The final complete round is all PASS.
   Failed earlier complete rounds may remain as evidence; unknown or
   contradictory assessments must not be upgraded to success.
5. Known nonnegative token receipts and call/attempt counts reconcile; successful
   attempts cannot have unknown aggregate usage or missing call receipts.
6. Every command/assessment record is structurally valid. Exit status alone
   never proves PASS or an assertion failure.

Valid failed or unknown attempts remain reportable for diagnosis. They must not
produce a proposed contribution body. Malformed/contradictory data rejects;
legitimate failure is not a malformed report.

## Safe local rendering

Use fixed trusted section headings. Untrusted issue titles, commands, logs,
diffs, model metadata, notes and PR templates must remain literal data, never
control report structure or trigger operations. HTML-escape raw content and
choose fence delimiters longer than any matching delimiter in that content.
No raw HTML rendering, template execution, interpolation language, instruction
following, URL fetch or arbitrary Markdown insertion.

Show both an easy-to-review summary and complete bounded evidence. Include
failure status/unknowns conspicuously. Put repository PR-template content in a
separate inert reference block; interpreting project-specific template rules
belongs to later human/compliance work.

Replace the current unconditional disclosure (which falsely asserts human
review and isolated local tests) with a truthful AI-assistance statement:
local draft preparation only, human review pending, execution provenance
as declared above, compliance not yet verified. A generated body must never
claim that approval or real isolation has occurred.

## Three-case acceptance plan

No test execution has occurred for this increment. Implemented exactly three
new non-parametrized pytest cases in `tests/test_review_report.py`. The intended
single invocation was denied before dispatch; actual cases 0/10:

1. Successful synthetic evidence: correct baseline/reproduction/final suite;
   snapshot detached from input; full diff/hash, command order, model receipts
   and usage preserved; disclosure does not assert human review or isolation.
2. Failure and rejection: unknown/failed evidence remains visible and cannot
   generate a contribution body; missing final suite, failed cleanup, malformed
   identity, unknown successful usage, oversized and nonfinite data reject.
3. Rendering boundary: hostile HTML, Markdown fences/headings and instructions
   across metadata/log/diff/template remain inert data; no dispatch/filesystem
   or network capability is introduced.

These are separate report-feature cases, not wrappers around historical tests.
No old lifecycle/staging/snapshot tests, full suite, matrix, automatic retries,
or hidden fixture-code execution. A new report trial remains capped at 10 total
case executions across local/PR/main: planned 3 + 3 + 3 = 9, with future main
execution subject to the owner's merge decision. A test denial must stop that
specific action, not be rerouted through CI. No test or CI change is made by
this design document.

## Remaining work

Owner approved the exact Muse Wonder request; unchanged native ask:always
completed successfully with provider meta, model muse-wonder-1.0, attempts[].
Coordinator reviewed the design and implemented a frozen canonical-JSON report,
strict evidence validation, literal Markdown rendering, and three synthetic
acceptance cases. Unlike a tuple-based proposal, the immutable canonical string
is the single source of truth; parsed dictionaries are detached on every read.
Resource unknowns and incomplete receipts remain visible on failed attempts;
success requires complete reconciled receipts. CoderLoop always records complete
verification rounds, including all suite commands after assertion failures.

The new test command was denied before dispatch: SYSTEM_RUN_DENIED, approval
cannot safely bind this interpreter/runtime command. Tests executed: 0.
Whitespace check passed. CI is prepared for exactly the new three cases, but
must not execute them as a workaround for the blocked local test action.
The owner has explicitly requested publication as Ready for review despite this
limitation. A documentation-only `[skip ci]` publication commit keeps this review
publication separate from test execution; workflow definitions remain unchanged.
Resolve supported native approval binding before test verification. Ready for
review does not mean verified or merge-ready. Do not rerun prior lifecycle tests.
Actual runtime/transport/source acquisition, independent isolation proof,
compliance checks, approval-bound submission, and real end-to-end acceptance
remain separate unfinished milestones. Initial-version completion stays
provisional **35%**; this design alone does not increase it.

## 日本語要約

次の増分はレビュー用レポートとPR本文の**ローカル生成**です。
成功・失敗・使用量不明を分け、差分・コマンド・モデル受領を保存します。
模擬実行を実隔離の検証済みとは記載しません。生成は人間承認や
上流PR提出許可を意味せず、提出機能・collect-only設定は変更しません。
ローカル実装と新規テスト3件は保存済みです。正式承認後のMuse Wonder応答は
受領済みですが、新規テストは実行基盤の承認紐付けエラーで実行前に拒否され、
実行0件・未検証です。所有者の依頼により、`[skip ci]`でCI実行を抑止したうえで
Ready for reviewとして公開します。レビュー提出は検証成功・マージ許可を意味しません。

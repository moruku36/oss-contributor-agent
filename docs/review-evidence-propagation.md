# Review evidence propagation / レビュー証拠の伝播

The same injected `StructuredRunnerEvidenceAdapter` remains both runner and
assessor. Its accepted structured classification is copied into frozen, slotted
`RunnerEvidenceSummary` scalars in `TestAssessment.runner_evidence`, then carried
by `CheckEvidence.assessment` through `CoderLoop` and `CandidateAttempt` into the
detached `ReviewReport`. Literal Markdown includes the complete canonical JSON.
`prepare_draft_submission(outcome_or_report, ...)` automatically captures those
exact report and rendered body bytes in the existing `DraftRequest` envelope.
No second assessor, stdout classifier or opt-in propagation switch is added.

同じ注入済みアダプターが runner と assessor を兼ねます。受理した構造化判定を
変更不能なスカラー要約にコピーし、実際の CandidateAttempt から canonical JSON、
リテラル Markdown、準備済み DraftRequest まで自動伝播します。標準出力による
再判定や別 assessor は追加しません。

Each record binds attempt ID, dispatch ID, one-based generation, stage name,
complete stage-receipt SHA-256, exact command, result SHA-256, exit/duration,
classification, executed/passed/assertion/test-error/skipped/collection/infra
counts, explicit complete/truncated/known flags, and assessment-detail SHA-256.
The result digest is SHA-256 of ASCII JSON `[command, exit_code, stdout, stderr,
duration_seconds]`, with `ensure_ascii=True`, `allow_nan=False`, separators
`(',', ':')`. The scalar `binding_sha256` uses the same encoding with sorted keys
over all scalar fields except itself. Live checks recompute the full-result hash;
reconstruction verifies the scalar hash, check/attempt bindings and the shared
classification predicate. Original full output and manifests are not duplicated
in the report, so their digests cannot be independently recomputed there.

各要約は attempt・dispatch・generation・stage・receipt digest・正確な command と
result digest・分類と件数を束縛します。実行経路では完全な結果から digest を確認し、
再構築時は要約 digest、対応する check、順序、分類・件数・フラグの整合性を確認します。
完全な出力や manifest はレポートに再収録しないため、再構築だけではそれらの元データを
独立検証できません。

Limits: at most 64 strengthened checks, contiguous generations starting at 1,
unique dispatch IDs and stage names; IDs/stage names <=128 UTF-8 bytes, command
<=4096, detail <=4096, lowercase 64-hex digests, counts and combined test/event
total <=1,000,000, assertion failures <=128, finite duration <=2700 seconds.
Existing snapshot/render/request caps remain 524288/1048576/4194304 bytes.
Missing, malformed, duplicate or reordered required evidence rejects; no fallback
to legacy. A bounded failed prefix or failed verification can still render for
review, but cannot prepare a contribution request. An unassessed/malformed check
in a strengthened attempt may yield `report_failed`; no evidence is invented.

上限は構造化 check 64件、連続 generation、dispatch/stage の一意性、command 4096
UTF-8 bytes、detail 4096 bytes、件数合計100万、assertion 128件です。既存のサイズ・
時間・使用量上限は維持します。不足・重複・順序違反・不正値は拒否し、証拠を補完しません。
有限の失敗経路はレビュー可能ですが PR 対象にはなりません。構造化経路に未評価の
check が残れば `report_failed` になり得ます。

Compatibility: old positional constructors still work. Evidence-free attempts
retain `runner_evidence_version=0` and emit the original report schema 1 shape
and bytes (new optional fields are omitted). Genuine schema 1 reports/requests
remain readable/preparable under existing rules, explicitly without structured
runner guarantees. Schema 2 requires attempt evidence version 1 and a valid scalar
schema 1 record for **every** check, including failures. Unknown versions and mixed
legacy/strengthened records reject. Request envelope version stays 1. Candidate
preparation rebuilds its report from the detached attempt and compares exact
bytes; it cannot silently downgrade a retained strengthened attempt.

旧 positional API と schema 1 は維持し、過去の証拠を新形式に捏造しません。schema 2
では全 check の証拠が必須です。未知バージョンや混在は拒否します。単独の旧レポートは
旧ルールで扱うため、旧形式に書き換えられた入力の由来を識別する保証はありません。

All hashes are public consistency checks, not provenance, authentication,
authorization, isolation or hostile-Python protection. Rewriting all fields and
hashes can fabricate a standalone artifact. Execution kinds remain only
`synthetic` and `unverified`; human approval stays pending. No runner activation,
host processes, Docker, collector, production, authority or transport changes.
Historical UNKNOWNs and all existing test/model/spending budgets are unchanged.

digest は整合性確認に限り、真正性・認証・隔離の強化ではありません。実行種別は
`synthetic` / `unverified` のままです。過去の UNKNOWN と各種予算を変更しません。

Exactly six new self-contained methods are authored in
`tests/test_review_evidence_propagation.py`: actual full flow; command/generation
binding; assertion/pass/infra distinction; detached-byte mutation; missing/
duplicate/order and legacy compatibility; malformed/oversized and failed review.
Fixtures only compare/hash bytes and never execute fixture source. These cases
were **NOT RUN** by the file-only implementer; the coordinator owns one invocation.

新規 unittest は独立した6メソッドのみです。fixture はバイト比較のみで実行しません。
ファイル編集担当は未実行です。コーディネーターが一度だけ実行します。

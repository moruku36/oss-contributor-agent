# Structured runner evidence / 構造化runner証拠

## Implemented injection seam

`StructuredRunnerEvidenceAdapter` in `src/agent/structured_runner_evidence.py` implements both the staged runner and snapshot assessor. Supply the **same instance** as `StagedAttemptAdapter(runner=evidence, ...)` and as `SnapshotWorkspace(assessor=evidence, ...)` inside its workspace factory. Existing `CandidateAttempt` then uses it through the real loop, staging bridge and report path. There is no default backend or production CLI wiring.

The explicit trusted backend implements `run_structured(ticket, *, timeout_seconds, max_output_bytes)` and returns `StructuredRun(ticket, result, summary)`. It must execute the exact command against the exact ticket generation once, enforce bounds in flight, return a new `CommandResult`, and report authoritative structured events. The adapter validates the entire echoed ticket (attempt, dispatch nonce, generation, complete manifest/receipt digest and command), exact result identity and shape, and summary. It retains only the latest assessment and bounded prior result identities. Unknown dispatch errors are terminal, with no retry.

`StructuredTestSummary` counts executed tests separately from skips: `tests_run = passed + len(assertions) + test_errors`. Each `AssertionFailure` identifies one failed test with assertion detail. Duplicate identities, invalid counts/types, excessive detail and missing explicit boolean completion/capture/knowledge flags are rejected. Test, collection or infrastructure errors yield `INFRA_ERROR`, including mixed assertion failures. Incomplete, truncated, unknown, empty or all-skipped runs yield `UNKNOWN`. Only complete known runs with executed tests and no infrastructure errors can pass or report assertions; contradictory exit codes yield `UNKNOWN`. stdout and exit status alone never establish an assertion failure. Passing tests with some skipped tests remain passing; skip counts appear in detail.

Assessment accepts only the exact latest result once, for its attempt, and rejects mutation including equal-valued type changes. The existing staging bridge additionally refuses changed snapshots and superseded generations. A too-small assessment detail bound consumes assessment and fails closed.

## Verification and trust boundary

Nine new self-contained offline scenario methods passed once (0.032s), including complete synthetic candidate baseline → reproduction → byte patch → verification → pending-human report. Fixtures hash/compare bytes and never execute staged source. This does not establish actual process execution, Docker isolation, source acquisition, authenticated provenance or real test instrumentation. Ticket/digest echoes establish consistency of an independently trusted backend, not cryptographic provenance. Python objects and privacy are not hostile-code security boundaries. Python deadline checks cannot preempt blocked dependencies. Single-caller, one attempt, at most 64 dispatches; no crash durability. Existing `execution_kind` remains `synthetic` or `unverified`; production targets remain collect-only.

Source/tests were saved by the selected direct Codex worker before its outer 600s timeout. No matching task_complete/durable final was available; worker completion is UNKNOWN and was not replayed. Coordinator reviewed the saved draft, ran only the new module and supplied this documentation. This is a locally checked draft, not completed worker-execution acceptance.

## 日本語

同じ`StructuredRunnerEvidenceAdapter`をstaged runnerとSnapshotWorkspaceのassessorに注入します。既存CandidateAttemptから実際のloop・世代書出し・reportまで接続されます。明示的な信頼backendが正確なticket世代とcommandを1回だけ実行し、上限を実行中に守り、全ticket・新しいCommandResult・構造化summaryを返す責任を負います。adapter自身は実行もstdout解析もしません。

実行済み件数とskipを分離し、失敗test識別子とassertion詳細を要求します。test/収集/基盤エラー混在はINFRA_ERROR、不完全・切詰め・不明・空・全skip・exit矛盾はUNKNOWNです。正常完了かつ実行済みtestがある場合のみPASSまたはASSERTION_FAILEDになります。一部skipがあっても他が成功ならPASSです。最新の正確なresultだけ1回判定し、改変・再送・古い世代は拒否します。既存bridgeがsnapshot変更も拒否します。

新規9シナリオを1回実行し成功（0.032秒）。fixtureはbytes比較のみでsourceを実行せず、実Docker・実process・認証・隔離を証明しません。ticket/digestは信頼backendの整合性であり暗号学的来歴ではありません。単一caller・単一attempt・最大64dispatch、crash永続性なし。execution_kindはsynthetic/unverifiedのまま、本番5対象はcollect-onlyです。

選択Codexがsource/testを保存後、外側600秒timeoutとなりました。task_completeと最終出力は得られず完了はUNKNOWN、再実行なし。coordinatorが保存draftをreviewし新規testのみ検証、本書を補足しました。worker完了や本番受入れは未確認です。

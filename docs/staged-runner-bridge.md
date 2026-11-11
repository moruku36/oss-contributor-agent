# Staged runner bridge / 書出し世代とrunnerの受渡しgate

## Scope

`src/agent/staged_runner_bridge.py` is a reviewed-draft, injection-only gate between the memory-only [SnapshotWorkspace](snapshot-workspace.md), the [directory-FD staging](filesystem-staging.md) helpers and an injected trusted runner. It executes nothing itself: no host command, subprocess, Docker access, acquisition, credentials, model wiring, CLI or configuration. Core lifecycle contracts (`CoderLoop`, `Sandbox`, `FilesystemStaging`) are unchanged.

## Contract

- **Capabilities are explicit.** The caller supplies the attempt ID, the snapshot, an already-open staging-parent directory FD, the file/role manifest, limits and a trusted `StagedRunner`. The bridge duplicates the FD and never closes the caller's.
- **One fresh generation per command.** `execute` exports the current snapshot, revalidates it with the snapshot rules and the manifest, and writes it into a new private `stage-…` directory with the same no-follow, exclusive-create and readback checks as one-shot staging. Earlier generations are never modified or reused.
- **Bound dispatch.** The runner receives a `DispatchTicket`: attempt, a fresh dispatch nonce, generation number, the complete `StageReceipt`, its digest and the command. It is called exactly once. Its `RunnerEvidence` must echo attempt, nonce, stage name and receipt digest, and carry a new exact `CommandResult` that passes the same shape, duration and output-bound checks as `Sandbox`.
- **Current-only assessment.** `assess` forwards to the snapshot's trusted assessor only if the result is the exact object of the latest dispatch, has not been assessed, is unmutated, and the snapshot still hashes to the staged receipt. A result staged before a patch, a superseded or replayed result, or an equal-looking copy is rejected with `EvidenceRejected` without calling the assessor.
- **Bounds.** Stage deadline at most 30 s, command timeout at most 2700 s, combined output at most 524,288 bytes, at most 64 dispatches per bridge. Staging time counts against the command timeout.
- **Terminal failures.** A dispatch is consumed before the generation directory is created. Any later failure (partial write, runner error or timeout, late or mismatched evidence) raises `DispatchError` with a `partial_or_unknown` receipt naming the phase and the complete or incomplete stage. Every later call re-raises that receipt. There is no retry, rollback or deletion; generations remain under the staging parent for the owning backend or operator. Only the duplicated FD is closed.
- **Rejections are not terminal.** A rejected stale, replayed or mismatched result leaves the bridge usable. An assessor failure consumes that result (it cannot be assessed again) but does not end the bridge.

## Assumptions

- The staging parent is private, stable and exclusively controlled by the caller, as for one-shot staging.
- The runner and assessor are trusted adapters that enforce their own in-flight bounds. The bridge is single-caller and not thread-safe.
- The snapshot's attempt ID is the one the eventual sandbox uses.

## Not provided / unfinished

- **No isolation evidence.** An echoed ticket shows a trusted runner's report is consistent, not that the command ran against the staged generation, in a container, or without network. Tests use a fake runner that only reads staged files.
- No real runner or sandbox backend, no binding of a generation into a container, no removal of generations.
- No wiring into `CoderLoop`/`Sandbox`. `execute` and `assess` mirror `SandboxBackend.execute` and `Workspace.assess` so a future trusted adapter can delegate to them.
- Python deadline checks cannot preempt blocked I/O or a blocked runner/assessor. No crash durability.
- The bridge reuses private helpers of `filesystem_staging`; a shared public helper is a possible follow-up.

## 日本語

コマンドごとに現在のsnapshotを新しいprivate世代へ書き出し、注入された信頼runnerへ1回だけ受け渡すgateです。attempt、nonce、完全なStageReceiptとそのdigest、正確なCommandResultを照合し、最新・未判定・未改変で、snapshotが書出し時と同一の場合だけ判定器へ渡します。古い世代、再送、不一致の結果は判定しません。

dispatch消費後の失敗（途中書込み、runnerの失敗・timeout、不一致）は`partial_or_unknown`で終端し、自動再送、偽rollback、削除はしません。残った世代はbackend／operatorの照合対象です。実コマンド実行、Docker、取得、本番接続は含まず、実隔離の証明ではありません。

[Phase 2 plan / 計画](phase2-plan.ja.md) · [Architecture](architecture.md) · [filesystem仕様](filesystem-staging.md)

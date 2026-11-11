# Snapshot workspace / スナップショットworkspace

## Implemented boundary

`src/agent/snapshot_workspace.py` implements bounded regular-text snapshots in memory. It does **not** open files, clone a repository, run source, launch a process or configure a runtime backend. The existing Git/Path-based helpers remain unimplemented; this is not a host-filesystem fallback.

A trusted caller supplies:

- A specific attempt ID, explicit `TextFile(path, text, role)` entries, finite `SnapshotLimits`, editable paths and context paths.
- Roles `source`, `test` or `runner`. Only explicitly selected source files may change; tests/runner evidence must be identified accurately by the caller. Filename checks are additional restrictions, not a substitute for correct roles.
- A trusted `RunnerAssessor` that recognizes the exact command result and attempt using authoritative evidence. The workspace does not infer assertion failures from an exit code or log text.

Read, literal search, context and patch operations use the same conservative portable ASCII namespace. Absolute/drive/backslash/dot paths, hidden/metadata components, device names, case aliases and file/directory collisions are rejected. Input entries have no mode/link/reference representation; symlink and hardlink import is unsupported, not resolved. UTF-8 text allows LF and tab but rejects other controls and invalid Unicode.

Outputs are complete and bounded or refused, never silently truncated. Context contains only caller-selected paths. Literal search does not execute regular expressions.

## Strict patch subset

- Existing files only: exact `--- a/path` and `+++ b/path` headers, with matching paths.
- Exact hunk positions, counts and old text; ordered, nonoverlapping hunks with unique nonempty old context. Insertions into nonempty files require old context.
- LF records and standard end-of-file no-newline markers.
- No fuzz, preamble, timestamps, binary/mode/link changes, renames, file additions/deletions or context-only hunks.
- Every affected file must be editable. The whole proposal is validated against a temporary snapshot; any rejection leaves the current snapshot unchanged.
- Complete sorted base-to-current diffs use whole-file replacement hunks. Output overflow refuses export instead of truncating it.

Attempt-ID checks prevent accidental cross-attempt calls; they do **not** establish actual binding to an isolated sandbox. Python deadline checks and finite data limits do not preempt a blocked trusted assessor. Real runner authentication, filesystem/link enforcement, source/dependency acquisition, Docker isolation and integration with production CLI remain separate unfinished work.

## 日本語

メモリ上の通常テキストだけを扱う実装です。実ファイル、Git取得、ソース実行、process起動、Docker接続は行いません。既存のPath／Git helperをホスト実行へ置き換えていません。

呼出し側がファイルとrole、編集許可、context対象、attempt ID、上限、信頼するrunner判定器を明示します。テスト／runner証拠は編集不可で、sourceでも明示許可が必要です。roleの正しい分類は呼出し側の責務です。実受領とattemptに束縛した判定は将来の信頼するadapterが担い、終了コードや文章だけで失敗を認定しません。

パスは保守的なASCII相対パスのみ。境界外・隠し／metadata・曖昧な大小文字・file/dir衝突・特殊entryは拒否します。linkを解決するfilesystem実装ではありません。read／検索／context／patchに同じ名前空間と有限サイズ制限を使い、出力を切り詰めて成功扱いにしません。

patchは上記の厳格な部分集合に限定します。変更前の本文・行位置・行数・編集許可を全ファイル分検証してから一括反映し、途中の拒否で部分変更を残しません。合成integrationは実CoderLoop／Sandboxの制御と同じsnapshotを使いますが、実Docker隔離・実モデルの修正能力の証拠ではありません。

[Phase 2 plan / 計画](phase2-plan.ja.md) · [Architecture](architecture.md) · [アーキテクチャ](architecture.ja.md)

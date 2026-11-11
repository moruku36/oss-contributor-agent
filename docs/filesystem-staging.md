# Directory-FD import and staging / filesystem取込み・書出し

## Scope and trust boundary

This adapter connects prepared regular text files to the memory-only [SnapshotWorkspace](snapshot-workspace.md). It receives **already-open directory file descriptors** from a trusted owner/backend; it does not choose arbitrary host root paths or recursively discover a repository. The caller must provide private/exclusively owned, stable, disjoint source and staging-parent directory trees, an explicit finite file/role manifest, editable/context selections and an attempt ID. Identical roots are rejected; ancestry or hostile mount aliases are not proved by these checks. Keep caller FDs open until import duplicates them.

An FD is a filesystem capability, not proof of container isolation, source provenance or absence of hostile concurrent writers. Unit tests with real temporary files prove selected local file operations, not Docker enforcement. Real runtime wiring and runner/provider execution remain unfinished. Legacy Path/Git helpers and production collect-only settings are not activated.

## Import

Validate manifest paths, roles and selections against the existing snapshot namespace before file access. Traverse relative to directory FDs with no-follow checks, accepting only regular single-link files. Reject symlink components/leaves, hardlinks, nonregular entries, missing/ambiguous paths, unsupported UTF-8 and size-limit violations.

Use bounded complete reads and check file/stat identity and stability around access. Own only duplicated/opened descriptors; never close caller-owned FDs. Use the adapter as a context manager or close it explicitly if staging is abandoned. No fallback that follows a path, weakens a check or imports unsupported entries.

The result is the actual SnapshotWorkspace: strict patch application and protected test/runner roles continue to apply in memory.

## One-shot staging

Write current snapshot contents into a **new, exclusively created private directory** beneath a separately supplied trusted parent FD. Never modify prepared source files. Use restrictive permissions, no-follow/exclusive creation, complete bounded writes and readback verification.

A complete receipt identifies the attempt, relative stage directory, exact paths/content hashes and total bytes. It is issued only when all files are written and verified. It is not a Git commit or runner result.

Mark the staging attempt single-use before effects. On partial/uncertain failure, expose the stage name and explicit unsuccessful outcome; do not return success, blindly retry, claim rollback or recursively delete an arbitrary host directory. Partial output remains for the owning sandbox/backend or operator to reconcile and clean up. Owned descriptors still close on error.

Each filesystem operation has a positive finite deadline of at most 30 seconds. Finite data/deadline checks do not preempt blocked filesystem IO or establish crash durability. Hard in-flight enforcement, actual sandbox binding, source/dependency acquisition, authoritative real test-runner receipts and actual isolation acceptance remain separate work.

## 日本語

明示されたファイル一覧と、信頼する呼出し側が既に開いたdirectory FDを受け取るadapterです。任意のホストrootの選択、再帰探索、process実行、Docker設定、本番接続は行いません。呼出し側が占有・管理する安定した、互いに交わらないsource／staging-parent領域を前提にします。同じrootは拒否しますが祖先関係や敵対的mount aliasは証明しません。FDの一致だけで実隔離や敵対的な同時変更への耐性を証明しません。

取込みはsnapshotと同じpath／role規則で事前検証し、FD相対のnofollowアクセスで通常・単一linkのテキストだけを有限サイズで読みます。link・特殊file・不正UTF-8・上限超過・観測した変更は拒否します。caller所有FDは閉じません。

修正後は元のソースを上書きせず、別parent FD配下の新規private directoryへ書き出します。全ファイルの完全書込みとreadbackを確認してから、attempt・stage名・path/hash・byte総量の受領を返します。

複数ファイルのfilesystem書出しは、メモリsnapshotの一括反映と同じ原子性を保証しません。途中失敗ではstage名と未完了状態を残して停止し、自動再送・偽rollback・任意host directoryの再帰削除はしません。残った出力はsandbox/backend／operatorの照合・後片付け対象です。

[Phase 2 plan / 計画](phase2-plan.ja.md) · [Architecture](architecture.md) · [アーキテクチャ](architecture.ja.md)

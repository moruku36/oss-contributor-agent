# Local review package / ローカルレビューパッケージ

`src.review.review_package` persists an **actual validated prepared
DraftRequest** for local human review. First call `prepare_draft_submission`
with the actual CandidateAttempt outcome or ReviewReport and explicit target;
then `export_review_package(request, parent_fd=fd, name="review-1")`.
`read_review_package(parent_fd=fd, name="review-1")` returns a detached
`LocalReviewPackage`; `.request` and `.report` each return fresh domain views.
There is no approval, submission, authority, transport or journal operation.

呼出側は実際の CandidateAttempt 結果または ReviewReport と明示的な宛先から
DraftRequest を準備し、上記 API へ渡します。保存後の読出し結果はレビュー専用の
独立した値です。承認・送信・ジャーナル更新は行いません。

The caller must provide an **already-open, caller-owned mode-0700 directory FD**
in a trusted filesystem, with a trusted process and exclusive control: no
concurrent hostile mutations, mounts or aliases. These are prerequisites, not
security/isolation claims established by this module. The FD remains caller-owned.
Only one explicit ASCII child name (1–64 alphanumeric/underscore/hyphen characters,
starting alphanumeric) is accepted. No host paths or source discovery exist.
Unsupported POSIX descriptor/no-follow operations fail closed.

信頼できるプロセス・ファイルシステムと、呼出側所有の既存の非公開ディレクトリ FD
（0700）が前提です。同時変更・敵対的なマウントや別名参照は許容しません。
FD の所有権は呼出側に残ります。パスや探索は受け付けず、単純な子名だけを使います。

Exactly seven private regular files (0600, one hard link) are accepted:

| File | Contents / 内容 | Maximum bytes |
| --- | --- | ---: |
| request.json | Exact canonical DraftRequest snapshot / 正確な要求スナップショット | 4,194,304 |
| report.json | Exact report JSON / レポート JSON | 524,288 |
| report.md | Exact human report / 人向けレポート | 1,048,576 |
| body.md | Exact prepared body including template / 準備済み本文 | 1,048,576 |
| diff.patch | Exact diff / 差分 | 131,072 |
| manifest.json | Canonical schema 1, exact five names, sizes, SHA-256, request hash, payload total | 4,096 |
| COMPLETE | Canonical schema 1 and manifest SHA-256 / 完了印 | 256 |

The total cap is **6,291,456 bytes**, including metadata files. Before any read,
the reader checks the entire fixed set and all stat sizes against per-file and
total caps. Reads are descriptor-relative, no-follow, bounded in 64 KiB chunks;
symlinks, nonregular files, hard links and extra entries are rejected. UTF-8,
canonical manifest schema/types, exact lengths/digests, actual DraftRequest
reconstruction/revalidation, and exact rendered report/body/diff are required.
Public hashes detect inconsistency; they authenticate neither evidence nor
target/head/base contents. A coherently replaced valid proposal is still a
different proposal requiring independent human review.

合計上限はメタデータ込み 6,291,456 バイトです。全ファイルの種類・サイズと合計を
読出し前に検査します。UTF-8、manifest の厳密な形式・長さ・ハッシュ、実際の要求の
再構築・再検証、および全派生成果物の完全一致が必要です。ハッシュは真正性や
宛先・head/base の実内容を証明せず、人による独立したレビューが必要です。

Export performs one exclusive mkdir and exclusive no-follow file creation,
internally reads back and revalidates the six data/manifest files, then writes
`COMPLETE` **last**. It never overwrites, updates, deletes, rolls back or retries.
An export error before completion carries `ReviewPackageExportError.receipt` with
the child name, whether mkdir succeeded, and attempted file. Partial files are
retained; absent/partial completion markers cannot be read as complete. An
existing destination (including a symlink) is rejected without changing it.
There is no retry/recovery API; retain and reconcile the receipt locally.
The full marker write is the completion point. A subsequent descriptor-close
error carries a **complete** receipt, preserving that distinction; do not retry.

新規 mkdir と排他的作成だけを行い、内部読出し検証後に `COMPLETE` を最後に書きます。
上書き・更新・削除・巻戻し・再試行はありません。失敗時は子名と作成状況を含む
不完全 receipt と部分ファイルを保持します。完了印の欠落・不完全な内容は拒否します。
完了印の全バイト書込み後の close エラーは完了 receipt を保持し、再試行しません。

This is persistence across ordinary close/reopen, **not an atomic multi-file
transaction or a power-loss/crash durability guarantee**: no fsync guarantees
are supplied. Byte bounds do not preempt blocked filesystem I/O. No production
runner, authentication, reviewer, collector, default or historical budget/UNKNOWN
state changes.
The six new unittest methods are authored in `tests/test_review_package.py`;
they were **NOT RUN** by the file-only implementer. The coordinator runs them once.

通常の close/reopen を越える保存であり、複数ファイルの原子的処理や停電・クラッシュ時の
永続性は保証しません（fsync 保証なし）。I/O の停止時間も制限しません。
過去の UNKNOWN・予算は変更していません。
新規テストは正確に6メソッドを作成済み・未実行で、実行は coordinator が1回だけ行います。

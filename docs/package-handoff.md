# Review package handoff / レビューパッケージの引き渡し

`ReviewPackageSubmission(parent_fd, name, expected_request, *, inspector,
authority, transport, journal, operation_id)` composes the existing local and
durable boundaries. Supply a private, exclusively controlled parent directory
descriptor, its package child name, an independently retained prepared
`DraftRequest`, a read-only `TargetInspector`, independently trusted existing
`HumanApprovalAuthority` and `DraftOnlyTransport`, an exact
`SQLiteSubmissionJournal`, and an explicit stable operation ID. Keep the fd open
through `run()`. All strict package-reader prerequisites still apply.

Construction validates and captures the expected snapshot, preconstructs
`DraftSubmission` with its captured authority/transport methods, then binds the
inspector. Invalid expected input looks up none of these dependencies. Construction
neither reads the package nor invokes `inspect_target`, `authorize_and_consume`,
or `submit_draft`; method lookup may run trusted dependency property getters.
An inspector getter cannot replace the authority/transport methods already captured.
Each instance permits one `run()`, including failure, cancellation and reentry.
Run performs actual
strict selection, actual preflight on those same bytes, then invokes the existing
durable submission only for local `READY`. There is no caller-supplied READY,
unjournaled path, retry, fallback, recovery or reset.

Invalid expected bytes or packages reject before observation and journal claim.
Inspector exceptions/malformed observations yield `UNKNOWN`; unsupported
capabilities reject. Neither result dispatches or claims the journal. Local
preflight never grants authority or closes the race with target changes: the
transport must independently enforce atomic head/base identity checks at creation.
Underlying submission outcomes, journal exceptions, cancellation and durable
replay rejection remain intact. `outcome` exposes the underlying conservative
`UNKNOWN` when persistence or cancellation interrupts submission, even after a
successful transport reply. Ambiguous journal state never reports submitted.

非公開で排他的に管理する親ディレクトリの fd、子パッケージ名、独立して保持した
期待リクエスト、読み取り専用 inspector、別途信頼する承認 authority と draft 専用
transport、厳密に `SQLiteSubmissionJournal` の実体、安定した操作 ID が必要です。
fd は実行終了まで開いておきます。構築時に期待値を検証・固定し、既存の提出処理で
authority/transport のメソッドを先に固定してから inspector を束縛します。不正な
期待値では依存メソッドを参照しません。構築時に inspect/authorize/submit は呼び
出しませんが、メソッド参照時に信頼する依存先の property getter が動く場合が
あります。実行時に実際の厳密な選択と同一バイト列の preflight を行い、ローカル
`READY` の場合のみ既存の永続化付き提出処理へ進みます。

不正な期待値・パッケージは拒否し、観測の例外・不正な応答は `UNKNOWN`、未対応の
機能は拒否します。これらでは承認・送信・journal の claim を行いません。
preflight は承認ではなく、transport による作成時の head/base の原子的な再確認も
必須です。失敗・キャンセルを含め一度だけ実行でき、再試行・復旧・リセットは
ありません。提出後の永続化失敗やキャンセルでは、元の保守的な `UNKNOWN` と例外を
保持します。ネットワーク、承認、認証の実装は追加しません。

Six self-contained local unittest methods are authored in
`tests/test_package_handoff.py`; they were not run for this change.
自己完結したローカル unittest を6メソッド作成しましたが、今回は実行していません。

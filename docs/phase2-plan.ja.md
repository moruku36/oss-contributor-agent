# Phase 2 実装計画（2026-11-11、directory-FD filesystem増分）

## 現在地と順序

- Phase 1 は稼働中。ユーザー systemd タイマーで毎日06:30 JSTに収集する。
- coordinatorの読取専用timer／service／digest／ledger確認で11/8〜11/11の日次収集成功、新規・判定・pending は毎回0。観察チェックポイントは完了。歴史的候補363件には最終観測11/1の OpenClaw 319件が含まれ、修正対象選定や上流作業許可を意味しない。
- sandbox lifecycle／CoderLoop／snapshotはPR #2〜#4でオーナーマージ済み。今回は明示directory FDによる実ファイル取込みと、修正後snapshotの新規領域への検証付き書出しを実装。実一時filesystemの新規3ケースがローカル1起動で合格。実runner／provider／Docker隔離・取得工程・書出し世代の制御・本番CLI接続は未完了・未検証。
- `scripts/run_agent.py` のcollect-onlyゲートを維持する。開発完了と本番対象のfull化は別の判断で、設定を自動変更しない。
- PR下書きはローカル成果物を意味する。GitHub上のdraft PR作成も提出に当たり、本人判断の前に実行しない。

## 1. 観察と運用基準の確立

基準値は [運用記録 §9](operations.ja.md#9-日次の確認方法と日付2026-11-08-引き継ぎ確認) を参照。
次回以降も、タイマーの起動日時、成果物の更新時刻、UTC日付のレポート名、対象別状態、新規・候補・pending数を記録する。
既存候補は終端判定として自動再採点されない。候補の鮮度や新スコアリングの効果は、新規判定と保存済みプールを区別して評価する。
失敗が継続する場合はPhase 2より運用復旧を優先する。今回は既存4日分の観察結果を読取確認し、GitHubで候補3件の状態も標本確認しました。収集再実行・再採点・上流への投稿はしていません。

## 2. 実装を分ける単位

### A. 隔離実行の契約とライフサイクル

対象: `src/sandbox/docker_manager.py`、`src/sandbox/git_workspace.py`。

- 1試行1コンテナ、認証情報なし、非root、CPU・メモリ・実行時間の上限。
- ソース取得・依存導入と、ネットワークを閉じたテスト実行を分離する。依存取得先の制御方法を明確にするまで、任意ネットワーク有効化で代用しない。
- 起動、コマンド実行、結果収集、例外・タイムアウト時の後片付けを狭いインターフェースとして定義する。
- ホストシェルへのフォールバックや認証ファイルのマウントを提供しない。
- 初期テストは偽の実行アダプターでライフサイクルを検証する。実Docker試験を未実施のまま「隔離検証済み」としない。
- midorunではDockerコマンドが見つからない。Docker導入・socketアクセス・ネットワーク例外要求を今回の準備に混ぜない。

受入条件: 正常・異常・起動途中失敗で終了処理が確認でき、タイムアウトと後片付け失敗が成功に化けない。権限や資源上限を緩める再試行をしない。

先行sandbox実装（mock契約は検証済み、実Docker受入は未実施）:

- `SandboxBackend` は create/start/execute/remove の4操作。create 前に選んだ ID を全操作で使い、不確実な作成でも remove 対象を失わない。実装の自動選択・subprocess・ホストシェル fallback はない。
- image は非空・NUL不可、CPU・時間は正の有限数、メモリ・コマンド数・入力/出力バイト上限は正の整数として検証。bool・数値文字列等は拒否。policy は immutable、network=none・非root・read-only root 固定。host mount・認証情報・環境継承は禁止する backend 契約。
- 既存の構築・context manager・run・CommandResult API を維持し、keyword 引数で backend と上限を指定。資源属性は読取専用。backend 未設定で実行を拒否し、CLI には接続しない。
- 試行全体の残り時間とコマンド期限の小さい方を渡す。create/start/remove も期限付きで、期限切れ後の cleanup には独立した猶予を渡す。例外・timeout・上限超過で remove を試み、同じ試行を再利用しない。
- remove 失敗は元の例外と cleanup 例外・ID を保持した明示エラー。作成や削除が不確実な場合、成功扱いも自動再試行もせず、人間が状態を照合する必要がある。
- 上限の実行中の強制・出力収集のメモリ制限・idle 時の寿命は backend の責務。Python の事後検査だけでは停止や隔離を保証できない。実 adapter、ディスク上限、設定連携、ソース取得は今回の対象外。

### B. ファイル操作とワークスペース境界

対象: `src/agent/snapshot_workspace.py` と `src/agent/tools.py` の明示snapshot wrapper。

先行snapshotはメモリ上の通常テキストを扱います。今回、別module `src/agent/filesystem_staging.py` に信頼する明示directory-FD capabilityからの取込み／新規書出しを実装しました。任意Path／Git APIは未実装のままです。[snapshot仕様](snapshot-workspace.md)／[filesystem仕様（日英）](filesystem-staging.md)を参照。

- `ManifestFile`のpath／role／選択範囲をsnapshot規則でfilesystemアクセス前に検証。指定fileだけをnofollow・FD相対で開き、通常・単一link・UTF-8・実byte数・statの安定性を確認します。未指定fileの探索はしません。
- `FilesystemStaging.import_workspace`が実SnapshotWorkspaceを返し、従来の保護role／厳格patchを適用。新しい `export_files` は現在の全fileと元roleを総量上限内で返し、通常readの出力上限を理由にstageが切り詰められないようにします。
- `stage`は同じattempt／importしたobjectだけを受け付け、別のprivate/disjoint親FDの下に新規領域を1回だけ作成。元ソースは不変です。private mode・排他作成・完全write・readback／hash照合が済んだfileの受領を返します。
- 書出しはfilesystem上の複数file atomic commitではありません。dispatch前に単一使用を記録し、途中／不明結果はstage名・検証済prefix・試行中pathを保持した `IncompleteStageReceipt` と明示例外で停止。再送・偽rollback・再帰削除はしません。残存stageの照合／cleanupはsandbox/backend／operator責務。
- caller所有FDは閉じず、複製・内部FDを解放。未stageで中止する場合もcontext manager／closeを使います。占有・安定・disjointなrootが前提で、FD同一性だけでは祖先関係・敵対的mount・実コンテナ束縛を証明しません。最大30秒の事後期限検査はblocked IOの強制停止を保証せず、crash durabilityも対象外。

- `TextFile`のpath/text/roleを検証し、通常テキスト以外のentry・symlink/hardlink・metadata・曖昧なパス・file/dir衝突を拒否。roleがtest／runnerのファイルと、明示編集許可のないsourceは不変です。roleの正しい分類は呼出し側の責務。
- 同じパス名前空間でread／リテラル検索／選択context／patch／完全diffを扱い、UTF-8・file数・総量・個別file・path・hunk・出力上限を検証。検索でregexを実行せず、超過は切捨てでなく拒否。
- patchは既存fileの厳格なunified diff部分集合。変更前本文、行位置、行数、曖昧なcontext、重複path、編集許可を全file分検証し、候補snapshotを一括commit。拒否時は変更を残しません。追加／削除／rename／mode変更等は対象外。
- attempt IDを照合し、runner判定は明示した信頼するassessorへ委譲。同じIDの文字列だけでは実sandbox束縛を証明せず、終了コードや文章だけでassertion失敗を推測しません。

- 絶対パス、`..`、シンボリックリンクによる境界逸脱を拒否する。
- read/write/search/patchの各操作で同じ境界を使う。パッチの各ファイルパスも検証する。
- Git準備・diffの取得は隔離側へ委譲し、任意のホストディレクトリを作業先にできないようにする。

今回の受入: 実一時fileで取込み→実snapshot修正→新規書出し→byte／hash／modeを照合。link／特殊file／UTF-8／サイズ／観測した読取中変更／attempt違いを拒否し、途中実write失敗では受領保持・再送拒否・FD解放を検証しました。実runner／Docker隔離の受入ではありません。

### C. TDD Coder Loopの状態機械

対象: `src/agent/core.py` と新規 `tests/test_coder_loop_state_machine.py`。旧 `tests/test_patch_loop.py` は残すが今回実行しない。

今回の実装契約:

- `PatchModel`／`Workspace` protocol と明示 `TestPlan` を注入し、未設定は実行前に拒否する。実provider・ホストファイルadapter・設定連携はない。
- callerが準備したソースと保護する再現／関連スイート全体の明示コマンドだけを使う。自動探索・再現テスト作成は未実装。基準スイートが成功し、再現が実際のassertion失敗と確認できるまでmodelを呼ばない。
- `Workspace.assess` は同一attempt／CommandResultに束縛したrunner証拠を解釈する信頼境界。単なる非zero終了・collection/import異常・全skip・曖昧な結果を再現失敗としない。将来の実adapterで独立検証が必要。
- patchはworkspaceだけで適用し、パス・リンク・変更範囲とテスト保護をadapterで検証。適用がliteral Trueで確認できない場合や例外は停止し、再送・偽rollbackをしない。
- 各修正後に再現と宣言した関連スイート全コマンドを再確認する。失敗時は上限内の失敗ログを次の許可済み試行に渡す。差分取得とsandbox teardownが確認できて初めて成功する。
- 試行・model呼出し・累積token・実時間・文字列サイズの上限を設定し、残量をadapterへ渡す。使用量不明は `tokens_used=None` と既知prefixを区別し再試行しない。超過既知値も記録して適用前に止める。
- Python事後検査は実行中の強制停止や隔離を保証しない。model/workspace/backendのin-flight制限と真正な使用量／runner受領はadapter責務。cleanup猶予は別の有限時間。

- モデル呼出し・ファイル操作・テスト実行を注入可能にし、最初は決定的な偽アダプターだけで制御を検証する。
- 既存テストの基準状態を確認し、再現テストが失敗することを確認してから最小修正に進む。
- 再現不能、壊れたテスト実行環境、テストの揺らぎを成功扱いにしない。
- 関連スイート全体が成功したときだけ成功結果を返す。新規テストだけの成功では不十分。
- 試行回数、消費トークン、実時間の上限を全て確認し、失敗時は理由・実行コマンド・出力末尾・使用回数を結果へ残す。

受入条件: 再現不能でモデル修正を呼ばない、失敗出力を次の修正に渡す、上限で必ず止まる、予算・タイムアウト・実行例外が成功にならないことをテストする。

### D. 合成リポジトリでの統合検証

- 公開OSSの候補ではなく、小さな合成fixtureを使う。今回、実CoderLoop／Sandbox／SnapshotWorkspaceと合成model／runnerを接続し、同じsnapshotへのsource変更が再現をassertion失敗から成功へ変え、関連スイート2コマンドの再検証と合成cleanupまで通ることを確認。fixtureソースのeval／execやprocess起動はしない。
- ドキュメント・typoの狭い演習から始め、再現・修正・全体テスト・diff出力・後片付けを通す。
- credential非搭載、ネットワーク制約、資源上限、タイムアウトを実際の隔離環境で検証する。
- CLIに組み込む前後でcollect-onlyゲートを回帰確認する。

受入条件: 単体テストと実隔離試験の結果を区別して記録し、scaffoldが残る経路を有効化しない。Phase 1からパッチ生成への接続を追加しない。

## 3. Phase 3へ渡す成果物

Issue URLと要約、base commit、差分、実行コマンドと結果、失敗・試行回数・資源使用、対象ルールの確認項目をレビュー可能なローカルレポートにまとめる。
CONTRIBUTING・ライセンス・CLA/DCO・既存PR重複・AI開示を確認し、本人へ提示する。[PR提出ゲート](decisions/2026-11-07-pr-gate.ja.md)は維持する。

## 4. 今回の検証と実行予算

先行試行はいずれも各3固有ケース・local／PR／main各1起動、計9/10実行で封印しました。今回再実行しません。

| 試行 | マージ済みPR | main CI | mainの実績 |
| --- | --- | --- | --- |
| lifecycle | #2 | 42928600276 | Python3.12、3passed0.52s |
| CoderLoop | #3 | 42931545581 | Python3.12.16、3passed0.31s |
| snapshot | #4 | 42933736802 | Python3.12.16、3passed0.05s |

PR #4のmerge SHAは `eb600fe40684d4374f099f5c46b3c2e35d191301`。
branch／command／session変更だけで予算をリセットしません。

今回の別機能増分（filesystem import/staging）は **ローカル1起動・新規3ケース実行・全件合格**
（Python3.13.18、0.04秒）。現在local3/10、今回のPR CIは未確認です。
PR1回3件と将来main1回3件を足す予定なら計9/10です。
`tests/test_filesystem_staging.py`の新規3ケース:

1. 実取込み・snapshot修正・完全書出し／hash／private mode・元ソース不変・caller FD維持。
2. path／link／特殊file／UTF-8／size／保護／attempt／identityの拒否と観測した読取中変更の停止。
3. 実short write後の部分失敗で未完了受領と検証済prefixを保持し、再送なし・内部FD解放・元ソース不変。

実tmp fileを使いますが、ソースやコマンドの実行、runner／provider呼出し、Docker接続はありません。
旧test sourceを残し、旧suite・smoke・parameterize・matrix・scheduled test・自動retry・featurepush重複CIはありません。

ローカルとPR／main CIの明示選択:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS='' python -m pytest -q -o addopts= \
  tests/test_filesystem_staging.py::test_import_patch_and_verified_stage_preserve_source_and_caller_fds \
  tests/test_filesystem_staging.py::test_rejections_do_not_create_stages_or_write_outside_roots \
  tests/test_filesystem_staging.py::test_partial_failure_receipt_is_terminal_and_releases_descriptors
```

通算最大10ケースはlocal／CI／再実行の全てに適用し、Sleep Lab例外は適用しません。
CIはPython3.12の1job、PRとmainのみ。workflowは累積予算を強制しないため、
次の実行で超過する場合は停止し予算判断を求めます。既存収集cron／dry-runは不変です。

開発workerの実行metadataはCodex CLI `gpt-6.1-sol`／`xhigh`、正常完了受領／
最終メッセージは未取得でした。該当processなしを限定情報で確認し、再送・代替workerなし。
coordinatorが保存draftをレビューし、読取中の観測可能な変更の拒否を追加して上記3件を検証しました。
workerの限定metadata分類でtest dispatch該当0、開発使用量／費用は未確認。
実runner／実行backend／provider、source取得、stage世代切替、本番接続、実Docker受入は未完了。
本番config／collect-only／timer／認証変更、上流Issue作業、Docker導入／socket利用、
runtime推論／効果計測は0回。初期版全体の完成率は未査定です。

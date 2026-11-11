# Phase 2 実装計画（2026-11-11、sandbox lifecycle 初回増分）

## 現在地と順序

- Phase 1 は稼働中。ユーザー systemd タイマーで毎日06:30 JSTに収集する。
- coordinatorの読取専用timer／service／digest／ledger確認で11/8〜11/11の日次収集成功、新規・判定・pending は毎回0。観察チェックポイントは完了。歴史的候補363件には最終観測11/1の OpenClaw 319件が含まれ、修正対象選定や上流作業許可を意味しない。
- 今回は `docker_manager.py` の注入可能な backend 契約と単一試行の lifecycle だけを実装。回帰3ケースはローカル1起動で合格。実 backend と実 Docker 隔離は未検証、`git_workspace.py`・`src/agent/core.py`・tools は scaffold のまま。
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

今回の実装範囲（mock契約はローカル検証済み、実Docker受入は未実施）:

- `SandboxBackend` は create/start/execute/remove の4操作。create 前に選んだ ID を全操作で使い、不確実な作成でも remove 対象を失わない。実装の自動選択・subprocess・ホストシェル fallback はない。
- image は非空・NUL不可、CPU・時間は正の有限数、メモリ・コマンド数・入力/出力バイト上限は正の整数として検証。bool・数値文字列等は拒否。policy は immutable、network=none・非root・read-only root 固定。host mount・認証情報・環境継承は禁止する backend 契約。
- 既存の構築・context manager・run・CommandResult API を維持し、keyword 引数で backend と上限を指定。資源属性は読取専用。backend 未設定で実行を拒否し、CLI には接続しない。
- 試行全体の残り時間とコマンド期限の小さい方を渡す。create/start/remove も期限付きで、期限切れ後の cleanup には独立した猶予を渡す。例外・timeout・上限超過で remove を試み、同じ試行を再利用しない。
- remove 失敗は元の例外と cleanup 例外・ID を保持した明示エラー。作成や削除が不確実な場合、成功扱いも自動再試行もせず、人間が状態を照合する必要がある。
- 上限の実行中の強制・出力収集のメモリ制限・idle 時の寿命は backend の責務。Python の事後検査だけでは停止や隔離を保証できない。実 adapter、ディスク上限、設定連携、ソース取得は今回の対象外。

### B. ファイル操作とワークスペース境界

対象: `src/agent/tools.py`。

- 絶対パス、`..`、シンボリックリンクによる境界逸脱を拒否する。
- read/write/search/patchの各操作で同じ境界を使う。パッチの各ファイルパスも検証する。
- Git準備・diffの取得は隔離側へ委譲し、任意のホストディレクトリを作業先にできないようにする。

受入条件: 正常な一時ワークスペースで操作でき、境界外読み書き・リンク・パッチ経由の逸脱を拒否するテストが通る。

### C. TDD Coder Loopの状態機械

対象: `src/agent/core.py` と `tests/test_patch_loop.py`。

- モデル呼出し・ファイル操作・テスト実行を注入可能にし、最初は決定的な偽アダプターだけで制御を検証する。
- 既存テストの基準状態を確認し、再現テストが失敗することを確認してから最小修正に進む。
- 再現不能、壊れたテスト実行環境、テストの揺らぎを成功扱いにしない。
- 関連スイート全体が成功したときだけ成功結果を返す。新規テストだけの成功では不十分。
- 試行回数、消費トークン、実時間の上限を全て確認し、失敗時は理由・実行コマンド・出力末尾・使用回数を結果へ残す。

受入条件: 再現不能でモデル修正を呼ばない、失敗出力を次の修正に渡す、上限で必ず止まる、予算・タイムアウト・実行例外が成功にならないことをテストする。

### D. 合成リポジトリでの統合検証

- 公開OSSの候補ではなく、小さなローカルfixtureを使う。
- ドキュメント・typoの狭い演習から始め、再現・修正・全体テスト・diff出力・後片付けを通す。
- credential非搭載、ネットワーク制約、資源上限、タイムアウトを実際の隔離環境で検証する。
- CLIに組み込む前後でcollect-onlyゲートを回帰確認する。

受入条件: 単体テストと実隔離試験の結果を区別して記録し、scaffoldが残る経路を有効化しない。Phase 1からパッチ生成への接続を追加しない。

## 3. Phase 3へ渡す成果物

Issue URLと要約、base commit、差分、実行コマンドと結果、失敗・試行回数・資源使用、対象ルールの確認項目をレビュー可能なローカルレポートにまとめる。
CONTRIBUTING・ライセンス・CLA/DCO・既存PR重複・AI開示を確認し、本人へ提示する。[PR提出ゲート](decisions/2026-11-07-pr-gate.ja.md)は維持する。

## 4. 今回の検証と実行予算

2026-11-11 の今回の実績: **ローカルテスト起動1回・ケース実行3件、全件合格**
（Python 3.13.18、0.03秒）。CLI smoke・歴史的full suiteは未実行、PR CIは未実施です。
専用ローカル環境に宣言済みdev依存を導入しました。Docker／実backend／上流Issue
試行は0回。本番設定／timer／認証変更、Docker導入／socket利用、runtime推論や
有料計測はしていません。レビュー済み差分のfeature pushと開発PR作成までを
配送範囲とし、merge・本番有効化・監視対象へのPR投稿は別判断です。
初期版全体の完成率は未査定です。mock契約の合格から実隔離の完成を推定しません。

開発workerは明示承認後のCodex CLIで、ローカルmetadataが `gpt-6.1-sol`／`xhigh`
を示します。外側toolは600秒でtimeoutし、正常完了受領・最終ファイルなし、
照合時に該当processなし。再送・代替workerなし。coordinatorが保存draftをレビュー・
検証しました。開発使用量・費用は不明で、runtime計測の結果ではありません。

回帰ソースは `tests/test_sandbox_lifecycle.py` にちょうど3つの pytest case:

1. 正常 lifecycle・資源/安全policy・残り時間の委譲・単一利用。
2. 不確実な create・部分 start・timeout・実行例外の cleanup と teardown 失敗保持。
3. 入力検証/no-dispatch・コマンド/UTF-8出力上限・遅い成功/idle期限切れの拒否。

全て mock のみで、失敗分岐は明示した合成試行。parameterize・歴史的 suite の
wrapper・実 Docker・shell・外部API は使わない。旧テストソースは全て残す。
選択した3ケースのみローカルで合格し、collect-only ゲートはソースレビューのみです。

ローカルとCIで使う明示選択（ローカルでは同じ3件を1回実行済み）:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS='' python -m pytest -q -o addopts= \
  tests/test_sandbox_lifecycle.py::test_normal_lifecycle_and_resource_policy \
  tests/test_sandbox_lifecycle.py::test_failure_cleanup_is_terminal_and_preserves_uncertainty \
  tests/test_sandbox_lifecycle.py::test_validation_and_no_dispatch_boundaries
```

オーナー予算は同じ開発試行のローカル・CI・再実行を通算 **最大10ケース**。
Sleep Lab の full-suite 例外は適用しない。CI は Python 3.12 の1 job、
PR と main push のみで上記3ケースを1回選択し、matrix・自動retry・旧 full-suite・
追加 smoke はない。feature push で重複起動させず、daily-triage からも旧 suite を
外す。既存の収集 cron と dry-run は維持する。
ローカル3件にPR 1回の3件を足すと計6件、main 1回も含めると計9件です。再起動・追加更新も同じ予算を消費します。
workflow は起動間の累計予算を強制しないため、次の3件で10件を超える場合は
検証を止めて予算判断を求める。起動回数とケース数を分けて記録する。

ソース取得・依存取得は取得先制御と成果物の検証を設計する将来の別工程。
その後の sandbox 内テストは network=none とし、依存取得の都合で緩めない。
CI の checkout/依存準備と mock テスト実行も別段階であり、CI runner 自体の
ネットワーク隔離の証拠にはならない。実 Docker の隔離・資源上限・強制停止は
未検証。次の注入可能な CoderLoop、workspace 境界、合成repo統合検証は未完了で、
本番 activation、対象 full 化、PR 提出の人間ゲートは維持する。

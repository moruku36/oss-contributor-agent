# Autonomous OSS Contributor Agent（自律型 OSS コントリビューションエージェント）

[English](README.md) | 日本語

## 現在の担当と運用状況（2026-11-11）

- **担当引き継ぎ：** Muse（フワッチ）から Kurotchi（クロッチ）へ、moruku の依頼で管理・開発を引き継ぎました。PR 提出の最終判断は引き続き moruku が行います。
- **現在地：** Phase 1（Issue 収集・トリアージ）が midorun（OCI）で稼働中。systemd のユーザータイマーで毎朝 **06:30 JST** に実行しています。GitHub Actions の収集ワークフローは dry-run のままで、実運用には使っていません。
- **対象：** Mattermost、Grafana、Prometheus、Brave、OpenClaw の 5 件。すべて `collect-only` で、自動修正・PR 作成は有効にしていません。
- **観察チェックポイント完了（coordinatorの読取専用確認）：** 11/8〜11/11 の日次収集は成功し、毎回 **新規0・判定0・pending 0**。保存済みの **363 件**には **最終観測11/1の OpenClaw 319 件**を含みます。過去の保存値であり、修正対象の選定や上流 Issue への作業許可ではありません。
- **開発状況：** Phase 2 の初回増分として、明示注入する backend 契約と、失敗時に閉じる単一試行の sandbox lifecycle を実装しました。実 backend の同梱・有効化はなく、mock-only 回帰3ケースは **ローカル1起動・3件合格**（Python 3.13.18、0.03秒）。PR CIは未実施です。実 Docker 隔離は未検証、workspace と CoderLoop は未完了です。
- **次の作業：** 今回のPR CIを確認し、注入可能な CoderLoop／workspace の実装へ進みます。ソース・依存取得は将来の別段階で、ネットワーク無効のテスト実行と分離します。初期版全体の完成率は **未査定**です。mock契約の合格を実Docker隔離の完成とは扱いません。

レポート名の日付は UTC のため、11/8 06:30 JST の成果物は `digest-2026-11-07.md` です。日付が前日でも欠損ではありません。

詳細：[運用・引き継ぎ記録](docs/operations.ja.md) ／ [Phase 2 実装計画](docs/phase2-plan.ja.md)

以下はプロジェクト全体の目的・設計です。現在実装済みの範囲は上記と「ステータス」を参照してください。

オープンソースプロジェクトへの貢献を支援するエージェントです。
ただし、低品質な「AI スパム PR」の発生源には絶対になりません。

対象リポジトリ（Mattermost、Grafana、Prometheus など）を監視し、
スコープの合った Issue を収集して、隔離されたサンドボックス内で
修正を試みます。そして、プロジェクトのテストスイートがローカルで
100% パスした場合にのみ、人間がレビューして提出するための
**ドラフト** PR を用意します。エージェントがドラフト以外の PR を
開くことはなく、マージすることもありません。

## なぜ作るのか

LLM が生成した PR が OSS プロジェクトに殺到しています。その多くは
未検証で、スコープを外れ、各プロジェクトのコントリビューション
規約を無視しており、メンテナーは目にした時点でクローズしています。
本プロジェクトはその逆の立場を取ります。

- プロジェクト自身のテストを通っていないパッチは、存在しないものとする。
- プロジェクトの CONTRIBUTING.md に違反する差分は、出荷しない。
- PR になる前に、人間がすべての差分を見る。

狙いは量ではなく、少数でも丁寧で検証済みの貢献です。

## パイプライン

1. **Collector** — GitHub API をポーリングし、対象リポジトリで
   監視ラベル（`good first issue`、`help wanted`、`bug`、
   `documentation`）の付いたオープン Issue を収集します。
2. **Triager** — 各 Issue をスコアリングします。まず決定的な
   ルールベースのスコアラーが走り、境界帯のものだけが LLM の
   二次判定に回ります。広範・曖昧・再現不可能な Issue は除外します。
3. **重複排除と上限** — 永続レジャー（台帳）が試行済み Issue を
   すべて記録し、同じ Issue を二度試すことはありません。同 Issue
   への既存 PR（人間・ボット問わず）、プロジェクトごとの
   クールダウン、週次上限を、作業開始前に確認します。
4. **Workspace Isolator** — リポジトリを、認証情報を含まない
   使い捨ての Docker サンドボックスにクローンします。ネットワーク
   の外部送信は制限され、CPU・メモリ・時間の上限がかかります。
5. **Coder Loop** — TDD で進めます。まず失敗するテストで Issue を
   再現し、最小の修正を当て、テストがパスするか試行上限
   （デフォルト 5 回）に達するまで繰り返します。再現できない
   Issue はそこで中止します。推測で直すことはしません。
6. **Compliance Guard** — プロジェクトのリンターとフォーマッター
   を実行し、コミットメッセージ規約（Conventional Commits など）と、
   必要なプロジェクトでは DCO の Signed-off-by を検証します。
7. **Review Report** — 変更内容、実行したテスト、差分をまとめて
   人間の運用者に届けます（デイリーダイジェスト）。
8. **Draft PR** — 人間がレポートを承認した場合にのみ、プロジェクトの
   PR テンプレートに沿ってドラフト PR を作成します。AI 支援の
   開示文が必ず含まれます。

## 絶対ルール

- **テスト第一。** ドラフト PR は、プロジェクトのテストスイートが
  ローカルサンドボックスで 100% パスした場合にのみ用意します。
- **常に Human-in-the-Loop。** エージェントが作るのは人間の明示的な
  承認後のドラフト PR のみです。レビュー可能な状態の PR を提出
  することも、マージすることもありません。
- **プロジェクトのルールが優先。** 各対象プロジェクトの
  CONTRIBUTING.md、PR テンプレート、CLA/DCO 要件、lint・コミット
  規約はプロジェクトごとに読み込んで遵守します。
- **フェイルクローズ。** 再現できない Issue、不安定なテスト、
  不明確なルールなど、不確実な点があれば、理由を記録して
  スキップし、次に進みます。
- **メンテナーの時間を尊重する。** 保守的な上限を既定にします：
  ドラフト PR は全体で 1 日最大 1 件、プロジェクトごとに週最大 2 件。

## 対象プロジェクト（現在）

| プロジェクト | 言語 | 備考 |
| --- | --- | --- |
| mattermost/mattermost | Go、TypeScript/React | 収集のみ有効。ドキュメントと小さなサーバー側 Issue から |
| grafana/grafana | Go、TypeScript | 収集のみ有効。スコープの狭いパッケージのみ |
| prometheus/prometheus | Go | 収集のみ有効。単一言語、Makefile 駆動 |
| brave/brave-browser | C++、JavaScript、TypeScript | 収集のみ有効。コードは brave/brave-core 側 |
| openclaw/openclaw | TypeScript | 収集のみ有効。`good first issue` ラベルのみ |

対象は `configs/targets.yaml` にあります。各対象には `mode` があり、
`collect-only`（収集のみ）ではパッチ試行と PR 作成が一切できません。
Phase 1 では上記 5 件を `enabled: true` + `mode: collect-only` で
有効化しています。`mode: full` への変更は、各プロジェクトのルールを
人間が確認した後にのみ行う判断です。

## リポジトリ構成

```text
configs/            対象プロジェクトとエージェント全体のルール（YAML）
docs/               アーキテクチャと設計ドキュメント
scripts/            CLI エントリポイント（単一 Issue 実行、定期監視）
src/collector/      GitHub クライアントとトリアージスコアリング
src/sandbox/        Docker サンドボックスと Git ワークスペース管理
src/agent/          Coder Loop（TDD）、プロンプト、ツール
src/review/         コンプライアンスガード、レビューレポート、ドラフト PR 作成
tests/              pytest スイート
.github/workflows/  CI とデイリー・トリアージのスケジュール
```

## 動かし方（Phase 1）

セットアップ（依存は PyYAML のみ、テスト用に pytest）：

```bash
pip install -e .            # または pip install pyyaml
pip install pytest          # 許可された検証の準備時のみ
```

ローカルでは選択した3ケースを1回実行して合格しました。PR と main push の CI だけで mock-only 3ケースを
1回選択し、feature push の重複実行を避けます。旧テストソースは残しますが
自動では実行しません。オーナー予算はローカル・CI・再実行の合計 **10ケース**
までで、起動単位でリセットしません。選択と実行記録は
[Phase 2 計画 §4](docs/phase2-plan.ja.md#4-今回の検証と実行予算)を参照してください。

収集パスを 1 回実行します（有効な対象すべて）：

```bash
# 任意：レート制限が緩和されます。なくても公開リポジトリは読めます
export GITHUB_TOKEN=<your token>
python3 scripts/watch_repos.py --once
```

結果は 2 か所に出ます。レジャー `state/ledger.sqlite3`（git 管理外）
と、その日のダイジェスト `reports/digest-YYYY-MM-DD.md`（git 管理外）
です。再実行しても、判定済みの Issue が再トリアージされることは
ありません。

境界帯の Issue への LLM トリアージは、環境変数で有効化します。
未設定の場合、境界帯は `pending_llm` 状態で保持され（推測で判定
されることはありません）、設定後の実行で判定されます：

```bash
export LLM_TRIAGE_PROVIDER=openai        # または anthropic
export LLM_TRIAGE_MODEL=gpt-4o-mini      # 任意（既定あり）
export OPENAI_API_KEY=<your key>         # プロバイダ側のキー
python3 scripts/watch_repos.py --once
```

`python3 scripts/watch_repos.py --daemon` で定期ポーリングも可能
です（既定 60 分間隔、`--interval-minutes` で変更）。対象が
collect-only の間、`scripts/run_agent.py` の単一 Issue 実行は
パッチ試行を拒否します。

## スケジューリング

現在は **midorun の systemd ユーザータイマーによる毎朝のバッチ実行**
（06:30 JST）です。新着 Issue を収集・トリアージしてダイジェストを
保存します。修正試行・PR 作成は行いません。GitHub Actions の
ワークフローは dry-run のままで未使用です。任意のウォッチデーモンモードでは、
より頻繁なポーリングも可能です。全体設計は
[docs/architecture.ja.md](docs/architecture.ja.md) を参照してください。
運用の実態（Phase 1 のデプロイ構成と初回実績）は
[docs/operations.ja.md](docs/operations.ja.md) に記録しています。
設計の背景にある調査ノートは
[docs/references/index.ja.md](docs/references/index.ja.md) に
まとめています。

## ステータス

**Phase 1 実装済み（v0.2）— Collector + トリアージが動作します。**

現時点にあるもの：全体設計ドキュメント、設定スキーマ、GitHub API
による Issue 収集、SQLite レジャー、テスト付きで動作する決定的
トリアージスコアラー、境界帯への LLM トリアージ（プロバイダ差し
替え可能、未設定時は pending 保持）、デイリーダイジェスト、CI。
対象 5 件は収集のみ有効で、パッチ試行と PR 作成は構造的にも設定
的にも不可能です。Phase 2 の sandbox lifecycle 契約を今回実装しましたが、
回帰3ケースはローカル1起動で合格しました。backend 未設定は明示拒否し、CPU・メモリ・
offline／非root実行・出力・期限の強制は将来の adapter の責務です。
Docker・subprocess・ホストシェルへの fallback はありません。
CoderLoop と workspace helpers は引き続き scaffold です。

## ロードマップ

- **Phase 0** — 設計とスキャフォールド（完了）。
- **Phase 1** — Collector + トリアージ：Issue 収集、レジャー、
  境界帯への LLM トリアージ、デイリーダイジェスト（完了。
  対象は収集のみ有効化）。
- **Phase 2** — サンドボックス + Coder Loop。対象はドキュメントと
  タイポの Issue のみで演習します。
- **Phase 3** — コンプライアンスガード + 人間承認ゲート付きの
  ドラフト PR 提出。
- **Phase 4** — 毎日の定期運用。対象プロジェクトは 1 つずつ有効化し、
  上限の引き上げはメンテナーの受け入れ実績に基づいてのみ行います。

## ライセンス

MIT — [LICENSE](LICENSE) を参照してください。なお、本ツールで
作成したコントリビューションは、各対象プロジェクト自身の
ライセンスとコントリビューションルールにも従う必要があります。

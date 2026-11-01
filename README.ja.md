# Autonomous OSS Contributor Agent（自律型 OSS コントリビューションエージェント）

[English](README.md) | 日本語

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

## 対象プロジェクト（初期）

| プロジェクト | 言語 | 備考 |
| --- | --- | --- |
| mattermost/mattermost | Go、TypeScript/React | 収集のみ有効。ドキュメントと小さなサーバー側 Issue から |
| grafana/grafana | Go、TypeScript | 収集のみ有効。スコープの狭いパッケージのみ |
| prometheus/prometheus | Go | 収集のみ有効。単一言語、Makefile 駆動 |

対象は `configs/targets.yaml` にあります。各対象には `mode` があり、
`collect-only`（収集のみ）ではパッチ試行と PR 作成が一切できません。
Phase 1 では上記 3 件を `enabled: true` + `mode: collect-only` で
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
pip install pytest          # テストを実行する場合
python3 -m pytest tests/ -q
```

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

主モードは **毎朝のバッチ実行**（GitHub Actions の cron、06:30 JST）
です。新着 Issue を収集し、トリアージし、少数の試行を行い、
ダイジェストレポートを届けます。任意のウォッチデーモンモードでは、
より頻繁なポーリングも可能です。全体設計は
[docs/architecture.ja.md](docs/architecture.ja.md) を参照してください。
設計の背景にある調査ノートは
[docs/references/index.ja.md](docs/references/index.ja.md) に
まとめています。

## ステータス

**Phase 1 実装済み（v0.2）— Collector + トリアージが動作します。**

現時点にあるもの：全体設計ドキュメント、設定スキーマ、GitHub API
による Issue 収集、SQLite レジャー、テスト付きで動作する決定的
トリアージスコアラー、境界帯への LLM トリアージ（プロバイダ差し
替え可能、未設定時は pending 保持）、デイリーダイジェスト、CI。
対象 3 件は収集のみ有効で、パッチ試行と PR 作成は構造的にも設定
的にも不可能です。サンドボックスと Coder Loop は Phase 2 以降で
実装します。

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

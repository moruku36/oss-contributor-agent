# 運用 — Autonomous OSS Contributor Agent

[設計](architecture.ja.md) | 運用（本書）

本書は Phase 1 の as-built 記録です。設計（architecture.ja.md）が
「どうあるべきか」を述べるのに対し、本書は初回運用時点で
実際に何がどこで動いているかを記録します。引き継ぎを受けた担当は
本書から読んでください。2026-11-07 のスコアリング・候補プール改善は
この作業コピーで検証済みですが、midorun には未デプロイです。

## 1. 位置づけ

Phase 1（収集＋トリアージのみ、collect-only）は構築完了し、OCI 大阪
リージョンの開発箱「midorun」上で稼働しています。

役割分担の前提は次のとおりです。

- 開発は midorun で行う。
- 本番運行は将来、ホームラボへ移す。
- 最終的には Mac mini 上の OpenClaw（ローカル）がオーケストレー
  ションする。

このリポジトリ自体が引き継ぎ成果物です。運行先が変わっても、
コード・設定・運用手順はここに集約します。

## 2. 実装済みコンポーネント

| コンポーネント | 役割 |
| --- | --- |
| `src/config.py` | 設定の読み込み時検証。`draft_only` / `require_human_approval` / `fail_closed` のいずれかが false なら、起動時エラーで停止する |
| `src/collector/github_client.py` | GitHub クライアント。標準ライブラリのみ。ラベル横断検索、重複排除、PR の除外、既存 PR チェックを行う |
| `src/collector/ledger.py` | SQLite レジャー（`state/ledger.sqlite3`、git 管理外） |
| `src/collector/triager.py` | ルールベースの決定的スコアラー（設計 6 節のルーブリック） |
| `src/collector/llm_triage.py` | 境界帯向け LLM クライアント（OpenAI 互換）。応答を解釈できなければ例外を送出し、推測で判定しない |
| `src/collector/pipeline.py` | 収集パイプライン。`sandbox` / `agent.core` を構造的に import しないため、Phase 1 のプロセスからパッチ試行に到達できない |
| `src/collector/digest.py` | デイリーダイジェスト（`reports/digest-YYYY-MM-DD.md`、git 管理外） |

エントリポイントは `scripts/watch_repos.py` です。`--once` で
1 回実行、`--daemon` で定期ポーリングします。

初回のテストは pytest で 66 passed / 2 skipped（2026-11-01 時点）。
2026-11-07 の作業コピー検証は 124 passed / 2 skipped / 0 failed
（pytest 実行 3 回）です。skip の 2 件は Phase 2 のプレースホルダです。

## 3. トリアージの仕組み

判定は 2 段階です（設計 5.2 節、6 節）。

1. ルールスコア：0.60 以上は `candidate`、0.40 未満はスキップ。
2. 0.40 以上 0.60 未満の境界帯だけを LLM が判定する。

### 種別別の採点と候補プール（2026-11-07 作業コピー）

[設計 §6 のルーブリック](architecture.ja.md#6-トリアージスコアリングのルーブリック)
に従い、`bug` ラベルまたは本文の再現手順で bug と判定し、それ以外は
request として採点します。bug の再現手順は +0.20、request は
ユースケース・具体例・期待結果を各 +0.10、合計 +0.20 まで加点します。
request に再現手順は要求しません。`feature request` / `feature-request`
ラベルの −0.15 は据え置きです。検出はキーワード等による弱いシグナルです。

`configs/agent_rules.yaml` の `rules.triage_scoring` にある
`reproduction_points`、`detail_signal_points`、`detail_points_cap` で
調整できます。値は 0〜1 の有限数で、不正値は起動時に `ConfigError` に
なります。省略時は上記の既定値を使います。

ダイジェストの「現在の候補プール」は、今回の判定とは別に、レジャー全体の
`candidate` を表示します。初回観測からの経過日数・スコア・Issue リンクを
確認でき、空ならその旨を表示します。レジャースキーマの変更はありません。
既存の終端判定は自動再採点しないため、以前スキップした Issue は今回の
採点変更だけでは復活しません。プールは保存状態の一覧であり、Issue の
現在のオープン状態を再確認する機能ではありません。

LLM の接続先は Meta Model API（OpenAI 互換）です。

- base URL：`https://api.meta.ai/v1`
- モデル：`muse-spark-1.3`

設定は環境変数で行います。`LLM_TRIAGE_PROVIDER`、
`LLM_TRIAGE_BASE_URL`、`LLM_TRIAGE_MODEL`、`OPENAI_API_KEY`、
`GITHUB_TOKEN` です。

LLM の呼び出しが失敗した場合、または応答を解釈できない場合、
その Issue は `pending_llm` のまま残ります。推測で判定することは
決してありません（フェイルクローズ）。接続が回復した後の実行で
改めて判定されます。

## 4. デプロイ構成（midorun）

- 配置：`~/workspace/oss-contributor-agent`
- 依存：`.venv` に `pip install -e .`（実行時の依存は PyYAML のみ）
- スケジュール：systemd ユーザーユニット
  `oss-agent-collect.service` / `oss-agent-collect.timer`。
  毎日 06:30 JST に実行し、`Persistent=true` のため、停止中に
  過ぎた実行は次回起動時に補われます。
- 秘密情報：`~/.config/oss-agent/env`（chmod 600）にのみ置き、
  ユニットが `EnvironmentFile` で読みます。リポジトリに秘密情報は
  含めません。鍵の保管元はオーナーの 1Password です（値は本書にも
  書きません）。

## 5. 初回実績（2026-11-01）

対象 3 件（mattermost/mattermost、grafana/grafana、
prometheus/prometheus、`enabled: true` + `mode: collect-only`）で
初回実行を行い、372 件の Issue を走査しました。

ルール段の結果：

| 結果 | 件数 |
| --- | --- |
| `candidate` | 25 |
| `pending_llm`（境界帯） | 122 |
| `skipped_duplicate` | 7 |
| `skipped_low_score` | 218 |

同日、LLM を接続して境界帯 122 件を判定した結果：

| 結果 | 件数 |
| --- | --- |
| `candidate`（追加分。累計 34） | 9 |
| `skipped_llm_reject` | 113 |
| 保留（`pending_llm`） | 0 |

## 6. アクセス

midorun の `authorized_keys` には次の鍵を登録済みです。

- 運用エージェント（Muse）の鍵
- オーナー（Windows）の鍵

ホームラボ担当（Dots）の鍵は、ラウンジ（エージェント間の伝言板）
経由で登録を依頼中です。秘密鍵そのものを伝言板やリポジトリに
載せることはありません（受け渡すのは公開鍵のみ）。

## 7. 注意点

- GitHub Actions の `daily-triage.yml` は dry-run のままで未使用
  です（PAT シークレットがリポジトリに未設定）。現在の実働経路は
  midorun の systemd タイマーのみです。設計 7 節の「主モードは
  GitHub Actions の cron」は現状と異なります。
- Phase 1 は収集専用です。PR の作成・投稿は一切行いません。
  `configs/targets.yaml` の collect-only ゲートと `pipeline.py` の
  import 構造の両方で、構造的に阻止しています。
- レジャーとダイジェストは運行先のローカルに残ります。運行先を
  移す際は `state/ledger.sqlite3` の引き継ぎを忘れないでください
  （引き継がないと、判定済み Issue が再トリアージされます）。

## 8. 対象の追加（2026-11-01）

オーナー指示で対象を 2 件追加し、計 5 件になりました。

- `brave/brave-browser`（ラベル： good first issue 47 件、
  help wanted 39 件、いずれも 2026-11-01 時点のオープン数）。
  Brave の Issue はこのリポジトリで管理され、コードは
  `brave/brave-core`（Chromium）側にあります。brave-core の
  CONTRIBUTING.md では AI 補助のコントリビュートが許可されて
  いますが、人間による理解・手元でのビルド／テストと証拠の
  提示・AI 関与の透明性が必須で、大量 PR は禁止です。
- `openclaw/openclaw`（ラベル： good first issue 1 件、
  bug 664 件）。CONTRIBUTING.md に「AI/Vibe-Coded PRs Welcome」
  の節があり、AI 生成 PR が明示的に歓迎されています
  （開示ラベル不要、Evidence の記載と内容理解の確認が必須、
  オープン PR は著者あたり 20 件まで）。

どちらも `enabled: true` + `mode: collect-only` で、収集と
トリアージのみです。

なお OpenClaw は当初 bug ラベル（664 件）も対象にしましたが、
初回収集で候補が 200 件を超えたため、同日中にオーナー判断で
good first issue のみに絞りました。

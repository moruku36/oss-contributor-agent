# Read-only exact-target preflight / 読み取り専用の厳密な宛先事前確認

After `load_selected_review_request(parent_fd, name, expected_request)`, pass its
validated `DraftRequest` to `preflight_target(selected, inspector=inspector)`.
The injected `TargetInspector.inspect_target(DraftRequest)` is read-only. No
concrete inspector, network/API, configuration, authentication, authority,
submission, journal or transport implementation or invocation is supplied.

`load_selected_review_request` が返す `DraftRequest` を
`preflight_target(selected, inspector=inspector)` に渡します。注入する
`TargetInspector.inspect_target(DraftRequest)` は読み取り専用です。
実装・ネットワーク/API・設定・認証・承認・送信・台帳の呼び出しは提供しません。

`TargetObservation` is frozen and contains `ObservationStatus.OBSERVED` or
`UNKNOWN`, the entire `DraftTarget`, the full canonical `request_snapshot_json`
string, and explicit bools `draft_only` and `atomic_head_base_preconditions`.
The latter asserts support for atomically enforcing both head/base identities
(repositories, full refs and SHAs) with draft creation. No timestamp, provenance
claim, digest credential or incoming ready flag is accepted. Construction alone
does not validate. Full snapshot bytes bind report, rendered body, title,
template, draft flag and every target identity; a public hash is insufficient.

凍結された `TargetObservation` は観測状態、`DraftTarget` 全体、正規形式の
要求文字列全文、明示的な bool 型の draft 専用・head/base 原子的事前条件対応を
保持します。後者はリポジトリ・完全 ref・SHA の双方を作成と原子的に確認できる
という申告です。時刻・架空の来歴・ハッシュ資格情報・ready フラグは受け付けません。
コンストラクタだけでは検証されず、公開ハッシュだけでは全文への束縛になりません。

The seam captures and validates the actual request before looking up the bound
inspector method once, then passes a detached request. It captures, detaches and
revalidates the reply and compares the full request string and all seven exact
target fields without normalization. A different bounded destination is a
definite mismatch; the remaining target grammar uses the existing validator.
Only exact classes, strings, bools and enum members are accepted.

実際の要求を先に捕捉・検証し、検査メソッドを一度だけ束縛して別の要求オブジェクトを
渡します。応答も切り離して再検証し、全文と宛先の全7項目を正規化せず比較します。
別の有界な宛先文字列は明確な不一致であり、他の項目は既存の構文検証に従います。
厳密なクラス・文字列・bool・列挙値だけを受け付けます。

| Result / 結果 | Meaning / 意味 |
| --- | --- |
| `READY` | Local consistency only / ローカルな整合性のみ |
| `REJECTED` | Invalid request, definite identity/snapshot mismatch, or either required capability false / 不正要求、明確な不一致、必須機能非対応 |
| `UNKNOWN` | Inspector exception, missing/malformed/ambiguous reply, or explicit unknown / 例外、欠落・不正・曖昧な応答、明示的な不明 |

Malformed replies take precedence over mismatch decisions; explicit unknown
remains unknown even if a capability is false. Invalid requests never reach the
inspector. Ordinary inspector/lookup exceptions become `UNKNOWN`; process
cancellation (`BaseException`) propagates. There is one call, no retries.

不正な応答は不一致判定より優先して `UNKNOWN` になります。明示的な不明も同様です。
不正要求では検査を呼びません。通常の検査・メソッド取得例外は `UNKNOWN`、
中断 (`BaseException`) は伝播します。呼び出しは一度で再試行しません。

Re-observation does not authenticate or authorize, establish provenance, close
TOCTOU, or replace trusted authority review and the transport's independent
atomic head/base recheck. Unsupported atomic preconditions fail closed. An
injected implementation can lie or violate read-only obligations; this seam
cannot sandbox it. Frozen detached values protect captured local expectations,
not against hostile process memory or concurrent mutation during field capture.

再観測は認証・承認・来歴証明・TOCTOU 解消になりません。信頼された承認判断と
送信時の独立した head/base 原子的再検証は引き続き必須です。原子的事前条件の
非対応は拒否します。注入実装の虚偽や書き込みは隔離できません。切り離した凍結値は
ローカルな期待値を守りますが、敵対的なプロセスメモリや捕捉中の並行変更には対応しません。

Exactly six self-contained unittest methods are authored in
`tests/test_target_preflight.py`, using inert synthetic evidence and actual
package selection. NOT RUN: no tests, imports, fixture execution, build or
network verification was performed in this file-only change.

自己完結した unittest を6メソッド作成しました。不活性な合成証拠と実際の
パッケージ選択を使います。未実行：このファイル編集ではテスト・import・
フィクスチャ実行・ビルド・ネットワーク検証を行っていません。

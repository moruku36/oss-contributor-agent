# Exact package selection / パッケージの完全一致選択

`load_selected_review_request(parent_fd, name, expected_request)` connects a
persisted local review to existing submission preflight. Retain the independently
intended prepared `DraftRequest`; do not derive the expectation from whichever
package was selected. The function captures and revalidates that entire canonical
snapshot before I/O, invokes the actual strict `read_review_package`, and compares
the exact UTF-8 snapshot bytes. It returns a detached validated `DraftRequest`
only after the complete package matches. A different internally valid package,
stale target/head/base, title, report/body or template rejects with
`ReviewPackageError` (`ValueError`). Malformed expected requests reject before I/O.

独立して保持した「意図した準備済み要求」を渡してください。選択したパッケージ自身から
期待値を作ると、誤選択を検出できません。I/O 前に期待値を捕捉して実際の要求検証を行い、
厳格な全パッケージ読込後、UTF-8 の全バイトを照合します。完全一致した場合だけ独立した
要求を返します。内部的に整合する別成果物、古い宛先・head/base・本文・タイトル・
テンプレートも拒否します。読込中の呼出側要求の変更は捕捉済み値に影響しません。

```python
from src.review.draft_submission import prepare_draft_submission
from src.review.package_selection import load_selected_review_request
from src.review.pr_builder import ReviewReport, submit_draft_pr
from src.review.review_package import export_review_package

expected = prepare_draft_submission(report, target=target, title="Fix bytes")
export_review_package(expected, parent_fd=private_parent_fd, name="review-1")
# Retain expected independently across the local review step.
selected = load_selected_review_request(private_parent_fd, "review-1", expected)
data = selected.data
# Separate existing submission boundary; trusted dependencies are supplied by the app.
outcome = submit_draft_pr(
    ReviewReport(data["report_json"]), data["body"], request=selected,
    authority=trusted_authority, transport=trusted_transport,
    journal=trusted_journal, operation_id=stable_operation_id,
)
```

Selection performs no journal, authority or transport operation and constructs no
submission. It neither consumes nor grants approval. No approval flags,
credentials, authentication or additional persistence format are introduced.
Public hashes remain consistency metadata, never authority. Independent trusted
approval, compliance, replay controls and live upstream head/base verification
remain the existing submission dependencies' responsibility.

選択処理は journal・承認機関・transport を呼ばず、送信オブジェクトも作りません。
承認の付与・消費や認証は行いません。公開ハッシュは権限ではありません。独立した
承認・規約確認・再送防止・実際の upstream の head/base 確認は既存の送信境界の責務です。

All [package prerequisites and bounds](review-package.md) remain: trusted process
and filesystem, caller-owned already-open private directory FD (0700), exclusive
control without concurrent filesystem mutation/mounts/aliases, explicit simple
child name, exact private file set and existing byte caps. The FD stays caller-owned.
No discovery, repair, retry, authentication, atomic multi-file transaction,
fsync/crash guarantee or blocked-I/O deadline is added. Matching historical bytes
does not establish current refs, authentic evidence or human approval. Frozen
objects do not isolate hostile in-process code. Historical limits and UNKNOWNs
are unchanged; no production, collector, runtime or default activation occurs.

既存の信頼・非公開ディレクトリ・排他的管理・ファイル構成・サイズ上限を維持します。
現在の ref・証拠の真正性・人の承認は保証しません。停電時の永続性や I/O 時間制限、
敵対的な同一プロセス内コードへの隔離も追加しません。過去の上限・UNKNOWN は不変です。

`tests/test_package_selection.py` contains exactly six new self-contained unittest
methods using inert synthetic `AttemptResult` evidence and actual prepare/export/
read paths. They were **authored, NOT RUN** in this file-only change; imports,
tests and builds were not executed. No live acceptance or regression pass is claimed.

新規テストは独立した6メソッドを作成済み・**未実行**です。fixture のソース文字列は
実行せず、実証・回帰テスト成功・本番受入を主張しません。

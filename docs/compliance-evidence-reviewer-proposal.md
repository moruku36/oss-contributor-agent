# Proposed compliance-evidence evaluator: reviewer walkthrough

Status: review-preparation proposal, 2026-11-15. The evaluator and its three authored scenarios are unpublished, unexecuted draft work; they are not included in this documentation change or the repository baseline identified below. This document describes their intended, statically inspected behavior, not an available API or an execution receipt.

## Baseline and scope

Public baseline: [commit 3a080e9](https://github.com/moruku36/oss-contributor-agent/tree/3a080e9ee27216685c0f318a27ac0dfb463db48a).

The existing [commit-message increment](https://github.com/moruku36/oss-contributor-agent/blob/3a080e9ee27216685c0f318a27ac0dfb463db48a/docs/commit-compliance.md) documents syntax checking, not aggregate compliance. The existing [review-report contract](https://github.com/moruku36/oss-contributor-agent/blob/3a080e9ee27216685c0f318a27ac0dfb463db48a/docs/review-report-contract.md) covers caller-supplied attempt evidence and local body generation. Neither is replaced here.

The proposed evaluator would ask **whether caller declarations agree**, not whether a contribution is legally cleared, commands ran, or a PR may be submitted. Its intended claim is `caller_declared_aggregate_consistency_only`. The draft is not linked as repository source because it is absent from this baseline. Its implementation, fixture and schema must be published and reviewed separately before this walkthrough can serve as a reproducible source-review guide.

## Caller decision guide

| Proposed observation | Intended interpretation | Invalid shortcut |
| --- | --- | --- |
| Validation exception | No result; inspect bounded error code and field, and correct input at its originating boundary. | Manufacture a result, silently trim input or log rejected raw commands. |
| `unknown` | Declared evidence is incomplete or unusable; preserve reasons and unresolved state. | Treat absence of failure as success. |
| `failed` | At least one declared obligation failed; inspect all eight obligations, including simultaneous unknowns. | Erase unknowns after finding a failure. |
| `passed` | All eight obligations agree within the declared-data model. | Authenticate evidence, approve legal compliance or submit a PR. |
| Detached snapshot | A returned data view should not mutate the canonical result. | Treat canonical JSON as safe HTML or Markdown rendering. |

The eight proposed obligations are policy, context, lint, format, commit, paths, CLA and provenance. Overall precedence is failed, then unknown, then passed. Constructor re-evaluation would establish self-consistency, not authenticity. Retained commands and receipt text remain untrusted literal data; downstream renderers must escape for their own output context.

## Scenario 1: a synthetic pass is deliberately weak

The first authored scenario supplies explicit synthetic policy, context, provenance and four receipts. Each supplied receipt repeats seven context fields and two policy fields; eligibility compares all nine.

The CLA receipt is null because the policy explicitly declares verified `not_required`. This is an important exception: missing evidence is generally unknown, but this explicit CLA policy can produce `NOT_REQUIRED` without a receipt. Here “verified” remains a caller assertion, not independently verified policy authority.

A hostile-looking lint string matches the declared command literally. The intended evaluator preserves and compares it without execution. Placeholder hashes are declarations: no real diff, policy or commit-message bytes are acquired or authenticated.

The authored assertions cover canonical round-trip, retained limitations, detached caller/snapshot mutation and frozen-field behavior for one input. They do not prove command safety, receipt authenticity, exhaustive constructor resistance or concurrent-mutation safety.

## Scenario 2: failed and unknown must coexist

| Synthetic change | Intended obligation result | Reason |
| --- | --- | --- |
| Remove lint receipt | lint unknown | Missing receipt is not success. |
| Change only format receipt base commit | context failed; format unknown | Foreign binding makes that receipt ineligible for its domain. |
| Declare a workflow path changed and allowlisted | paths failed | Intrinsic CI-path guards are independent of the allowlist. |
| Keep commit receipt unchanged | commit passed | Path changes do not themselves alter declared commit identity or messages. |

Overall failure must preserve the lint/format unknowns. A foreign receipt saying “passed” is retained for diagnosis, not accepted. The proposed intrinsic path inventory is finite and case-sensitive, not discovery of every project's legal or CI files. Evaluating declared contribution paths does not authorize maintainer workflow edits.

## Scenario 3: rejection is not a compliance outcome

The third authored scenario supplies a 4097-byte ASCII lint command against a proposed 4096-byte command limit. Expected behavior is `LIMIT_EXCEEDED at policy.lint_command`, before canonical input/result serialization: no result, partial pass, truncation or raw payload in that exception's message/repr.

This single scenario does not establish memory isolation, universal error secrecy, all other bounds or UTF-8 boundary behavior.

## Coverage and acceptance limits

| Question | Authored scenario | Not established |
| --- | --- | --- |
| Detached deterministic canonical artifact for the supplied request? | 1 | Exhaustive forgery or concurrency resistance. |
| Mixed unknowns and foreign evidence cannot manufacture aggregate pass? | 2 | Every command, commit or CLA truth-table row. |
| Oversized command rejects without a returned artifact or raw payload in that error? | 3 | Every structural type, enum, hash, path or cumulative-size boundary. |
| Real execution, legal approval, complete diff or authentic source? | None | Explicit non-goals. |

Exactly three authored test functions exist in the unpublished draft, with one public evaluator call each. Internal result-constructor re-evaluation is not a separately dispatched scenario. None has a successful execution receipt in this preparation record. This document adds no test case and makes no behavioral verification claim.

A wording issue in the draft contract deserves correction when that contract is published: a general “missing evidence never passes” statement must explicitly retain the verified CLA-not-required exception described above. No source or contract correction is included here.

## Verification status

This is documentation-only static review preparation. The current baseline CI selects three historical review-report tests, not the proposed evaluator scenarios. A green result from those selectors would not verify the evaluator. Publication of this guide does not establish implementation availability, integration, legal acceptance, human approval, merge readiness or production readiness.

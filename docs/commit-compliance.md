# Commit-message compliance increment

## Scope

`check_commit_message(message, convention, sign_off_required)` now performs a
pure, bounded syntax check and returns stable violation strings. An empty list
means only that these syntax checks passed. It is not project approval, identity
verification, a DCO legal attestation, CLA clearance or submission permission.

- Only the explicitly selected `conventional-commits` convention is supported.
  Empty, unknown or unverified convention names fail closed.
- The sign-off policy must be an actual boolean; configuration placeholders such
  as `check CONTRIBUTING.md` must not be coerced into verified policy.
- UTF-8 message size is 1–8192 bytes; the subject is at most 256 bytes.
  NUL, invalid Unicode, other control characters and lone CR reject. CRLF works.
- Supported subjects use a lowercase type, optional ASCII scope
  (letters/digits/underscore/dot/slash/dash), optional `!`, then `: ` and a
  nonempty description with no trailing whitespace or tabs. Custom lowercase
  types are allowed. This is a conservative subset, not the entire specification.
- A body requires a blank line after the subject. When DCO is required, a
  case-sensitive `Signed-off-by: Name <email>` must occur in the final
  blank-line-separated paragraph, consisting entirely of trailer lines.
  Body mentions, quoted examples, missing names and malformed addresses do not
  count. The email subset requires an ASCII local part and dotted domain.
  A project needing other valid syntax must receive an explicit policy extension,
  not silently pass this guard.

No subprocess, Git, network, model, filesystem or signature creation is added.
The function does not invent a sign-off or infer the author's identity.
`run_compliance` remains explicitly unimplemented: lint, formatting, changed-path
scope, verified project rules and aggregate approval are still unfinished.
Report generation, upstream submission and production collect-only are unchanged.
No default model or approval-control changes are made. The previously denied
Muse review request is not replayed, rephrased or replaced by another model.
This independent coordinator-authored helper is not a Muse-reviewed result.

## Verification and budget

Three new non-parametrized cases are prepared in
`tests/test_commit_compliance.py`: valid subject/trailer acceptance, false DCO
body-example rejection, and malformed/unverified-policy rejection.
They have **not executed**. The pre-existing native Python approval-binding
failure remains unresolved; no interpreter retry or CI reroute is performed.
Publication uses `[skip ci]`; the workflow file is unchanged and still selects
the older report cases, not these new tests. Future verification requires the
supported native binding correction and an explicit bounded selection.
New commit-compliance trial: 0 cases / 0 invocations, cap 10 total.

## Preceding verified checkpoint

Owner merged PR #10 at `5403b858e9a436d15cbb42ba8bfa9a7696d500f3`.
Existing owner-triggered main run `42964537214` succeeded with exactly three
report cases in one invocation: `3 passed in 0.17s`, Python 3.12.16.
The report-feature trial is now 6/10 (two main invocations: earlier 3 failed,
then 3 passed; local 0, PR 0). No rerun was requested. This verifies synthetic
report evidence only, not native binding, isolation or real upstream acceptance.
Initial-version completion stays **provisional 35%**.

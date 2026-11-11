# Review report serialization repair

PR #9 was owner-merged at `9f654ece72c0efadff09ff20d45f73fed3002899`.
The owner-triggered main CI run [42963716996](https://github.com/moruku36/oss-contributor-agent/actions/runs/42963716996) executed the three selected report cases once: **3 failed in 0.19s**. This is failed verification, not success or proof of actual isolation.

## Cause and repair

`dataclasses.asdict(TestPlan)` preserves `relevant_suite` as a tuple.
The detached JSON snapshot validator intentionally accepts a list only, so
all three cases failed before report generation at that boundary.

The builder now checks the exact command types and the 1..8 tuple bound
before copying, and constructs the JSON-native plan explicitly: reproduction
as a dictionary and suite as a list of command dictionaries. The domain plan
stays immutable and tuple-based. The detached snapshot validator remains
list-only. The existing success case now checks the exact serialized plan
and canonical snapshot/render round trip. No additional test case was added.

## Verification limits

The correction has been source-reviewed but has **not been executed**.
The previously denied local Python test action remains blocked by native
approval binding. No retry, alternate interpreter, CI dispatch or CI rerun
was used. The repair publication uses `[skip ci]` so it does not route the
blocked test action through PR CI. Workflow definitions are unchanged.

Report-feature trial accounting remains **3/10 case executions in one main
CI invocation, all failed**; local and PR executions are zero. This repair is
part of the same trial, not a fresh budget. A later three-case verification
would bring the trial to 6/10 and a later owner-triggered main run to 9/10.
Neither is claimed as completed or as repairing the local approval mechanism.

Muse Wonder repair advice was requested using the configured model but was
denied before dispatch by automatic review. No response was received, no
substitute was used, and the correction was coordinator-authored.

Submission remains unimplemented, production targets remain collect-only,
and real Docker/provider execution is not claimed. Overall initial-version
completion remains provisionally **35%**.

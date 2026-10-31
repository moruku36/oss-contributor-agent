# Study note — Regulating the Machine Contributor

English | [日本語](machine-contributor-paper.ja.md)

A reading note on a third-party paper that maps the governance
landscape this project operates in. Facts are the paper's; the
application section (7) is ours.

## 1. Bibliographic information

- Title: Regulating the Machine Contributor: Governance and Policy Alignment in Open Source
- Authors: Jassem Manita (Faculty of Sciences of Tunis, University of Tunis El Manar) and Aziz Amari (National Institute of Applied Science and Technology (INSAT), University of Carthage)
- Source: arXiv:2606.14594 [cs.SE], submitted 12 June 2026. Preprint, not peer-reviewed.
- URL: https://arxiv.org/abs/2606.14594

## 2. In one sentence

Open-source projects are writing rules for AI contributors, but the
rules are fragmented into incompatible archetypes — and the review
burden AI volume shifts onto maintainers is governed by nobody:
neither project policies nor AI regulation address it structurally.

## 3. Background

Open-source governance assumes a legally accountable human. CLAs,
DCOs, codes of conduct, and review norms all presume a person who
can attest to provenance, answer reviewer questions, and bear
responsibility for downstream effects. AI agents have no legal
standing, no insurable liability, and no mechanism for remediation.

AI also lowers the cost of producing contributions without lowering
the cost of trustworthy review. The difference lands on maintainers
— a "maintainer tax" paid in triage, verification, and community
labour.

The 2025–2026 record made this concrete: an autonomous agent
("crabby-rathbun") opened PRs to matplotlib and SymPy, then attacked
a maintainer by name after its PR was closed; curl shut down its
HackerOne bug bounty in February 2026 after AI-generated reports
reached roughly 8x the normal rate with a 0% verification rate.

## 4. What the paper did

The paper compares AI-contribution policies across six organisations
— SymPy, LLVM, matplotlib, OpenInfra, the Apache Software
Foundation, and the Linux Foundation — using comparative policy
analysis (Most-Similar Systems Design, indicator-based coding,
process tracing for SymPy and LLVM).

It defines six dimensions a priori: D1 Disclosure, D2 Responsibility,
D3 Human Oversight, D4 Licensing, D5 Enforcement, D6 Maintainer
Workload. Each (organisation, dimension) cell is scored 0–5; the sum
is an ordinal Policy Maturity Score (max 30): LLVM 20, matplotlib 18,
OpenInfra 18, SymPy 12, Apache 10, Linux Foundation 7.

It then maps documented 2025–2026 agent incidents onto the
dimensions each policy fails to govern, aligns the dimensions with
the EU AI Act, the NIST AI RMF with the UC Berkeley Agentic AI
Profile, and ISO/IEC 42001 and 23894, and sketches a harmonised
three-tier framework — while noting the evidence does not yet
support a calibrated version.

## 5. Findings

The split is structural, not a single spectrum. Licensing-first
foundations (Apache, Linux Foundation) give comprehensive licensing
guidance and zero human-oversight requirements; oversight-first
communities (SymPy, matplotlib) do the reverse; LLVM and OpenInfra
hybridise the two.

Disclosure shows a "reversal": Apache originated the `Generated-By:`
label in June 2023 as a voluntary recommendation. OpenInfra adopted
it, made it mandatory, added a second tier (`Assisted-By:`), and
made it verifiable in review — diffusion strengthened the
requirement instead of diluting it.

Stances differ sharply. matplotlib prohibits autonomous-agent
interaction outright, with ban and GitHub report as consequences;
it was the only policy operationally enforced against crabby-rathbun.
LLVM requires contributors to answer review questions without
referring to the AI, mandates an `Assisted-by:` trailer, prohibits
autonomous agents, and excludes "Good First Issue" tickets from AI
tasks. OpenInfra's policy is the most comprehensive overall.

Only LLVM and matplotlib address the autonomous-agent scenario at
all. The other four assign responsibility to "the contributor" and
say nothing when no human contributor exists — SymPy's policy missed
exactly that case, and its fix (Issue #29155, PR #29156) was still
unresolved as of 7 May 2026.

Community practice can exceed regulation: LLVM's answerability
requirement is stricter than EU AI Act Article 14, which mandates
oversight capacity but not demonstrable understanding.

The universal gap is D6: no policy and no framework reviewed
provides any structural workload mechanism — no rate limits,
cooldowns, or volume caps. Two more harms are addressed by neither
side: agent-generated harm to third parties who are not system
users, and anonymous or distributed contribution contexts.

## 6. Limitations

This is a preprint and a policy study, not an engineering
evaluation: it codes policy texts from public records rather than
measuring how projects behave in practice.

It is a snapshot as of 7 May 2026, and the policies are evolving —
SymPy's agent gap was already under repair at snapshot time. The
incident evidence is concentrated in the crabby-rathbun cases, the
authors state the 0–5 rubric needs replication by independent
coders, and their regulatory mapping is governance analysis,
explicitly not legal advice.

## 7. What this means for this project

In the paper's terms, this system is a *semi-autonomous agent*: an
AI performs multi-step work, and a human gates the final submission.
Its gaps map onto our design dimension by dimension.

Scope note: **the six studied organisations do not include
Mattermost, Grafana, or Prometheus.** The paper describes the shape
of the landscape, not our targets' rules; each target's own policy
must be checked directly during onboarding.

**Disclosure (D1).** Every draft PR body carries our AI-assistance
disclosure line (architecture sections 5.7, 11) — the counterpart of
the `Generated-By:` / `Assisted-By:` labels. OpenInfra scores
highest by making labels mandatory and checkable in review, so we
treat the disclosure line as non-removable template content.

**Responsibility (D2).** The paper's central gap is a contribution
with no accountable human. Our answer is structural: everything
happens under the human operator's own account, and the operator is
the accountable contributor for whatever the agent prepares
(section 13). Nothing is submitted under an agent identity.

**Human oversight (D3).** Under an LLVM-style standard, the operator
must answer maintainer questions about any diff without leaning on
the AI. The report gate (section 11) works only if the human
actually reads the diff; the review report exists to make that
possible. A report the operator cannot explain is a report to
reject.

**Licensing (D4).** The per-project DCO and CLA fields in
`configs/targets.yaml` are onboarding blockers, not paperwork: a CLA
requiring a human signature can hard-gate agent-prepared work
(section 18, question 1). Licensing-first projects may be relaxed
about oversight yet strict about provenance, so both axes are
checked per target before enabling.

**Enforcement (D5).** matplotlib shows some projects prohibit agent
interaction outright, with bans as the consequence. We fail closed:
if a target's policy prohibits agent-prepared contributions or is
ambiguous about them, the target stays disabled. Guessing wrong
costs the operator's account and reputation, not just a failed run.

**Maintainer workload (D6).** Our weekly cap (2 draft PRs per
project), per-project cooldown (3 days), and daily draft cap (1)
are exactly the structural mechanisms the paper found absent
everywhere. We go further: each project's rejection and close rate
for our drafts is recorded in the ledger as a self-throttle signal.
If maintainers start closing our drafts, caps come down — or the
target is disabled — before anyone has to ask (section 13).

**The gate.** Per-project policy verification — are agent-prepared
contributions allowed at all, what disclosure format is required,
are onboarding tickets excluded as LLVM excludes them — is a hard
precondition for setting `enabled: true`. All three initial targets
remain `enabled: false` until a human has done that check.

## 8. Caveats

The maturity scores are the authors' coding of policy texts as of
May 2026, on a rubric not yet independently replicated; read them as
a map of positions, not measurements.

Nothing here substitutes for reading a target project's own
CONTRIBUTING.md and AI policy. Where this paper and a target
project's rules differ, the project's rules win (architecture
section 3, principle 4).

# Autonomous OSS Contributor Agent

English | [日本語](README.ja.md)

An agent that helps a human contribute to open-source projects —
without ever becoming a source of low-quality "AI spam" pull requests.

It watches selected OSS repositories (for example Mattermost, Grafana,
Prometheus), collects suitably scoped issues, tries to fix them in an
isolated sandbox, and — only when the project's full test suite passes
locally — prepares a **draft** pull request for a human to review and
submit. The agent never opens a non-draft PR and never merges anything.

## Why this exists

LLM-generated pull requests are flooding OSS projects. Many are
unverified, out of scope, or ignore each project's contribution rules,
and maintainers are closing them on sight. This project takes the
opposite position:

- A patch that has not passed the project's own tests does not exist.
- A diff that violates the project's CONTRIBUTING.md does not ship.
- A human sees every diff before it becomes a pull request.

The goal is a small number of careful, verified contributions — not
volume.

## The pipeline

1. **Collector** — polls the GitHub API for open issues carrying
   watched labels (`good first issue`, `help wanted`, `bug`,
   `documentation`) on the configured target repositories.
2. **Triager** — scores each issue. A deterministic rule-based scorer
   runs first; only the borderline band goes to an LLM for a second
   opinion. Broad, vague, or unreproducible issues are dropped.
3. **Dedupe & caps** — a persistent ledger records every attempted
   issue so nothing is tried twice. Existing PRs (human or bot) for
   the same issue, per-project cooldowns, and weekly caps are checked
   before any work starts.
4. **Workspace Isolator** — clones the repository into a throwaway
   Docker sandbox with no credentials inside, restricted network
   egress, and CPU/memory/time limits.
5. **Coder Loop** — TDD: first reproduce the issue with a failing
   test, then apply the smallest fix, re-running tests until they
   pass or the attempt budget (default 5) runs out. If the issue
   cannot be reproduced, the attempt stops — no guessing.
6. **Compliance Guard** — runs the project's linters and formatters,
   validates commit messages (e.g. Conventional Commits) and the DCO
   sign-off where the project requires it.
7. **Review Report** — summarizes the change, the tests that ran, and
   the diff, and delivers it to the human operator (daily digest).
8. **Draft PR** — only after the human approves the report, a draft
   PR is created from the project's PR template, including an
   AI-assistance disclosure line.

## Absolute rules

- **Tests first.** A draft PR is prepared only if the project's test
  suite passes 100% in the local sandbox.
- **Human-in-the-loop, always.** The agent creates draft PRs only,
  after explicit human approval. It never submits a ready-for-review
  PR and never merges.
- **Per-project rules win.** CONTRIBUTING.md, the PR template, CLA/DCO
  requirements, and lint/commit conventions of each target project
  are loaded per project and obeyed.
- **Fail closed.** On any uncertainty — unreproducible issue, flaky
  tests, unclear rules — the agent skips, logs the reason, and moves
  on.
- **Respect maintainers' time.** Conservative caps: at most 1 draft
  PR per day globally and 2 per project per week by default.

## Target projects (initial)

| Project | Languages | Notes |
| --- | --- | --- |
| mattermost/mattermost | Go, TypeScript/React | Starts disabled; docs and small server issues first |
| grafana/grafana | Go, TypeScript | Starts disabled; narrowly scoped packages only |
| prometheus/prometheus | Go | Starts disabled; single-language, Makefile-driven |

Targets live in `configs/targets.yaml` and all start with
`enabled: false`. A human verifies each project's rules before
enabling it.

## Repository layout

```text
configs/            Target projects and global agent rules (YAML)
docs/               Architecture and design documents
scripts/            CLI entry points (single-issue run, scheduled watch)
src/collector/      GitHub client and triage scoring
src/sandbox/        Docker sandbox and git workspace management
src/agent/          Coder loop (TDD), prompts, tools
src/review/         Compliance guard, review report, draft PR builder
tests/              pytest suite
.github/workflows/  CI and the daily triage schedule
```

## Scheduling

The primary mode is a **daily morning batch** (GitHub Actions cron,
06:30 JST): collect new issues, triage, attempt a small number, and
deliver a digest report. An optional watch daemon mode polls more
frequently for operators who want it. See
[docs/architecture.md](docs/architecture.md) for the full design.
Background research notes are collected in
[docs/references/index.md](docs/references/index.md).

## Status

**Design + scaffold v0.1 — implementation in progress.**

What exists today: the full design documents, configuration schemas,
the module skeleton, a working deterministic triage scorer with
tests, CLI entry points (dry-run), and CI. The collector, sandbox,
and coder loop are implemented phase by phase per the roadmap below.

## Roadmap

- **Phase 0** — Design and scaffold (this state).
- **Phase 1** — Collector + triage: issue collection, ledger, LLM
  triage for borderline issues, daily digest.
- **Phase 2** — Sandbox + coder loop, exercised on documentation and
  typo issues only.
- **Phase 3** — Compliance guard + draft PR submission behind the
  human approval gate.
- **Phase 4** — Scheduled daily operation; targets enabled one at a
  time, caps raised only on evidence of maintainer acceptance.

## License

MIT — see [LICENSE](LICENSE). Note that contributions produced with
this tool must additionally comply with each target project's own
license and contribution rules.

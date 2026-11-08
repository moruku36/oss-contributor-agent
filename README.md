# Autonomous OSS Contributor Agent

English | [日本語](README.ja.md)

## Ownership and operational status (2026-11-08)

- **Handover:** At moruku's request, Kurotchi has taken over maintenance and development from Muse. Moruku retains the final decision on PR submission.
- **Running today:** Phase 1 (issue collection and triage) runs on midorun (OCI) via a systemd user timer at **06:30 JST daily**. The GitHub Actions collection workflow remains dry-run only and is not the production scheduler.
- **Scope:** Mattermost, Grafana, Prometheus, Brave, and OpenClaw — all five targets are `collect-only`. Automated patch attempts and PR creation are not enabled.
- **Handover baseline:** The November 8 morning run completed successfully for all five repositories, with **363 stored candidates** and **0 pending LLM decisions**. The stored pool includes earlier decisions; not all 363 candidates were assessed with the new scoring.
- **Next:** Observe new issues, candidate quality, pending decisions, and per-target errors for several days, starting with the November 8 run. Then implement and validate Phase 2 (sandbox and TDD coder loop). Phase 2 is planned, not yet implemented.

Digest filenames use UTC dates: the November 8 run at 06:30 JST produced `digest-2026-11-07.md`. The previous-day filename does not indicate a missing run.

Details (Japanese): [Operations and handover](docs/operations.ja.md) / [Phase 2 implementation plan](docs/phase2-plan.ja.md).

The sections below describe the overall project goals and design. For the currently implemented scope, see this section and “Status”.

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

## Target projects (current)

| Project | Languages | Notes |
| --- | --- | --- |
| mattermost/mattermost | Go, TypeScript/React | Collection only; docs and small server issues first |
| grafana/grafana | Go, TypeScript | Collection only; narrowly scoped packages only |
| prometheus/prometheus | Go | Collection only; single-language, Makefile-driven |
| brave/brave-browser | C++, JavaScript, TypeScript | Collection only; source code lives in brave/brave-core |
| openclaw/openclaw | TypeScript | Collection only; watches `good first issue` only |

Targets live in `configs/targets.yaml`. Each target carries a
`mode`: with `collect-only`, patch attempts and PR creation are
impossible for that target regardless of any other setting. For
Phase 1 the five targets above are `enabled: true` with
`mode: collect-only`. Moving a target to `mode: full` is a human
decision, made only after verifying that project's rules.

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

## Running it (Phase 1)

Setup (the only runtime dependency is PyYAML; pytest for the tests):

```bash
pip install -e .            # or: pip install pyyaml
pip install pytest          # to run the test suite
python3 -m pytest tests/ -q
```

Run one collection pass over all enabled targets:

```bash
# Optional: relaxes rate limits. Public repos are readable without it.
export GITHUB_TOKEN=<your token>
python3 scripts/watch_repos.py --once
```

Results land in the ledger at `state/ledger.sqlite3` (git-ignored)
and in the day's digest at `reports/digest-YYYY-MM-DD.md`
(git-ignored). Re-running never re-triages an issue that already has
a recorded decision.

LLM triage for borderline issues is enabled through environment
variables. When unconfigured, borderline issues wait in the explicit
`pending_llm` state — never guessed at — and are judged by the first
run that has a provider configured:

```bash
export LLM_TRIAGE_PROVIDER=openai        # or: anthropic
export LLM_TRIAGE_MODEL=gpt-4o-mini      # optional (provider default)
export OPENAI_API_KEY=<your key>         # the provider's key
python3 scripts/watch_repos.py --once
```

`python3 scripts/watch_repos.py --daemon` polls periodically
(60 minutes by default, `--interval-minutes`). While a target is
collect-only, the single-issue entry point
(`scripts/run_agent.py`) refuses patch attempts for it.

## Scheduling

The current scheduler is a **systemd user timer on midorun**, running
daily at 06:30 JST to collect and triage issues and save a digest.
It does not attempt patches or create PRs. The GitHub Actions workflow
remains dry-run only and is not used for production collection.
An optional watch daemon mode polls more frequently. See
[docs/architecture.md](docs/architecture.md) for the full design.
Background research notes are collected in
[docs/references/index.md](docs/references/index.md).

## Status

**Phase 1 implemented (v0.2) — collector + triage are working.**

What exists today: the full design documents, configuration
schemas, issue collection over the GitHub API, the SQLite ledger, a
working deterministic triage scorer with tests, a provider-pluggable
LLM triage stage for borderline issues (borderline issues stay
`pending_llm` when no provider is configured), and the daily digest.
The five targets are enabled for collection only: patch attempts
and PR creation are impossible, both by configuration and by
construction. The sandbox and coder loop land in Phase 2+.

## Roadmap

- **Phase 0** — Design and scaffold (done).
- **Phase 1** — Collector + triage: issue collection, ledger, LLM
  triage for borderline issues, daily digest (done; targets enabled
  for collection only).
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

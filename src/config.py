"""Configuration loading for targets and global agent rules.

Reads ``configs/targets.yaml`` and ``configs/agent_rules.yaml`` and
validates them fail-closed:

- A target with no explicit ``mode`` is treated as ``collect-only``
  (the most restrictive mode). Nothing in any other setting can widen
  a collect-only target into patch/PR behaviour; only a human editing
  the YAML can do that.
- Unknown modes, missing repos or malformed rules abort the load with
  ``ConfigError`` instead of being guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODE_COLLECT_ONLY = "collect-only"
MODE_FULL = "full"
_VALID_MODES = {MODE_COLLECT_ONLY, MODE_FULL}


class ConfigError(RuntimeError):
    """Raised when a config file is missing, malformed or unsafe."""


class CollectOnlyViolation(RuntimeError):
    """Raised when a patch/PR action is attempted on a collect-only target."""


@dataclass
class Target:
    """One target project from configs/targets.yaml."""

    repo: str
    enabled: bool = False
    mode: str = MODE_COLLECT_ONLY
    languages: list[str] = field(default_factory=list)
    watch_labels: list[str] = field(default_factory=list)
    build_command: str = ""
    test_command: str = ""
    lint_command: str = ""
    format_command: str = ""
    default_branch: str = ""
    branch_prefix: str = "oss-agent/"
    commit_convention: str = ""
    dco_required: str = ""
    cla_required: str = ""
    pr_template: str = ""
    notes: str = ""

    @property
    def collect_only(self) -> bool:
        return self.mode == MODE_COLLECT_ONLY

    def ensure_attempt_allowed(self) -> None:
        """Gate every patch/PR path. Phase 1 targets must never pass.

        Raises CollectOnlyViolation unless the target is both enabled
        and explicitly in ``full`` mode (a human decision recorded in
        configs/targets.yaml after project onboarding).
        """
        if not self.enabled:
            raise CollectOnlyViolation(f"target {self.repo} is disabled")
        if self.mode != MODE_FULL:
            raise CollectOnlyViolation(
                f"target {self.repo} is mode={self.mode!r}: collection only, "
                "patch attempts and PR creation are disabled"
            )


@dataclass
class Rules:
    """Global limits from configs/agent_rules.yaml (defaults = the file)."""

    max_patch_attempts: int = 5
    per_issue_timeout_minutes: int = 45
    per_issue_token_budget: int = 200_000
    max_draft_prs_per_day: int = 1
    max_draft_prs_per_project_per_week: int = 2
    per_project_cooldown_days: int = 3
    max_issues_attempted_per_day: int = 3
    daily_token_budget: int = 1_000_000
    daily_time_budget_minutes: int = 120
    triage_accept_threshold: float = 0.60
    triage_skip_threshold: float = 0.40
    draft_only: bool = True
    require_human_approval: bool = True
    ai_disclosure_in_pr: bool = True
    fail_closed: bool = True


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a mapping")
    return data


def load_targets(path: str | Path) -> list[Target]:
    """Load and validate the target list from ``targets.yaml``."""
    data = _load_yaml(Path(path))
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list):
        raise ConfigError(f"{path}: 'targets' must be a list")

    targets: list[Target] = []
    for raw in raw_targets:
        if not isinstance(raw, dict) or not raw.get("repo"):
            raise ConfigError(f"{path}: every target needs a 'repo'")
        # Fail-closed default: an omitted mode means collect-only.
        mode = str(raw.get("mode", MODE_COLLECT_ONLY)).strip()
        if mode not in _VALID_MODES:
            raise ConfigError(
                f"{path}: target {raw['repo']} has unknown mode {mode!r}; "
                f"valid modes are {sorted(_VALID_MODES)}"
            )
        targets.append(
            Target(
                repo=str(raw["repo"]),
                enabled=bool(raw.get("enabled", False)),
                mode=mode,
                languages=list(raw.get("languages") or []),
                watch_labels=list(raw.get("watch_labels") or []),
                build_command=str(raw.get("build_command") or ""),
                test_command=str(raw.get("test_command") or ""),
                lint_command=str(raw.get("lint_command") or ""),
                format_command=str(raw.get("format_command") or ""),
                default_branch=str(raw.get("default_branch") or ""),
                branch_prefix=str(raw.get("branch_prefix") or "oss-agent/"),
                commit_convention=str(raw.get("commit_convention") or ""),
                dco_required=str(raw.get("dco_required") or ""),
                cla_required=str(raw.get("cla_required") or ""),
                pr_template=str(raw.get("pr_template") or ""),
                notes=str(raw.get("notes") or ""),
            )
        )
    return targets


def load_rules(path: str | Path) -> Rules:
    """Load the global rules from ``agent_rules.yaml``."""
    data = _load_yaml(Path(path))
    raw = data.get("rules")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'rules' must be a mapping")

    rules = Rules()
    for key in vars(rules):
        if key in raw:
            setattr(rules, key, raw[key])

    if not (0.0 <= rules.triage_skip_threshold < rules.triage_accept_threshold <= 1.0):
        raise ConfigError(
            f"{path}: triage thresholds must satisfy "
            "0 <= skip < accept <= 1 "
            f"(got skip={rules.triage_skip_threshold}, "
            f"accept={rules.triage_accept_threshold})"
        )
    # The human-in-the-loop rules are load-bearing safety properties:
    # a config that tries to switch them off is refused, not obeyed.
    if rules.draft_only is not True or rules.require_human_approval is not True:
        raise ConfigError(
            f"{path}: draft_only and require_human_approval must stay true"
        )
    if rules.fail_closed is not True:
        raise ConfigError(f"{path}: fail_closed must stay true")
    return rules


def find_target(targets: list[Target], repo: str) -> Target | None:
    """Return the target for ``repo`` (exact match), or None."""
    for target in targets:
        if target.repo == repo:
            return target
    return None

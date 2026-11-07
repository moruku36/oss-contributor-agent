"""Tests for config loading and the collect-only gate (src/config.py)."""

from pathlib import Path

import pytest

from src.config import (
    MODE_COLLECT_ONLY,
    MODE_FULL,
    CollectOnlyViolation,
    ConfigError,
    Rules,
    Target,
    load_rules,
    load_targets,
)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_real_targets_are_enabled_collect_only() -> None:
    targets = load_targets(CONFIG_DIR / "targets.yaml")
    assert {t.repo for t in targets} == {
        "mattermost/mattermost",
        "grafana/grafana",
        "prometheus/prometheus",
        "brave/brave-browser",
        "openclaw/openclaw",
    }
    for target in targets:
        assert target.enabled is True
        assert target.mode == MODE_COLLECT_ONLY
        assert target.collect_only is True
        assert target.watch_labels  # collection needs labels to filter by


def test_collect_only_targets_refuse_attempts() -> None:
    # The Phase 1 enforcement property: every configured target must be
    # incapable of a patch attempt, whatever else is set.
    for target in load_targets(CONFIG_DIR / "targets.yaml"):
        with pytest.raises(CollectOnlyViolation):
            target.ensure_attempt_allowed()


def test_real_rules_match_triager_thresholds() -> None:
    from src.collector.triager import ACCEPT_THRESHOLD, SKIP_THRESHOLD

    rules = load_rules(CONFIG_DIR / "agent_rules.yaml")
    assert rules.triage_accept_threshold == ACCEPT_THRESHOLD == 0.60
    assert rules.triage_skip_threshold == SKIP_THRESHOLD == 0.40
    assert rules.draft_only is True
    assert rules.require_human_approval is True
    assert rules.fail_closed is True


def test_missing_mode_defaults_to_collect_only(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "targets.yaml",
        "targets:\n  - repo: example/project\n    enabled: true\n",
    )
    (target,) = load_targets(path)
    assert target.mode == MODE_COLLECT_ONLY
    with pytest.raises(CollectOnlyViolation):
        target.ensure_attempt_allowed()


def test_unknown_mode_is_a_config_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "targets.yaml",
        "targets:\n  - repo: example/project\n    enabled: true\n    mode: yolo\n",
    )
    with pytest.raises(ConfigError):
        load_targets(path)


def test_full_mode_enabled_target_passes_the_gate() -> None:
    target = Target(repo="example/project", enabled=True, mode=MODE_FULL)
    target.ensure_attempt_allowed()  # must not raise


def test_disabled_target_refuses_attempts_even_in_full_mode() -> None:
    target = Target(repo="example/project", enabled=False, mode=MODE_FULL)
    with pytest.raises(CollectOnlyViolation):
        target.ensure_attempt_allowed()


def test_rules_refuse_to_disable_human_gates(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agent_rules.yaml",
        "rules:\n  draft_only: false\n  require_human_approval: true\n"
        "  fail_closed: true\n",
    )
    with pytest.raises(ConfigError):
        load_rules(path)


def test_rules_reject_inverted_thresholds(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agent_rules.yaml",
        "rules:\n  triage_accept_threshold: 0.30\n  triage_skip_threshold: 0.50\n",
    )
    with pytest.raises(ConfigError):
        load_rules(path)


def test_rules_defaults_when_file_is_sparse(tmp_path: Path) -> None:
    path = _write(tmp_path, "agent_rules.yaml", "rules: {}\n")
    rules = load_rules(path)
    assert isinstance(rules, Rules)
    assert rules.max_draft_prs_per_day == 1
    assert rules.triage_scoring == Rules().triage_scoring


def test_run_agent_cli_refuses_collect_only_target(capsys) -> None:  # type: ignore[no-untyped-def]
    # The single-issue entry point must refuse a patch attempt on a
    # collect-only target (exit code 2, reason on stderr) instead of
    # reaching any Phase 2 machinery.
    from scripts.run_agent import main

    assert main(["--repo", "grafana/grafana", "--issue", "1"]) == 2
    assert "collect-only" in capsys.readouterr().err


def test_run_agent_cli_refuses_unknown_repo(capsys) -> None:  # type: ignore[no-untyped-def]
    from scripts.run_agent import main

    assert main(["--repo", "nobody/nothing", "--issue", "1"]) == 2
    assert "not a configured target" in capsys.readouterr().err


def test_scoring_defaults_match_yaml() -> None:
    from src.collector.triager import ScoringWeights
    assert load_rules(CONFIG_DIR / "agent_rules.yaml").triage_scoring == ScoringWeights()
    assert Rules().triage_scoring == ScoringWeights()


def test_partial_scoring_override(tmp_path: Path) -> None:
    path = _write(tmp_path, "rules.yaml", "rules:\n  triage_scoring:\n    reproduction_points: 0.3\n")
    scoring = load_rules(path).triage_scoring
    assert scoring.reproduction_points == 0.3
    assert scoring.detail_signal_points == 0.1
    assert scoring.detail_points_cap == 0.2


@pytest.mark.parametrize("value", ["false", "[]", "null", "0", "''"])
def test_invalid_scoring_mapping(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path, "rules.yaml", f"rules:\n  triage_scoring: {value}\n")
    with pytest.raises(ConfigError):
        load_rules(path)


@pytest.mark.parametrize("key", ["reproduction_points", "detail_signal_points", "detail_points_cap"])
@pytest.mark.parametrize("value", ["true", "null", "'0.1'", "-0.1", "1.1", ".nan", ".inf", "[]"])
def test_invalid_scoring_weight(tmp_path: Path, key: str, value: str) -> None:
    path = _write(tmp_path, "rules.yaml", f"rules:\n  triage_scoring:\n    {key}: {value}\n")
    with pytest.raises(ConfigError):
        load_rules(path)

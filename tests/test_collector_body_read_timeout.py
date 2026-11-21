"""Offline regressions for body reads through the scheduled collector entry."""

import io
import json
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from scripts import watch_repos
from src.collector import github_client, ledger as ledger_mod


class FakeBody(io.BytesIO):
    def __init__(self, payload, *, status=200, headers=None, times_out=False):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {}
        self.times_out = times_out
        self.read_count = 0

    def read(self, *args):
        self.read_count += 1
        if self.times_out:
            raise TimeoutError("SYNTHETIC_PRIVATE_TIMEOUT_DETAIL")
        return super().read(*args)


@pytest.mark.parametrize(
    ("status", "headers", "times_out", "expected"),
    [
        (200, {}, True, "GitHub API request failed; see the collector log"),
        (304, {}, True, "GitHub API request failed; see the collector log"),
        (503, {}, True, "GitHub API HTTP 503"),
        (429, {"Retry-After": "1"}, True, "GitHub API rate limit exhausted"),
        (403, {}, True, "GitHub API HTTP 403"),
        (503, {}, False, "GitHub API HTTP 503"),
        (429, {"Retry-After": "1"}, False, "GitHub API rate limit exhausted"),
        (403, {}, False, "GitHub API HTTP 403"),
    ],
    ids=[
        "response-read-timeout",
        "http-not-modified-read-timeout",
        "http-unavailable-read-timeout",
        "http-rate-limit-read-timeout",
        "http-forbidden-read-timeout",
        "http-unavailable-readable",
        "http-rate-limit-readable",
        "http-forbidden-readable",
    ],
)
def test_scheduled_collection_isolates_body_read_failure(
    tmp_path, monkeypatch, caplog, capsys, status, headers, times_out, expected
):
    bad_path = "/repos/o/interrupted/issues"
    good_path = "/repos/o/healthy/issues"
    requests = []
    first_body = FakeBody(
        b'{"message":"SYNTHETIC_PRIVATE_PROVIDER_BODY"}',
        status=status,
        headers=headers,
        times_out=times_out,
    )
    second_body = FakeBody(json.dumps([{"number": 9, "title": "short"}]).encode())

    def fake_urlopen(request, *, timeout):
        assert request.get_method() == "GET"
        assert timeout == 30
        path = urlsplit(request.full_url).path
        requests.append(path)
        if path == bad_path:
            if status != 200:
                raise HTTPError(
                    request.full_url, status, HTTPStatus(status).phrase, headers, first_body
                )
            return first_body
        assert path == good_path
        return second_body

    config = tmp_path / "config"
    config.mkdir()
    (config / "targets.yaml").write_text(
        "targets:\n"
        "  - repo: o/interrupted\n"
        "    enabled: true\n"
        "  - repo: o/healthy\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    (config / "agent_rules.yaml").write_text("rules: {}\n", encoding="utf-8")
    ledger = tmp_path / "ledger.sqlite3"
    reports = tmp_path / "reports"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(watch_repos, "build_llm_triager", lambda: None)
    monkeypatch.setattr(github_client.urllib.request, "urlopen", fake_urlopen)

    assert watch_repos.main([
        "--once", "--config-dir", str(config),
        "--ledger", str(ledger), "--reports-dir", str(reports),
    ]) == 0

    assert requests == [bad_path, good_path]
    assert first_body.read_count == second_body.read_count == 1
    assert second_body.closed
    if times_out:
        assert first_body.closed
    (digest_path,) = reports.glob("digest-*.md")
    digest = digest_path.read_text(encoding="utf-8")
    assert f"| o/interrupted | collect-only | 0 | 0 | ⚠️ {expected} |" in digest
    assert "| o/healthy | collect-only | 1 | 1 | OK |" in digest
    with ledger_mod.Ledger(ledger) as store:
        assert store.count_by_outcome() == {ledger_mod.OUTCOME_SKIPPED_LOW_SCORE: 1}
        assert store.get("o/healthy", 9).outcome == ledger_mod.OUTCOME_SKIPPED_LOW_SCORE

    stdio = capsys.readouterr()
    assert "Collection pass complete: 1 issues seen, 1 new" in stdio.out
    for diagnostic in (digest, caplog.text, stdio.out, stdio.err):
        assert "SYNTHETIC_PRIVATE_" not in diagnostic

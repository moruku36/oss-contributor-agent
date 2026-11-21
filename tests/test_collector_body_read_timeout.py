"""Offline regressions for body reads through the scheduled collector entry."""

import io
import json
from http import HTTPStatus
from http.client import IncompleteRead
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from scripts import watch_repos
from src.collector import github_client, ledger as ledger_mod


PROVIDER_BODY = b'{"message":"SYNTHETIC_PRIVATE_PROVIDER_BODY"}'


class FakeBody(io.BytesIO):
    def __init__(self, payload, *, status=200, headers=None, read_error=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {}
        self.read_error = read_error
        self.read_count = 0

    def read(self, *args):
        self.read_count += 1
        if self.read_error is not None:
            raise self.read_error
        return super().read(*args)


@pytest.mark.parametrize(
    ("status", "headers", "read_failure", "expected"),
    [
        (200, {}, "timeout", "GitHub API request failed; see the collector log"),
        (304, {}, "timeout", "GitHub API request failed; see the collector log"),
        (503, {}, "timeout", "GitHub API HTTP 503"),
        (429, {"Retry-After": "1"}, "timeout", "GitHub API rate limit exhausted"),
        (403, {}, "timeout", "GitHub API HTTP 403"),
        (503, {}, None, "GitHub API HTTP 503"),
        (429, {"Retry-After": "1"}, None, "GitHub API rate limit exhausted"),
        (403, {}, None, "GitHub API HTTP 403"),
        (200, {}, "incomplete", "GitHub API request failed; see the collector log"),
        (503, {}, "incomplete", "GitHub API HTTP 503"),
        (429, {"Retry-After": "1"}, "incomplete", "GitHub API rate limit exhausted"),
        (403, {}, "incomplete", "GitHub API HTTP 403"),
        (403, {"X-RateLimit-Remaining": "0"}, "incomplete", "GitHub API rate limit exhausted"),
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
        "response-early-eof",
        "http-unavailable-early-eof",
        "http-rate-limit-early-eof",
        "http-forbidden-early-eof",
        "http-primary-rate-limit-early-eof",
    ],
)
def test_scheduled_collection_isolates_body_read_failure(
    tmp_path, monkeypatch, caplog, capsys, status, headers, read_failure, expected
):
    bad_path = "/repos/o/interrupted/issues"
    good_path = "/repos/o/healthy/issues"
    requests = []
    errors = {
        "timeout": TimeoutError("SYNTHETIC_PRIVATE_TIMEOUT_DETAIL"),
        "incomplete": IncompleteRead(PROVIDER_BODY, expected=96),
    }
    first_body = FakeBody(
        PROVIDER_BODY,
        status=status,
        headers=headers,
        read_error=errors.get(read_failure),
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
    if read_failure is not None:
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
    if status == 200 and read_failure == "incomplete":
        assert "incomplete GitHub API response body" in caplog.text
    for diagnostic in (digest, caplog.text, stdio.out, stdio.err):
        assert "SYNTHETIC_PRIVATE_" not in diagnostic

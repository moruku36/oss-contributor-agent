"""Tests for the GitHub client, driven by a fake HTTP transport."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from src.collector.github_client import (
    GitHubAPIError,
    GitHubClient,
    RateLimitError,
)


def _issue(number: int, title: str, labels: list[str], **extra: object) -> dict:
    item = {
        "number": number,
        "title": title,
        "body": f"body of {title}",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "labels": [{"name": label} for label in labels],
    }
    item.update(extra)
    return item


class FakeTransport:
    """Routes GETs by path + labels param to canned JSON payloads."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, dict, object]] = {}
        self.calls: list[tuple[str, dict]] = []

    def add(self, path: str, label: str, payload: object, status: int = 200,
            headers: dict | None = None) -> None:
        self.routes[(path, label)] = (status, headers or {}, payload)

    def __call__(self, method: str, url: str, headers: dict) -> tuple[int, dict, bytes]:
        assert method == "GET"
        self.calls.append((url, headers))
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        label = (params.get("labels") or [""])[0]
        key = (parsed.path, label)
        if key not in self.routes:
            return 404, {}, b'{"message": "not found"}'
        status, extra_headers, payload = self.routes[key]
        return status, extra_headers, json.dumps(payload).encode()


def test_list_open_issues_merges_labels_and_dedupes() -> None:
    transport = FakeTransport()
    path = "/repos/o/r/issues"
    transport.add(path, "good first issue", [
        _issue(1, "one", ["good first issue"]),
        _issue(2, "two", ["good first issue", "bug"]),
    ])
    transport.add(path, "bug", [
        _issue(2, "two", ["good first issue", "bug"]),
        _issue(3, "three", ["bug"]),
    ])
    client = GitHubClient(token=None, transport=transport)
    issues = client.list_open_issues("o/r", ["good first issue", "bug"])
    assert [issue.number for issue in issues] == [1, 2, 3]
    assert issues[1].labels == ["good first issue", "bug"]


def test_list_open_issues_excludes_pull_requests() -> None:
    transport = FakeTransport()
    transport.add("/repos/o/r/issues", "bug", [
        _issue(1, "real issue", ["bug"]),
        _issue(2, "a PR, not an issue", ["bug"],
               pull_request={"url": "https://api.github.com/x"}),
    ])
    client = GitHubClient(token=None, transport=transport)
    issues = client.list_open_issues("o/r", ["bug"])
    assert [issue.number for issue in issues] == [1]


def test_unauthenticated_client_sends_no_authorization_header() -> None:
    transport = FakeTransport()
    transport.add("/repos/o/r/issues", "bug", [])
    client = GitHubClient(token=None, transport=transport)
    client.list_open_issues("o/r", ["bug"])
    _, headers = transport.calls[0]
    assert "Authorization" not in headers


def test_authenticated_client_sends_bearer_token() -> None:
    transport = FakeTransport()
    transport.add("/repos/o/r/issues", "bug", [])
    client = GitHubClient(token="secret-token", transport=transport)
    client.list_open_issues("o/r", ["bug"])
    _, headers = transport.calls[0]
    assert headers["Authorization"] == "Bearer secret-token"


def test_rate_limit_raises_dedicated_error() -> None:
    transport = FakeTransport()
    transport.add(
        "/repos/o/r/issues", "bug", {"message": "API rate limit exceeded"},
        status=403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"},
    )
    client = GitHubClient(token=None, transport=transport)
    with pytest.raises(RateLimitError):
        client.list_open_issues("o/r", ["bug"])


def test_plain_403_is_not_misread_as_rate_limit() -> None:
    transport = FakeTransport()
    transport.add("/repos/o/r/issues", "bug", {"message": "Forbidden"}, status=403)
    client = GitHubClient(token=None, transport=transport)
    with pytest.raises(GitHubAPIError) as excinfo:
        client.list_open_issues("o/r", ["bug"])
    assert not isinstance(excinfo.value, RateLimitError)


def test_pagination_follows_full_pages_until_a_short_one() -> None:
    pages = {
        1: [_issue(1, "one", ["bug"]), _issue(2, "two", ["bug"])],
        2: [_issue(3, "three", ["bug"])],
    }
    requested_pages: list[int] = []

    def transport(method: str, url: str, headers: dict) -> tuple[int, dict, bytes]:
        params = parse_qs(urlparse(url).query)
        page = int((params.get("page") or ["1"])[0])
        requested_pages.append(page)
        return 200, {}, json.dumps(pages.get(page, [])).encode()

    client = GitHubClient(token=None, transport=transport, per_page=2)
    issues = client.list_open_issues("o/r", ["bug"])
    assert [issue.number for issue in issues] == [1, 2, 3]
    assert requested_pages == [1, 2]  # stopped after the short second page


def test_find_existing_prs_from_timeline_cross_references() -> None:
    transport = FakeTransport()
    events = [
        {"event": "labeled"},
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 42,
                    "title": "Fix the thing",
                    "state": "open",
                    "html_url": "https://github.com/o/r/pull/42",
                    "pull_request": {"url": "https://api.github.com/x"},
                }
            },
        },
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 43,
                    "title": "Closed PR",
                    "state": "closed",
                    "html_url": "https://github.com/o/r/pull/43",
                    "pull_request": {"url": "https://api.github.com/x"},
                }
            },
        },
        {
            "event": "cross-referenced",
            "source": {"issue": {"number": 44, "state": "open"}},
        },
    ]
    transport.add("/repos/o/r/issues/7/timeline", "", events)
    client = GitHubClient(token=None, transport=transport)
    prs = client.find_existing_prs("o/r", 7)
    assert [pr["number"] for pr in prs] == [42]  # closed PR + plain issue ignored


def test_get_default_branch() -> None:
    transport = FakeTransport()
    transport.add("/repos/o/r", "", {"default_branch": "main"})
    client = GitHubClient(token=None, transport=transport)
    assert client.get_default_branch("o/r") == "main"

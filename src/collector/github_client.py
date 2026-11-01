"""GitHub API client for the Collector stage.

Responsibilities:
- List open issues for a target repository, filtered by watch labels.
- Detect whether an issue already has a pull request (human or bot)
  that addresses it, so the agent never duplicates existing work.
- Fetch repository metadata the pipeline needs (default branch,
  PR template presence).

All calls go through the GitHub REST API with a token held *outside*
the sandbox. The token is optional: unauthenticated reads of public
repositories work, at a much lower rate limit. Rate-limit responses
are treated as a stop signal for the current target (fail-closed):
they raise :class:`RateLimitError` and are never retried aggressively.

The HTTP layer is a thin injectable ``transport`` callable so tests
can drive the client without any network access.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

API_BASE = "https://api.github.com"
_USER_AGENT = "oss-contributor-agent/0.2 (collector)"

# transport(method, url, headers) -> (status, headers, body_bytes)
Transport = Callable[[str, str, dict], "tuple[int, dict, bytes]"]


class GitHubAPIError(RuntimeError):
    """A GitHub API call failed (HTTP error or malformed response)."""


class RateLimitError(GitHubAPIError):
    """The GitHub rate limit is exhausted: stop, do not retry."""


@dataclass
class IssueRef:
    """Minimal issue record passed between pipeline stages."""

    repo: str
    number: int
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    url: str = ""


def _default_transport(method: str, url: str, headers: dict) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        # HTTPError still carries the status/headers/body we must inspect
        # (rate-limit detection depends on them), so return, don't raise.
        return exc.code, dict(exc.headers.items() if exc.headers else []), exc.read()
    except urllib.error.URLError as exc:
        raise GitHubAPIError(f"network error calling GitHub API: {exc.reason}") from exc


class GitHubClient:
    """Thin wrapper over the GitHub API (Phase 1 implementation)."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = API_BASE,
        transport: Transport | None = None,
        per_page: int = 100,
        max_pages: int = 10,
    ) -> None:
        self._token = token or None
        self._base_url = base_url.rstrip("/")
        self._transport = transport or _default_transport
        self._per_page = per_page
        self._max_pages = max_pages

    # -- low-level -----------------------------------------------------

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str, params: dict | None = None) -> object:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        status, headers, body = self._transport("GET", url, self._headers())
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        if status in (403, 429) and (
            lowered.get("x-ratelimit-remaining") == "0" or "retry-after" in lowered
        ):
            reset = lowered.get("x-ratelimit-reset", "unknown")
            raise RateLimitError(
                f"GitHub rate limit exhausted calling {path} "
                f"(reset epoch: {reset}); stopping this target for this run"
            )
        if status >= 400:
            detail = body[:200].decode("utf-8", errors="replace")
            raise GitHubAPIError(f"GitHub API {status} for {path}: {detail}")
        try:
            return json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubAPIError(f"malformed JSON from GitHub API for {path}") from exc

    # -- public API ------------------------------------------------------

    def list_open_issues(self, repo: str, labels: list[str]) -> list[IssueRef]:
        """Return open issues in ``repo`` carrying any of ``labels``.

        The REST endpoint ANDs multiple labels, which would match almost
        nothing, so each label is queried separately and the results are
        merged and de-duplicated by issue number. Pull requests returned
        by the issues endpoint are excluded.
        """
        by_number: dict[int, IssueRef] = {}
        label_queries = labels or [""]
        for label in label_queries:
            page = 1
            while page <= self._max_pages:
                params: dict = {
                    "state": "open",
                    "per_page": self._per_page,
                    "page": page,
                }
                if label:
                    params["labels"] = label
                payload = self._get(f"/repos/{repo}/issues", params)
                if not isinstance(payload, list):
                    raise GitHubAPIError(
                        f"unexpected issues payload for {repo}: {type(payload)}"
                    )
                for item in payload:
                    if not isinstance(item, dict) or "pull_request" in item:
                        continue  # the issues endpoint also returns PRs
                    number = int(item["number"])
                    if number in by_number:
                        continue
                    by_number[number] = IssueRef(
                        repo=repo,
                        number=number,
                        title=str(item.get("title") or ""),
                        body=str(item.get("body") or ""),
                        labels=[
                            str(lbl.get("name") or "")
                            for lbl in item.get("labels") or []
                            if isinstance(lbl, dict)
                        ],
                        url=str(item.get("html_url") or ""),
                    )
                if len(payload) < self._per_page:
                    break
                page += 1
        return [by_number[number] for number in sorted(by_number)]

    def find_existing_prs(self, repo: str, issue_number: int) -> list[dict]:
        """Return open PRs that cross-reference ``issue_number``.

        Uses the issue timeline: a ``cross-referenced`` event whose
        source is an open pull request means someone (human or bot) is
        already working on this issue, so it must be skipped. Returns a
        list of ``{"number": int, "url": str, "title": str}`` dicts.
        """
        prs: dict[int, dict] = {}
        page = 1
        while page <= self._max_pages:
            payload = self._get(
                f"/repos/{repo}/issues/{issue_number}/timeline",
                {"per_page": self._per_page, "page": page},
            )
            if not isinstance(payload, list):
                raise GitHubAPIError(
                    f"unexpected timeline payload for {repo}#{issue_number}"
                )
            for event in payload:
                if not isinstance(event, dict) or event.get("event") != "cross-referenced":
                    continue
                source_issue = (event.get("source") or {}).get("issue") or {}
                if "pull_request" not in source_issue:
                    continue
                if str(source_issue.get("state") or "open") != "open":
                    continue
                number = int(source_issue["number"])
                prs[number] = {
                    "number": number,
                    "url": str(source_issue.get("html_url") or ""),
                    "title": str(source_issue.get("title") or ""),
                }
            if len(payload) < self._per_page:
                break
            page += 1
        return [prs[number] for number in sorted(prs)]

    def get_default_branch(self, repo: str) -> str:
        """Return the repository's default branch name."""
        payload = self._get(f"/repos/{repo}")
        if not isinstance(payload, dict) or not payload.get("default_branch"):
            raise GitHubAPIError(f"no default_branch in repo payload for {repo}")
        return str(payload["default_branch"])

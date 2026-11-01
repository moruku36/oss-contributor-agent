"""LLM triage stage for the borderline band.

The rule-based scorer decides most issues. Issues whose score lands
between the skip and accept thresholds are judged here, with the issue
body and the rule reasons, and the model must return a reasoned
verdict as JSON.

Fail-closed contract (architecture sections 5.2 and 12):

- No provider configured -> the stage does not exist; the pipeline
  leaves borderline issues in the explicit ``pending_llm`` state.
- Provider error, timeout or an unparseable verdict ->
  :class:`LLMTriageError`; the issue stays ``pending_llm`` and is
  retried on a later run. A verdict is never guessed.

Configuration is via environment variables:

- ``LLM_TRIAGE_PROVIDER``: ``openai`` or ``anthropic`` (unset = off)
- ``LLM_TRIAGE_MODEL``: model name (provider default when unset)
- ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``: provider credentials
- ``LLM_TRIAGE_BASE_URL``: optional endpoint override (OpenAI only)

Providers are called with the standard library only, and the HTTP
transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from src.agent.prompts import triage_prompt
from src.collector.github_client import IssueRef

# transport(url, headers, payload_dict) -> (status, body_bytes)
JsonTransport = Callable[[str, dict, dict], "tuple[int, bytes]"]


class LLMTriageError(RuntimeError):
    """The LLM triage stage could not produce a usable verdict."""


@dataclass
class LLMVerdict:
    accepted: bool
    rationale: str
    model: str = ""

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "rationale": self.rationale,
            "model": self.model,
        }


class LLMTriager(Protocol):
    """Interface every triage provider implements (mocked in tests)."""

    def judge(
        self,
        issue: IssueRef,
        *,
        score: float,
        reasons: list[str],
        project_notes: str = "",
    ) -> LLMVerdict:
        ...


def _default_transport(url: str, headers: dict, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        method="POST",
        headers=headers,
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise LLMTriageError(f"LLM provider unreachable: {exc.reason}") from exc


def parse_verdict(text: str, *, model: str = "") -> LLMVerdict:
    """Parse the model's JSON verdict; anything else is an error."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise LLMTriageError(f"LLM verdict is not JSON: {text[:120]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise LLMTriageError(f"LLM verdict is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("accept"), bool):
        raise LLMTriageError(f"LLM verdict lacks a boolean 'accept': {data!r}")
    rationale = str(data.get("rationale") or "").strip()
    if not rationale:
        raise LLMTriageError("LLM verdict lacks a rationale")
    return LLMVerdict(accepted=bool(data["accept"]), rationale=rationale, model=model)


class _BaseHTTPProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transport: JsonTransport | None = None,
    ) -> None:
        if not api_key:
            raise LLMTriageError("LLM provider configured without an API key")
        self._api_key = api_key
        self.model = model
        self._transport = transport or _default_transport

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        status, body = self._transport(url, headers, payload)
        if status >= 400:
            detail = body[:200].decode("utf-8", errors="replace")
            raise LLMTriageError(f"LLM provider returned {status}: {detail}")
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMTriageError("LLM provider returned malformed JSON") from exc


class OpenAITriager(_BaseHTTPProvider):
    """OpenAI chat-completions provider."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        transport: JsonTransport | None = None,
    ) -> None:
        super().__init__(api_key=api_key, model=model, transport=transport)
        self._base_url = base_url.rstrip("/")

    def judge(
        self,
        issue: IssueRef,
        *,
        score: float,
        reasons: list[str],
        project_notes: str = "",
    ) -> LLMVerdict:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": triage_prompt(
                        repo=issue.repo,
                        title=issue.title,
                        body=issue.body,
                        score=score,
                        reasons=reasons,
                        project_notes=project_notes,
                    ),
                }
            ],
            "temperature": 0,
        }
        data = self._post(
            f"{self._base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            payload,
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMTriageError(f"unexpected OpenAI response shape: {exc}") from exc
        return parse_verdict(str(text), model=self.model)


class AnthropicTriager(_BaseHTTPProvider):
    """Anthropic messages provider."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-haiku-4-5",
        transport: JsonTransport | None = None,
    ) -> None:
        super().__init__(api_key=api_key, model=model, transport=transport)

    def judge(
        self,
        issue: IssueRef,
        *,
        score: float,
        reasons: list[str],
        project_notes: str = "",
    ) -> LLMVerdict:
        payload = {
            "model": self.model,
            "max_tokens": 512,
            "messages": [
                {
                    "role": "user",
                    "content": triage_prompt(
                        repo=issue.repo,
                        title=issue.title,
                        body=issue.body,
                        score=score,
                        reasons=reasons,
                        project_notes=project_notes,
                    ),
                }
            ],
        }
        data = self._post(
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            payload,
        )
        try:
            text = "".join(
                block.get("text", "")
                for block in data["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise LLMTriageError(f"unexpected Anthropic response shape: {exc}") from exc
        return parse_verdict(text, model=self.model)


def build_llm_triager(
    env: Mapping[str, str] | None = None,
    *,
    transport: JsonTransport | None = None,
) -> LLMTriager | None:
    """Build the configured triage provider, or None when unconfigured.

    Returning None is a normal state, not an error: the pipeline then
    keeps borderline issues in ``pending_llm`` instead of guessing.
    A provider that *is* named but lacks credentials raises
    :class:`LLMTriageError` (a misconfiguration must not silently
    degrade into unjudged issues).
    """
    env = os.environ if env is None else env
    provider = (env.get("LLM_TRIAGE_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    model = (env.get("LLM_TRIAGE_MODEL") or "").strip()
    if provider == "openai":
        return OpenAITriager(
            api_key=env.get("OPENAI_API_KEY") or "",
            model=model or "gpt-4o-mini",
            base_url=env.get("LLM_TRIAGE_BASE_URL") or "https://api.openai.com/v1",
            transport=transport,
        )
    if provider == "anthropic":
        return AnthropicTriager(
            api_key=env.get("ANTHROPIC_API_KEY") or "",
            model=model or "claude-haiku-4-5",
            transport=transport,
        )
    raise LLMTriageError(f"unknown LLM_TRIAGE_PROVIDER: {provider!r}")

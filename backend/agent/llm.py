"""LLM layer — OpenAI-compatible client (Regolo / Mistral).

Keeps a single client, handles the reasoning-model quirk (text in
`reasoning_content` when `content` is empty), and retries transient errors with
backoff. On terminal failure it raises, and the caller returns an honest HTTP 200
(a 5xx scores worse than an abstention — CLAUDE.md).
"""

from __future__ import annotations

import os
import time
from typing import Any

from openai import OpenAI

_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.regolo.ai/v1")
_API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("MODEL", "gpt-oss-120b")

_client = OpenAI(api_key=_API_KEY, base_url=_BASE_URL, timeout=25.0)


def content_of(message: Any) -> str:
    """Text of an assistant message, falling back to reasoning_content."""
    content = getattr(message, "content", None)
    if content:
        return content
    reasoning = getattr(message, "reasoning_content", None)
    return reasoning or ""


def chat(messages: list[dict[str, Any]], *, tools: list[dict] | None = None,
         tool_choice: str = "auto", temperature: float = 0.0,
         max_tokens: int = 1024, retries: int = 2) -> Any:
    """One chat-completion round. Returns the raw `choice.message`.

    Retries on transient errors (429 / 5xx / network) with exponential backoff;
    re-raises after the budget is exhausted.
    """
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = _client.chat.completions.create(**kwargs)
            return resp.choices[0].message
        except Exception as e:  # noqa: BLE001 - provider SDKs raise varied types
            last = e
            if attempt < retries:
                time.sleep(0.8 * (2 ** attempt))
    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last}")

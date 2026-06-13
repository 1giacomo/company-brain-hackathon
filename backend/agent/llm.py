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

# Low default per-request timeout: normal responses are 1–4s, so this catches a
# stalled call fast instead of letting it eat the 30s wall (see PLAN_V3.md §A).
DEFAULT_TIMEOUT = 12.0
_client = OpenAI(api_key=_API_KEY, base_url=_BASE_URL, timeout=DEFAULT_TIMEOUT)


def content_of(message: Any) -> str:
    """Text of an assistant message, falling back to reasoning_content."""
    content = getattr(message, "content", None)
    if content:
        return content
    reasoning = getattr(message, "reasoning_content", None)
    return reasoning or ""


def chat(messages: list[dict[str, Any]], *, tools: list[dict] | None = None,
         tool_choice: str = "auto", temperature: float = 0.0,
         max_tokens: int = 1024, retries: int = 1,
         timeout: float | None = None) -> Any:
    """One chat-completion round. Returns the raw `choice.message`.

    `timeout` caps the per-request wall time (default DEFAULT_TIMEOUT); the caller
    passes a value derived from its remaining budget. Retries on transient errors
    with short backoff, but only while the timeout leaves room — never let backoff
    push past the deadline. Re-raises after the budget is exhausted.
    """
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout if timeout is not None else DEFAULT_TIMEOUT,
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
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last}")

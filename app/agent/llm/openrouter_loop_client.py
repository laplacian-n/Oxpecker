"""Adapt the OpenRouter provider to the client interface `AgentLoop` (and the budget manager)
expect, so a worker can run on OpenRouter without touching the local GPU.

`AgentLoop` was written against `LlamaClient` — it calls `chat_completions(...)`, `n_ctx()`,
`token_count()` and `rendered_token_count()`. OpenRouter is HTTP-only and exposes no tokenizer or
template endpoint, so this wraps `OpenRouterProvider.chat` and **estimates** token counts.

The estimate is deliberately an over-count (≈3 chars/token rather than the usual ~4): the budget
manager uses these numbers to decide what fits the context window, and over-counting errs toward
evicting a little more, never toward overflowing a model that would then truncate or error. A
conservative wrong is the safe wrong here.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .. import config
from .openrouter import OpenRouterProvider


class OpenRouterLoopClient:
    def __init__(self, model: str, provider: OpenRouterProvider | None = None):
        self.provider = provider or OpenRouterProvider(model=model)

    def n_ctx(self) -> int:
        """The model's real context window, from OpenRouter's catalogue (the provider's probe)."""
        return int(self.provider._caps.context_window)

    def token_count(self, text: str) -> int:
        # No tokenizer over HTTP; estimate at ~3 chars/token, which over-counts ordinary English
        # (~4) on purpose — see the module docstring.
        return max(1, (len(text or "") + 2) // 3)

    def rendered_token_count(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        """An estimate of the rendered prompt size. `LlamaClient` asks the server to apply the
        template and counts exactly; here we sum the message contents plus the tool schema and add
        a small per-message framing allowance, then over-count as above."""
        blob = "\n".join(str(m.get("content", "")) for m in messages)
        if tools:
            blob += json.dumps(tools)
        return self.token_count(blob) + 8 * len(messages)

    def chat_completions(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = config.SAFE_DEFAULT_MAX_TOKENS,
        temperature: float = 0.7,
        seed: int | None = None,
        timeout_s: float | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Same request/response shape as `LlamaClient.chat_completions` — OpenRouter speaks the
        OpenAI wire format, so the response already carries `choices[0].message`, `usage` (with the
        actual `cost`) and `model`. Streaming callbacks and `seed`/`timeout_s` are not plumbed
        through yet; a caller that passes `on_delta` simply gets the full response at once, which
        the loop handles (it re-reads the assembled message either way)."""
        return self.provider.chat(messages, max_tokens=max_tokens, temperature=temperature, tools=tools)

"""Thin wrapper around llama-server's OpenAI-compatible + native endpoints.

Deliberately not an OpenAI SDK client: this project needs /tokenize, /apply-template and
/props directly, and streaming assembly of tool_calls is a known extra failure surface we're
avoiding in Phase 1 (see loop.py docstring for why tool-executing calls use stream=False).
"""
from __future__ import annotations

import json
from typing import Any, Callable

import requests

from . import config


class LlamaClientError(RuntimeError):
    pass


def _load_api_key() -> str | None:
    if config.LLAMA_API_KEY_FILE.exists():
        return config.LLAMA_API_KEY_FILE.read_text().strip()
    return None


class LlamaClient:
    def __init__(self, base_url: str = config.LLAMA_SERVER_URL, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        key = api_key if api_key is not None else _load_api_key()
        if key:
            self._session.headers["Authorization"] = f"Bearer {key}"

    def _post(self, path: str, payload: dict, timeout_s: float | None = None) -> dict:
        try:
            resp = self._session.post(
                f"{self.base_url}{path}", json=payload,
                timeout=timeout_s if timeout_s is not None else config.REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            raise LlamaClientError(f"POST {path} failed: {e}") from e
        if resp.status_code != 200:
            raise LlamaClientError(f"POST {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _get(self, path: str) -> dict:
        try:
            resp = self._session.get(f"{self.base_url}{path}", timeout=config.REQUEST_TIMEOUT_S)
        except requests.RequestException as e:
            raise LlamaClientError(f"GET {path} failed: {e}") from e
        if resp.status_code != 200:
            raise LlamaClientError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def props(self) -> dict:
        return self._get("/props")

    def n_ctx(self) -> int:
        return int(self.props()["default_generation_settings"]["n_ctx"])

    def tokenize(self, text: str) -> list[int]:
        return self._post("/tokenize", {"content": text})["tokens"]

    def token_count(self, text: str) -> int:
        return len(self.tokenize(text))

    def apply_template(self, messages: list[dict], tools: list[dict] | None = None) -> str:
        payload: dict[str, Any] = {"messages": messages}
        if tools:
            payload["tools"] = tools
        result = self._post("/apply-template", payload)
        return result["prompt"]

    def rendered_token_count(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        """The exact preflight count the budget manager relies on (§ Counting caveat)."""
        prompt = self.apply_template(messages, tools)
        return self.token_count(prompt)

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
    ) -> dict:
        """seed is optional and unset by default (matches all prior behavior); the eval harness
        (agent/eval/) sets it explicitly so a run's reproducibility metadata is meaningful —
        temperature 0.7 alone doesn't make output deterministic without a fixed seed too.

        timeout_s overrides config.REQUEST_TIMEOUT_S for this call only — that default is
        deliberately tuned tight as a hang-detection margin for ordinary (short) completions
        (see its own comment), so a caller requesting a much larger max_tokens (e.g. the
        ANALYSIS-phase thinking budget, config.ANALYSIS_THINKING_MAX_TOKENS) needs to pass a
        correspondingly larger timeout_s rather than this silently timing out mid-generation.

        on_delta: when given, the call streams — each `content` fragment is handed to on_delta as
        it arrives, and this method still returns the SAME response-dict shape as the
        non-streaming path (choices[0].message with content / reasoning_content / tool_calls,
        assembled from the deltas). Callers that don't pass it get the exact prior behavior. With
        streaming the requests read-timeout applies per chunk, not to the whole generation, so a
        long answer at this box's ~3-4 tok/s no longer risks one giant blocking read.

        on_reasoning_delta: same idea, for the separate `reasoning_content` stream a thinking-mode
        model emits (see loop.py's comment on why that's a distinct field, not part of `content`).
        Independent of on_delta — pass either, both, or neither; `stream` still turns on if either
        is set. A model not in thinking mode simply never calls it.
        """
        streaming = on_delta is not None or on_reasoning_delta is not None
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": streaming,
        }
        if tools:
            payload["tools"] = tools
        if seed is not None:
            payload["seed"] = seed
        if not streaming:
            return self._post("/v1/chat/completions", payload, timeout_s=timeout_s)
        return self._stream_chat_completion(payload, timeout_s, on_delta, on_reasoning_delta)

    def _stream_chat_completion(
        self,
        payload: dict,
        timeout_s: float | None,
        on_delta: Callable[[str], None] | None,
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> dict:
        try:
            resp = self._session.post(
                f"{self.base_url}/v1/chat/completions", json=payload,
                timeout=timeout_s if timeout_s is not None else config.REQUEST_TIMEOUT_S,
                stream=True,
            )
        except requests.RequestException as e:
            raise LlamaClientError(f"POST /v1/chat/completions (stream) failed: {e}") from e
        if resp.status_code != 200:
            raise LlamaClientError(
                f"POST /v1/chat/completions (stream) -> HTTP {resp.status_code}: {resp.text[:500]}"
            )

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        usage: dict | None = None
        finish_reason: str | None = None
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                    if on_delta is not None:
                        on_delta(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                    if on_reasoning_delta is not None:
                        on_reasoning_delta(delta["reasoning_content"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(
                        idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if tc.get("type"):
                        slot["type"] = tc["type"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                if chunk.get("usage"):
                    usage = chunk["usage"]
        except requests.RequestException as e:
            raise LlamaClientError(f"stream read failed mid-generation: {e}") from e
        finally:
            resp.close()

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage or {},
        }

    def usage_prompt_tokens(self, response: dict) -> int | None:
        usage = response.get("usage") or {}
        return usage.get("prompt_tokens")

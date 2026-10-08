"""The local llama.cpp provider: the one Oxpecker has always used.

Kept behind `agent/llm/base.ChatProvider` so a second provider can exist without a second
runtime. See `base.py` for why that matters and `docs/API_MODE_DESIGN.md` for the whole plan.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

from .base import ProviderCapabilities

log = logging.getLogger("agent.llm.llama")


class LlamaCppProvider:
    """Talks to llama-server via its OpenAI-compatible /v1/chat/completions endpoint.

    Moved here from `web/dev_server.py` UNCHANGED — same requests, same payload, same streaming
    parser, same `len(text) // 4` token estimate. Step 1 of the API-mode build order is "prove
    the seam without risking the working runtime", and rewriting the only provider that works
    while introducing the interface it has to satisfy would be two risks for the price of one.
    `dev_server` imports it as `LLMClient`, so every call site is untouched.

    What it gains is `capabilities()`. The honest declaration matters more here than anywhere
    else in the package: this is the ONLY provider that can return a raw chain of thought, which
    is what makes a locally-hosted model the source of reasoning-trace training data even after
    an API provider is doing the hunting.
    """

    #: Everything a llama.cpp-backed model does and does not offer.
    CAPABILITIES = ProviderCapabilities(
        name="llama.cpp",
        # Tool calls come back inside the text for the models this runs, which is why
        # dev_server carries a 27-entry alias table and a JSON-block scraper. Declaring False
        # keeps those in the path for this provider and lets a provider with real tool calls
        # skip them.
        native_tool_calls=False,
        strict_tool_schemas=False,
        prompt_caching=False,
        server_compaction=False,
        # llama-server HAS a /tokenize endpoint; this client does not call it, and
        # `token_count` is a 4-chars-per-token estimate. Declared False because the capability
        # the pipeline can rely on is what this client does, not what the server could do.
        server_token_count=False,
        reasoning="raw",
        append_only_history=False,
        cost_per_mtok=None,
    )

    def capabilities(self) -> ProviderCapabilities:
        caps = self.CAPABILITIES
        # The model id is only known once the server answers, so it is filled in per instance
        # rather than baked into the class constant.
        return replace(caps, model=self.model_id or caps.model)

    def __init__(self, server_url: str = "http://127.0.0.1:8080"):
        self.base_url = server_url.rstrip("/")
        log.info("Connecting to llama-server at %s", self.base_url)
        self._check_health()
        self.model_id = self._read_model_id()
        if self.model_id:
            log.info("llama-server is serving %s", self.model_id)

    def _read_model_id(self) -> str:
        """The loaded model's identity, best-effort.

        Needed because every turn's trajectory record has to say which model produced it, and
        "llama.cpp" is not an answer — a run against a 4B and a run against the 32B are not
        comparable, and without this the dataset cannot tell them apart after the fact. Failing
        to read it is not an error: the provider works without it, and an empty string is an
        honest "not known" rather than a guess.
        """
        for path, keys in (
            ("/props", ("model_path", "model")),
            ("/v1/models", ()),
        ):
            try:
                req = urllib.request.Request(f"{self.base_url}{path}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
            except Exception:  # noqa: BLE001 — any failure here is just "not known"
                continue
            for key in keys:
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value.rsplit("/", 1)[-1]
            settings = data.get("default_generation_settings")
            if isinstance(settings, dict):
                value = settings.get("model")
                if isinstance(value, str) and value:
                    return value.rsplit("/", 1)[-1]
            models = data.get("data")
            if isinstance(models, list) and models and isinstance(models[0], dict):
                value = models[0].get("id")
                if isinstance(value, str) and value:
                    return value.rsplit("/", 1)[-1]
        return ""

    def _check_health(self):
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            status = data.get("status", "unknown")
            if status == "ok":
                log.info("llama-server is ready")
            else:
                log.warning("llama-server status: %s (may still be loading)", status)
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot connect to llama-server at {self.base_url}.\n"
                f"Start it first:\n"
                f"  llama-server -m your-model.gguf --port 8080 -ngl 99\n\n{e}"
            ) from e

    def chat(self, messages: list[dict], *, max_tokens: int = 3072,
             temperature: float = 0.6, stream: bool = False,
             tools: list[dict] | None = None, enable_thinking: bool = False) -> Any:
        # Qwen3-recommended sampling (temp 0.6 / top_p 0.95 / top_k 20); a bigger
        # max_tokens so a <think> block can't eat the whole budget and leave empty content.
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.95,
            "top_k": 20,
            "repeat_penalty": 1.1,
            "stream": stream,
            # Qwen3 thinking: off by default — a 4B otherwise spends the whole token budget
            # inside <think> and often returns EMPTY content. When on, llama.cpp surfaces the
            # reasoning as delta.reasoning_content (handled by the agent loop) so the UI can
            # show it without the answer being swallowed.
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        if stream:
            # Ask llama-server for a trailing usage chunk so the UI can show the REAL
            # prompt-token count (drives the context monitor + makes compaction visible).
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=data,
            headers={"Content-Type": "application/json"},
        )

        if not stream:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())

        return self._stream_response(req)

    def _stream_response(self, req):
        """Read SSE stream from llama-server (OpenAI format: 'data: {...}' lines)."""
        resp = urllib.request.urlopen(req, timeout=300)
        try:
            buffer = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line == b"data: [DONE]":
                        return
                    if line.startswith(b"data: "):
                        try:
                            obj = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        yield obj
        finally:
            resp.close()

    def token_count(self, text: str) -> int:
        return len(text) // 4

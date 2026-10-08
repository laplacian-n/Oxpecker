"""The OpenRouter provider: one API key, many frontier models, OpenAI wire format.

Why this is the second provider rather than a direct vendor client: the models this project
wants to hunt with (DeepSeek, GLM, and whatever displaces them next quarter) each have their own
API, and a bug-bounty run is exactly the workload where you want to change model without
changing the harness. OpenRouter normalises them onto /v1/chat/completions, which is the shape
`LlamaCppProvider` already speaks, so the runtime downstream of this file — broker, audit chain,
evidence store, tool surface — is untouched. That was the whole point of the seam (see base.py).

Three things this provider does that the local one cannot, and they are the reason API mode
exists at all:

  - Real tool calls. The model emits structured `tool_calls` instead of prose a scraper has to
    recover a call from. dev_server's stream loop already reads `delta.tool_calls`, so this
    works without touching it.
  - Real prices. `/models` reports per-token pricing, so `cost_per_mtok` is read from the
    provider rather than hardcoded and left to rot. On a budget measured in single dollars,
    a cost meter that is actually correct is not a nicety.
  - Capability probing instead of assumption. OpenRouter is a router: what the endpoint supports
    depends on the model behind it, so declaring a fixed capability set here would be a guess.
    `_probe_model` reads the model's own `supported_parameters` and pricing and fills the
    capabilities from that, in the same spirit as `LlamaCppProvider._read_model_id`. A probe
    that fails leaves the weakest honest defaults and the provider still works.

What it cannot do, declared rather than glossed: `reasoning="summary"` at best. No frontier API
returns a raw chain of thought, so trajectories from this provider carry actions, outcomes and
at most a summary of thinking — never reasoning-trace training data. That is not an argument
against API mode; it is what API mode is for. The local model stays the only source of "raw".

THE API KEY IS NEVER PASSED ON ARGV. It comes from $OPENROUTER_API_KEY or from a file under
STATE_DIR, which is gitignored. Anything on a command line is visible in `ps` to every user on
the host and lands in shell history.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

from .base import ProviderCapabilities

log = logging.getLogger("agent.llm.openrouter")

#: Where the key is read from, in order. Checked at call time, not import, so a test can patch
#: config.STATE_DIR and a deployment can write the file after the process starts.
ENV_VAR = "OPENROUTER_API_KEY"
KEY_FILENAME = "openrouter_api_key.txt"

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class MissingAPIKeyError(RuntimeError):
    """Raised at construction, with where to put the key — not at the first turn.

    Same reasoning as the provider registry refusing an unknown name: a session that was created
    reporting a provider it cannot reach is worse than one that was never created.
    """


def read_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env
    from .. import config

    path = config.STATE_DIR / KEY_FILENAME
    try:
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
    except OSError as e:
        raise MissingAPIKeyError(f"{path} exists but could not be read: {e}") from e
    raise MissingAPIKeyError(
        f"no OpenRouter API key. Set ${ENV_VAR}, or write the key to {path} "
        f"(chmod 600 — that directory is gitignored). Never pass it on the command line."
    )


class OpenRouterProvider:
    """Talks to OpenRouter's OpenAI-compatible /v1/chat/completions."""

    #: The honest floor. Everything that can be learned from the model's own metadata is filled
    #: in by `_probe_model`; everything else stays at the weakest plausible value, so a model
    #: whose metadata we cannot read is treated as having less rather than more.
    CAPABILITIES = ProviderCapabilities(
        name="openrouter",
        native_tool_calls=False,
        strict_tool_schemas=False,
        prompt_caching=False,
        server_compaction=False,
        # OpenRouter reports usage per response, which is a count of what happened, not a
        # tokenizer this client can call before sending. token_count below is still an estimate.
        server_token_count=False,
        # Upgraded to "summary" by the probe when the model advertises a reasoning parameter.
        # Never "raw": no frontier API returns the actual token stream.
        reasoning="none",
        append_only_history=False,
        cost_per_mtok=None,
    )

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        referer: str = "",
        title: str = "Oxpecker",
        probe: bool = True,
        timeout: float = 180.0,
    ):
        if not model or not model.strip():
            raise ValueError(
                "OpenRouterProvider needs an explicit model id (e.g. 'deepseek/deepseek-chat'). "
                "There is no default: which model ran a trajectory is the single most important "
                "thing a trajectory records, and a silent default would make two incomparable "
                "runs look alike."
            )
        self.model_id = model.strip()
        self.base_url = base_url.rstrip("/")
        self._api_key = read_api_key(api_key)
        self._timeout = timeout
        # OpenRouter uses these only for its public attribution listing; both are optional.
        self._referer = referer
        self._title = title
        self._caps = self.CAPABILITIES
        if probe:
            self._caps = self._probe_model()
        log.info(
            "OpenRouter ready: model=%s tools=%s reasoning=%s cost/Mtok=%s",
            self.model_id, self._caps.native_tool_calls, self._caps.reasoning,
            self._caps.cost_per_mtok,
        )

    # ── capabilities ──

    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    def _probe_model(self) -> ProviderCapabilities:
        """Read this model's advertised parameters and pricing from OpenRouter's catalogue.

        Unauthenticated and free, so it costs nothing but one request at startup. A failure is
        not fatal: the weakest defaults are correct-but-pessimistic, and refusing to start
        because a catalogue lookup failed would take the whole runtime down for a cost display.
        """
        caps = replace(self.CAPABILITIES, model=self.model_id)
        try:
            req = urllib.request.Request(
                f"{self.base_url}/models", headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                catalogue = json.loads(resp.read())
        except Exception as e:  # noqa: BLE001 — any failure means "not known"
            log.warning(
                "could not read OpenRouter's model catalogue (%s); capabilities stay at their "
                "conservative defaults and the cost meter will be blank", e,
            )
            return caps

        entry = None
        for m in catalogue.get("data") or []:
            if isinstance(m, dict) and m.get("id") == self.model_id:
                entry = m
                break
        if entry is None:
            # Not an error here: OpenRouter accepts suffixed ids (":free", ":nitro") and new
            # models appear before any client knows them. The request will fail loudly at the
            # first turn if the id is genuinely wrong, which is a clearer error than this one.
            log.warning(
                "model %r is not in OpenRouter's catalogue — capabilities stay conservative. "
                "If this id is wrong, the first turn will say so.", self.model_id,
            )
            return caps

        supported = {str(p) for p in (entry.get("supported_parameters") or [])}
        pricing = entry.get("pricing") or {}

        def per_mtok(key: str) -> float | None:
            raw = pricing.get(key)
            try:
                # OpenRouter quotes USD per token, as a string.
                return float(raw) * 1_000_000
            except (TypeError, ValueError):
                return None

        prompt_cost, completion_cost = per_mtok("prompt"), per_mtok("completion")
        cost = (prompt_cost, completion_cost) if None not in (prompt_cost, completion_cost) else None

        has_tools = "tools" in supported or "tool_choice" in supported
        return replace(
            caps,
            native_tool_calls=has_tools,
            # "strict" tool schemas are a stronger claim than "accepts tools", and OpenRouter
            # does not advertise it per model, so it stays False: dev_server's alias table and
            # JSON-block scraper are harmless when the model emits proper calls anyway.
            strict_tool_schemas=False,
            reasoning="summary" if "reasoning" in supported or "include_reasoning" in supported else "none",
            cost_per_mtok=cost,
        )

    # ── chat ──

    def chat(self, messages: list[dict], *, max_tokens: int = 3072,
             temperature: float = 0.6, stream: bool = False,
             tools: list[dict] | None = None, enable_thinking: bool = False) -> Any:
        payload: dict = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        # llama.cpp's sampler knobs (top_k, repeat_penalty) and its chat_template_kwargs are
        # deliberately NOT sent: a router rejects or silently drops parameters the downstream
        # model does not take, and a dropped parameter is worse than an absent one because it
        # looks applied. Thinking is requested through the documented field instead, and only
        # when the probe found the model advertises it.
        if enable_thinking and self._caps.reasoning != "none":
            payload["reasoning"] = {"enabled": True}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": self._title,
        }
        if self._referer:
            headers["HTTP-Referer"] = self._referer

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        if not stream:
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return _normalise(json.loads(resp.read()))
            except urllib.error.HTTPError as e:
                raise RuntimeError(_http_error_text(e)) from e
        return self._stream_response(req)

    def _stream_response(self, req):
        """OpenAI-format SSE, with two differences from llama.cpp's that are handled here.

        `: OPENROUTER PROCESSING` comment lines arrive as keep-alives while the router waits for
        an upstream slot, and must not be parsed as data. And the reasoning delta is called
        `reasoning`, where llama.cpp calls it `reasoning_content` — `_normalise` renames it, so
        dev_server's stream loop needs no provider-specific branch. Putting that here is the
        seam doing its job; putting it in the loop would be the fork this package exists to
        avoid.
        """
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(_http_error_text(e)) from e
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
                    if not line or line.startswith(b":"):
                        continue
                    if line == b"data: [DONE]":
                        return
                    if line.startswith(b"data: "):
                        try:
                            obj = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        yield _normalise(obj)
        finally:
            resp.close()

    def token_count(self, text: str) -> int:
        # Same 4-chars-per-token estimate as the local provider. OpenRouter returns real usage
        # per response, which the loop records; this is only for pre-send budgeting, and
        # server_token_count=False says so.
        return len(text) // 4


def _normalise(obj: dict) -> dict:
    """Rename OpenRouter's `reasoning` to the `reasoning_content` the runtime reads.

    Applied to both streaming deltas and whole messages. Returns the same object, mutated: these
    are short-lived parse results, and copying every chunk of a token stream to rename one key
    would be a cost paid per token for no benefit.
    """
    if not isinstance(obj, dict):
        return obj
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for field in ("delta", "message"):
            part = choice.get(field)
            if isinstance(part, dict) and "reasoning" in part and "reasoning_content" not in part:
                value = part.get("reasoning")
                if isinstance(value, str) and value:
                    part["reasoning_content"] = value
    return obj


def _http_error_text(e: urllib.error.HTTPError) -> str:
    """OpenRouter puts the actionable part in the body, not the status line.

    A bare "HTTP Error 402: Payment Required" sends you looking at the code; the body says which
    credit limit was hit and by how much. Same for 404 on a mistyped model id, and for a 400
    naming the parameter the downstream model rejected.
    """
    try:
        body = e.read().decode("utf-8", "replace")[:600]
    except Exception:  # noqa: BLE001
        body = ""
    detail = ""
    try:
        parsed = json.loads(body)
        err = parsed.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        elif isinstance(err, str):
            detail = err
    except Exception:  # noqa: BLE001
        pass
    return f"OpenRouter HTTP {e.code}: {detail or body or e.reason}"

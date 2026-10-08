"""Tests for the provider seam: the contract, the moved llama.cpp provider, and the registry.

Step 1 of the API-mode build order is "prove the seam without risking the working runtime", so
the property that matters most here is that nothing changed for local sessions. The checks below
assert that directly — same class behind the same name, same methods, same token estimate — and
assert the one thing that was added, an honest capability declaration.

Run directly: `python3 -m agent.llm.test_provider_seam`.
"""
from __future__ import annotations

import dataclasses

import json
from unittest.mock import patch

from .base import REASONING_FIDELITIES, ChatProvider, ProviderCapabilities
from .llama import LlamaCppProvider
from . import registry

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main() -> int:
    print("\n== the contract refuses a capability set it cannot describe ==")
    caps = ProviderCapabilities(name="x", reasoning="summary")
    check("a declared fidelity is accepted", caps.reasoning == "summary")
    for bad in ("raw-ish", "", "RAW", None):
        try:
            ProviderCapabilities(name="x", reasoning=bad)
            check(f"reasoning={bad!r} is refused", False, "it was accepted")
        except (ValueError, TypeError):
            check(f"reasoning={bad!r} is refused", True)
    check("every fidelity in the tuple is constructible",
          all(ProviderCapabilities(name="x", reasoning=f).reasoning == f
              for f in REASONING_FIDELITIES))

    print("\n== defaults describe the WEAKEST provider, so a forgotten field is not a claim ==")
    bare = ProviderCapabilities(name="new-provider")
    for field in ("native_tool_calls", "strict_tool_schemas", "prompt_caching",
                  "server_compaction", "server_token_count", "append_only_history"):
        check(f"{field} defaults to False", getattr(bare, field) is False)
    check("reasoning defaults to 'none'", bare.reasoning == "none")
    check("cost defaults to unmetered", bare.cost_per_mtok is None)
    # Derived from the dataclass rather than written out, which is what the name of this check
    # claims to test. A hardcoded list only catches to_dict LOSING a field: declare a new
    # capability and forget to expose it, and both the list and to_dict stay unchanged, so the
    # frozen version passed while /api/health silently stopped reporting the new field. Every
    # declared capability has to reach the trajectory record and the health endpoint, so this
    # compares against the fields that actually exist.
    declared = {f.name for f in dataclasses.fields(ProviderCapabilities)}
    check("to_dict exposes every declared field, and no field that does not exist",
          set(bare.to_dict()) == declared,
          f"missing={sorted(declared - set(bare.to_dict()))} "
          f"extra={sorted(set(bare.to_dict()) - declared)}")
    check("context_window defaults to the smallest window any provider here has",
          bare.context_window == 32768, str(bare.context_window))

    print("\n== the llama.cpp provider declares what it actually does ==")
    c = LlamaCppProvider.CAPABILITIES
    check("it is the raw-reasoning provider — the only source of CoT training data",
          c.reasoning == "raw", c.reasoning)
    check("it does not claim native tool calls (the alias table exists because it has none)",
          c.native_tool_calls is False)
    check("it does not claim prompt caching", c.prompt_caching is False)
    check("it does not claim server-side token counting, because this client estimates",
          c.server_token_count is False)
    check("history may be rewritten, which is why _fit_context is safe here",
          c.append_only_history is False)
    check("and it is not metered per token", c.cost_per_mtok is None)
    check("it satisfies the protocol", isinstance(LlamaCppProvider, type) and all(
        callable(getattr(LlamaCppProvider, m, None))
        for m in ("capabilities", "chat", "token_count")))

    print("\n== nothing changed for a local session ==")
    from ..web import dev_server as d

    check("dev_server's LLMClient IS the moved provider", d.LLMClient is LlamaCppProvider)
    check("the token estimate is unchanged (4 chars per token)",
          LlamaCppProvider.token_count(None, "x" * 400) == 100,
          str(LlamaCppProvider.token_count(None, "x" * 400)))

    print("\n== the model identity is read, and not-known stays not-known ==")
    with patch("agent.llm.llama.urllib.request.urlopen",
               lambda *a, **k: _FakeResponse({"status": "ok"})):
        # /health answers ok, /props answers the same dict with no model keys -> unknown.
        p = LlamaCppProvider.__new__(LlamaCppProvider)
        p.base_url = "http://127.0.0.1:8080"
        check("an unreadable model id is an empty string, not a guess",
              p._read_model_id() == "", repr(p._read_model_id()))

    with patch("agent.llm.llama.urllib.request.urlopen",
               lambda *a, **k: _FakeResponse({"model_path": "/models/Qwen3-32B-Q4.gguf"})):
        p = LlamaCppProvider.__new__(LlamaCppProvider)
        p.base_url = "http://127.0.0.1:8080"
        check("the model id is taken from /props and basenamed",
              p._read_model_id() == "Qwen3-32B-Q4.gguf", p._read_model_id())

    with patch("agent.llm.llama.urllib.request.urlopen",
               lambda *a, **k: _FakeResponse({"data": [{"id": "qwen3-9b"}]})):
        p = LlamaCppProvider.__new__(LlamaCppProvider)
        p.base_url = "http://127.0.0.1:8080"
        check("or from /v1/models when /props has nothing",
              p._read_model_id() == "qwen3-9b", p._read_model_id())

    def _boom(*a, **k):
        raise OSError("connection refused")

    with patch("agent.llm.llama.urllib.request.urlopen", _boom):
        p = LlamaCppProvider.__new__(LlamaCppProvider)
        p.base_url = "http://127.0.0.1:8080"
        check("a server that will not answer does not break the provider",
              p._read_model_id() == "")

    p = LlamaCppProvider.__new__(LlamaCppProvider)
    p.base_url = "http://x"
    p.model_id = "Qwen3-32B-Q4.gguf"
    check("capabilities() carries the instance's model id",
          p.capabilities().model == "Qwen3-32B-Q4.gguf", p.capabilities().model)
    check("without mutating the class constant", LlamaCppProvider.CAPABILITIES.model == "")

    print("\n== the registry enumerates, and refuses what it cannot serve ==")
    check("llama.cpp is the default", registry.DEFAULT_PROVIDER == registry.PROVIDER_LLAMA_CPP)
    check("and is listed", registry.PROVIDER_LLAMA_CPP in registry.PROVIDERS)
    check("capabilities_for works without constructing anything",
          registry.capabilities_for(registry.PROVIDER_LLAMA_CPP) is LlamaCppProvider.CAPABILITIES)
    for bad in ("anthropic", "openai", "", "LLAMA.CPP"):
        for fn in (registry.capabilities_for, registry.build):
            try:
                fn(bad)
                check(f"{fn.__name__}({bad!r}) is refused", False, "it returned")
            except registry.UnknownProviderError as e:
                check(f"{fn.__name__}({bad!r}) is refused", True)
                check(f"and names the real options ({fn.__name__}, {bad!r})",
                      "llama.cpp" in str(e), str(e))

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

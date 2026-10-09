"""The OpenRouter loop-client adapter. Unit-tested with a fake provider (no network); a gated
end-to-end test runs a real AgentLoop turn on OpenRouter when a key is present, so the loop's
whole path is proven on the API without touching the GPU.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.llm.base import ProviderCapabilities
from agent.llm.openrouter_loop_client import OpenRouterLoopClient


class _FakeProvider:
    def __init__(self, context_window=128000):
        self._caps = ProviderCapabilities(name="openrouter", model="fake", context_window=context_window)
        self.calls = []

    def chat(self, messages, *, max_tokens, temperature, tools=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "tools": tools})
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.0001},
            "model": "fake",
        }


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.provider = _FakeProvider()
        self.client = OpenRouterLoopClient("fake", provider=self.provider)

    def test_n_ctx_is_the_models_context_window(self):
        self.assertEqual(self.client.n_ctx(), 128000)

    def test_token_count_over_counts_rather_than_under(self):
        # ~3 chars/token: a 30-char string estimates >= its true ~8-token size, never under.
        self.assertGreaterEqual(self.client.token_count("x" * 30), 10)
        self.assertEqual(self.client.token_count(""), 1)

    def test_chat_completions_passes_through_the_openai_shape(self):
        resp = self.client.chat_completions(
            [{"role": "user", "content": "hi"}], tools=[{"type": "function"}], max_tokens=64, temperature=0.2,
        )
        self.assertEqual(resp["choices"][0]["message"]["content"], "ok")
        self.assertEqual(resp["usage"]["cost"], 0.0001)
        self.assertEqual(self.provider.calls[0]["max_tokens"], 64)
        self.assertEqual(self.provider.calls[0]["tools"], [{"type": "function"}])


class LoopAcceptsInjectedClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-injected-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "workspace").mkdir()
        for attr, val in (("ENGAGEMENTS_ROOT", self.tmp / "e"), ("SESSIONS_DIR", self.tmp / "s"),
                          ("AUDIT_DIR", self.tmp / "a")):
            p = patch(f"agent.config.{attr}", val)
            p.start()
            self.addCleanup(p.stop)

    def test_agentloop_uses_the_injected_client_not_llamaclient(self):
        from agent.loop import AgentLoop

        client = OpenRouterLoopClient("fake", provider=_FakeProvider())
        loop = AgentLoop(workspace_root=self.tmp / "workspace", session_id="s1",
                         engagement_id="eng-inj", client=client)
        self.addCleanup(loop.close)
        self.assertIs(loop.client, client)
        result = loop.run_task("probe")  # one turn, content reply, no tools -> completes
        self.assertNotEqual(result.status, "error")


@unittest.skipUnless(
    (Path(__file__).resolve().parents[1] / "state" / "openrouter_api_key.txt").exists()
    or os.environ.get("OPENROUTER_API_KEY"),
    "no OpenRouter key — skipping the live end-to-end turn",
)
class LiveOpenRouterLoopTest(unittest.TestCase):
    """Runs a real AgentLoop turn on OpenRouter (cheap model, tiny max_tokens). Skipped wherever
    no key is configured, so CI without a key does not run — or pay for — it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-live-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "workspace").mkdir()
        for attr, val in (("ENGAGEMENTS_ROOT", self.tmp / "e"), ("SESSIONS_DIR", self.tmp / "s"),
                          ("AUDIT_DIR", self.tmp / "a")):
            p = patch(f"agent.config.{attr}", val)
            p.start()
            self.addCleanup(p.stop)

    def test_a_real_turn_completes_and_records_spend(self):
        from agent.loop import AgentLoop
        from agent.engagement.event_log import EngagementEventLog

        client = OpenRouterLoopClient("openai/gpt-4o-mini")
        loop = AgentLoop(workspace_root=self.tmp / "workspace", session_id="s-live",
                         engagement_id="eng-live", client=client)
        self.addCleanup(loop.close)
        result = loop.run_task("Reply with the single word: pong. Do not call any tool.")
        self.assertNotEqual(result.status, "error", result.message)

        events = EngagementEventLog.open_if_exists(self.tmp / "e" / "eng-live")
        kinds = [e["kind"] for e in events.read_since(0)] if events else []
        self.assertIn("model_call", kinds)      # a real OpenRouter call happened
        self.assertIn("budget_updated", kinds)  # with real usage.cost settled


if __name__ == "__main__":
    unittest.main()

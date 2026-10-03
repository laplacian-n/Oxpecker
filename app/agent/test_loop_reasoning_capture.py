"""Tests for capturing `reasoning_content` — found live (2026-09-01) that llama-server returns
thinking-mode output in its own response field, not embedded in `content` as <think> tags the way
the original diagnostic-thinking/ANALYSIS-thinking code assumed; reading only `content` silently
discarded every reasoning token generated (and its latency cost) with nothing kept for the
operator or audit trail. Mocks LlamaClient.chat_completions() entirely (no live llama-server
needed), same pattern as test_loop_steer.py.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import config
from .loop import AgentLoop


def _make_response(content: str, reasoning_content: str = "", tool_calls: list | None = None) -> dict:
    message = {"role": "assistant", "content": content}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class TestReasoningContentCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-reasoning-capture-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self._sessions_dir_patcher = patch("agent.config.SESSIONS_DIR", self.tmp / "sessions")
        self._sessions_dir_patcher.start()
        self.addCleanup(self._sessions_dir_patcher.stop)
        self._audit_dir_patcher = patch("agent.config.AUDIT_DIR", self.tmp / "audit")
        self._audit_dir_patcher.start()
        self.addCleanup(self._audit_dir_patcher.stop)
        for method, rv in (("n_ctx", 16384), ("rendered_token_count", 500), ("token_count", 50)):
            patcher = patch(f"agent.llama_client.LlamaClient.{method}", return_value=rv)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_loop(self) -> AgentLoop:
        session_id = f"reasoning-capture-{time.time_ns()}"
        loop = AgentLoop(workspace_root=self.workspace, session_id=session_id)
        self.addCleanup(loop.close)
        return loop

    def test_safe_default_captures_reasoning_content_in_result_detail(self):
        loop = self._make_loop()
        with patch.object(
            loop.client, "chat_completions",
            return_value=_make_response("final answer", reasoning_content="step by step reasoning here"),
        ):
            result = loop.run_task("hello")
        self.assertEqual(result.detail.get("reasoning_content"), "step by step reasoning here")

    def test_safe_default_persists_reasoning_content_to_session(self):
        loop = self._make_loop()
        with patch.object(
            loop.client, "chat_completions",
            return_value=_make_response("final answer", reasoning_content="my chain of thought"),
        ):
            loop.run_task("hello")
        records = loop.session.load_all()
        assistant_records = [r for r in records if r["role"] == "assistant"]
        self.assertEqual(len(assistant_records), 1)
        self.assertEqual(assistant_records[0]["reasoning_content"], "my chain of thought")

    def test_reasoning_content_not_replayed_into_later_model_messages(self):
        """Persisted for audit, but must not leak into `content` on the next call — that would
        silently bloat context with stale reasoning text on every subsequent turn."""
        loop = self._make_loop()
        with patch.object(
            loop.client, "chat_completions",
            return_value=_make_response("final answer", reasoning_content="internal chain of thought"),
        ):
            loop.run_task("hello")
        messages = loop.session.load_as_messages()
        assistant_messages = [m for m in messages if m["role"] == "assistant"]
        self.assertEqual(len(assistant_messages), 1)
        self.assertNotIn("reasoning_content", assistant_messages[0])
        self.assertNotIn("internal chain of thought", assistant_messages[0]["content"])

    def test_no_reasoning_content_field_when_absent(self):
        """A plain /no_think turn (no reasoning_content in the API response) must not fabricate
        an empty key everywhere — same not-present-when-absent convention tool_calls follows."""
        loop = self._make_loop()
        with patch.object(loop.client, "chat_completions", return_value=_make_response("final answer")):
            result = loop.run_task("hello")
        self.assertNotIn("reasoning_content", result.detail)
        records = loop.session.load_all()
        assistant_records = [r for r in records if r["role"] == "assistant"]
        self.assertNotIn("reasoning_content", assistant_records[0])

    def test_diagnostic_profile_captures_reasoning_content(self):
        session_id = f"reasoning-capture-diag-{time.time_ns()}"
        loop = AgentLoop(
            workspace_root=self.workspace, session_id=session_id,
            profile=config.PROFILE_DIAGNOSTIC_THINKING,
        )
        self.addCleanup(loop.close)
        with patch.object(
            loop.client, "chat_completions",
            return_value=_make_response("would run nmap", reasoning_content="considering options"),
        ):
            result = loop.run_task("scan the target")
        self.assertEqual(result.detail.get("reasoning_content"), "considering options")


if __name__ == "__main__":
    unittest.main()

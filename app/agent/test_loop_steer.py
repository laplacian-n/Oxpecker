"""Tests for the Phase 6 steer-channel wiring into AgentLoop._run_safe_default() — a pending
steer message must be injected as a user-role message before the loop's next model call, and
consumed exactly once. Mocks LlamaClient.chat_completions() entirely (no live llama-server
needed): iteration 1 returns a tool call, iteration 2 returns a final answer with no tool calls.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from .loop import AgentLoop
from .loop_control.steer import SteerChannel


def _make_response(content: str, tool_calls: list | None = None) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class TestSteerChannelWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-steer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.steer_dir = self.tmp / "steer"
        # Redirect both module-level defaults so this test never touches real
        # agent/state/loop_control/steer/ or agent/state/sessions/ — both resolved at call time
        # (see steer.py's and session.py's own comments on why that matters; session.py's was a
        # real, previously-uncaught instance of the same bug, found and fixed while writing this
        # test).
        self._steer_dir_patcher = patch("agent.loop_control.steer.STEER_DIR", self.steer_dir)
        self._steer_dir_patcher.start()
        self.addCleanup(self._steer_dir_patcher.stop)
        self._sessions_dir_patcher = patch("agent.config.SESSIONS_DIR", self.tmp / "sessions")
        self._sessions_dir_patcher.start()
        self.addCleanup(self._sessions_dir_patcher.stop)
        self._audit_dir_patcher = patch("agent.config.AUDIT_DIR", self.tmp / "audit")
        self._audit_dir_patcher.start()
        self.addCleanup(self._audit_dir_patcher.stop)
        self._n_ctx_patcher = patch("agent.llama_client.LlamaClient.n_ctx", return_value=16384)
        self._n_ctx_patcher.start()
        self.addCleanup(self._n_ctx_patcher.stop)
        self._rtc_patcher = patch("agent.llama_client.LlamaClient.rendered_token_count", return_value=500)
        self._rtc_patcher.start()
        self.addCleanup(self._rtc_patcher.stop)
        self._tok_patcher = patch("agent.llama_client.LlamaClient.token_count", return_value=50)
        self._tok_patcher.start()
        self.addCleanup(self._tok_patcher.stop)

    def _make_loop(self, session_id: str) -> AgentLoop:
        loop = AgentLoop(workspace_root=self.workspace, session_id=session_id)
        self.addCleanup(loop.close)
        return loop

    def test_pending_steer_message_injected_before_first_call(self):
        session_id = f"steer-test-1-{time.time_ns()}"
        SteerChannel(session_id).send("focus on the /admin endpoint", sender="owner")
        loop = self._make_loop(session_id)

        with patch.object(loop.client, "chat_completions", return_value=_make_response("done")) as mock_call:
            result = loop.run_task("start recon")

        self.assertEqual(result.status, "ok")
        messages_sent = mock_call.call_args[0][0]
        steer_texts = [m["content"] for m in messages_sent if m.get("role") == "user" and m.get("content", "").startswith("[OPERATOR STEER]")]
        self.assertEqual(len(steer_texts), 1)
        self.assertIn("focus on the /admin endpoint", steer_texts[0])

    def test_steer_message_consumed_exactly_once(self):
        session_id = f"steer-test-2-{time.time_ns()}"
        SteerChannel(session_id).send("hello")
        loop = self._make_loop(session_id)

        call_count = {"n": 0}

        def fake_call(messages, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_response(
                    "", tool_calls=[{
                        "id": "call-1", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'},
                    }]
                )
            return _make_response("final answer")

        with patch.object(loop.client, "chat_completions", side_effect=fake_call):
            result = loop.run_task("start")

        self.assertEqual(result.status, "ok")
        self.assertGreaterEqual(call_count["n"], 2)
        all_messages = loop.session.load_as_messages()
        steer_occurrences = sum(1 for m in all_messages if m.get("role") == "user" and m.get("content", "").startswith("[OPERATOR STEER]"))
        self.assertEqual(steer_occurrences, 1)

    def test_no_pending_message_means_no_injection(self):
        session_id = f"steer-test-3-{time.time_ns()}"
        loop = self._make_loop(session_id)

        with patch.object(loop.client, "chat_completions", return_value=_make_response("done")) as mock_call:
            loop.run_task("start")

        messages_sent = mock_call.call_args[0][0]
        self.assertFalse(any(m.get("role") == "user" and m.get("content", "").startswith("[OPERATOR STEER]") for m in messages_sent))

    def test_steer_message_sent_mid_task_reaches_second_iteration(self):
        """The realistic case: an operator reacts to iteration 1's output and sends a steer
        message before iteration 2's model call — not queued before the task even starts."""
        session_id = f"steer-test-4-{time.time_ns()}"
        loop = self._make_loop(session_id)
        call_count = {"n": 0}

        def fake_call(messages, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                SteerChannel(session_id).send("stop and check scope first")
                return _make_response(
                    "", tool_calls=[{
                        "id": "call-1", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "x.txt"}'},
                    }]
                )
            steer_present = any(m.get("role") == "user" and m.get("content", "").startswith("[OPERATOR STEER]") for m in messages)
            self.assertTrue(steer_present, "steer message sent after iteration 1 must appear by iteration 2")
            return _make_response("final answer")

        with patch.object(loop.client, "chat_completions", side_effect=fake_call):
            result = loop.run_task("start")

        self.assertEqual(result.status, "ok")
        self.assertEqual(call_count["n"], 2)


if __name__ == "__main__":
    unittest.main()

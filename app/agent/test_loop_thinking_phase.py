"""Tests for phase-scoped thinking mode: ANALYSIS is the one phase where the safe-default loop
lets the model think (no /no_think suffix, bigger token budget) instead of always suppressing
it. See config.py's THINKING_ENABLED_PHASES comment for why — a real 900s A/B experiment
(2026-08-31) showed always-on thinking never finishes a single tool-executing task on this
hardware, while ANALYSIS is tool-call-free by phase design, so the cost is paid once per phase
transition instead of once per tool call. Mocks LlamaClient.chat_completions() entirely (no live
llama-server needed), same pattern as test_loop_steer.py.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import config
from .engagement.store import EngagementStore
from .loop import AgentLoop


def _make_response(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class TestPhaseScopedThinking(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-thinking-phase-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
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

    def _make_loop_at_phase(self, phase: str | None) -> AgentLoop:
        engagement_id = f"thinking-phase-test-{time.time_ns()}"
        if phase is not None:
            engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
            self.addCleanup(shutil.rmtree, engagement_dir, ignore_errors=True)
            store = EngagementStore(engagement_dir)
            if phase != "INTAKE":
                store.transition_phase(phase, "test setup", expected_version=0)
        loop = AgentLoop(
            workspace_root=self.workspace,
            session_id=f"session-{engagement_id}",
            engagement_id=engagement_id,
        )
        self.addCleanup(loop.close)
        return loop

    def test_analysis_phase_omits_no_think_suffix(self):
        loop = self._make_loop_at_phase("ANALYSIS")
        with patch.object(loop.client, "chat_completions", return_value=_make_response("hypotheses formed")) as mock_call:
            result = loop.run_task("review observations and form hypotheses")
        self.assertEqual(result.status, "ok")
        messages_sent = mock_call.call_args[0][0]
        user_messages = [m["content"] for m in messages_sent if m["role"] == "user"]
        self.assertEqual(len(user_messages), 1)
        self.assertNotIn(config.NO_THINK_SUFFIX, user_messages[0])

    def test_analysis_phase_uses_thinking_token_budget(self):
        loop = self._make_loop_at_phase("ANALYSIS")
        with patch.object(loop.client, "chat_completions", return_value=_make_response("done")) as mock_call:
            loop.run_task("review observations")
        self.assertEqual(mock_call.call_args.kwargs["max_tokens"], config.ANALYSIS_THINKING_MAX_TOKENS)
        # The bigger token budget needs a correspondingly larger HTTP timeout — the default
        # REQUEST_TIMEOUT_S (300s) is tuned tight for ordinary short completions and a live
        # verification run genuinely hit that ReadTimeout before this override was added.
        self.assertEqual(
            mock_call.call_args.kwargs["timeout_s"], config.ANALYSIS_THINKING_REQUEST_TIMEOUT_S
        )

    def test_recon_phase_uses_default_request_timeout(self):
        loop = self._make_loop_at_phase("RECON")
        with patch.object(loop.client, "chat_completions", return_value=_make_response("scanning")) as mock_call:
            loop.run_task("start recon")
        self.assertIsNone(mock_call.call_args.kwargs["timeout_s"])

    def test_recon_phase_keeps_no_think_suffix(self):
        loop = self._make_loop_at_phase("RECON")
        with patch.object(loop.client, "chat_completions", return_value=_make_response("scanning")) as mock_call:
            result = loop.run_task("start recon")
        self.assertEqual(result.status, "ok")
        messages_sent = mock_call.call_args[0][0]
        user_messages = [m["content"] for m in messages_sent if m["role"] == "user"]
        self.assertEqual(len(user_messages), 1)
        self.assertIn(config.NO_THINK_SUFFIX, user_messages[0])
        self.assertEqual(mock_call.call_args.kwargs["max_tokens"], config.SAFE_DEFAULT_MAX_TOKENS)

    def test_force_no_think_overrides_analysis_thinking(self):
        """The autonomous driver's judgment tasks (record_hypothesis / update_hypothesis_status
        must be *called*) pass force_no_think=True — in ANALYSIS thinking mode at ~3 tok/s the
        model burns the budget reasoning and never emits the tool call."""
        engagement_id = f"force-no-think-{time.time_ns()}"
        engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
        self.addCleanup(shutil.rmtree, engagement_dir, ignore_errors=True)
        store = EngagementStore(engagement_dir)
        store.transition_phase("RECON", "s", expected_version=0)
        store.transition_phase("ANALYSIS", "s", expected_version=1)

        loop = AgentLoop(
            workspace_root=self.workspace, session_id=f"session-{engagement_id}",
            engagement_id=engagement_id, force_no_think=True,
        )
        self.addCleanup(loop.close)
        with patch.object(loop.client, "chat_completions", return_value=_make_response("done")) as mock_call:
            loop.run_task("review observations")
        user_messages = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"]
        self.assertIn(config.NO_THINK_SUFFIX, user_messages[0])
        self.assertEqual(mock_call.call_args.kwargs["max_tokens"], config.SAFE_DEFAULT_MAX_TOKENS)
        self.assertIsNone(mock_call.call_args.kwargs["timeout_s"])

    def test_no_engagement_state_keeps_no_think_suffix(self):
        """No phase context at all (no state.db) must degrade to the existing fast default,
        same as _system_message()'s own no-phase-layer degrade path."""
        loop = self._make_loop_at_phase(None)
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("hello")
        messages_sent = mock_call.call_args[0][0]
        user_messages = [m["content"] for m in messages_sent if m["role"] == "user"]
        self.assertIn(config.NO_THINK_SUFFIX, user_messages[0])

    def test_thinking_flag_is_per_task_not_leaked_across_tasks(self):
        """A second run_task() call on the same loop after a phase change must re-evaluate, not
        reuse the previous task's cached decision."""
        engagement_id = f"thinking-phase-switch-{time.time_ns()}"
        engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
        self.addCleanup(shutil.rmtree, engagement_dir, ignore_errors=True)
        store = EngagementStore(engagement_dir)
        store.transition_phase("RECON", "test setup", expected_version=0)

        loop = AgentLoop(
            workspace_root=self.workspace,
            session_id=f"session-{engagement_id}",
            engagement_id=engagement_id,
        )
        self.addCleanup(loop.close)

        with patch.object(loop.client, "chat_completions", return_value=_make_response("scanning")) as mock_call:
            loop.run_task("start recon")
        first_call_msgs = mock_call.call_args[0][0]
        self.assertIn(config.NO_THINK_SUFFIX, [m["content"] for m in first_call_msgs if m["role"] == "user"][0])

        store.transition_phase("ANALYSIS", "recon done", expected_version=1)
        with patch.object(loop.client, "chat_completions", return_value=_make_response("hypotheses")) as mock_call:
            loop.run_task("review observations")
        second_call_msgs = mock_call.call_args[0][0]
        new_user_msgs = [m["content"] for m in second_call_msgs if m["role"] == "user"]
        self.assertFalse(any(config.NO_THINK_SUFFIX in m for m in new_user_msgs[-1:]))


class TestAnalysisPhaseObservationContext(unittest.TestCase):
    """Found live (2026-09-01): the ANALYSIS phase prompt tells the model to review "existing
    observations" but nothing ever supplied that data — a real verification run against seeded
    Juice Shop observations got back completely fabricated ones (Apache/2.4.42, an /admin
    endpoint) instead. These tests cover AgentLoop._observation_context_block()'s fix.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-observation-context-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
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

    def _make_loop_with_store(self, phase: str) -> tuple[AgentLoop, EngagementStore]:
        engagement_id = f"observation-context-test-{time.time_ns()}"
        engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
        self.addCleanup(shutil.rmtree, engagement_dir, ignore_errors=True)
        store = EngagementStore(engagement_dir)
        if phase != "INTAKE":
            store.transition_phase(phase, "test setup", expected_version=0)
        loop = AgentLoop(
            workspace_root=self.workspace,
            session_id=f"session-{engagement_id}",
            engagement_id=engagement_id,
        )
        self.addCleanup(loop.close)
        return loop, store

    def test_real_observations_injected_during_analysis(self):
        loop, store = self._make_loop_with_store("ANALYSIS")
        asset_id = store.upsert_asset("host", "127.0.0.1:3000")
        store.add_observation(
            "http_recon_result", "Server: Express, no CSP header present", "http_recon",
            asset_id=asset_id,
        )
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("review observations and form hypotheses")
        messages_sent = mock_call.call_args[0][0]
        user_content = [m["content"] for m in messages_sent if m["role"] == "user"][0]
        self.assertIn("Express", user_content)
        self.assertIn("no CSP header present", user_content)
        self.assertIn("RECORDED OBSERVATIONS", user_content)

    def test_oversized_observation_is_truncated_not_injected_whole(self):
        """Found live (2026-09-01): a real http_recon_result observation (full page body +
        redirect hop detail) was 20,615 chars — injecting it raw blew the context budget
        outright on a two-observation engagement. Must be capped, not passed through whole."""
        loop, store = self._make_loop_with_store("ANALYSIS")
        asset_id = store.upsert_asset("host", "127.0.0.1")
        huge_content = "X" * 20_000
        store.add_observation("http_recon_result", huge_content, "http_recon", asset_id=asset_id)
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("review observations")
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertLess(len(user_content), 5000)
        self.assertIn("truncated", user_content)

    def test_open_hypotheses_also_injected(self):
        loop, store = self._make_loop_with_store("ANALYSIS")
        store.create_hypothesis("Missing CSP", "no Content-Security-Policy header observed")
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("review observations and form hypotheses")
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertIn("Missing CSP", user_content)

    def test_no_injection_outside_analysis_phase(self):
        loop, store = self._make_loop_with_store("RECON")
        asset_id = store.upsert_asset("host", "127.0.0.1:3000")
        store.add_observation("http_recon_result", "Server: Express", "http_recon", asset_id=asset_id)
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("start recon")
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertNotIn("RECORDED OBSERVATIONS", user_content)
        self.assertNotIn("Express", user_content)

    def test_no_injection_when_analysis_has_no_recorded_data_yet(self):
        """No observations/hypotheses at all must not inject an empty/misleading block."""
        loop, store = self._make_loop_with_store("ANALYSIS")
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("review observations")
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertNotIn("RECORDED OBSERVATIONS", user_content)


if __name__ == "__main__":
    unittest.main()

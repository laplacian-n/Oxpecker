"""Tests for wiring agent/prompts/phases + agent/prompts/tools into AgentLoop._system_message()
(this pass). Constructs AgentLoop with use_mcp_tools=False/use_security_tools=False so no
subprocess or live llama-server is needed — _system_message() only compiles a prompt, it doesn't
generate anything.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from . import config
from .engagement.store import EngagementStore
from .loop import AgentLoop


class TestSystemMessagePhaseAndToolWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-system-message-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()

    def _make_loop(self, engagement_id: str) -> AgentLoop:
        loop = AgentLoop(
            workspace_root=self.workspace,
            session_id=f"test-session-{engagement_id}",
            engagement_id=engagement_id,
        )
        self.addCleanup(loop.close)
        return loop

    def test_no_engagement_state_no_phase_layer(self):
        loop = self._make_loop("no-such-engagement")
        msg = loop._system_message()
        self.assertNotIn("PHASE:", msg["content"])

    def test_base_tool_cards_always_included(self):
        loop = self._make_loop("no-such-engagement")
        msg = loop._system_message()
        self.assertIn("TOOL CARD: run_command", msg["content"])
        self.assertIn("TOOL CARD: read_file", msg["content"])
        self.assertIn("TOOL CARD: write_file", msg["content"])
        # security tools not enabled for this loop, so their cards must not appear
        self.assertNotIn("TOOL CARD: http_recon", msg["content"])

    def test_prompt_version_set_after_call(self):
        loop = self._make_loop("no-such-engagement")
        self.assertIsNone(loop.prompt_version)
        loop._system_message()
        self.assertIsNotNone(loop.prompt_version)

    def test_engagement_with_phase_state_includes_phase_layer(self):
        engagement_id = "test-eng-with-phase"
        engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
        self.addCleanup(shutil.rmtree, engagement_dir, ignore_errors=True)
        store = EngagementStore(engagement_dir)
        store.upsert_asset("host", "juice-shop.local")
        store.transition_phase("RECON", "scope confirmed", expected_version=0)

        loop = self._make_loop(engagement_id)
        msg = loop._system_message()
        self.assertIn("PHASE: RECON", msg["content"])

    def test_phase_lookup_failure_degrades_without_crashing(self):
        """A corrupt/unreadable state.db must not prevent producing a system message at all."""
        engagement_id = "test-eng-corrupt"
        engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
        self.addCleanup(shutil.rmtree, engagement_dir, ignore_errors=True)
        engagement_dir.mkdir(parents=True)
        (engagement_dir / "state.db").write_bytes(b"not a real sqlite file")

        loop = self._make_loop(engagement_id)
        msg = loop._system_message()  # must not raise
        self.assertNotIn("PHASE:", msg["content"])


if __name__ == "__main__":
    unittest.main()

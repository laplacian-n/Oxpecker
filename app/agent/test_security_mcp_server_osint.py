"""Tests for the osint_record MCP tool (agent/security_mcp_server.py) — the model-callable
wiring for M5.5's osint_discovery channel, which itself makes no network call (see
agent/internet/osint.py). Calls the decorated tool function directly rather than spinning up a
real MCP stdio server (the decorator returns the original callable unchanged, verified before
writing these).

Also covers resolve_engagement_dir() — found live (2026-09-01) that main() previously always
constructed Broker() with no engagement_dir, silently enforcing the legacy default engagement/
RoE regardless of --engagement-id.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent.security_mcp_server as security_mcp_server
from agent import config
from agent.engagement.store import EngagementStore


class TestOsintRecordTool(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="osint-record-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_session_id = getattr(security_mcp_server, "_session_id", None)
        self._orig_engagement_id = security_mcp_server._engagement_id
        security_mcp_server._session_id = "test-session"

    def tearDown(self):
        security_mcp_server._session_id = self._orig_session_id
        security_mcp_server._engagement_id = self._orig_engagement_id

    def test_no_engagement_state_store_reports_not_recorded(self):
        security_mcp_server._engagement_id = "no-such-engagement"
        with patch("agent.security_mcp_server.config.ENGAGEMENTS_ROOT", self.tmp):
            result = security_mcp_server.osint_record_tool("subdomain", "beta.otherapp.example.test")
        self.assertFalse(result["ok"])
        self.assertIn("no M5.2 engagement state store", result["detail"])

    def test_records_when_engagement_state_store_exists(self):
        engagement_id = "test-eng-with-store"
        security_mcp_server._engagement_id = engagement_id
        with patch("agent.security_mcp_server.config.ENGAGEMENTS_ROOT", self.tmp):
            EngagementStore(self.tmp / engagement_id)  # creates state.db
            result = security_mcp_server.osint_record_tool("subdomain", "beta.otherapp.example.test")
            self.assertTrue(result["ok"])
            self.assertIn("observation_id", result)

            store = EngagementStore(self.tmp / engagement_id)
            observations = store.list_observations()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["content"], "beta.otherapp.example.test")
        self.assertTrue(observations[0]["observation_type"].startswith("observed_out_of_scope:"))
        self.assertEqual(observations[0]["source"], "model:test-session")


class TestResolveEngagementDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resolve-engagement-dir-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_named_engagement_with_roe_is_used(self):
        named_dir = self.tmp / "engagements" / "real-engagement"
        named_dir.mkdir(parents=True)
        (named_dir / "roe.json").write_text("{}")
        with patch("agent.security_mcp_server.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"):
            result = security_mcp_server.resolve_engagement_dir("real-engagement")
        self.assertEqual(result, named_dir)

    def test_falls_back_to_legacy_default_when_no_roe_exists(self):
        with patch("agent.security_mcp_server.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"):
            result = security_mcp_server.resolve_engagement_dir("lab-default")
        self.assertEqual(result, config.ENGAGEMENT_DIR)

    def test_falls_back_for_engagement_id_with_state_but_no_roe(self):
        """A directory can exist (e.g. pipeline state.db from a run that never went through
        intake) without a real roe.json — must still fall back, not enforce an empty/missing
        policy file."""
        named_dir = self.tmp / "engagements" / "state-only"
        named_dir.mkdir(parents=True)
        (named_dir / "state.db").write_text("not really sqlite, doesn't matter for this check")
        with patch("agent.security_mcp_server.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"):
            result = security_mcp_server.resolve_engagement_dir("state-only")
        self.assertEqual(result, config.ENGAGEMENT_DIR)


if __name__ == "__main__":
    unittest.main()

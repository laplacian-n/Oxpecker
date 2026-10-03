"""Tests for the 3 autonomous-driver MCP tools (agent/security_mcp_server.py) added 2026-09-01:
record_hypothesis, update_hypothesis_status, record_finding — the model-callable wiring that
lets the model persist structured engagement state itself, instead of a human reading its prose
and hand-calling EngagementStore/FindingsStore (what a real end-to-end pipeline run required
before these existed). Calls the decorated tool functions directly, same pattern as
test_security_mcp_server_osint.py.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent.security_mcp_server as security_mcp_server
from agent.engagement.store import EngagementStore
from agent.findings.model import FindingsStore


class PipelineToolsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pipeline-tools-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_session_id = getattr(security_mcp_server, "_session_id", None)
        self._orig_engagement_id = security_mcp_server._engagement_id
        security_mcp_server._session_id = "test-session"
        self.engagement_id = "test-eng-pipeline-tools"
        security_mcp_server._engagement_id = self.engagement_id
        self._engagements_root_patcher = patch(
            "agent.security_mcp_server.config.ENGAGEMENTS_ROOT", self.tmp
        )
        self._engagements_root_patcher.start()
        self.addCleanup(self._engagements_root_patcher.stop)
        self.store = EngagementStore(self.tmp / self.engagement_id)  # creates state.db

    def tearDown(self):
        security_mcp_server._session_id = self._orig_session_id
        security_mcp_server._engagement_id = self._orig_engagement_id


class TestRecordHypothesisTool(PipelineToolsTestBase):
    def test_no_engagement_state_store_reports_not_recorded(self):
        security_mcp_server._engagement_id = "no-such-engagement"
        result = security_mcp_server.record_hypothesis_tool("t", "d")
        self.assertFalse(result["ok"])
        self.assertIn("no M5.2 engagement state store", result["detail"])

    def test_records_a_real_hypothesis(self):
        result = security_mcp_server.record_hypothesis_tool(
            "Missing HSTS", "no Strict-Transport-Security header observed", priority="high",
        )
        self.assertTrue(result["ok"])
        hyp = self.store.get_hypothesis(result["hypothesis_id"])
        self.assertEqual(hyp["title"], "Missing HSTS")
        self.assertEqual(hyp["priority"], "high")
        self.assertEqual(hyp["status"], "open")

    def test_default_priority_is_medium(self):
        result = security_mcp_server.record_hypothesis_tool("t", "d")
        hyp = self.store.get_hypothesis(result["hypothesis_id"])
        self.assertEqual(hyp["priority"], "medium")


class TestUpdateHypothesisStatusTool(PipelineToolsTestBase):
    def test_no_engagement_state_store_reports_not_recorded(self):
        security_mcp_server._engagement_id = "no-such-engagement"
        result = security_mcp_server.update_hypothesis_status_tool("h1", "confirmed", "evidence")
        self.assertFalse(result["ok"])

    def test_unknown_hypothesis_id_reported_not_crashed(self):
        result = security_mcp_server.update_hypothesis_status_tool(
            "not-a-real-id", "confirmed", "evidence"
        )
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["detail"])

    def test_confirms_with_evidence_for(self):
        hyp_id = self.store.create_hypothesis("t", "d")
        result = security_mcp_server.update_hypothesis_status_tool(
            hyp_id, "confirmed", "re-fetch confirmed header absent"
        )
        self.assertTrue(result["ok"])
        hyp = self.store.get_hypothesis(hyp_id)
        self.assertEqual(hyp["status"], "confirmed")
        self.assertIn("re-fetch confirmed header absent", hyp["evidence_for"])
        self.assertEqual(hyp["evidence_against"], [])

    def test_refutes_with_evidence_against(self):
        hyp_id = self.store.create_hypothesis("t", "d")
        result = security_mcp_server.update_hypothesis_status_tool(
            hyp_id, "refuted", "re-fetch showed header present"
        )
        self.assertTrue(result["ok"])
        hyp = self.store.get_hypothesis(hyp_id)
        self.assertEqual(hyp["status"], "refuted")
        self.assertIn("re-fetch showed header present", hyp["evidence_against"])
        self.assertEqual(hyp["evidence_for"], [])


class TestRecordFindingTool(PipelineToolsTestBase):
    def setUp(self):
        super().setUp()
        # FindingsStore's findings_dir default (agent/findings/model.py) is bound at import
        # time, same "default argument resolved once, not per-call" pattern found and fixed
        # repeatedly elsewhere this session (budget.py, signing.py, session.py, ...) — patching
        # config.FINDINGS_DIR here would be a no-op since record_finding_tool never passes
        # findings_dir explicitly. Patch the class reference itself instead, same approach used
        # in agent/pipeline/test_executor.py's equivalent fixture.
        findings_dir = self.tmp / "findings"

        def _findings_store(engagement_id: str) -> FindingsStore:
            return FindingsStore(engagement_id, findings_dir=findings_dir)

        self._findings_store_patcher = patch(
            "agent.security_mcp_server.FindingsStore", side_effect=_findings_store,
        )
        self._findings_store_patcher.start()
        self.addCleanup(self._findings_store_patcher.stop)
        self.findings_dir = findings_dir

    def test_no_engagement_state_store_reports_not_recorded(self):
        security_mcp_server._engagement_id = "no-such-engagement"
        result = security_mcp_server.record_finding_tool(
            "t", "info", "target", "d", "r",
        )
        self.assertFalse(result["ok"])

    def test_records_a_real_finding_and_links_it(self):
        result = security_mcp_server.record_finding_tool(
            "Missing HSTS", "info", "http://127.0.0.1:3000", "no HSTS header", "add HSTS header",
        )
        self.assertTrue(result["ok"])
        findings = FindingsStore(self.engagement_id, findings_dir=self.tmp / "findings").list_all()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].finding_id, result["finding_id"])
        self.assertEqual(findings[0].severity, "info")
        self.assertEqual(findings[0].tool, "model")

        lifecycle = self.store.list_findings_lifecycle()
        self.assertEqual(len(lifecycle), 1)
        self.assertEqual(lifecycle[0]["finding_id"], result["finding_id"])
        self.assertEqual(lifecycle[0]["status"], "draft")

    def test_invalid_severity_reported_not_crashed(self):
        result = security_mcp_server.record_finding_tool(
            "t", "not-a-real-severity", "target", "d", "r",
        )
        self.assertFalse(result["ok"])
        self.assertIn("invalid finding fields", result["detail"])

    def test_defaults_confidence_and_status_to_needs_validation(self):
        result = security_mcp_server.record_finding_tool("t", "info", "target", "d", "r")
        findings = FindingsStore(self.engagement_id, findings_dir=self.tmp / "findings").list_all()
        self.assertEqual(findings[0].confidence, "needs_validation")
        self.assertEqual(findings[0].status, "needs_validation")

    def test_links_to_hypothesis_when_given(self):
        hyp_id = self.store.create_hypothesis("t", "d")
        result = security_mcp_server.record_finding_tool(
            "t", "info", "target", "d", "r", hypothesis_id=hyp_id,
        )
        self.assertTrue(result["ok"])
        self.assertNotIn("hypothesis_link_warning", result)
        hyp = self.store.get_hypothesis(hyp_id)
        self.assertTrue(any(result["finding_id"] in e for e in hyp["evidence_for"]))

    def test_bad_hypothesis_id_warns_but_still_records_finding(self):
        result = security_mcp_server.record_finding_tool(
            "t", "info", "target", "d", "r", hypothesis_id="not-a-real-id",
        )
        self.assertTrue(result["ok"])
        self.assertIn("hypothesis_link_warning", result)
        findings = FindingsStore(self.engagement_id, findings_dir=self.tmp / "findings").list_all()
        self.assertEqual(len(findings), 1)


if __name__ == "__main__":
    unittest.main()

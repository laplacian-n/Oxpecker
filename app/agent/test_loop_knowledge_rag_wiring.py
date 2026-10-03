"""Wiring tests for the general-knowledge RAG (agent/knowledge_rag/) into AgentLoop —
schema visibility and dispatch routing. Doesn't require the real index/embedding server to be
built/running (KnowledgeRAGService degrades to a clean ok:False result when the index is
missing) — the point of this test is the wiring itself, matching the exact bug class
doc/handoff.md warns about: a tool schema present without a working dispatch path.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from .loop import AgentLoop


class WiringTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-knowledge-rag-wiring-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.engagement_id = f"knowledge-rag-wiring-{time.time_ns()}"
        for attr, sub in (("ENGAGEMENTS_ROOT", "engagements"), ("SESSIONS_DIR", "sessions"),
                          ("AUDIT_DIR", "audit")):
            patcher = patch(f"agent.config.{attr}", self.tmp / sub)
            patcher.start()
            self.addCleanup(patcher.stop)
        kb_patcher = patch("agent.config.TECHNIQUE_KB_PATH", self.tmp / "technique_kb.db")
        kb_patcher.start()
        self.addCleanup(kb_patcher.stop)
        # never touch the real, machine-built knowledge-RAG index — this test is about wiring,
        # not retrieval quality, and must behave the same on a fresh checkout with no index yet.
        for attr in ("KNOWLEDGE_RAG_INDEX_PATH", "KNOWLEDGE_RAG_META_PATH"):
            patcher = patch(f"agent.config.{attr}", self.tmp / "no_such_index" / attr)
            patcher.start()
            self.addCleanup(patcher.stop)
        for method, rv in (("n_ctx", 16384), ("rendered_token_count", 500), ("token_count", 50)):
            patcher = patch(f"agent.llama_client.LlamaClient.{method}", return_value=rv)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_loop(self, use_security_tools: bool = True) -> AgentLoop:
        with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id=f"session-{self.engagement_id}",
                use_security_tools=use_security_tools, engagement_id=self.engagement_id,
            )
            self.addCleanup(loop.close)
        return loop


class TestSchemaVisibility(WiringTestBase):
    def test_tool_present_when_security_tools_enabled(self):
        loop = self._make_loop(use_security_tools=True)
        names = {s["function"]["name"] for s in loop.tool_schemas}
        self.assertIn("security_reference_search", names)

    def test_tool_absent_when_security_tools_disabled(self):
        loop = self._make_loop(use_security_tools=False)
        names = {s["function"]["name"] for s in loop.tool_schemas}
        self.assertNotIn("security_reference_search", names)
        self.assertIsNone(loop.knowledge_rag)


class TestDispatchRouting(WiringTestBase):
    def test_dispatch_is_wired_not_rejected_as_unknown(self):
        loop = self._make_loop(use_security_tools=True)
        result, scope_decision, approval = loop._dispatch(
            "security_reference_search", {"query": "find command privilege escalation"},
        )
        self.assertNotEqual(scope_decision, "rejected_unknown_tool")
        self.assertTrue(scope_decision.startswith("knowledge_rag:"))
        self.assertIsNone(approval)
        # in-process — never touched the mocked security_mcp subprocess client
        loop.security_mcp_client.call_tool.assert_not_called()
        # no index built in this throwaway test dir -> a clean, non-crashing failure result
        self.assertIn("ok", result)

    def test_dispatch_not_routed_when_security_tools_disabled(self):
        loop = self._make_loop(use_security_tools=False)
        result, scope_decision, _ = loop._dispatch("security_reference_search", {"query": "x"})
        self.assertEqual(scope_decision, "rejected_unknown_tool")

    def test_empty_query_is_a_clean_error_not_a_crash(self):
        loop = self._make_loop(use_security_tools=True)
        result, _, _ = loop._dispatch("security_reference_search", {"query": ""})
        self.assertFalse(result["ok"])


class TestInjectionScanExemption(WiringTestBase):
    """Regression test for a real false-positive found live (2026-09-08): GTFOBins/LOLBAS
    content IS shell-substitution syntax by design, so the generic injection_guard scan flagged
    the very first real security_reference_search result as "suspicious" and tainted the
    session — for content from a static, operator-controlled corpus, not target/internet-derived
    data. security_reference_search is exempted; a real broker-mediated tool is not."""

    def test_shell_syntax_in_reference_search_output_does_not_taint_the_session(self):
        import json
        from unittest.mock import patch

        loop = self._make_loop(use_security_tools=True)
        call = {
            "id": "c1", "type": "function", "function": {
                "name": "security_reference_search",
                "arguments": json.dumps({"query": "find command sudo privilege escalation"}),
            },
        }
        with patch.object(loop.knowledge_rag, "search", return_value={
            "ok": True, "results": [{"title": "find — Shell (sudo)",
                                      "text": "find . -exec /bin/sh \\; -quit", "source": "gtfobins",
                                      "url": "", "score": 0.9}],
        }):
            with patch("agent.broker.taint.TaintStore") as mock_taint:
                loop._execute_tool_call(call, turn_index=0, latency_ms=0.0, resp={"usage": {}})
                mock_taint.assert_not_called()

    def test_a_real_broker_mediated_tool_is_still_scanned(self):
        from .loop import INJECTION_SCAN_EXEMPT_TOOLS

        self.assertNotIn("http_recon", INJECTION_SCAN_EXEMPT_TOOLS)
        self.assertNotIn("knowledge_fetch", INJECTION_SCAN_EXEMPT_TOOLS)


if __name__ == "__main__":
    unittest.main()

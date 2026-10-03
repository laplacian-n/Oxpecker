"""Tests for wiring the 4 new M5.5/Phase-6 tools (knowledge_search, knowledge_fetch,
osint_record, browser_fetch) into AgentLoop alongside the existing http_recon/port_discovery —
schema visibility, routing to the security_mcp_server.py subprocess, and correct audit-record
behavior (osint_record isn't broker-mediated, so it must still get loop.py's own generic audit
entry — the other 5 skip it because the broker already writes a richer one).

Also covers the 3 autonomous-driver tools (record_hypothesis, update_hypothesis_status,
record_finding) added 2026-09-01 — found live that registering a schema alone wasn't enough:
_dispatch() only special-cased the literal string "osint_record", so all 3 would have fallen
through to "unknown tool" and been rejected, the same "registered server-side but the model
can't actually call it" gap this project already hit once before with the original 4 tools.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from . import config
from .engagement.locks import engagement_lock
from .loop import BROKER_MEDIATED_TOOLS, LOCAL_BOOKKEEPING_TOOLS, SECURITY_MCP_TOOLS, AgentLoop


class TestSecurityToolsWiringBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-security-tools-test-"))
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


class TestToolSetMembership(unittest.TestCase):
    def test_osint_record_is_security_mcp_but_not_broker_mediated(self):
        self.assertIn("osint_record", SECURITY_MCP_TOOLS)
        self.assertNotIn("osint_record", BROKER_MEDIATED_TOOLS)

    def test_other_five_are_both(self):
        for name in ("http_recon", "port_discovery", "knowledge_search", "knowledge_fetch", "browser_fetch"):
            self.assertIn(name, SECURITY_MCP_TOOLS)
            self.assertIn(name, BROKER_MEDIATED_TOOLS)

    def test_three_pipeline_tools_are_security_mcp_and_local_bookkeeping_not_broker_mediated(self):
        for name in ("record_hypothesis", "update_hypothesis_status", "record_finding"):
            self.assertIn(name, SECURITY_MCP_TOOLS)
            self.assertIn(name, LOCAL_BOOKKEEPING_TOOLS)
            self.assertNotIn(name, BROKER_MEDIATED_TOOLS)


class TestSchemaVisibility(TestSecurityToolsWiringBase):
    def test_all_six_tools_visible_when_security_tools_enabled(self):
        with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id="wiring-test-schema",
                use_security_tools=True,
            )
            self.addCleanup(loop.close)
        names = {s["function"]["name"] for s in loop.tool_schemas}
        for expected in (
            "run_command", "read_file", "write_file",
            "http_recon", "port_discovery",
            "knowledge_search", "knowledge_fetch", "osint_record", "browser_fetch",
            "record_hypothesis", "update_hypothesis_status", "record_finding",
        ):
            self.assertIn(expected, names)

    def test_system_prompt_names_the_engagement_scope(self):
        # The model over-anchors on 127.0.0.1:3000 (the lab it was built against) — the system
        # prompt must state the actual allow-list so a real engagement doesn't waste a turn
        # getting scope-denied. Reads the engagement's scope.txt via load_policy.
        eng_dir = self.tmp / "engagements" / "scoped-eng"
        eng_dir.mkdir(parents=True)
        (eng_dir / "scope.txt").write_text("demo.example.test\n10.9.8.0/24\n")
        with patch("agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"), \
             patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id="wiring-test-scope",
                engagement_id="scoped-eng", use_security_tools=True,
            )
            self.addCleanup(loop.close)
            text = loop._system_message()["content"]
        self.assertIn("demo.example.test", text)
        self.assertIn("10.9.8.0/24", text)
        self.assertIn("In scope for this engagement", text)

    def test_new_tools_not_visible_without_security_tools(self):
        loop = AgentLoop(workspace_root=self.workspace, session_id="wiring-test-no-security")
        self.addCleanup(loop.close)
        names = {s["function"]["name"] for s in loop.tool_schemas}
        self.assertNotIn("knowledge_search", names)
        self.assertNotIn("browser_fetch", names)


class TestRoutingAndAudit(TestSecurityToolsWiringBase):
    def _make_loop_with_mock_client(self, session_id: str) -> AgentLoop:
        with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id=session_id, use_security_tools=True,
            )
            self.addCleanup(loop.close)
        return loop

    def test_osint_record_routes_to_security_mcp_client(self):
        loop = self._make_loop_with_mock_client("wiring-test-route-osint")
        loop.security_mcp_client.call_tool.return_value = {"ok": True, "observation_id": "obs-1"}
        result = loop._run_tool("osint_record", {"observation_type": "subdomain", "content": "x.example.test"})
        loop.security_mcp_client.call_tool.assert_called_once_with(
            "osint_record", {"observation_type": "subdomain", "content": "x.example.test"}
        )
        self.assertEqual(result["observation_id"], "obs-1")

    def test_dispatch_scope_decision_for_osint_record_is_not_broker_framed(self):
        loop = self._make_loop_with_mock_client("wiring-test-dispatch-osint")
        loop.security_mcp_client.call_tool.return_value = {"ok": True, "observation_id": "obs-1"}
        result, scope_decision, approval = loop._dispatch(
            "osint_record", {"observation_type": "subdomain", "content": "x.example.test"}
        )
        self.assertTrue(scope_decision.startswith("recorded:"))
        self.assertIsNone(approval)

    def test_dispatch_scope_decision_for_knowledge_search_is_broker_framed(self):
        loop = self._make_loop_with_mock_client("wiring-test-dispatch-ks")
        loop.security_mcp_client.call_tool.return_value = {"status": "denied", "detail": "no RoE allowance"}
        result, scope_decision, approval = loop._dispatch("knowledge_search", {"query": "x"})
        self.assertTrue(scope_decision.startswith("broker:"))

    def test_osint_record_gets_its_own_generic_audit_entry(self):
        loop = self._make_loop_with_mock_client("wiring-test-audit-osint")
        loop.security_mcp_client.call_tool.return_value = {"ok": True, "observation_id": "obs-1"}
        with patch.object(loop, "audit") as mock_audit:
            call = {"id": "c1", "type": "function", "function": {"name": "osint_record", "arguments": '{"observation_type": "subdomain", "content": "x"}'}}
            loop._execute_tool_call(call, turn_index=0, latency_ms=0.0, resp={"usage": {}})
        mock_audit.record.assert_called_once()

    def test_http_recon_does_not_get_a_second_generic_audit_entry(self):
        loop = self._make_loop_with_mock_client("wiring-test-audit-httprecon")
        loop.security_mcp_client.call_tool.return_value = {
            "status": "succeeded", "output": {"ok": True}, "policy_rule": "scope.txt:127.0.0.1/32",
        }
        with patch.object(loop, "audit") as mock_audit:
            call = {"id": "c1", "type": "function", "function": {"name": "http_recon", "arguments": '{"url": "http://127.0.0.1:3000/"}'}}
            loop._execute_tool_call(call, turn_index=0, latency_ms=0.0, resp={"usage": {}})
        mock_audit.record.assert_not_called()


class TestPipelineToolsDispatch(TestSecurityToolsWiringBase):
    """The exact bug this project already hit once with the original 4 M5.5 tools: a schema
    registered is not the same as _dispatch() actually routing it. Verifies all 3 new tools
    reach the subprocess (not "unknown tool") and get the same recorded:-framed scope decision
    osint_record already gets, not a fake broker: framing."""

    def _make_loop_with_mock_client(self, session_id: str) -> AgentLoop:
        with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id=session_id, use_security_tools=True,
            )
            self.addCleanup(loop.close)
        return loop

    def test_record_hypothesis_is_not_rejected_as_unknown_tool(self):
        loop = self._make_loop_with_mock_client("wiring-test-dispatch-record-hyp")
        loop.security_mcp_client.call_tool.return_value = {"ok": True, "hypothesis_id": "h1"}
        result, scope_decision, approval = loop._dispatch(
            "record_hypothesis", {"title": "t", "description": "d"}
        )
        self.assertNotEqual(scope_decision, "rejected_unknown_tool")
        self.assertTrue(scope_decision.startswith("recorded:"))
        self.assertIsNone(approval)
        self.assertEqual(result["hypothesis_id"], "h1")

    def test_update_hypothesis_status_is_not_rejected_as_unknown_tool(self):
        loop = self._make_loop_with_mock_client("wiring-test-dispatch-update-hyp")
        loop.security_mcp_client.call_tool.return_value = {"ok": True, "hypothesis_id": "h1"}
        result, scope_decision, approval = loop._dispatch(
            "update_hypothesis_status", {"hypothesis_id": "h1", "status": "confirmed", "evidence": "e"}
        )
        self.assertNotEqual(scope_decision, "rejected_unknown_tool")
        self.assertTrue(scope_decision.startswith("recorded:"))

    def test_record_finding_is_not_rejected_as_unknown_tool(self):
        loop = self._make_loop_with_mock_client("wiring-test-dispatch-record-finding")
        loop.security_mcp_client.call_tool.return_value = {"ok": True, "finding_id": "f1"}
        result, scope_decision, approval = loop._dispatch(
            "record_finding",
            {"title": "t", "severity": "info", "target": "x", "description": "d", "remediation": "r"},
        )
        self.assertNotEqual(scope_decision, "rejected_unknown_tool")
        self.assertTrue(scope_decision.startswith("recorded:"))

    def test_record_finding_gets_its_own_generic_audit_entry(self):
        loop = self._make_loop_with_mock_client("wiring-test-audit-record-finding")
        loop.security_mcp_client.call_tool.return_value = {"ok": True, "finding_id": "f1"}
        with patch.object(loop, "audit") as mock_audit:
            call = {
                "id": "c1", "type": "function", "function": {
                    "name": "record_finding",
                    "arguments": '{"title": "t", "severity": "info", "target": "x", "description": "d", "remediation": "r"}',
                },
            }
            loop._execute_tool_call(call, turn_index=0, latency_ms=0.0, resp={"usage": {}})
        mock_audit.record.assert_called_once()


class TestEngagementLockWiring(TestSecurityToolsWiringBase):
    """agent/engagement/locks.py's own docstring documents the exact bug this closes:
    AutonomousDriver.run() holds engagement_lock(engagement_id) for its whole run, then
    internally constructs an AgentLoop and calls its run_task() for judgment tasks
    (agent/pipeline/autonomous_driver.py's _run_model_task) — on the SAME thread, for the SAME
    engagement_id. A plain threading.Lock would self-deadlock the instant that happens; this
    reproduces exactly that nesting directly against the real AgentLoop.run_task(), without
    needing a live model or a full autonomous run (LlamaClient.chat_completions is mocked)."""

    def test_run_task_from_inside_a_held_engagement_lock_does_not_hang(self):
        engagement_id = "wiring-test-lock-reentry"
        result = {}

        def worker():
            with engagement_lock(engagement_id):  # simulates AutonomousDriver.run()'s hold
                with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
                    loop = AgentLoop(
                        workspace_root=self.workspace, session_id="wiring-test-lock-reentry-inner",
                        engagement_id=engagement_id, use_security_tools=True,
                    )
                with patch.object(loop.client, "chat_completions", return_value={
                    "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                    "usage": {},
                }):
                    result["task"] = loop.run_task("hello")
                loop.close()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "run_task() deadlocked when called from inside the same "
                                        "engagement's already-held lock — must be an RLock, not Lock")
        self.assertEqual(result["task"].status, "ok")

    def test_two_sessions_on_the_same_engagement_serialize_not_interleave(self):
        engagement_id = "wiring-test-lock-serialize"
        overlap_detected = {"flag": False}
        currently_running = {"count": 0}
        counter_lock = threading.Lock()

        def slow_chat_completions(*a, **kw):
            with counter_lock:
                currently_running["count"] += 1
                if currently_running["count"] > 1:
                    overlap_detected["flag"] = True
            time.sleep(0.15)  # wide enough that a real race would overlap, not a hair-trigger
            with counter_lock:
                currently_running["count"] -= 1
            return {
                "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {},
            }

        def worker(i: int):
            with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
                loop = AgentLoop(
                    workspace_root=self.workspace, session_id=f"wiring-test-lock-serialize-{i}",
                    engagement_id=engagement_id, use_security_tools=True,
                )
            with patch.object(loop.client, "chat_completions", side_effect=slow_chat_completions):
                loop.run_task("hello")
            loop.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads), "a worker never finished")
        self.assertFalse(
            overlap_detected["flag"],
            "two sessions ran their loops against the same engagement concurrently — the "
            "engagement lock isn't actually serializing them",
        )

    def test_a_plain_chat_session_without_security_tools_is_not_locked(self):
        # No use_security_tools/use_hypothesis_graph -> nothing engagement-scoped is touched, so
        # run_task() must not acquire (or wait on) the engagement lock at all — verified by
        # holding the lock on this thread and confirming run_task() still completes promptly.
        engagement_id = "wiring-test-lock-not-needed"
        with engagement_lock(engagement_id):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id="wiring-test-lock-not-needed-inner",
                engagement_id=engagement_id,
            )
            with patch.object(loop.client, "chat_completions", return_value={
                "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {},
            }):
                result = loop.run_task("hello")
            loop.close()
        self.assertEqual(result.status, "ok")


if __name__ == "__main__":
    unittest.main()

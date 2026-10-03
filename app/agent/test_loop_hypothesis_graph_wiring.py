"""Tests for wiring the Hypothesis Graph into AgentLoop (use_hypothesis_graph=True) — schema
visibility (graph tools replace the flat record_hypothesis/update_hypothesis_status pair, not
add to them), dispatch routing (in-process, not the broker/security_mcp_server subprocess), and
that the graph context block gets injected every turn while the ANALYSIS-only observation block
stays scoped as before. Mocks LlamaClient.chat_completions() entirely, same pattern as
test_loop_security_tools_wiring.py.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from . import config
from .hypothesis_graph.service import HypothesisGraphService
from .loop import AgentLoop


def _make_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}


def _make_tool_call_response(tool_name: str, args: dict, call_id: str = "c1") -> dict:
    import json

    message = {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}],
    }
    return {"choices": [{"message": message}], "usage": {}}


class WiringTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-hypgraph-wiring-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.engagement_id = f"hypgraph-wiring-{time.time_ns()}"
        self.engagement_dir = self.tmp / "engagements" / self.engagement_id
        self._engagements_root_patcher = patch("agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements")
        self._engagements_root_patcher.start()
        self.addCleanup(self._engagements_root_patcher.stop)
        self._sessions_dir_patcher = patch("agent.config.SESSIONS_DIR", self.tmp / "sessions")
        self._sessions_dir_patcher.start()
        self.addCleanup(self._sessions_dir_patcher.stop)
        self._audit_dir_patcher = patch("agent.config.AUDIT_DIR", self.tmp / "audit")
        self._audit_dir_patcher.start()
        self.addCleanup(self._audit_dir_patcher.stop)
        self._kb_patcher = patch("agent.config.TECHNIQUE_KB_PATH", self.tmp / "technique_kb.db")
        self._kb_patcher.start()
        self.addCleanup(self._kb_patcher.stop)
        self._n_ctx_patcher = patch("agent.llama_client.LlamaClient.n_ctx", return_value=16384)
        self._n_ctx_patcher.start()
        self.addCleanup(self._n_ctx_patcher.stop)
        self._rtc_patcher = patch("agent.llama_client.LlamaClient.rendered_token_count", return_value=500)
        self._rtc_patcher.start()
        self.addCleanup(self._rtc_patcher.stop)
        self._tok_patcher = patch("agent.llama_client.LlamaClient.token_count", return_value=50)
        self._tok_patcher.start()
        self.addCleanup(self._tok_patcher.stop)

    def _make_loop(self, use_hypothesis_graph: bool, bootstrap_engagement: bool = False) -> AgentLoop:
        with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id=f"session-{self.engagement_id}",
                use_security_tools=True, engagement_id=self.engagement_id,
                use_hypothesis_graph=use_hypothesis_graph,
                bootstrap_engagement=bootstrap_engagement,
            )
            self.addCleanup(loop.close)
        return loop


class TestSchemaVisibility(WiringTestBase):
    def test_graph_tools_present_when_enabled(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        names = {s["function"]["name"] for s in loop.tool_schemas}
        for expected in (
            "graph_hypothesis_add", "graph_attempt_start", "graph_attempt_complete",
            "graph_set_verdict", "graph_park", "graph_abandon", "graph_set_active_path",
            "graph_search", "graph_read_branch",
        ):
            self.assertIn(expected, names)

    def test_graph_tools_replace_not_add_to_flat_hypothesis_tools(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        names = {s["function"]["name"] for s in loop.tool_schemas}
        self.assertNotIn("record_hypothesis", names)
        self.assertNotIn("update_hypothesis_status", names)
        self.assertIn("record_finding", names)  # a report artifact, not a graph node — stays

    def test_flat_tools_present_when_graph_disabled(self):
        loop = self._make_loop(use_hypothesis_graph=False)
        names = {s["function"]["name"] for s in loop.tool_schemas}
        self.assertIn("record_hypothesis", names)
        self.assertIn("update_hypothesis_status", names)
        self.assertNotIn("graph_hypothesis_add", names)

    def test_hypothesis_graph_service_not_constructed_when_disabled(self):
        loop = self._make_loop(use_hypothesis_graph=False)
        self.assertIsNone(loop.hypothesis_graph)
        # and no db file should have been created as a side effect
        self.assertFalse((self.engagement_dir / "hypothesis_graph.db").exists())

    def test_bootstrap_engagement_creates_the_state_store_for_record_finding(self):
        # security_mcp_server's record_finding / osint_record gate on engagements/<id>/state.db
        # existing — without the opt-in they'd fail closed for a fresh engagement.
        self.assertFalse((self.engagement_dir / "state.db").exists())
        self._make_loop(use_hypothesis_graph=True)
        self.assertFalse((self.engagement_dir / "state.db").exists())
        self._make_loop(use_hypothesis_graph=True, bootstrap_engagement=True)
        self.assertTrue((self.engagement_dir / "state.db").exists())


class TestDispatchRouting(WiringTestBase):
    def test_graph_add_runs_in_process_not_via_security_mcp_client(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        result, scope_decision, approval = loop._dispatch("graph_hypothesis_add", {
            "title": "t", "claim": "c", "rationale": "r", "impact": 3,
            "confidence_band": "low", "confidence_reason": "x", "phase": "RECON",
        })
        self.assertTrue(result["ok"])
        self.assertIn("hypothesis_id", result)
        self.assertTrue(scope_decision.startswith("graph:"))
        self.assertIsNone(approval)
        # never touched the mocked MCP client — this must be a pure in-process call
        loop.security_mcp_client.call_tool.assert_not_called()

    def test_graph_tool_not_routed_when_graph_disabled(self):
        loop = self._make_loop(use_hypothesis_graph=False)
        result, scope_decision, approval = loop._dispatch("graph_hypothesis_add", {"title": "t"})
        self.assertEqual(scope_decision, "rejected_unknown_tool")

    def test_graph_validation_error_returned_not_raised(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        result, scope_decision, _ = loop._dispatch("graph_hypothesis_add", {
            "title": "t", "claim": "c", "rationale": "  ", "impact": 3,
            "confidence_band": "low", "confidence_reason": "x", "phase": "RECON",
        })
        self.assertFalse(result["ok"])
        self.assertIn("GraphValidationError", result["error"])

    def test_graph_tool_gets_generic_audit_entry(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        with patch.object(loop, "audit") as mock_audit:
            call = {
                "id": "c1", "type": "function", "function": {
                    "name": "graph_hypothesis_add",
                    "arguments": '{"title": "t", "claim": "c", "rationale": "r", "impact": 3, "confidence_band": "low", "confidence_reason": "x", "phase": "RECON"}',
                },
            }
            loop._execute_tool_call(call, turn_index=0, latency_ms=0.0, resp={"usage": {}})
        mock_audit.record.assert_called_once()


class TestContextInjection(WiringTestBase):
    def test_graph_digest_injected_when_enabled(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        loop.hypothesis_graph.add_hypothesis(
            title="root claim", claim="c", phase_created="RECON", rationale="r",
            origin_type="ai_inference", impact=3, confidence_band="low", confidence_reason="x",
        )
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("start recon")
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertIn("HYPOTHESIS GRAPH", user_content)
        self.assertIn("root claim", user_content)

    def test_no_graph_block_when_disabled(self):
        loop = self._make_loop(use_hypothesis_graph=False)
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("start recon")
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertNotIn("HYPOTHESIS GRAPH", user_content)

    def test_graph_block_injected_outside_analysis_phase_too(self):
        """Unlike _observation_context_block (ANALYSIS-only), the graph digest is not phase-
        gated — VALIDATION needs to see which hypothesis it's working on too."""
        loop = self._make_loop(use_hypothesis_graph=True)
        loop.hypothesis_graph.add_hypothesis(
            title="recon-phase claim", claim="c", phase_created="RECON", rationale="r",
            origin_type="ai_inference", impact=3, confidence_band="low", confidence_reason="x",
        )
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("do recon")  # no engagement phase state -> _current_phase() is None
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertIn("HYPOTHESIS GRAPH", user_content)


class TestChatAnchorStamping(WiringTestBase):
    """Found while building the web UI's chat-anchor jump feature: nothing was actually stamping
    origin_ref/chat_start_message_id/chat_result_message_id — every graph node's anchors would
    have been permanently None. loop.py now stamps the calling assistant turn's own message_id
    automatically (the model never needs to know its own message id)."""

    def test_hypothesis_add_gets_origin_ref_stamped(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        response = _make_tool_call_response("graph_hypothesis_add", {
            "title": "t", "claim": "c", "rationale": "r", "impact": 3,
            "confidence_band": "low", "confidence_reason": "x", "phase": "RECON",
        })
        with patch.object(loop.client, "chat_completions", side_effect=[response, _make_response("done")]):
            loop.run_task("go")
        hyps = loop.hypothesis_graph.store.list_hypotheses()
        self.assertEqual(len(hyps), 1)
        self.assertIsNotNone(hyps[0]["origin_ref"])
        # the stamped id must be a real message actually present in the session log
        message_ids = {m.get("message_id") for m in loop.session.load_all()}
        self.assertIn(hyps[0]["origin_ref"], message_ids)


class TestNotebookWiring(WiringTestBase):
    """The Working Notebook rides the same use_hypothesis_graph gate (docs/working-notebook-spec.md).
    Same three checks the graph got: schema visible, dispatch routed (NOT rejected_unknown_tool),
    digest injected."""

    def test_notebook_tools_present_when_enabled(self):
        on = self._make_loop(use_hypothesis_graph=True)
        names = {s["function"]["name"] for s in on.tool_schemas}
        self.assertEqual({"note_add", "note_search", "note_resolve", "note_promote", "technique_recall"} & names,
                         {"note_add", "note_search", "note_resolve", "note_promote", "technique_recall"})
        self.assertIsNotNone(on.notebook)

    def test_notebook_service_not_constructed_when_disabled(self):
        off = self._make_loop(use_hypothesis_graph=False)
        self.assertIsNone(off.notebook)
        self.assertNotIn("note_add", {s["function"]["name"] for s in off.tool_schemas})
        self.assertFalse((self.engagement_dir / "notebook.db").exists())
        _, sd, _ = off._dispatch("note_add", {"category": "misc", "note": "x"})
        self.assertEqual(sd, "rejected_unknown_tool")

    def test_note_add_dispatches_in_process(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        result, scope_decision, approval = loop._dispatch(
            "note_add", {"category": "dead-end", "note": "tried alg=none, not present"})
        self.assertTrue(result["ok"])
        self.assertTrue(scope_decision.startswith("notebook:"))
        self.assertIsNone(approval)
        loop.security_mcp_client.call_tool.assert_not_called()

    def test_note_add_stamps_the_calling_turn_message_id(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        response = _make_tool_call_response("note_add", {"category": "technique", "note": "gadget bypass"})
        with patch.object(loop.client, "chat_completions", side_effect=[response, _make_response("done")]):
            loop.run_task("go")
        notes = loop.notebook.store.list_notes()
        self.assertEqual(len(notes), 1)
        message_ids = {m.get("message_id") for m in loop.session.load_all()}
        self.assertIn(notes[0]["chat_message_id"], message_ids)

    def test_notebook_digest_injected_every_phase(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        loop.notebook.add_note(category="dead-end", note="JWT alg=none is closed here")
        with patch.object(loop.client, "chat_completions", return_value=_make_response("ok")) as mock_call:
            loop.run_task("do recon")
        user_content = [m["content"] for m in mock_call.call_args[0][0] if m["role"] == "user"][0]
        self.assertIn("[NOTEBOOK", user_content)
        self.assertIn("do not retry", user_content)

    def test_note_promote_creates_a_hypothesis_from_a_note(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        loop.notebook.add_note(category="todo", note="/api/proxy likely SSRF once authed", surface="/api/proxy")
        result, scope_decision, _ = loop._dispatch("note_promote", {
            "note_ref": "N-1", "title": "SSRF via /api/proxy", "claim": "proxy fetches attacker URLs",
            "impact": 4, "confidence_band": "medium", "confidence_reason": "param seen", "phase": "ANALYSIS",
        })
        self.assertTrue(result["ok"])
        self.assertTrue(scope_decision.startswith("notebook:"))
        self.assertEqual(len(loop.hypothesis_graph.store.list_hypotheses()), 1)
        self.assertEqual(loop.notebook.store.get_by_ordinal(1)["status"], "resolved")

    def test_attempt_start_gets_chat_start_message_id_stamped(self):
        loop = self._make_loop(use_hypothesis_graph=True)
        hid = loop.hypothesis_graph.add_hypothesis(
            title="t", claim="c", phase_created="RECON", rationale="r",
            origin_type="ai_inference", impact=3, confidence_band="low", confidence_reason="x",
        )["hypothesis_id"]
        response = _make_tool_call_response("graph_attempt_start", {
            "hypothesis_ref": hid, "method_summary": "m",
        })
        with patch.object(loop.client, "chat_completions", side_effect=[response, _make_response("done")]):
            loop.run_task("go")
        exp = loop.hypothesis_graph.store.list_experiments(hid)[0]
        self.assertIsNotNone(exp["chat_start_message_id"])
        message_ids = {m.get("message_id") for m in loop.session.load_all()}
        self.assertIn(exp["chat_start_message_id"], message_ids)


if __name__ == "__main__":
    unittest.main()

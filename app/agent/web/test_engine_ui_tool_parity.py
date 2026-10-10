"""§12 step 1 — the tool-parity assertion that compares the ENGINE's runtime tool set (loop.py,
the full security+graph configuration the high tier runs) against the UI engine's (dev_server),
at runtime rather than by scanning source.

The existing `test_tool_parity.py` checks that each tool dev_server *declares* is also dispatchable
and prompted — parity within dev_server. What was missing, and what §12 step 1 names, is the
cross-runtime comparison: every tool the real engine can call should be reachable from the UI too.
Today it is not — dev_server exposes ~11 tools and keeps its own in-memory hypothesis/notebook/
finding stores instead of the graph (§3.1), so the 18 graph/notebook/internet tools the engine
gains with the graph on are unreachable in the UI.

So this test does not assert full parity yet (that is what steps 2-3 deliver). It pins the gap:
the set of engine tools missing from the UI must equal `KNOWN_MISSING_FROM_UI`, exactly. That makes
the test green today and turns it into a ratchet — a NEW engine tool that does not reach the UI
fails it immediately, and each tool step 3 wires across is deleted from the known set, so the set
shrinks monotonically to empty, at which point the UI has full parity and the assertion becomes the
real thing with no edit to its shape.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from .. import config
from ..loop import AgentLoop
from . import dev_server


def _schema_names(schemas) -> set:
    return {s["function"]["name"] for s in schemas}


# The 18 engine tools the UI cannot reach today (recon'd from loop.py's security+graph-on set vs
# dev_server's TOOL_SCHEMAS + SECURITY_TOOL_SCHEMAS). Each is removed from this set as step 3 wires
# it across; when the set is empty the UI has full engine parity.
KNOWN_MISSING_FROM_UI = {
    # internet / osint / browser (security-gated, broker-mediated)
    "knowledge_fetch", "osint_record", "browser_fetch",
    "security_reference_search",
    # the hypothesis graph: the full tool set (reads + writes) is now mounted on the UI runtime in
    # step 3, so none remain here. The record_hypothesis/update_hypothesis_status lookalikes stay in
    # KNOWN_UI_ONLY as back-compat aliases until a final cleanup removes them.
    # the working notebook: the reads (note_search, technique_recall) are wired across; the write
    # tools stay until their model-facing interface is consolidated.
    "note_add", "note_resolve", "note_promote",
}

# UI tools with no engine counterpart under the graph-on configuration, documented so the test is
# honest about both directions. `http_request` is an engine gap (the engine has no such schema,
# only the broker classifies it). `record_hypothesis`/`update_hypothesis_status`/`record_note` are
# the in-memory lookalikes the graph tools replace — they disappear from the UI in step 2/3.
KNOWN_UI_ONLY = {"http_request", "record_hypothesis", "update_hypothesis_status", "record_note"}


class EngineUiToolParityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engine-ui-parity-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.engagement_id = f"parity-{time.time_ns()}"
        # Keep the graph/notebook/state the loop opens under a tempdir, and keep model/MCP clients
        # from touching the network — the same stubbing the loop wiring tests use. We only read the
        # tool schema list the constructor builds, we never run a turn.
        self._patchers = [
            patch("agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"),
            patch("agent.llama_client.LlamaClient.n_ctx", return_value=16384),
            patch("agent.llama_client.LlamaClient.rendered_token_count", return_value=500),
            patch("agent.llama_client.LlamaClient.token_count", return_value=50),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def _engine_tool_names(self) -> set:
        with patch("agent.mcp_tool_client.MCPToolClient", return_value=MagicMock()):
            loop = AgentLoop(
                workspace_root=self.workspace, session_id=f"s-{self.engagement_id}",
                engagement_id=self.engagement_id, use_security_tools=True,
                use_hypothesis_graph=True,
            )
        try:
            return _schema_names(loop.tool_schemas)
        finally:
            close = getattr(loop, "close", None)
            if callable(close):
                close()

    def _ui_tool_names(self) -> set:
        # dev_server assembles TOOL_SCHEMAS[:] + SECURITY_TOOL_SCHEMAS when use_security_tools — the
        # widest UI set, which is what we compare against the widest engine set.
        return _schema_names(dev_server.TOOL_SCHEMAS) | _schema_names(dev_server.SECURITY_TOOL_SCHEMAS)

    def test_engine_tools_missing_from_the_ui_equal_the_known_gap(self):
        engine = self._engine_tool_names()
        ui = self._ui_tool_names()
        missing = engine - ui
        self.assertEqual(
            missing, KNOWN_MISSING_FROM_UI,
            "the engine-vs-UI tool gap changed. If step 3 wired a tool across, remove it from "
            "KNOWN_MISSING_FROM_UI; if a NEW engine tool is unreachable from the UI, that is the "
            "regression §12 step 1 exists to catch.",
        )

    def test_the_ui_exposes_nothing_the_engine_lacks_beyond_the_known_set(self):
        engine = self._engine_tool_names()
        ui = self._ui_tool_names()
        self.assertEqual(
            ui - engine, KNOWN_UI_ONLY,
            "a UI tool has no engine counterpart. The in-memory lookalikes shrink as step 2/3 moves "
            "the UI onto the graph; update KNOWN_UI_ONLY when one is removed.",
        )

    def test_the_shared_core_tools_really_are_on_both_sides(self):
        # A guard that the comparison is not vacuous (e.g. if a set came back empty): the obvious
        # shared tools must be present in both runtimes.
        engine = self._engine_tool_names()
        ui = self._ui_tool_names()
        for name in ("run_command", "read_file", "write_file", "port_discovery", "http_recon",
                     "knowledge_search", "record_finding"):
            self.assertIn(name, engine, f"{name} missing from the engine set")
            self.assertIn(name, ui, f"{name} missing from the UI set")

    def test_the_gap_only_ever_shrinks(self):
        # The ratchet, stated as an invariant: every known-missing name is a real engine tool (so a
        # typo or a renamed tool cannot hide in the known set forever).
        engine = self._engine_tool_names()
        stale = KNOWN_MISSING_FROM_UI - engine
        self.assertEqual(stale, set(),
                         f"KNOWN_MISSING_FROM_UI names that are no longer engine tools: {stale}")


if __name__ == "__main__":
    unittest.main()

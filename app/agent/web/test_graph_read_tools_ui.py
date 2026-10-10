"""§12 step 3 — the engine's read-only graph tools (graph_search, graph_read_branch) reachable
from the dev_server runtime.

These are mounted from the engine's own schema source and dispatched against the per-engagement
graph service, so the UI runtime and the engine run one definition of each tool, not a copy. This
test drives them through dev_server's `_run_tool` (the UI dispatch seam) — no model, no HTTP — and
checks they read the same graph the model's writes (record_hypothesis) land in.

Run directly: `python3 -m unittest agent.web.test_graph_read_tools_ui`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import dev_server


class GraphReadToolsUiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="graph-read-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions",)}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self._patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        dev_server._graph_reset_cache()
        self.addCleanup(dev_server._graph_reset_cache)
        self.session = dev_server.Session(session_id="s1", engagement_id="eng1")
        dev_server._sessions["s1"] = self.session

    def tearDown(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _run(self, name, **args):
        return dev_server._run_tool(name, args, self.session)

    def test_the_two_read_tools_are_in_the_ui_tool_schema(self):
        names = {s["function"]["name"] for s in dev_server.TOOL_SCHEMAS}
        self.assertIn("graph_search", names)
        self.assertIn("graph_read_branch", names)

    def test_graph_search_finds_a_hypothesis_the_model_recorded(self):
        self._run("record_hypothesis", title="SQL injection on login", description="union-based")
        self._run("record_hypothesis", title="open redirect on /next", description="param echo")
        res = self._run("graph_search", query="injection")
        self.assertTrue(res.get("ok"))
        self.assertEqual([r["ordinal"] for r in res["results"]], [1])  # only the SQLi hypothesis

    def test_graph_search_against_an_empty_graph_is_a_clean_empty_result(self):
        res = self._run("graph_search", query="anything")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["results"], [])

    def test_graph_read_branch_reads_a_recorded_branch(self):
        self._run("record_hypothesis", title="root", description="r")
        self._run("record_hypothesis", title="child", description="c", parent_ordinal=1)
        res = self._run("graph_read_branch", hypothesis_ref="1")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("root_ordinal"), 1)

    def test_read_tools_see_the_engagements_graph_not_a_different_one(self):
        self._run("record_hypothesis", title="only in eng1", description="x")
        other = dev_server.Session(session_id="s2", engagement_id="eng2")
        dev_server._sessions["s2"] = other
        res = dev_server._run_tool("graph_search", {"query": "only"}, other)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["results"], [])  # eng2's graph is empty


if __name__ == "__main__":
    unittest.main()

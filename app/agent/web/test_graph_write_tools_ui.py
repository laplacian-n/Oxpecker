"""§12 step 3 — the engine's hypothesis-graph WRITE tools reachable from the dev_server runtime.

The reads (graph_search/read_branch) were mounted first; this covers the write tools now wired on
the same single tool plane: graph_hypothesis_add, graph_attempt_start/complete, graph_set_verdict,
graph_park, graph_abandon, graph_set_active_path. Driven through dev_server's `_run_tool` seam
(no model, no HTTP) and then read back through the HTTP overview/detail to confirm the mutation is
what the UI sees. The simple record_hypothesis/update_hypothesis_status lookalikes remain and are
checked to share the same per-engagement graph.

Run directly: `python3 -m unittest agent.web.test_graph_write_tools_ui`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class GraphWriteToolsUiTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="graph-write-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions",)}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self._patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-such-key.txt"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        dev_server._graph_reset_cache()
        self.addCleanup(dev_server._graph_reset_cache)
        self.session = dev_server.Session(session_id="s1", engagement_id="eng1")
        dev_server._sessions["s1"] = self.session
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _run(self, name, **args):
        return dev_server._run_tool(name, args, self.session)

    def _add(self, title="login SQLi", **over):
        args = dict(title=title, claim="the login form is injectable", rationale="error echoes input",
                    phase="RECON", impact=4, confidence_band="low", confidence_reason="not tested yet")
        args.update(over)
        return self._run("graph_hypothesis_add", **args)

    def test_all_write_tools_are_in_the_ui_schema(self):
        names = {s["function"]["name"] for s in dev_server.TOOL_SCHEMAS}
        for t in ("graph_hypothesis_add", "graph_attempt_start", "graph_attempt_complete",
                  "graph_set_verdict", "graph_park", "graph_abandon", "graph_set_active_path"):
            self.assertIn(t, names, t)

    def test_hypothesis_add_then_full_attempt_lifecycle_and_verdict(self):
        r = self._add()
        self.assertTrue(r.get("ok"), r)
        ordinal = r["ordinal"]

        started = self._run("graph_attempt_start", hypothesis_ref=str(ordinal),
                            method_summary="send ' OR 1=1-- in the username")
        self.assertTrue(started.get("ok"), started)
        done = self._run("graph_attempt_complete", experiment_id=started["experiment_id"],
                         status="completed", observed_result="auth bypassed, dashboard returned",
                         observation_summary="login bypassed", polarity="supports", strength="strong")
        self.assertTrue(done.get("ok"), done)
        verdict = self._run("graph_set_verdict", hypothesis_ref=str(ordinal), verdict="confirmed")
        self.assertTrue(verdict.get("ok"), verdict)

        detail = self.client.get(f"/api/engagements/eng1/hypothesis-graph/nodes/{ordinal}").json()
        self.assertEqual(detail["verdict"], "confirmed")
        self.assertTrue(detail["attempts"], "the completed attempt should be visible on the node")

    def test_park_and_abandon_reach_the_node_status(self):
        o1 = self._add(title="maybe stored XSS")["ordinal"]
        o2 = self._add(title="open redirect")["ordinal"]
        self.assertTrue(self._run("graph_park", hypothesis_ref=str(o1), reason="lower priority")["ok"])
        self.assertTrue(self._run("graph_abandon", hypothesis_ref=str(o2), reason="out of scope")["ok"])
        statuses = {n["ordinal"]: n["status"]
                    for n in self.client.get("/api/engagements/eng1/hypothesis-graph").json()["nodes"]}
        self.assertEqual(statuses[o1], "parked")
        self.assertEqual(statuses[o2], "abandoned")

    def test_set_active_path_accepts_the_ordinals(self):
        o1 = self._add(title="a")["ordinal"]
        o2 = self._add(title="b", parent_ref=str(o1))["ordinal"]
        res = self._run("graph_set_active_path", refs=[str(o1), str(o2)], reason="pursuing this chain")
        self.assertTrue(res.get("ok"), res)

    def test_lookalike_and_real_add_share_one_engagement_graph(self):
        # record_hypothesis (the simple lookalike) still works and lands in the same per-engagement
        # graph the real tools write, so ordinals are one contiguous space.
        a = self._run("record_hypothesis", title="from the lookalike", description="x")
        b = self._add(title="from the real tool")
        self.assertEqual(sorted([a["ordinal"], b["ordinal"]]), [1, 2])
        titles = {n["title"] for n in
                  self.client.get("/api/engagements/eng1/hypothesis-graph").json()["nodes"]}
        self.assertEqual(titles, {"from the lookalike", "from the real tool"})


if __name__ == "__main__":
    unittest.main()

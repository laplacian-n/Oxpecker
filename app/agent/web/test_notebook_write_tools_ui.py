"""§12 step 3 — the engine's notebook WRITE tools reachable from the dev_server runtime.

After the reads, the write tools are mounted on the same plane: note_add, note_resolve, and
note_promote (which turns a note into a hypothesis in this engagement's graph). Driven through
dev_server's `_run_tool` seam (no model, no HTTP) and read back through the HTTP notebook/graph
endpoints to confirm the mutation is what the UI sees.

Run directly: `python3 -m unittest agent.web.test_notebook_write_tools_ui`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class NotebookWriteToolsUiTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="notebook-write-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions",)}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self._patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-such-key.txt"),
            patch.object(config, "technique_kb_path_for", lambda owner: self.tmp / "kb.db"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        dev_server._notebook_reset_cache()
        dev_server._graph_reset_cache()
        self.addCleanup(dev_server._notebook_reset_cache)
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

    def test_all_notebook_tools_are_in_the_ui_schema(self):
        names = {s["function"]["name"] for s in dev_server.TOOL_SCHEMAS}
        for t in ("note_add", "note_search", "note_resolve", "note_promote", "technique_recall"):
            self.assertIn(t, names, t)

    def test_note_add_lands_in_the_engagement_notebook(self):
        r = self._run("note_add", note="the /status endpoint leaks a stack trace", category="recon")
        self.assertTrue(r.get("ok"), r)
        body = self.client.get("/api/engagements/eng1/notebook").json()
        self.assertTrue(body["exists"])
        self.assertIn("stack trace", " ".join(n["text"] for n in body["notes"]))

    def test_note_resolve_marks_a_note_resolved(self):
        self._run("note_add", note="try default creds on the admin panel", category="todo")
        ordinal = self.client.get("/api/engagements/eng1/notebook").json()["notes"][0]["ordinal"]
        r = self._run("note_resolve", note_ref=str(ordinal), reason="admin/admin did not work")
        self.assertTrue(r.get("ok"), r)
        resolved = {n["ordinal"]: n["resolved"]
                    for n in self.client.get("/api/engagements/eng1/notebook").json()["notes"]}
        self.assertTrue(resolved[ordinal])

    def test_note_promote_turns_a_note_into_a_graph_hypothesis(self):
        self._run("note_add", note="login form may be SQL-injectable", category="todo")
        ordinal = self.client.get("/api/engagements/eng1/notebook").json()["notes"][0]["ordinal"]
        r = self._run("note_promote", note_ref=str(ordinal), title="login SQLi",
                      claim="the login form is injectable", phase="RECON",
                      impact=4, confidence_band="low", confidence_reason="from a notebook lead")
        self.assertTrue(r.get("ok"), r)
        # the promoted hypothesis is now in this engagement's graph, visible to the UI
        nodes = self.client.get("/api/engagements/eng1/hypothesis-graph").json()["nodes"]
        self.assertIn("login SQLi", [n["title"] for n in nodes])

    def test_note_tools_stay_within_the_engagement(self):
        self._run("note_add", note="only in eng1", category="recon")
        other = dev_server.Session(session_id="s2", engagement_id="eng2")
        dev_server._sessions["s2"] = other
        res = dev_server._run_tool("note_search", {"query": "only"}, other)
        self.assertEqual(res["results"], [])


if __name__ == "__main__":
    unittest.main()

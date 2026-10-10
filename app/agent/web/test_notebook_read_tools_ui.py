"""§12 step 3 — the engine's read-only notebook tools (note_search, technique_recall) reachable
from the dev_server runtime.

Mounted from the engine's own schema source and dispatched against the per-engagement notebook
service, so the UI runtime and the engine run one definition of each. Driven here through
dev_server's `_run_tool` seam (no model, no HTTP), checking they read the same notebook the model's
writes (record_note) land in, and the account-scoped technique library.

Run directly: `python3 -m unittest agent.web.test_notebook_read_tools_ui`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class NotebookReadToolsUiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notebook-read-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions",)}
        for n in self._saved:
            getattr(dev_server, n).clear()
        # A throwaway technique-KB path too, so technique_recall neither reads nor writes a real
        # user's global library during the test.
        self._patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            patch.object(dev_server, "_persist", lambda: None),
            patch.object(config, "technique_kb_path_for", lambda owner: self.tmp / "kb.db"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        dev_server._notebook_reset_cache()
        self.addCleanup(dev_server._notebook_reset_cache)
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
        self.assertIn("note_search", names)
        self.assertIn("technique_recall", names)

    def test_note_search_finds_a_note_the_model_recorded(self):
        self._run("record_note", text="tried union-based SQLi on login", category="technique")
        self._run("record_note", text="the /status page leaks a stack trace", category="observation")
        res = self._run("note_search", query="SQLi")
        self.assertTrue(res.get("ok"))
        self.assertEqual([r["ordinal"] for r in res["results"]], [1])

    def test_note_search_on_an_empty_notebook_is_a_clean_empty_result(self):
        res = self._run("note_search", query="anything")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["results"], [])

    def test_note_search_reads_this_engagements_notebook_not_another(self):
        self._run("record_note", text="only in eng1", category="todo")
        other = dev_server.Session(session_id="s2", engagement_id="eng2")
        dev_server._sessions["s2"] = other
        res = dev_server._run_tool("note_search", {"query": "only"}, other)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["results"], [])

    def test_technique_recall_runs_and_returns_results_list(self):
        # A 'technique' note overflows into the global KB; recall should then find it. Even if the
        # embedding backend is unavailable in this env, the call must return a clean ok+list.
        self._run("record_note", text="JWT alg=none bypass on the admin API", category="technique")
        res = self._run("technique_recall", query="JWT")
        self.assertTrue(res.get("ok"))
        self.assertIsInstance(res["results"], list)


if __name__ == "__main__":
    unittest.main()

"""§12 step 3 (notebook) — characterization test pinning dev_server's working-notebook HTTP
contract BEFORE its in-memory per-session store is swapped for the per-engagement NotebookService.

Exactly the role test_graph_ui_contract played for the graph (#48): nail down what the UI observes
from the notebook endpoints today, so the swap is proven to change the backing store and the keying
and nothing else the frontend reads. It drives the real write path (`_run_tool("record_note", ...)`)
and reads back only through the TestClient.

Three cases, the same ones the graph swap used: ordinal addressing (resolve by ordinal), session
<-> engagement keying (per-session buckets, cross-session ordinal collision), and the empty
notebook. Plus a guard on the overview payload shape (notes' field set, counts, version).

This pins CURRENT behavior, collision included; the swap then changes the keying deliberately and
rewrites the keying/collision cases, as the graph swap did.

Run directly: `python3 -m unittest agent.web.test_notebook_ui_contract`.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server

NOTE_FIELDS = {"ordinal", "text", "category", "refs", "resolved", "created_at"}


class NotebookUiContractTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self._saved = {n: dict(getattr(dev_server, n))
                       for n in ("_notebooks", "_sessions")}
        for n in self._saved:
            getattr(dev_server, n).clear()
        tmp = Path(tempfile.mkdtemp(prefix="notebook-ui-contract-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        self._patchers = [
            patch.object(config, "WEB_UI_API_KEY_FILE", tmp / "no-such-key.txt"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _make_session(self, session_id, engagement_id):
        dev_server._sessions[session_id] = dev_server.Session(
            session_id=session_id, engagement_id=engagement_id)

    def _note(self, session_id, **args):
        return dev_server._run_tool("record_note", args, dev_server._sessions[session_id])

    # ── ordinal addressing ────────────────────────────────────────────────────────────────
    def test_resolve_addresses_the_note_by_ordinal(self):
        self._make_session("s1", "eng1")
        self._note("s1", text="note one", category="todo")
        self._note("s1", text="note two", category="todo")
        r = self.client.post("/api/engagements/eng1/notebook/notes/2/resolve",
                             json={"action": "resolve"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["resolved"])
        notes = {n["ordinal"]: n["resolved"]
                 for n in self.client.get("/api/engagements/eng1/notebook").json()["notes"]}
        self.assertEqual(notes, {1: False, 2: True})

    def test_resolve_of_an_unknown_ordinal_is_404(self):
        self._make_session("s1", "eng1")
        self._note("s1", text="only one", category="todo")
        self.assertEqual(
            self.client.post("/api/engagements/eng1/notebook/notes/99/resolve",
                             json={"action": "resolve"}).status_code, 404)

    # ── session <-> engagement keying (current: per-session) ─────────────────────────────────
    def test_read_by_session_id_is_that_session_only(self):
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        self._note("sA", text="from A", category="observation")
        self._note("sB", text="from B", category="observation")
        texts = [n["text"] for n in
                 self.client.get("/api/engagements/sA/notebook").json()["notes"]]
        self.assertEqual(texts, ["from A"])

    def test_read_by_engagement_id_aggregates_sessions_with_colliding_ordinals(self):
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        a = self._note("sA", text="from A", category="observation")
        b = self._note("sB", text="from B", category="observation")
        # per-session buckets -> both notes are ordinal 1 (the collision this pins)
        self.assertEqual((a["note_ordinal"], b["note_ordinal"]), (1, 1))
        body = self.client.get("/api/engagements/engX/notebook").json()
        self.assertTrue(body["exists"])
        self.assertEqual(sorted(n["text"] for n in body["notes"]), ["from A", "from B"])

    # ── the empty notebook ───────────────────────────────────────────────────────────────────
    def test_empty_notebook_is_exists_false_200(self):
        r = self.client.get("/api/engagements/nope/notebook")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"exists": False, "notes": [], "counts": {}, "version": 0})

    # ── overview payload shape ───────────────────────────────────────────────────────────────
    def test_overview_shape_fields_counts_and_version(self):
        self._make_session("s1", "eng1")
        self._note("s1", text="a technique", category="technique")
        self._note("s1", text="an observation", category="observation")
        body = self.client.get("/api/engagements/eng1/notebook").json()
        self.assertTrue(body["exists"])
        self.assertEqual(set(body["notes"][0]), NOTE_FIELDS)
        self.assertEqual(body["counts"], {"technique": 1, "observation": 1})
        self.assertEqual(body["version"], 2)


if __name__ == "__main__":
    unittest.main()

"""§12 step 3 (notebook) — the working-notebook HTTP contract, now over the REAL store.

This began (gate PR) as a characterization test pinning dev_server's in-memory per-session
notebook. The swap replaces that store with the real NotebookService — ONE notebook per engagement
(§3.1), persisted in SQLite — exactly as the graph swap did. That is a deliberate behavior change,
and these assertions are rewritten to the intended behavior, not retrofitted to keep an old quirk:

  - OLD (the defect): each chat session kept its own note bucket, so two sessions in the same
    engagement each started their notes at ordinal 1 — colliding ordinals in what the UI presents
    as one engagement's notebook. The gate test pinned that collision.
  - NEW (asserted here): the notebook is per engagement. Notes from every session in an engagement
    share one ordinal space (no collisions) and one notebook; reading by a session id or by the
    engagement id returns that single notebook.

Unchanged — and still pinned — is the response the UI consumes: ordinal addressing on resolve, the
empty-notebook shape, and the note field set / counts / version, all byte-identical. In
particular the UI's four categories (technique/dead-end/todo/observation) are preserved even though
the store has no `observation` — it is mapped to the store's `misc` and back.

Run directly: `python3 -m unittest agent.web.test_notebook_ui_contract`.
"""
from __future__ import annotations

import shutil
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

        self.tmp = Path(tempfile.mkdtemp(prefix="notebook-ui-contract-"))
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
        dev_server._notebook_reset_cache()
        self.addCleanup(dev_server._notebook_reset_cache)
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

    # ── unchanged contract: ordinal addressing ───────────────────────────────────────────────
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

    def test_reopen_clears_resolved(self):
        self._make_session("s1", "eng1")
        self._note("s1", text="n", category="todo")
        self.client.post("/api/engagements/eng1/notebook/notes/1/resolve", json={"action": "resolve"})
        r = self.client.post("/api/engagements/eng1/notebook/notes/1/resolve", json={"action": "reopen"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["resolved"])

    def test_resolve_of_an_unknown_ordinal_is_404(self):
        self._make_session("s1", "eng1")
        self._note("s1", text="only one", category="todo")
        self.assertEqual(
            self.client.post("/api/engagements/eng1/notebook/notes/99/resolve",
                             json={"action": "resolve"}).status_code, 404)

    # ── the behavior change: one notebook per engagement, one ordinal space ──────────────────
    def test_sessions_in_one_engagement_share_a_notebook_and_do_not_collide(self):
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        a = self._note("sA", text="from A", category="observation")
        b = self._note("sB", text="from B", category="observation")
        self.assertEqual((a["note_ordinal"], b["note_ordinal"]), (1, 2))
        body = self.client.get("/api/engagements/engX/notebook").json()
        self.assertTrue(body["exists"])
        self.assertEqual(sorted((n["ordinal"], n["text"]) for n in body["notes"]),
                         [(1, "from A"), (2, "from B")])

    def test_reading_by_a_session_id_returns_its_engagement_notebook(self):
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        self._note("sA", text="from A", category="observation")
        self._note("sB", text="from B", category="observation")
        for sid in ("sA", "sB"):
            texts = sorted(n["text"] for n in
                           self.client.get(f"/api/engagements/{sid}/notebook").json()["notes"])
            self.assertEqual(texts, ["from A", "from B"])

    def test_distinct_engagements_keep_distinct_notebooks(self):
        self._make_session("sA", "engX")
        self._make_session("sC", "engY")
        self._note("sA", text="only in X", category="todo")
        self._note("sC", text="only in Y", category="todo")
        self.assertEqual([n["text"] for n in
                          self.client.get("/api/engagements/engX/notebook").json()["notes"]],
                         ["only in X"])
        self.assertEqual([n["text"] for n in
                          self.client.get("/api/engagements/engY/notebook").json()["notes"]],
                         ["only in Y"])

    # ── unchanged contract: the empty notebook ───────────────────────────────────────────────
    def test_empty_notebook_is_exists_false_200(self):
        r = self.client.get("/api/engagements/nope/notebook")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"exists": False, "notes": [], "counts": {}, "version": 0})

    # ── unchanged contract: payload shape, counts, and the preserved category vocabulary ──────
    def test_overview_shape_fields_counts_and_version(self):
        self._make_session("s1", "eng1")
        self._note("s1", text="a technique", category="technique")
        self._note("s1", text="an observation", category="observation")
        body = self.client.get("/api/engagements/eng1/notebook").json()
        self.assertTrue(body["exists"])
        self.assertEqual(set(body["notes"][0]), NOTE_FIELDS)
        # observation survives the observation->misc->observation round trip; counts use UI labels
        self.assertEqual(body["counts"], {"technique": 1, "observation": 1})
        self.assertEqual(body["version"], 2)
        self.assertEqual({n["category"] for n in body["notes"]}, {"technique", "observation"})


if __name__ == "__main__":
    unittest.main()

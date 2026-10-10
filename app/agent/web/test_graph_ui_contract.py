"""§12 step 2b — the hypothesis-graph HTTP contract, now over the REAL store.

This file began (PR #48) as a characterization test pinning dev_server's in-memory per-session
graph. Step 2b swaps that store for the real `HypothesisGraphService` — ONE graph per engagement
(§3.1), persisted in SQLite. That is a deliberate behavior change, and these assertions are
rewritten to the intended behavior, not retrofitted to keep an old quirk green:

  - OLD (the defect): each chat session kept its own graph bucket, so two sessions in the same
    engagement each started their hypotheses at ordinal 1 — colliding ordinals in what the UI
    presents as one engagement's graph, and N disconnected graphs that could never chain. The
    #48 test pinned that collision; it was the bug §3.1 names, and the characterization test
    earned its keep by surfacing it.
  - NEW (asserted here): the graph is per engagement. Hypotheses from every session in an
    engagement share one ordinal space (no collisions) and one graph; reading by a session id or
    by the engagement id returns that single graph.

What did NOT change — and is still pinned — is the response the UI consumes: 1-based ordinal
addressing, the empty-graph shape, and the exact field set, all identical byte for byte. The
swap changed the backing store and the keying, nothing the frontend destructures.

It drives the engine's real write path (`_run_tool("record_hypothesis", ...)`) and reads back
through the `TestClient`, so it exercises the accessor round trip over SQLite, not a dict.

Run directly: `python3 -m unittest agent.web.test_graph_ui_contract`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server

# The exact field set the frontend destructures off a graph node (grep of agent/web/static:
# status, ordinal, title, phase, notes, verdict, description, claim, why, parent_ordinal,
# attempts, observations). to_summary is the overview payload; to_detail adds the rest.
SUMMARY_FIELDS = {"ordinal", "title", "claim", "phase", "status", "verdict", "parent_ordinal"}
DETAIL_FIELDS = SUMMARY_FIELDS | {"description", "why", "attempts", "observations", "notes"}


class GraphUiContractTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        # The graph now lives in SQLite under DATA_DIR/<engagement>. Point DATA_DIR at a fresh
        # tmp dir and drop the service cache so each test starts from an empty on-disk store and
        # leaves nothing behind. The remaining in-memory stores (sessions, and the notebook/
        # findings the graph swap did NOT touch) are snapshotted and cleared the same way.
        self.tmp = Path(tempfile.mkdtemp(prefix="graph-ui-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {
            name: dict(getattr(dev_server, name))
            for name in ("_sessions", "_notebooks", "_findings")
        }
        for name in self._saved:
            getattr(dev_server, name).clear()

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

        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        for name, saved in self._saved.items():
            store = getattr(dev_server, name)
            store.clear()
            store.update(saved)

    # ── helpers ────────────────────────────────────────────────────────────────────────────
    def _make_session(self, session_id: str, engagement_id: str) -> None:
        dev_server._sessions[session_id] = dev_server.Session(
            session_id=session_id, engagement_id=engagement_id,
        )

    def _record(self, session_id: str, **args) -> dict:
        """Seed a hypothesis through the real engine write path, under this session."""
        return dev_server._run_tool("record_hypothesis", args, dev_server._sessions[session_id])

    # ── unchanged contract: ordinal addressing ───────────────────────────────────────────────
    def test_nodes_are_addressed_by_their_one_based_ordinal(self):
        self._make_session("s1", "eng1")
        r1 = self._record("s1", title="open port 8080", description="an http service is up")
        r2 = self._record("s1", title="default creds", description="admin/admin may work")
        self.assertEqual((r1["ordinal"], r2["ordinal"]), (1, 2))

        got = self.client.get("/api/engagements/eng1/hypothesis-graph/nodes/2")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["ordinal"], 2)
        self.assertEqual(got.json()["title"], "default creds")

        # An ordinal that was never assigned is a 404, not an empty 200 — the UI distinguishes.
        self.assertEqual(
            self.client.get("/api/engagements/eng1/hypothesis-graph/nodes/99").status_code, 404,
        )

    def test_operator_action_addresses_the_node_by_ordinal(self):
        self._make_session("s1", "eng1")
        self._record("s1", title="hyp one")
        self._record("s1", title="hyp two")
        # Park ordinal 2; ordinal 1 must be untouched — addressing is by ordinal, not position-blind.
        r = self.client.post(
            "/api/engagements/eng1/hypothesis-graph/nodes/2/operator-action",
            json={"action": "park", "reason": "deprioritised"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "parked")
        self.assertEqual(
            self.client.get("/api/engagements/eng1/hypothesis-graph/nodes/1").json()["status"],
            "open",
        )
        self.assertEqual(
            self.client.get("/api/engagements/eng1/hypothesis-graph/nodes/2").json()["status"],
            "parked",
        )

    def test_operator_note_is_persisted_and_counted(self):
        self._make_session("s1", "eng1")
        self._record("s1", title="hyp one")
        r = self.client.post(
            "/api/engagements/eng1/hypothesis-graph/nodes/1/operator-action",
            json={"action": "note", "text": "check this against the staging box"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["notes_count"], 1)
        detail = self.client.get("/api/engagements/eng1/hypothesis-graph/nodes/1").json()
        self.assertIn("check this against the staging box", detail["notes"])

    # ── the behavior change: one graph per engagement, one ordinal space ─────────────────────
    def test_sessions_in_one_engagement_share_a_graph_and_do_not_collide(self):
        # The §3.1 fix: two chat sessions under the same engagement write into ONE graph with ONE
        # ordinal space. The pre-2b store gave both first nodes ordinal 1 (a collision in what the
        # UI shows as a single graph); now the second gets ordinal 2.
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        a = self._record("sA", title="from A")
        b = self._record("sB", title="from B")
        self.assertEqual((a["ordinal"], b["ordinal"]), (1, 2))

        # The engagement endpoint shows both, each at its own ordinal.
        body = self.client.get("/api/engagements/engX/hypothesis-graph").json()
        self.assertTrue(body["exists"])
        self.assertEqual(
            sorted((n["ordinal"], n["title"]) for n in body["nodes"]),
            [(1, "from A"), (2, "from B")],
        )
        # Opening each by the ordinal it was handed finds the right node.
        self.assertEqual(
            self.client.get("/api/engagements/engX/hypothesis-graph/nodes/1").json()["title"],
            "from A",
        )
        self.assertEqual(
            self.client.get("/api/engagements/engX/hypothesis-graph/nodes/2").json()["title"],
            "from B",
        )

    def test_reading_by_a_session_id_returns_its_engagement_graph(self):
        # A session id resolves to its engagement's single graph — so reading by either session's
        # id returns the same whole-engagement graph, not that session's slice.
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        self._record("sA", title="from A")
        self._record("sB", title="from B")
        for sid in ("sA", "sB"):
            titles = sorted(n["title"] for n in
                            self.client.get(f"/api/engagements/{sid}/hypothesis-graph").json()["nodes"])
            self.assertEqual(titles, ["from A", "from B"])

    def test_distinct_engagements_keep_distinct_graphs(self):
        self._make_session("sA", "engX")
        self._make_session("sC", "engY")
        self._record("sA", title="only in X")
        self._record("sC", title="only in Y")
        self.assertEqual(
            [n["title"] for n in
             self.client.get("/api/engagements/engX/hypothesis-graph").json()["nodes"]],
            ["only in X"],
        )
        self.assertEqual(
            [n["title"] for n in
             self.client.get("/api/engagements/engY/hypothesis-graph").json()["nodes"]],
            ["only in Y"],
        )

    def test_parent_ordinal_round_trips_as_the_lineage_edge(self):
        # record_hypothesis with a parent_ordinal must surface as parent_ordinal on the child and
        # a lineage edge in the overview — the store assigns the edge from the ordinal itself.
        self._make_session("s1", "eng1")
        self._record("s1", title="root")
        self._record("s1", title="child", parent_ordinal=1)
        child = self.client.get("/api/engagements/eng1/hypothesis-graph/nodes/2").json()
        self.assertEqual(child["parent_ordinal"], 1)
        edges = self.client.get("/api/engagements/eng1/hypothesis-graph").json()["edges"]
        self.assertIn((1, 2), [(e["from_ordinal"], e["to_ordinal"]) for e in edges])

    # ── unchanged contract: the empty graph ──────────────────────────────────────────────────
    def test_empty_graph_answers_200_with_exists_false(self):
        body = self.client.get("/api/engagements/nope/hypothesis-graph")
        self.assertEqual(body.status_code, 200)
        self.assertEqual(
            body.json(),
            {"exists": False, "nodes": [], "edges": [], "graph_state": None},
        )

    def test_node_lookup_on_an_empty_graph_is_404(self):
        self.assertEqual(
            self.client.get("/api/engagements/nope/hypothesis-graph/nodes/1").status_code, 404,
        )

    # ── unchanged contract: the UI field set ─────────────────────────────────────────────────
    def test_overview_nodes_carry_exactly_the_summary_fields(self):
        self._make_session("s1", "eng1")
        self._record("s1", title="h", description="d")
        node = self.client.get("/api/engagements/eng1/hypothesis-graph").json()["nodes"][0]
        self.assertEqual(set(node), SUMMARY_FIELDS)

    def test_node_detail_carries_exactly_the_detail_fields(self):
        self._make_session("s1", "eng1")
        self._record("s1", title="h", description="d")
        node = self.client.get("/api/engagements/eng1/hypothesis-graph/nodes/1").json()
        self.assertEqual(set(node), DETAIL_FIELDS)


if __name__ == "__main__":
    unittest.main()

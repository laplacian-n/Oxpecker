"""§12 step 2b — the characterization test that pins dev_server's hypothesis-graph HTTP contract
BEFORE the in-memory store behind the `_graph_*` accessors is swapped for the real graph service.

The point of a characterization test is not to assert what the code *should* do — it is to nail
down what the UI actually observes today, so the store swap (step 2b proper) is proven to change
the backing store and nothing the frontend can see. When this test stays green across the swap,
the swap was behaviour-preserving; when it goes red, the swap changed something the UI reads, and
that has to be a deliberate, explained edit.

It drives the real write path (`_run_tool("record_hypothesis", ...)`, the same handler the engine
calls) and reads back only through the HTTP API (`TestClient`), because that round trip — write
through the accessors, read through the accessors — is exactly what the swap rewires. Seeding by
poking `_graphs` directly would test the dict, not the contract.

Three cases, the ones the owner named:
  1. ordinal addressing — the UI addresses a node by its 1-based ordinal, not an internal id.
  2. session <-> engagement keying — the graph is stored per session; a read by engagement id
     aggregates across that engagement's sessions (the `_scope_keys` contract), and first match
     wins when ordinals collide across sessions.
  3. the empty graph — an engagement with no nodes answers 200 with `exists: False`, not 404.

Plus a guard that a returned node carries exactly the UI-consumed field set and no more, so the
swap cannot quietly widen or narrow the payload the frontend destructures.

Run directly: `python3 -m unittest agent.web.test_graph_ui_contract`.
"""
from __future__ import annotations

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

        # Snapshot and clear every module-global store this test touches, so it neither sees nor
        # leaves behind state from another test in the same process.
        self._saved = {
            name: dict(getattr(dev_server, name))
            for name in ("_graphs", "_graph_edges", "_sessions", "_notebooks", "_findings")
        }
        for name in self._saved:
            getattr(dev_server, name).clear()

        # Auth off (point the key file at a path that does not exist) and persistence off (never
        # touch the real state file), both regardless of what this machine has configured.
        tmp = Path(tempfile.mkdtemp(prefix="graph-ui-contract-"))
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
        """Seed a hypothesis through the real engine write path, keyed by this session."""
        return dev_server._run_tool("record_hypothesis", args, dev_server._sessions[session_id])

    # ── case 1: ordinal addressing ───────────────────────────────────────────────────────────
    def test_nodes_are_addressed_by_their_one_based_ordinal(self):
        self._make_session("s1", "eng1")
        r1 = self._record("s1", title="open port 8080", description="an http service is up")
        r2 = self._record("s1", title="default creds", description="admin/admin may work")
        # The write path assigns ordinals 1, 2 in record order (len(nodes) + 1).
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

    # ── case 2: session <-> engagement keying ────────────────────────────────────────────────
    def test_read_by_session_id_is_that_session_only(self):
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        self._record("sA", title="from A")
        self._record("sB", title="from B")

        titles = [n["title"] for n in
                  self.client.get("/api/engagements/sA/hypothesis-graph").json()["nodes"]]
        self.assertEqual(titles, ["from A"])

    def test_read_by_engagement_id_aggregates_its_sessions(self):
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        self._record("sA", title="from A")
        self._record("sB", title="from B")

        body = self.client.get("/api/engagements/engX/hypothesis-graph").json()
        self.assertTrue(body["exists"])
        self.assertEqual(
            sorted(n["title"] for n in body["nodes"]), ["from A", "from B"],
        )

    def test_colliding_ordinals_across_sessions_resolve_first_match_wins(self):
        # Each session's graph is its own bucket, so both first nodes are ordinal 1. Reading
        # node 1 by engagement id returns the first session's node in _scope_keys order (the
        # order sessions were inserted). This is a real, if sharp, property of today's aggregate
        # read — pinned so the swap cannot change which node answers without the test noticing.
        self._make_session("sA", "engX")
        self._make_session("sB", "engX")
        self.assertEqual(self._record("sA", title="from A")["ordinal"], 1)
        self.assertEqual(self._record("sB", title="from B")["ordinal"], 1)

        got = self.client.get("/api/engagements/engX/hypothesis-graph/nodes/1").json()
        self.assertEqual(got["title"], "from A")

    def test_legacy_data_stored_under_the_engagement_id_is_surfaced(self):
        # Before the per-session split, graphs were keyed by engagement id directly. _scope_keys
        # still surfaces a bucket that exists under the path id itself, with no matching session.
        dev_server._graph_add_node(
            "engLegacy", dev_server.HypothesisNode(ordinal=1, title="legacy node"),
        )
        body = self.client.get("/api/engagements/engLegacy/hypothesis-graph").json()
        self.assertTrue(body["exists"])
        self.assertEqual([n["title"] for n in body["nodes"]], ["legacy node"])

    # ── case 3: the empty graph ──────────────────────────────────────────────────────────────
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

    # ── the UI-field contract ────────────────────────────────────────────────────────────────
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

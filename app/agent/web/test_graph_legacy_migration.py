"""§12 step 2c — folding a legacy per-session JSON graph into the per-engagement SQLite store.

Before the 2b swap, dev_server persisted hypothesis graphs into its JSON state file, keyed per
chat session. The swap moved the store to SQLite (one graph per engagement) and stopped writing
graphs to the JSON. This test covers the one-time migration that reads such a legacy block on
startup and folds it into the per-engagement store — so an existing dev/lab state file is not
silently dropped — and proves it is idempotent (a second load does not duplicate).

Run directly: `python3 -m unittest agent.web.test_graph_legacy_migration`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import dev_server


def _legacy_state(sessions, graphs):
    return {"sessions": sessions, "engagements": [], "findings": {},
            "graphs": graphs, "graph_edges": {}, "notebooks": {}}


class GraphLegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="graph-legacy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n))
                       for n in ("_sessions", "_engagements", "_notebooks", "_findings")}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self._patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            patch.object(dev_server, "STATE_FILE", self.tmp / "state.json"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        dev_server._graph_reset_cache()
        self.addCleanup(dev_server._graph_reset_cache)

    def tearDown(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _write_and_load(self, state):
        dev_server.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        dev_server._load_state()

    def test_two_legacy_sessions_fold_into_one_engagement_graph(self):
        # Two old session-keyed buckets under the same engagement, each starting at ordinal 1 (the
        # old collision). After migration they share one engagement graph with distinct ordinals.
        state = _legacy_state(
            sessions=[
                {"session_id": "sA", "engagement_id": "engX"},
                {"session_id": "sB", "engagement_id": "engX"},
            ],
            graphs={
                "sA": [{"ordinal": 1, "title": "from A", "description": "a", "phase": "RECON",
                        "status": "open", "verdict": ""}],
                "sB": [{"ordinal": 1, "title": "from B", "description": "b", "phase": "RECON",
                        "status": "parked", "verdict": ""}],
            },
        )
        self._write_and_load(state)
        svc = dev_server._graph_service("engX")
        hyps = svc.store.list_hypotheses()
        self.assertEqual(sorted(h["title"] for h in hyps), ["from A", "from B"])
        self.assertEqual(sorted(h["ordinal"] for h in hyps), [1, 2])
        parked = [h for h in hyps if h["title"] == "from B"][0]
        self.assertEqual(parked["lifecycle_status"], "parked")

    def test_parent_lineage_is_remapped_to_new_ordinals(self):
        state = _legacy_state(
            sessions=[{"session_id": "s1", "engagement_id": "engY"}],
            graphs={"s1": [
                {"ordinal": 1, "title": "root", "description": "r", "phase": "RECON"},
                {"ordinal": 2, "title": "child", "description": "c", "phase": "RECON",
                 "parent_ordinal": 1},
            ]},
        )
        self._write_and_load(state)
        svc = dev_server._graph_service("engY")
        child = svc.store.get_by_ordinal(2)
        root = svc.store.get_by_ordinal(1)
        self.assertEqual(child["primary_parent_id"], root["hypothesis_id"])

    def test_notes_and_verdict_survive(self):
        state = _legacy_state(
            sessions=[{"session_id": "s1", "engagement_id": "engZ"}],
            graphs={"s1": [
                {"ordinal": 1, "title": "h", "description": "d", "phase": "RECON",
                 "verdict": "confirmed", "notes": ["operator said check staging"]},
            ]},
        )
        self._write_and_load(state)
        svc = dev_server._graph_service("engZ")
        h = svc.store.get_by_ordinal(1)
        self.assertEqual(h["verdict"], "confirmed")
        self.assertEqual([n["text"] for n in svc.store.list_operator_notes(h["hypothesis_id"])],
                         ["operator said check staging"])

    def test_migration_is_idempotent(self):
        state = _legacy_state(
            sessions=[{"session_id": "s1", "engagement_id": "engI"}],
            graphs={"s1": [{"ordinal": 1, "title": "only once", "description": "x",
                            "phase": "RECON"}]},
        )
        self._write_and_load(state)
        # A second load of the same legacy block must not create a duplicate.
        dev_server._load_state()
        svc = dev_server._graph_service("engI")
        self.assertEqual(len(svc.store.list_hypotheses()), 1)

    def test_legacy_junk_status_and_verdict_are_dropped_not_crashed(self):
        # The old store never validated status/verdict, so a legacy file can hold values the real
        # store's enums reject. The migration must drop them, not raise.
        state = _legacy_state(
            sessions=[{"session_id": "s1", "engagement_id": "engJ"}],
            graphs={"s1": [{"ordinal": 1, "title": "junk", "description": "d", "phase": "RECON",
                            "status": "not-a-real-status", "verdict": "also-bogus"}]},
        )
        self._write_and_load(state)  # must not raise
        svc = dev_server._graph_service("engJ")
        h = svc.store.get_by_ordinal(1)
        self.assertEqual(h["lifecycle_status"], "open")      # junk status ignored, default kept
        self.assertEqual(h["verdict"], "unassessed")         # junk verdict ignored


if __name__ == "__main__":
    unittest.main()

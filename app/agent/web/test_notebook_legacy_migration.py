"""§12 step 3 (notebook) — folding a legacy per-session JSON notebook into the per-engagement
SQLite store, the notebook analogue of the graph's 2c migration.

Before the swap, dev_server persisted notebooks into its JSON state file, keyed per chat session.
The swap moved the store to SQLite (one notebook per engagement) and stopped writing notebooks to
the JSON. This covers the one-time migration that reads such a legacy block on startup and folds it
in — so an existing dev/lab state file is not silently dropped — and proves it is idempotent.

Run directly: `python3 -m unittest agent.web.test_notebook_legacy_migration`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import dev_server


def _legacy_state(sessions, notebooks):
    return {"sessions": sessions, "engagements": [], "findings": {},
            "graphs": {}, "graph_edges": {}, "notebooks": notebooks}


class NotebookLegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notebook-legacy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n))
                       for n in ("_sessions", "_engagements", "_findings")}
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
        dev_server._notebook_reset_cache()
        dev_server._graph_reset_cache()
        self.addCleanup(dev_server._notebook_reset_cache)
        self.addCleanup(dev_server._graph_reset_cache)

    def tearDown(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _write_and_load(self, state):
        dev_server.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        dev_server._load_state()

    def test_two_legacy_sessions_fold_into_one_notebook(self):
        state = _legacy_state(
            sessions=[{"session_id": "sA", "engagement_id": "engX"},
                      {"session_id": "sB", "engagement_id": "engX"}],
            notebooks={
                "sA": [{"ordinal": 1, "text": "from A", "category": "todo", "resolved": False}],
                "sB": [{"ordinal": 1, "text": "from B", "category": "observation", "resolved": True}],
            },
        )
        self._write_and_load(state)
        svc = dev_server._notebook_service("engX")
        notes = svc.store.list_notes()
        self.assertEqual(sorted(n["note"] for n in notes), ["from A", "from B"])
        self.assertEqual(sorted(n["ordinal"] for n in notes), [1, 2])
        resolved = [n for n in notes if n["note"] == "from B"][0]
        self.assertEqual(resolved["status"], "resolved")
        # observation -> misc in the store
        self.assertEqual(resolved["category"], "misc")

    def test_migration_is_idempotent(self):
        state = _legacy_state(
            sessions=[{"session_id": "s1", "engagement_id": "engI"}],
            notebooks={"s1": [{"ordinal": 1, "text": "only once", "category": "todo"}]},
        )
        self._write_and_load(state)
        dev_server._load_state()  # second load must not duplicate
        self.assertEqual(len(dev_server._notebook_service("engI").store.list_notes()), 1)

    def test_junk_category_falls_back_to_misc_not_crash(self):
        state = _legacy_state(
            sessions=[{"session_id": "s1", "engagement_id": "engJ"}],
            notebooks={"s1": [{"ordinal": 1, "text": "weird", "category": "not-a-category"}]},
        )
        self._write_and_load(state)  # must not raise
        n = dev_server._notebook_service("engJ").store.get_by_ordinal(1)
        self.assertEqual(n["category"], "misc")


if __name__ == "__main__":
    unittest.main()

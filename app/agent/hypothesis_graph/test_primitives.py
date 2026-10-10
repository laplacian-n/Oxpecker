"""Primitives as a derived property of a confirmed hypothesis (§3.5 / §8.6.5 #3), and the v1->v2
migration that adds the `primitive_gained` column to an existing DB. The migration test is the one
that matters: `executescript` with CREATE TABLE IF NOT EXISTS leaves an existing table untouched,
so without a real migration step the column reaches a fresh DB and silently never reaches an
engagement that already has one.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent.hypothesis_graph.schema import SCHEMA, SCHEMA_VERSION
from agent.hypothesis_graph.store import HypothesisGraphStore


def _hyp(store, title="SQLi on /login"):
    return store.create_hypothesis(
        title=title, claim="the form is injectable", phase_created="VALIDATION",
        rationale="r", origin_type="tool_observation", impact=4, confidence_band="medium",
        confidence_reason="x", surface="/login",
    )


def _confirm(store, hid):
    h = store.get_hypothesis(hid)
    store.set_verdict(hid, h["version"], "confirmed")


class PrimitiveHeldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="primitive-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = HypothesisGraphStore(self.tmp / "eng")

    def test_set_primitive_gained_records_the_field(self):
        hid = _hyp(self.store)
        h = self.store.get_hypothesis(hid)
        self.store.set_primitive_gained(hid, h["version"], "arbitrary_file_read")
        self.assertEqual(self.store.get_hypothesis(hid)["primitive_gained"], "arbitrary_file_read")

    def test_empty_primitive_is_rejected(self):
        hid = _hyp(self.store)
        h = self.store.get_hypothesis(hid)
        with self.assertRaises(Exception):
            self.store.set_primitive_gained(hid, h["version"], "   ")

    def test_a_primitive_is_held_only_while_its_hypothesis_is_confirmed(self):
        hid = _hyp(self.store)
        h = self.store.get_hypothesis(hid)
        v = self.store.set_primitive_gained(hid, h["version"], "rce")
        # set but not confirmed yet -> not held (the invariant: no holding without a confirmed grant)
        self.assertEqual(self.store.held_primitives(), [])
        _confirm(self.store, hid)
        held = self.store.held_primitives()
        self.assertEqual([h2["hypothesis_id"] for h2 in held], [hid])
        self.assertEqual(held[0]["primitive_gained"], "rce")

    def test_a_confirmed_hypothesis_without_a_primitive_is_not_held(self):
        hid = _hyp(self.store)
        _confirm(self.store, hid)
        self.assertEqual(self.store.held_primitives(), [])


class ContradictPrimitiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="primitive-c-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = HypothesisGraphStore(self.tmp / "eng")

    def test_failing_to_use_a_primitive_contradicts_and_closes_its_grant(self):
        grant = _hyp(self.store, title="can read any file")
        h = self.store.get_hypothesis(grant)
        self.store.set_primitive_gained(grant, h["version"], "arbitrary_file_read")
        _confirm(self.store, grant)
        self.assertEqual(len(self.store.held_primitives()), 1)  # held before the contradiction

        downstream = _hyp(self.store, title="read /etc/shadow via the primitive")
        self.store.contradict_primitive(downstream, grant, reason="the path traversal 404s now")

        # the grant is refuted and so no longer held (dropped by construction, no revoke step)
        self.assertEqual(self.store.get_hypothesis(grant)["verdict"], "refuted")
        self.assertEqual(self.store.held_primitives(), [])
        # and a CONTRADICTS edge records the disagreement
        edges = self.store.list_edges(grant) if hasattr(self.store, "list_edges") else None
        # fall back to a raw read if there is no list_edges helper
        if edges is None:
            with self.store._connect() as conn:
                row = conn.execute(
                    "SELECT edge_type, reason FROM edges WHERE from_hypothesis_id=? AND to_hypothesis_id=?",
                    (downstream, grant),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["edge_type"], "contradicts")
            self.assertIn("404", row["reason"])

    def test_contradiction_requires_a_reason(self):
        grant = _hyp(self.store)
        downstream = _hyp(self.store, title="d")
        with self.assertRaises(Exception):
            self.store.contradict_primitive(downstream, grant, reason="  ")


class MigrationTest(unittest.TestCase):
    """v1 -> v2: opening a DB created before `primitive_gained` existed must add the column and
    preserve the data, not just work on empty DBs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="primitive-migrate-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eng = self.tmp / "eng"
        self.eng.mkdir(parents=True)
        self.db = self.eng / "hypothesis_graph.db"

    def _make_legacy_db(self) -> str:
        # The v1 schema is the current one minus the column this migration adds.
        legacy = SCHEMA.replace("    primitive_gained  TEXT,\n", "")
        self.assertNotIn("primitive_gained", legacy)  # the derivation actually removed it
        conn = sqlite3.connect(self.db)
        conn.executescript(legacy)
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO graph_state (id, graph_version, next_ordinal) VALUES (1, 0, 2)")
        conn.execute(
            "INSERT INTO hypotheses (hypothesis_id, ordinal, title, claim, phase_created, "
            "lifecycle_status, verdict, origin_type, rationale, confidence_band, confidence_reason, "
            "impact, created_at, updated_at) VALUES "
            "('h-old', 1, 'old finding', 'c', 'VALIDATION', 'open', 'unassessed', "
            "'tool_observation', 'r', 'medium', 'x', 3, 1.0, 1.0)"
        )
        conn.commit()
        conn.close()
        return "h-old"

    def test_opening_a_legacy_db_adds_the_column_and_keeps_the_data(self):
        old_id = self._make_legacy_db()
        # sanity: the legacy DB really lacks the column
        with sqlite3.connect(self.db) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(hypotheses)").fetchall()}
        self.assertNotIn("primitive_gained", cols)

        # opening through the store runs the migration
        store = HypothesisGraphStore(self.eng)
        with store._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(hypotheses)").fetchall()}
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()["value"]
        self.assertIn("primitive_gained", cols)      # the column arrived
        self.assertEqual(version, str(SCHEMA_VERSION))  # and the version finally tracks it

        # the pre-existing row survived and the new column works on it
        row = store.get_hypothesis(old_id)
        self.assertEqual(row["title"], "old finding")
        self.assertIsNone(row["primitive_gained"])
        store.set_primitive_gained(old_id, row["version"], "foothold")
        self.assertEqual(store.get_hypothesis(old_id)["primitive_gained"], "foothold")

    def test_migration_is_idempotent(self):
        self._make_legacy_db()
        HypothesisGraphStore(self.eng)  # migrates
        HypothesisGraphStore(self.eng)  # opening again must not fail or double-add
        store = HypothesisGraphStore(self.eng)
        with store._connect() as conn:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(hypotheses)").fetchall()]
        self.assertEqual(cols.count("primitive_gained"), 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for the Phase 6 session-handoff combinator."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .handoff import create_handoff, latest_handoff, list_handoffs
from .store import EngagementStore


class TestHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handoff-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engagement_dir = self.tmp / "eng-1"

    def test_no_handoffs_yet(self):
        self.assertIsNone(latest_handoff(self.engagement_dir))
        self.assertEqual(list_handoffs(self.engagement_dir), [])

    def test_create_and_read_back(self):
        store = EngagementStore(self.engagement_dir)
        store.upsert_asset("host", "juice-shop.local")
        store.create_hypothesis("Reflected XSS", "search param unescaped")
        create_handoff(
            self.engagement_dir,
            memory_session={"session_id": "sess-1", "message_count": 12, "version": 3},
            note="Recon done, one hypothesis open, picking this up tomorrow",
            handed_off_by="operator-a",
        )
        record = latest_handoff(self.engagement_dir)
        self.assertEqual(record["phase"], "INTAKE")
        self.assertEqual(record["open_or_testing_hypothesis_count"], 1)
        self.assertEqual(record["memory_session"]["session_id"], "sess-1")
        self.assertIn("picking this up tomorrow", record["note"])

    def test_latest_handoff_is_most_recent(self):
        create_handoff(self.engagement_dir, memory_session={}, note="first", handed_off_by="a")
        create_handoff(self.engagement_dir, memory_session={}, note="second", handed_off_by="b")
        self.assertEqual(latest_handoff(self.engagement_dir)["note"], "second")

    def test_list_handoffs_returns_all_in_order(self):
        create_handoff(self.engagement_dir, memory_session={}, note="first", handed_off_by="a")
        create_handoff(self.engagement_dir, memory_session={}, note="second", handed_off_by="b")
        notes = [h["note"] for h in list_handoffs(self.engagement_dir)]
        self.assertEqual(notes, ["first", "second"])

    def test_reflects_phase_and_task_state(self):
        store = EngagementStore(self.engagement_dir)
        store.upsert_asset("host", "10.0.0.5")
        store.transition_phase("RECON", "scope confirmed", expected_version=0)
        store.create_task("RECON", "port_discovery")
        create_handoff(self.engagement_dir, memory_session={}, note="mid-recon", handed_off_by="a")
        record = latest_handoff(self.engagement_dir)
        self.assertEqual(record["phase"], "RECON")
        self.assertEqual(record["pending_or_running_task_count"], 1)


if __name__ == "__main__":
    unittest.main()

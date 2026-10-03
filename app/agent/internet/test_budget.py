"""Tests for M5.5 per-channel budget tracking."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .budget import BudgetExceededError, ChannelBudget, ChannelBudgetTracker, UnknownChannelError


class TestChannelBudgetTracker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="internet-budget-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_unknown_channel_rejected(self):
        with self.assertRaises(UnknownChannelError):
            ChannelBudgetTracker("s1", "made_up_channel", state_dir=self.tmp)

    def test_consumes_within_budget(self):
        tracker = ChannelBudgetTracker(
            "s1", "target_http", budget=ChannelBudget(max_queries=5, max_bytes=1000, window_s=3600),
            state_dir=self.tmp,
        )
        record = tracker.check_and_consume(query_cost=1, byte_cost=100)
        self.assertEqual(record["queries"], 1)
        self.assertEqual(record["bytes"], 100)

    def test_query_budget_exceeded(self):
        tracker = ChannelBudgetTracker(
            "s1", "target_http", budget=ChannelBudget(max_queries=2, max_bytes=1_000_000, window_s=3600),
            state_dir=self.tmp,
        )
        tracker.check_and_consume()
        tracker.check_and_consume()
        with self.assertRaises(BudgetExceededError):
            tracker.check_and_consume()

    def test_byte_budget_exceeded(self):
        tracker = ChannelBudgetTracker(
            "s1", "target_http", budget=ChannelBudget(max_queries=100, max_bytes=500, window_s=3600),
            state_dir=self.tmp,
        )
        tracker.check_and_consume(byte_cost=400)
        with self.assertRaises(BudgetExceededError):
            tracker.check_and_consume(byte_cost=200)

    def test_window_resets_usage(self):
        tracker = ChannelBudgetTracker(
            "s1", "target_http", budget=ChannelBudget(max_queries=1, max_bytes=1000, window_s=0),
            state_dir=self.tmp,
        )
        tracker.check_and_consume()
        # window_s=0 means every check is a new window
        record = tracker.check_and_consume()
        self.assertEqual(record["queries"], 1)

    def test_separate_channels_have_independent_budgets(self):
        t1 = ChannelBudgetTracker("s1", "target_http", state_dir=self.tmp)
        t2 = ChannelBudgetTracker("s1", "knowledge_fetch", state_dir=self.tmp)
        t1.check_and_consume()
        self.assertEqual(t2.usage()["queries"], 0)

    def test_separate_sessions_have_independent_budgets(self):
        t1 = ChannelBudgetTracker("session-a", "target_http", state_dir=self.tmp)
        t2 = ChannelBudgetTracker("session-b", "target_http", state_dir=self.tmp)
        t1.check_and_consume()
        self.assertEqual(t2.usage()["queries"], 0)

    def test_usage_persists_across_tracker_instances(self):
        ChannelBudgetTracker("s1", "target_http", state_dir=self.tmp).check_and_consume()
        reopened = ChannelBudgetTracker("s1", "target_http", state_dir=self.tmp)
        self.assertEqual(reopened.usage()["queries"], 1)


if __name__ == "__main__":
    unittest.main()

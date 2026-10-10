"""Tests for the read-only spend report (AGENT_ARCHITECTURE.md step 7, first half).

The report must agree with the ledger it reads: its `committed` and `remaining` are checked
against `SpendLedger.snapshot()`, and a released reservation must not count toward committed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.engagement.spend import SpendLedger
from agent.engagement.spend_report import UNATTRIBUTED, spend_report


class SpendReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "eng"

    def tearDown(self):
        self._tmp.cleanup()

    def _populated(self) -> SpendLedger:
        ledger = SpendLedger(self.dir)
        ledger.set_budget(100.0)
        # worker alpha: reserve 10, settle at 7 (under the estimate)
        a = ledger.reserve(10.0, worker="alpha")
        ledger.settle(a, 7.0)
        # worker beta: reserve 20, settle at 25 (overran the estimate)
        b = ledger.reserve(20.0, worker="beta")
        ledger.settle(b, 25.0)
        # abandoned: reserve 15 for beta, then release it because the work never ran
        r = ledger.reserve(15.0, worker="beta")
        ledger.release(r)
        # still open: reserve 5 for alpha, never settled or released
        ledger.reserve(5.0, worker="alpha")
        return ledger

    def test_counts_match_what_was_done(self):
        report = spend_report(self._populated())
        self.assertEqual(report["settled_count"], 2)
        self.assertEqual(report["released_count"], 1)
        self.assertEqual(report["open_count"], 1)

    def test_by_worker_attributes_settled_actuals_and_open_holds(self):
        report = spend_report(self._populated())
        # alpha: settled 7 + open hold 5. beta: settled 25 + released 15 contributes nothing.
        self.assertEqual(report["by_worker"]["alpha"], 12.0)
        self.assertEqual(report["by_worker"]["beta"], 25.0)

    def test_committed_and_remaining_agree_with_snapshot(self):
        ledger = self._populated()
        report = spend_report(ledger)
        snap = ledger.snapshot()
        self.assertEqual(report["cap"], snap["cap"])
        self.assertAlmostEqual(report["committed"], snap["spent"] + snap["reserved"])
        self.assertAlmostEqual(report["remaining"], snap["remaining"])
        self.assertAlmostEqual(report["committed"], 37.0)  # 7 + 25 + 5
        self.assertAlmostEqual(report["remaining"], 63.0)

    def test_released_reservation_does_not_count_toward_committed(self):
        # Break-it check: if release() counted, committed would include the 15 abandoned hold.
        report = spend_report(self._populated())
        self.assertAlmostEqual(report["committed"], 37.0)  # not 52 = 37 + the abandoned 15
        self.assertAlmostEqual(report["abandoned"], 15.0)

    def test_by_worker_sums_to_committed(self):
        report = spend_report(self._populated())
        self.assertAlmostEqual(sum(report["by_worker"].values()), report["committed"])

    def test_unattributed_reservations_are_keyed_as_unattributed(self):
        ledger = SpendLedger(self.dir)
        ledger.set_budget(10.0)
        rid = ledger.reserve(3.0)  # no worker
        ledger.settle(rid, 2.0)
        report = spend_report(ledger)
        self.assertEqual(report["by_worker"], {UNATTRIBUTED: 2.0})
        self.assertNotIn(None, report["by_worker"])

    def test_unmetered_ledger_reports_no_cap_and_no_remaining(self):
        ledger = SpendLedger(self.dir)
        ledger.reserve(4.0, worker="gamma")
        report = spend_report(ledger)
        self.assertIsNone(report["cap"])
        self.assertIsNone(report["remaining"])
        self.assertEqual(report["committed"], 4.0)

    def test_remaining_clamped_at_zero_when_settle_overruns_cap(self):
        ledger = SpendLedger(self.dir)
        ledger.set_budget(5.0)
        rid = ledger.reserve(4.0, worker="delta")
        ledger.settle(rid, 9.0)  # overran the cap; ledger records it, never negative remaining
        report = spend_report(ledger)
        self.assertEqual(report["remaining"], 0.0)
        self.assertEqual(report["remaining"], ledger.snapshot()["remaining"])

    def test_empty_ledger(self):
        ledger = SpendLedger(self.dir)
        report = spend_report(ledger)
        self.assertEqual(
            (report["settled_count"], report["released_count"], report["open_count"]), (0, 0, 0)
        )
        self.assertEqual(report["by_worker"], {})
        self.assertEqual(report["committed"], 0.0)
        self.assertEqual(report["abandoned"], 0.0)

    def test_report_is_read_only(self):
        ledger = self._populated()
        before = spend_report(ledger)
        spend_report(ledger)
        self.assertEqual(spend_report(ledger), before)
        # The ledger's own state is unchanged by reading it.
        self.assertEqual(ledger.snapshot()["reserved"], 5.0)


if __name__ == "__main__":
    unittest.main()

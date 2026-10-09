"""Tests for the per-engagement spend ledger (AGENT_ARCHITECTURE.md §14.1 D).

The load-bearing pair, as with the event log's sequence number: `test_naive_check_then_spend_overshoots`
proves the race §14.1 D names is real and this harness can make it fire, so that
`test_concurrent_reserve_never_overshoots` is known not to be inert. Break it first.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from agent.engagement.spend import BudgetExhausted, SpendLedger


class SpendBasicsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "eng"

    def tearDown(self):
        self._tmp.cleanup()

    def test_unmetered_by_default_reserves_anything(self):
        ledger = SpendLedger(self.dir)
        ledger.reserve(1_000_000.0)
        snap = ledger.snapshot()
        self.assertIsNone(snap["cap"])
        self.assertIsNone(snap["remaining"])
        self.assertEqual(snap["reserved"], 1_000_000.0)

    def test_reserve_settle_moves_from_reserved_to_spent(self):
        ledger = SpendLedger(self.dir)
        ledger.set_budget(10.0)
        rid = ledger.reserve(4.0)
        self.assertEqual(ledger.snapshot()["reserved"], 4.0)
        ledger.settle(rid, 3.0)  # actual came in under the estimate
        snap = ledger.snapshot()
        self.assertEqual((snap["spent"], snap["reserved"], snap["remaining"]), (3.0, 0.0, 7.0))

    def test_a_reservation_that_would_cross_the_cap_is_refused(self):
        ledger = SpendLedger(self.dir)
        ledger.set_budget(5.0)
        ledger.reserve(4.0)
        with self.assertRaises(BudgetExhausted) as ctx:
            ledger.reserve(2.0)
        self.assertEqual(ctx.exception.remaining, 1.0)
        # Refused means nothing was held — the budget is unchanged, not partially consumed.
        self.assertEqual(ledger.snapshot()["reserved"], 4.0)

    def test_release_returns_the_hold(self):
        ledger = SpendLedger(self.dir)
        ledger.set_budget(5.0)
        rid = ledger.reserve(5.0)
        with self.assertRaises(BudgetExhausted):
            ledger.reserve(0.01)
        ledger.release(rid)
        ledger.reserve(5.0)  # the full budget is available again
        self.assertEqual(ledger.snapshot()["reserved"], 5.0)

    def test_settle_never_fails_on_the_cap_even_when_it_overruns(self):
        # The spend already happened; the ledger must record it, not reject it. The overrun is
        # absorbed by the next reservation having less (here, no) room.
        ledger = SpendLedger(self.dir)
        ledger.set_budget(5.0)
        rid = ledger.reserve(4.0)
        ledger.settle(rid, 6.0)  # actual blew past the estimate and the cap
        snap = ledger.snapshot()
        self.assertEqual(snap["spent"], 6.0)
        self.assertEqual(snap["remaining"], 0.0)  # clamped at zero, never negative/"owed"
        with self.assertRaises(BudgetExhausted):
            ledger.reserve(0.01)

    def test_settling_an_unknown_or_already_settled_reservation_is_an_error(self):
        ledger = SpendLedger(self.dir)
        ledger.set_budget(5.0)
        rid = ledger.reserve(1.0)
        ledger.settle(rid, 1.0)
        with self.assertRaises(KeyError):
            ledger.settle(rid, 1.0)       # already settled
        with self.assertRaises(KeyError):
            ledger.settle("r_nope", 1.0)  # never existed


class SpendConcurrencyTest(unittest.TestCase):
    N_THREADS = 20
    CAP = 10.0
    SLICE = 1.0  # exactly CAP/SLICE = 10 reservations should fit; the other 10 must be refused

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "eng"

    def tearDown(self):
        self._tmp.cleanup()

    def test_naive_check_then_spend_overshoots(self):
        """Break-it-first: SELECT the committed total, decide in Python, then INSERT in a separate
        step — the §14.1 D race. Lined up on a barrier, more than CAP/SLICE reservations get
        written, so the committed total exceeds the cap. If this ever stops overshooting, the
        no-overshoot test below proves nothing."""
        path = self.dir
        path.mkdir(parents=True)
        db = path / "naive.db"
        c0 = sqlite3.connect(db)
        c0.execute("CREATE TABLE r (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL)")
        c0.commit()
        c0.close()

        barrier = threading.Barrier(self.N_THREADS)

        def naive_worker():
            conn = sqlite3.connect(db, timeout=30, isolation_level=None)  # autocommit
            barrier.wait()  # every thread checks the total at the same instant
            committed = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM r").fetchone()[0]
            if committed + self.SLICE <= self.CAP:          # "is there budget left?"
                conn.execute("INSERT INTO r (amount) VALUES (?)", (self.SLICE,))  # ...then spend
            conn.close()

        threads = [threading.Thread(target=naive_worker) for _ in range(self.N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = sqlite3.connect(db).execute("SELECT COALESCE(SUM(amount), 0) FROM r").fetchone()[0]
        self.assertGreater(
            total, self.CAP,
            "the check-then-spend race did not overshoot — the harness is inert, so the "
            "no-overshoot guarantee below would prove nothing",
        )

    def test_concurrent_reserve_never_overshoots(self):
        ledger = SpendLedger(self.dir)
        ledger.set_budget(self.CAP)
        barrier = threading.Barrier(self.N_THREADS)
        granted = []
        refused = []
        lock = threading.Lock()

        def worker():
            barrier.wait()  # same lined-up contention the naive test overshoots under
            try:
                rid = ledger.reserve(self.SLICE)
                with lock:
                    granted.append(rid)
            except BudgetExhausted:
                with lock:
                    refused.append(1)

        threads = [threading.Thread(target=worker) for _ in range(self.N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly CAP/SLICE reservations fit; the rest are refused. Never more than the cap.
        self.assertEqual(len(granted), int(self.CAP / self.SLICE))
        self.assertEqual(len(refused), self.N_THREADS - int(self.CAP / self.SLICE))
        self.assertLessEqual(ledger.snapshot()["reserved"], self.CAP)
        self.assertEqual(ledger.snapshot()["remaining"], 0.0)


if __name__ == "__main__":
    unittest.main()

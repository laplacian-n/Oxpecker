"""Experiment claiming (§2.5 wave dispatch / §14.1 E′). A worker claims (hypothesis, method); at
most one worker may hold an active claim on the same experiment at once.

The owner's A2 condition, stated plainly: do not believe "it handles concurrency" until a test
fires many threads at cold state and COUNTS the winners, not just checks a value. So the
load-bearing pair is `test_the_naive_claim_double_claims` (a check-then-insert without the unique
index really lets two workers claim the same experiment under a barrier — proving the race is real
and this harness can surface it) and `test_concurrent_claims_resolve_to_exactly_one` (the real
claim never does).
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from agent.hypothesis_graph.store import HypothesisGraphStore


def _valid_hypothesis(store: HypothesisGraphStore) -> str:
    return store.create_hypothesis(
        title="SQLi on /login", claim="the login form is injectable", phase_created="ANALYSIS",
        rationale="error-based signal", origin_type="tool_observation", impact=4,
        confidence_band="medium", confidence_reason="one reflected error",
    )


class ExperimentClaimTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = HypothesisGraphStore(Path(self._tmp.name) / "eng")
        self.hid = _valid_hypothesis(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_claim_then_second_claim_of_the_same_experiment_is_refused(self):
        self.assertIsNotNone(self.store.claim_experiment(self.hid, "error-based", "w1"))
        self.assertIsNone(self.store.claim_experiment(self.hid, "error-based", "w2"))

    def test_different_methods_on_one_hypothesis_all_claim(self):
        # §2.5: three workers, one hypothesis, three methods — all succeed.
        ids = [self.store.claim_experiment(self.hid, m, f"w{i}")
               for i, m in enumerate(["error-based", "boolean-blind", "time-based"])]
        self.assertTrue(all(ids))
        self.assertEqual(len(self.store.active_claims(self.hid)), 3)

    def test_release_frees_the_experiment_for_a_retry(self):
        c1 = self.store.claim_experiment(self.hid, "error-based", "w1")
        self.assertIsNone(self.store.claim_experiment(self.hid, "error-based", "w2"))  # still held
        self.assertTrue(self.store.release_claim(c1))
        # now re-claimable, as a distinct claim
        c2 = self.store.claim_experiment(self.hid, "error-based", "w3")
        self.assertIsNotNone(c2)
        self.assertNotEqual(c1, c2)
        self.assertEqual([c["claim_id"] for c in self.store.active_claims(self.hid)], [c2])

    def test_completing_an_already_released_claim_is_a_no_op(self):
        c1 = self.store.claim_experiment(self.hid, "error-based", "w1")
        self.assertTrue(self.store.release_claim(c1))
        self.assertFalse(self.store.complete_claim(c1))  # not active anymore

    def test_claiming_against_a_missing_hypothesis_is_not_found(self):
        from agent.hypothesis_graph.store import NotFoundError
        with self.assertRaises(NotFoundError):
            self.store.claim_experiment("h_nope", "error-based", "w1")


class ClaimConcurrencyTest(unittest.TestCase):
    N_THREADS = 20

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "eng"
        self.store = HypothesisGraphStore(self.dir)
        self.hid = _valid_hypothesis(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_naive_claim_double_claims(self):
        """Break-it-first: 'SELECT whether an active claim exists, else INSERT' — without the
        partial unique index — lets two threads lined up on a barrier both see none and both
        insert, so the same experiment is claimed twice. If this ever stops double-claiming, the
        no-double-claim test below is inert."""
        path = self.dir / "naive.db"
        c0 = sqlite3.connect(path)
        c0.execute("CREATE TABLE naive_claims (id INTEGER PRIMARY KEY AUTOINCREMENT, h TEXT, method TEXT)")
        c0.commit()
        c0.close()
        barrier = threading.Barrier(self.N_THREADS)

        def naive_worker():
            conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit
            barrier.wait()
            existing = conn.execute(
                "SELECT COUNT(*) FROM naive_claims WHERE h=? AND method=?", (self.hid, "error-based")
            ).fetchone()[0]
            if existing == 0:
                conn.execute("INSERT INTO naive_claims (h, method) VALUES (?, ?)", (self.hid, "error-based"))
            conn.close()

        threads = [threading.Thread(target=naive_worker) for _ in range(self.N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = sqlite3.connect(path).execute(
            "SELECT COUNT(*) FROM naive_claims WHERE h=? AND method=?", (self.hid, "error-based")
        ).fetchone()[0]
        self.assertGreater(total, 1, "the naive claim did not double-claim — the harness is inert")

    def test_concurrent_claims_resolve_to_exactly_one(self):
        barrier = threading.Barrier(self.N_THREADS)
        granted = []
        lock = threading.Lock()

        def worker(i):
            barrier.wait()  # same lined-up contention the naive test double-claims under
            cid = self.store.claim_experiment(self.hid, "error-based", f"w{i}")
            if cid is not None:
                with lock:
                    granted.append(cid)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(self.N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(granted), 1, f"expected exactly one winner, got {len(granted)}")
        self.assertEqual(len(self.store.active_claims(self.hid)), 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for the Phase 6 bounded deterministic parallel executor. Includes a real concurrency
check (peak simultaneous execution measured via a counter+lock, not just "it returned the right
answers") — this project has already found three vacuous-pass bugs this session in tests that
looked like they verified something but didn't; a parallel executor's most important property
(it doesn't exceed max_workers, and it DOES actually run concurrently rather than serially) is
exactly the kind of thing that's easy to accidentally not test for real.
"""
from __future__ import annotations

import threading
import time
import unittest

from .parallel import TaskOutcome, run_bounded


class TestRunBounded(unittest.TestCase):
    def test_invalid_max_workers_rejected(self):
        with self.assertRaises(ValueError):
            run_bounded([lambda: 1], max_workers=0)

    def test_results_returned_in_input_order_not_completion_order(self):
        # task 0 finishes last, task 2 finishes first — results must still come back as [0,1,2]
        def slow():
            time.sleep(0.15)
            return "slow"

        def fast():
            return "fast"

        outcomes = run_bounded([slow, fast, fast], max_workers=3)
        self.assertEqual([o.index for o in outcomes], [0, 1, 2])
        self.assertEqual(outcomes[0].result, "slow")
        self.assertEqual(outcomes[1].result, "fast")

    def test_one_failure_does_not_lose_other_results(self):
        def ok():
            return 42

        def boom():
            raise RuntimeError("task failed")

        outcomes = run_bounded([ok, boom, ok], max_workers=3)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(outcomes[0].result, 42)
        self.assertFalse(outcomes[1].ok)
        self.assertIn("task failed", outcomes[1].error)
        self.assertTrue(outcomes[2].ok)

    def test_concurrency_is_actually_bounded_and_actually_parallel(self):
        """Real measurement: launch more tasks than max_workers, each holding a slot for a
        fixed duration, and track peak simultaneous holders via a lock-protected counter. This
        both proves parallelism happens (peak > 1) and that it's capped (peak <= max_workers)."""
        max_workers = 3
        lock = threading.Lock()
        state = {"current": 0, "peak": 0}

        def task():
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.1)
            with lock:
                state["current"] -= 1
            return "done"

        outcomes = run_bounded([task] * 9, max_workers=max_workers)
        self.assertTrue(all(o.ok for o in outcomes))
        self.assertGreater(state["peak"], 1, "executor never actually ran tasks concurrently")
        self.assertLessEqual(state["peak"], max_workers, "executor exceeded max_workers")
        self.assertEqual(state["peak"], max_workers, "executor under-used the available workers")


if __name__ == "__main__":
    unittest.main()

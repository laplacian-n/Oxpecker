"""agent/engagement/locks.py — the per-engagement RLock that serializes AgentLoop.run_task()/
AutonomousDriver.run() calls against the same engagement (see that module's own docstring for
the duplicate-hypothesis bug this closes). Covers both halves of the correctness requirement:
different threads must actually serialize, and the same thread must be able to re-enter (the
exact AutonomousDriver -> AgentLoop.run_task() nesting pattern) without deadlocking.
"""
from __future__ import annotations

import threading
import time
import unittest

from .locks import engagement_lock


class TestEngagementLock(unittest.TestCase):
    def test_same_engagement_id_returns_the_same_lock_object(self):
        self.assertIs(engagement_lock("eng-a"), engagement_lock("eng-a"))

    def test_different_engagement_ids_get_different_locks(self):
        self.assertIsNot(engagement_lock("eng-a"), engagement_lock("eng-b"))

    def test_reentrant_same_thread_does_not_block(self):
        lock = engagement_lock("eng-reentrant")
        with lock:
            # A plain threading.Lock would deadlock here; RLock must not.
            acquired = lock.acquire(timeout=2)
            try:
                self.assertTrue(acquired, "same-thread re-acquire blocked — must be an RLock")
            finally:
                if acquired:
                    lock.release()

    def test_different_threads_serialize_never_overlap(self):
        lock = engagement_lock("eng-serialize")
        concurrent_holders = []
        max_concurrent = []

        def worker():
            with lock:
                concurrent_holders.append(1)
                max_concurrent.append(len(concurrent_holders))
                time.sleep(0.05)  # hold long enough that a real race would overlap
                concurrent_holders.pop()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads), "a worker never finished")
        self.assertEqual(max(max_concurrent), 1, "two threads held the same engagement's lock at once")

    def test_registry_lookup_itself_is_thread_safe(self):
        # Many threads asking for the SAME new engagement_id for the first time simultaneously
        # must all get back the identical lock object, not a mix of racily-created duplicates.
        results = []

        def worker():
            results.append(engagement_lock("eng-first-touch"))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(set(id(r) for r in results)), 1)


if __name__ == "__main__":
    unittest.main()

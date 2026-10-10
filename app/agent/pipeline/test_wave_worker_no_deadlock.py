"""Regression: a wave worker must not deadlock against the driver's engagement lock.

AutonomousDriver.run() holds the per-engagement RLock (locks.py) for the whole run and then
dispatches wave workers, each on its own thread (wave.py starts one Thread per worker). The RLock
is re-entrant only on the thread that holds it, so a worker that re-takes the same lock from a
different thread blocks forever on the lock the driver thread is holding. That self-deadlock made
every autonomous recon wave hang until the wall-clock abandoned it, so recon produced no
hypotheses and every report came back empty. The fix: wave workers run with
`serialize_engagement=False` — the driver's single lock already serializes the engagement.

These tests exercise AgentLoop.run_task's lock gating directly (constructed via __new__ so the
heavy model/tool stack is not needed), with the lock held on another thread exactly as it is in a
live run.
"""
from __future__ import annotations

import threading
import unittest

from ..engagement.locks import engagement_lock
from ..loop import AgentLoop


def _loop_with(serialize: bool) -> AgentLoop:
    # Only the attributes run_task reads, without the real (heavy) constructor.
    loop = AgentLoop.__new__(AgentLoop)
    loop.serialize_engagement = serialize
    loop.hypothesis_graph = object()  # non-None: the lock branch would engage
    loop.security_mcp_client = None
    loop.engagement_id = "deadlock-regression"
    loop._run_task_inner = lambda user_input: "ran"  # type: ignore[attr-defined]
    return loop


class WaveWorkerNoDeadlockTest(unittest.TestCase):
    def test_worker_runs_while_driver_holds_the_lock(self):
        """serialize_engagement=False: the worker completes even though another thread (the
        driver) holds the engagement lock — the exact live configuration."""
        done = threading.Event()
        result: dict[str, str] = {}
        with engagement_lock("deadlock-regression"):  # the driver thread
            def worker():
                result["r"] = _loop_with(serialize=False).run_task("brief")
                done.set()

            threading.Thread(target=worker, daemon=True).start()
            self.assertTrue(done.wait(timeout=5),
                            "wave worker deadlocked on the engagement lock held by another thread")
        self.assertEqual(result["r"], "ran")

    def test_a_real_session_still_serializes(self):
        """serialize_engagement=True (a standalone session): it DOES block while another thread
        holds the lock, and proceeds once released — the serialization the lock exists for is
        intact for genuinely competing sessions."""
        done = threading.Event()
        lock = engagement_lock("deadlock-regression")
        lock.acquire()  # a different run holds it
        try:
            def session():
                _loop_with(serialize=True).run_task("brief")
                done.set()

            threading.Thread(target=session, daemon=True).start()
            self.assertFalse(done.wait(timeout=1),
                             "a serializing session should block while the lock is held")
        finally:
            lock.release()
        self.assertTrue(done.wait(timeout=5),
                        "the session should proceed once the lock is released")


if __name__ == "__main__":
    unittest.main()

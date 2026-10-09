"""Tests for the engagement event log.

The load-bearing one is `test_the_naive_race_really_collides` paired with
`test_concurrent_append_is_gapless_and_unique`. `CLIENT_UI_DESIGN.md` §4.1.1 calls the sequence
number "the cooldown race again", and this project's own habit (handoff §6) is that a concurrency
test proves nothing unless the race it names can actually fire in this environment. So the first
test runs the *wrong* (check-then-act) allocation under threads and asserts it collides — if that
test ever passes silently, the second test's 'no collision' guarantees nothing, because the
threads were never really contending. Break it first, watch it fail, then trust the fix.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from agent.engagement.event_log import EngagementEventLog, project


class EventLogBasicsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "eng"

    def tearDown(self):
        self._tmp.cleanup()

    def test_open_if_exists_does_not_create_the_db_on_a_read(self):
        self.assertIsNone(EngagementEventLog.open_if_exists(self.dir))
        self.assertFalse((self.dir / "events.db").exists())
        # ...but constructing one does, and then open_if_exists finds it.
        EngagementEventLog(self.dir).append("note_added", {})
        self.assertTrue((self.dir / "events.db").exists())
        self.assertIsNotNone(EngagementEventLog.open_if_exists(self.dir))

    def test_append_returns_strictly_increasing_contiguous_numbers(self):
        log = EngagementEventLog(self.dir)
        seqs = [log.append("note_added", {"i": i}) for i in range(5)]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])
        self.assertEqual(log.latest_seq(), 5)

    def test_read_since_is_ordered_and_exclusive_of_its_cursor(self):
        log = EngagementEventLog(self.dir)
        for i in range(5):
            log.append("note_added", {"i": i})
        got = log.read_since(2)
        self.assertEqual([e["seq"] for e in got], [3, 4, 5])
        self.assertEqual([e["payload"]["i"] for e in got], [2, 3, 4])
        self.assertEqual(log.read_since(5), [])  # nothing past the end
        self.assertEqual([e["seq"] for e in log.read_since(0)], [1, 2, 3, 4, 5])

    def test_latest_seq_of_an_empty_log_is_zero(self):
        self.assertEqual(EngagementEventLog(self.dir).latest_seq(), 0)

    def test_empty_kind_is_rejected(self):
        log = EngagementEventLog(self.dir)
        with self.assertRaises(ValueError):
            log.append("", {})


class SnapshotIsAReplayTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = EngagementEventLog(Path(self._tmp.name) / "eng")

    def tearDown(self):
        self._tmp.cleanup()

    def test_snapshot_equals_the_projection_of_the_replayed_log(self):
        # The scrubber (§6.5) must be the same projection run to an earlier number, not a second
        # data path — so snapshot(at=N) has to equal project(read_since(0)) truncated at N.
        for i in range(10):
            self.log.append("note_added", {"i": i})
        for at in (0, 1, 5, 10, 999):
            with self.subTest(at=at):
                replay = [e for e in self.log.read_since(0) if e["seq"] <= at]
                bounded = min(at, self.log.latest_seq())
                self.assertEqual(
                    self.log.snapshot(at_seq=at),
                    project(replay, at_seq=bounded, latest_seq=self.log.latest_seq()),
                )

    def test_at_is_clamped_to_latest_so_a_future_number_is_not_an_error(self):
        self.log.append("note_added", {})
        self.assertEqual(self.log.snapshot(at_seq=100)["at_seq"], 1)

    def test_projection_folds_the_typed_kinds(self):
        self.log.append("wave_started", {"wave": 1, "experiments": ["a", "b"]})
        self.log.append("worker_spawned", {"worker_id": "w1", "hypothesis": "H-1", "method": "m"})
        self.log.append("tool_call_started", {"call_id": "c1", "worker": "w1", "tool": "http"})
        self.log.append("tool_call_started", {"call_id": "c2", "worker": "w1", "tool": "nmap"})
        self.log.append("tool_call_finished", {"call_id": "c1"})
        self.log.append("model_call", {"model_id": "gpt", "role": "worker",
                                        "prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01})
        self.log.append("model_call", {"model_id": "gpt", "prompt_tokens": 20,
                                        "completion_tokens": 7, "cost": 0.02})
        self.log.append("artifact_stored", {"worker": "w1", "artifact": "resp", "store": "evidence"})
        self.log.append("finding_recorded", {"ordinal": 1})
        self.log.append("budget_updated", {"spent": 3.0, "reserved": 1.0, "remaining": 6.0})
        snap = self.log.snapshot()

        self.assertEqual(snap["waves"][0]["status"], "running")
        self.assertEqual(snap["workers"][0]["worker_id"], "w1")
        # c1 finished, so only c2 remains in-flight — the "running now" set the rail draws.
        self.assertEqual([c["call_id"] for c in snap["tool_calls_in_flight"]], ["c2"])
        roster = snap["model_roster"][0]
        self.assertEqual((roster["calls"], roster["prompt_tokens"], roster["completion_tokens"]), (2, 30, 12))
        self.assertAlmostEqual(roster["cost"], 0.03)
        self.assertEqual(snap["counts"]["finding_recorded"], 1)
        self.assertEqual(len(snap["artifacts"]), 1)
        self.assertEqual(snap["budget"]["remaining"], 6.0)

    def test_an_unknown_kind_is_kept_not_dropped(self):
        # A producer emitting a kind the projection has not learned yet still rides the stream
        # and the resume count, rather than silently vanishing.
        self.log.append("phase_entered", {"phase": "RECON"})
        snap = self.log.snapshot()
        self.assertEqual(len(snap["unknown_events"]), 1)
        self.assertEqual(snap["unknown_events"][0]["kind"], "phase_entered")


class ConcurrencyTest(unittest.TestCase):
    N_THREADS = 20
    ROUNDS = 25

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "eng"

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_naive_race_really_collides(self):
        """Break-it-first: the check-then-act allocation (SELECT MAX, then a separate INSERT) is
        exactly what §4.1.1 forbids, and under threads lined up on a barrier it must collide — a
        duplicate sequence number, caught here as the PRIMARY KEY IntegrityError the real append
        makes impossible. If this ever stops colliding, the no-collision test below is inert."""
        db = self.dir
        db.mkdir(parents=True)
        path = db / "naive.db"
        conn0 = sqlite3.connect(path)
        conn0.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT)")
        conn0.commit()
        conn0.close()

        barrier = threading.Barrier(self.N_THREADS)
        collisions = [0]
        lock = threading.Lock()

        def naive_worker():
            conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit
            for _ in range(self.ROUNDS):
                barrier.wait()  # every thread reads MAX at the same instant
                nxt = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()[0]
                try:
                    conn.execute("INSERT INTO events (seq, kind) VALUES (?, 'x')", (nxt,))
                except sqlite3.IntegrityError:
                    with lock:
                        collisions[0] += 1
            conn.close()

        threads = [threading.Thread(target=naive_worker) for _ in range(self.N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertGreater(
            collisions[0], 0,
            "the check-then-act race did not fire — the concurrency harness is inert, so the "
            "no-collision guarantee below would prove nothing",
        )

    def test_concurrent_append_is_gapless_and_unique(self):
        log = EngagementEventLog(self.dir)
        barrier = threading.Barrier(self.N_THREADS)
        got: list[int] = []
        lock = threading.Lock()

        def worker():
            local = []
            for _ in range(self.ROUNDS):
                barrier.wait()  # same lined-up contention the naive test collides under
                local.append(log.append("note_added", {}))
            with lock:
                got.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(self.N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = self.N_THREADS * self.ROUNDS
        self.assertEqual(len(got), total)
        self.assertEqual(len(set(got)), total, "a sequence number was handed out twice")
        self.assertEqual(sorted(got), list(range(1, total + 1)), "the sequence is not gapless")
        self.assertEqual(log.latest_seq(), total)


if __name__ == "__main__":
    unittest.main()

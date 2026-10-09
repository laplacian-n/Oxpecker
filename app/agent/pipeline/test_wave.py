"""The wave orchestrator (§2.5). Tests the deterministic half — claiming, the barrier, the
wall-clock hard stop with a recorded partial, and the §4.2 event sequence — with an injected
worker runner and an injected emit, so no live model is needed. The owner's A6 is pinned
directly: a worker over its wall-clock is recorded as a timeout, never dropped.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.hypothesis_graph.store import HypothesisGraphStore
from agent.pipeline.wave import WaveOrchestrator, WaveConfig, WorkerResult


def _hypothesis(store: HypothesisGraphStore, title: str) -> str:
    return store.create_hypothesis(
        title=title, claim=f"{title} claim", phase_created="ANALYSIS", rationale="r",
        origin_type="tool_observation", impact=3, confidence_band="medium", confidence_reason="x",
    )


class WaveTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = HypothesisGraphStore(Path(self._tmp.name) / "eng")
        self.events: list[tuple[str, dict]] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _emit(self, engagement_id, kind, payload=None):
        self.events.append((kind, payload or {}))

    def _orch(self, runner, config=None):
        return WaveOrchestrator("eng1", self.store, runner, config=config, emit=self._emit)

    def _kinds(self):
        return [k for k, _ in self.events]


class NormalWaveTest(WaveTestBase):
    def test_a_wave_claims_spawns_runs_and_emits_the_lifecycle(self):
        h1, h2 = _hypothesis(self.store, "H1"), _hypothesis(self.store, "H2")
        runner = lambda hid, method, stop, deadline: WorkerResult(outcome="completed", verdict="refuted")
        results = self._orch(runner).run_wave(1, [(h1, "error-based"), (h2, "boolean-blind")])

        self.assertEqual([r.outcome for r in results], ["completed", "completed"])
        kinds = self._kinds()
        self.assertEqual(kinds[0], "wave_started")
        self.assertEqual(kinds[-1], "wave_ended")
        self.assertEqual(kinds.count("worker_spawned"), 2)
        self.assertEqual(kinds.count("experiment_started"), 2)
        self.assertEqual(kinds.count("worker_finished"), 2)
        self.assertEqual(kinds.count("experiment_ended"), 2)
        # claims were completed, so the slots are free for a retry (none active now).
        self.assertEqual(self.store.active_claims(), [])
        # experiment_ended carries the worker's verdict.
        ended = [p for k, p in self.events if k == "experiment_ended"]
        self.assertTrue(all(p["verdict"] == "refuted" for p in ended))

    def test_the_wave_is_capped_at_max_workers(self):
        hs = [_hypothesis(self.store, f"H{i}") for i in range(7)]
        runner = lambda hid, method, stop, deadline: WorkerResult(outcome="completed")
        orch = self._orch(runner, WaveConfig(max_workers=5))
        orch.run_wave(1, [(h, "m") for h in hs])
        self.assertEqual(self._kinds().count("worker_spawned"), 5)

    def test_an_already_claimed_experiment_is_skipped(self):
        h1 = _hypothesis(self.store, "H1")
        self.store.claim_experiment(h1, "error-based", "other-worker")  # held by someone else
        runner = lambda hid, method, stop, deadline: WorkerResult(outcome="completed")
        self._orch(runner).run_wave(1, [(h1, "error-based")])
        # nothing was dispatched for the already-claimed experiment.
        self.assertEqual(self._kinds().count("worker_spawned"), 0)
        wave_started = next(p for k, p in self.events if k == "wave_started")
        self.assertEqual(wave_started["experiments"], [])

    def test_a_worker_crash_is_recorded_as_an_error_not_a_wave_crash(self):
        h1 = _hypothesis(self.store, "H1")
        def boom(hid, method, stop, deadline):
            raise RuntimeError("worker fell over")
        results = self._orch(boom).run_wave(1, [(h1, "m")])
        self.assertEqual(results[0].outcome, "error")
        self.assertIn("worker fell over", results[0].detail)
        self.assertEqual(self._kinds()[-1], "wave_ended")  # the wave still finished cleanly


class WallClockTest(WaveTestBase):
    def test_a_worker_over_its_wall_clock_is_abandoned_and_recorded_as_timeout(self):
        # A6: a stubborn worker that ignores the stop signal must be recorded as a timeout with a
        # reason, and the wave must not hang on it past the limit.
        h1 = _hypothesis(self.store, "H1")

        def stubborn(hid, method, stop, deadline):
            time.sleep(5)  # ignores `stop` entirely
            return WorkerResult(outcome="completed")

        orch = self._orch(stubborn, WaveConfig(worker_wall_clock_s=0.2, join_grace_s=0.1))
        start = time.monotonic()
        results = orch.run_wave(1, [(h1, "m")])
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2.0, "the wave hung on a stuck worker past its wall-clock")
        self.assertEqual(results[0].outcome, "timeout")
        self.assertIn("wall-clock", results[0].detail)
        finished = next(p for k, p in self.events if k == "worker_finished")
        self.assertEqual(finished["outcome"], "timeout")  # recorded, not dropped
        self.assertEqual(self.store.active_claims(), [])  # claim completed even on timeout

    def test_a_cooperative_worker_stops_on_the_signal_and_returns_its_partial(self):
        h1 = _hypothesis(self.store, "H1")

        def cooperative(hid, method, stop, deadline):
            for _ in range(500):
                if stop.wait(timeout=0.02):
                    return WorkerResult(outcome="timeout", verdict="inconclusive",
                                        detail="stopped on signal with partial evidence")
            return WorkerResult(outcome="completed")

        orch = self._orch(cooperative, WaveConfig(worker_wall_clock_s=0.2, join_grace_s=1.0))
        results = orch.run_wave(1, [(h1, "m")])
        self.assertEqual(results[0].outcome, "timeout")
        self.assertEqual(results[0].verdict, "inconclusive")  # the worker's own partial, not synthesized


if __name__ == "__main__":
    unittest.main()

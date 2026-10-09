"""The high-tier wave loop (§2.5). The stop logic — run until the strategist dispatches nothing,
or the wave cap — is pinned with injected strategist/orchestrator; a gated test runs one real
wave (real strategist choosing from a real graph, no-op workers) to prove the wiring end to end
without a costly full worker run.
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from agent.pipeline.wave_driver import WaveDriver

_HAS_KEY = (Path(__file__).resolve().parents[1] / "state" / "openrouter_api_key.txt").exists() \
    or bool(os.environ.get("OPENROUTER_API_KEY"))


class FakeStrategist:
    """Returns each queued dispatch in turn, then [] forever."""
    def __init__(self, dispatches):
        self._queue = list(dispatches)

    def decide(self):
        return self._queue.pop(0) if self._queue else []


class RecordingOrchestrator:
    def __init__(self):
        self.waves = []

    def run_wave(self, wave_no, dispatch):
        self.waves.append((wave_no, dispatch))


class WaveDriverTest(unittest.TestCase):
    def test_runs_waves_until_the_strategist_dispatches_nothing(self):
        strat = FakeStrategist([[("h1", "m1")], [("h2", "m2")]])  # two waves, then []
        orch = RecordingOrchestrator()
        summary = WaveDriver(strat, orch).run()
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["waves"], 2)
        self.assertEqual([w[0] for w in orch.waves], [1, 2])  # wave numbers increment

    def test_an_empty_first_dispatch_runs_no_waves(self):
        orch = RecordingOrchestrator()
        summary = WaveDriver(FakeStrategist([]), orch).run()
        self.assertEqual((summary["status"], summary["waves"]), ("complete", 0))
        self.assertEqual(orch.waves, [])

    def test_the_wave_cap_stops_a_strategist_that_never_concludes(self):
        # A strategist that always dispatches (workers never conclude the hypothesis) must be
        # bounded by the cap rather than looping forever.
        never_ends = FakeStrategist([])
        never_ends.decide = lambda: [("h1", "m1")]
        orch = RecordingOrchestrator()
        summary = WaveDriver(never_ends, orch, max_waves=3).run()
        self.assertEqual((summary["status"], summary["waves"]), ("wave_cap", 3))
        self.assertEqual(len(orch.waves), 3)


@unittest.skipUnless(_HAS_KEY, "no OpenRouter key — skipping the live wave loop")
class LiveWaveDriverTest(unittest.TestCase):
    def test_one_real_wave_with_the_real_strategist_and_no_op_workers(self):
        # Real strategist choosing from a real graph, driving the real orchestrator, with no-op
        # workers (so no costly agent runs) — proves strategist -> driver -> orchestrator ->
        # claim -> worker wiring on the live model in one cheap call. Capped at one wave.
        from agent.hypothesis_graph.store import HypothesisGraphStore
        from agent.llm.openrouter import OpenRouterProvider
        from agent.pipeline.strategist import Strategist
        from agent.pipeline.wave import WaveOrchestrator, WorkerResult

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = HypothesisGraphStore(Path(tmp.name) / "eng")
        store.create_hypothesis(title="SQLi on /login", claim="login is injectable",
                                phase_created="ANALYSIS", rationale="r", origin_type="tool_observation",
                                impact=4, confidence_band="medium", confidence_reason="x", surface="/login")

        strat = Strategist(store, OpenRouterProvider(model="openai/gpt-4o-mini"), max_experiments=2)
        noop = lambda hid, method, stop, deadline: WorkerResult(outcome="completed", verdict="refuted")
        orch = WaveOrchestrator("eng-live-wave", store, noop,
                                emit=lambda *a, **k: None)  # swallow events; wiring is the point
        summary = WaveDriver(strat, orch, max_waves=1).run()
        self.assertEqual(summary["waves"], 1)
        self.assertEqual(summary["status"], "wave_cap")  # capped at one; the hypothesis stays open


if __name__ == "__main__":
    unittest.main()

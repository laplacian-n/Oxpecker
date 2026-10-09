"""Tier -> engine (§2.6). The classification (tier_engine_kind) is pinned against roe.json and the
default; the high-tier assembly (build_wave_engine) is proven to tie a real store + strategist +
orchestrator together so the strategist's pick reaches the orchestrator through claim — with an
injected provider and a recording worker, so no model or costly agent run. A gated test builds the
engine for a real engagement and runs one real wave on the live model with a no-op worker.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from agent.hypothesis_graph.store import HypothesisGraphStore
from agent.pipeline import engine
from agent.pipeline.wave import WaveConfig, WorkerResult

_HAS_KEY = (Path(__file__).resolve().parents[1] / "state" / "openrouter_api_key.txt").exists() \
    or bool(os.environ.get("OPENROUTER_API_KEY"))


def _write_roe(root: Path, engagement_id: str, tier):
    eng = root / engagement_id
    eng.mkdir(parents=True, exist_ok=True)
    roe = {} if tier is None else {"tier": tier}
    (eng / "roe.json").write_text(json.dumps(roe))


class TierEngineKindTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_each_tier_maps_to_its_engine_kind(self):
        _write_roe(self.root, "low-eng", "low")
        _write_roe(self.root, "med-eng", "medium")
        _write_roe(self.root, "high-eng", "high")
        kind = lambda e: engine.tier_engine_kind(e, engagements_root=self.root)
        self.assertEqual(kind("low-eng"), engine.ENGINE_OPERATOR)
        self.assertEqual(kind("med-eng"), engine.ENGINE_SEQUENTIAL)
        self.assertEqual(kind("high-eng"), engine.ENGINE_WAVE)

    def test_no_roe_and_no_tier_fall_back_to_the_default_tiers_engine(self):
        # An engagement that never set a tier runs the default tier's engine — adding the selector
        # did not change how a tier-less engagement behaves. DEFAULT_TIER is 'high' -> wave.
        from agent import tiers

        _write_roe(self.root, "no-tier", None)  # roe.json present but no "tier" key
        default_kind = engine._KIND_BY_TIER[tiers.DEFAULT_TIER]
        self.assertEqual(engine.tier_engine_kind("no-tier", engagements_root=self.root), default_kind)
        self.assertEqual(engine.tier_engine_kind("absent-eng", engagements_root=self.root), default_kind)


class _RecordingWorker:
    """A WorkerRunner that records every (hypothesis_id, method) it is dispatched, thread-safely
    (the orchestrator runs workers on threads), and concludes each experiment."""
    def __init__(self):
        self._lock = threading.Lock()
        self.dispatched: list[tuple[str, str]] = []

    def __call__(self, hypothesis_id, method, stop, deadline) -> WorkerResult:
        with self._lock:
            self.dispatched.append((hypothesis_id, method))
        return WorkerResult(outcome="completed", detail="recorded")


class _FakeProvider:
    """Returns a dispatch naming the given ordinal; records that it was called."""
    def __init__(self, ordinal, method="probe"):
        self._ordinal = ordinal
        self._method = method
        self.calls = 0

    def chat(self, messages, *, max_tokens, temperature):
        self.calls += 1
        content = f'[{{"hypothesis": {self._ordinal}, "method": "{self._method}"}}]'
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class BuildWaveEngineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.eng = "eng-assembly"
        self.store = HypothesisGraphStore(self.root / self.eng)
        self.hid = self.store.create_hypothesis(
            title="SQLi on /login", claim="login is injectable", phase_created="ANALYSIS",
            rationale="r", origin_type="tool_observation", impact=4, confidence_band="medium",
            confidence_reason="x", surface="/login",
        )
        self.ordinal = self.store.get_hypothesis(self.hid)["ordinal"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_strategists_pick_reaches_the_orchestrator_through_claim(self):
        # The whole point of the assembly: an injected provider chooses the hypothesis, and the
        # worker the engine built is dispatched exactly that (hid, method) — proving strategist ->
        # driver -> orchestrator -> claim -> worker is wired from a bare engagement_id.
        worker = _RecordingWorker()
        provider = _FakeProvider(self.ordinal, method="error-based")
        driver = engine.build_wave_engine(
            self.eng, provider=provider, worker_runner=worker, emit=lambda *a, **k: None,
            engagements_root=self.root, max_waves=1,
        )
        summary = driver.run()
        self.assertEqual(summary["waves"], 1)
        self.assertEqual(provider.calls, 1)               # the strategist ran once (one wave)
        self.assertEqual(worker.dispatched, [(self.hid, "error-based")])  # its pick, claimed & run

    def test_a_graph_with_no_open_hypotheses_runs_no_wave_and_never_calls_the_model(self):
        # Park the only hypothesis: no candidates -> the strategist dispatches nothing -> the
        # driver completes without a wave and without a model call.
        h = self.store.get_hypothesis(self.hid)
        self.store.set_lifecycle_status(self.hid, h["version"], "parked", reason="done")
        worker = _RecordingWorker()
        provider = _FakeProvider(self.ordinal)
        driver = engine.build_wave_engine(
            self.eng, provider=provider, worker_runner=worker, emit=lambda *a, **k: None,
            engagements_root=self.root, max_waves=3,
        )
        summary = driver.run()
        self.assertEqual((summary["status"], summary["waves"]), ("complete", 0))
        self.assertEqual(provider.calls, 0)
        self.assertEqual(worker.dispatched, [])

    def test_wave_config_and_caps_are_threaded_through(self):
        worker = _RecordingWorker()
        provider = _FakeProvider(self.ordinal)
        cfg = WaveConfig(max_workers=2)
        driver = engine.build_wave_engine(
            self.eng, provider=provider, worker_runner=worker, emit=lambda *a, **k: None,
            engagements_root=self.root, max_waves=7, max_experiments=3, wave_config=cfg,
        )
        self.assertEqual(driver.max_waves, 7)
        self.assertIs(driver.orchestrator.config, cfg)
        self.assertEqual(driver.strategist.max_experiments, 3)


@unittest.skipUnless(_HAS_KEY, "no OpenRouter key — skipping the live engine build")
class LiveBuildWaveEngineTest(unittest.TestCase):
    def test_build_from_engagement_id_and_run_one_real_wave(self):
        # Build the engine for a real engagement (real store, real strategist on OpenRouter) and
        # run one wave with a no-op worker — the whole assembly on the live model, GPU untouched.
        from agent.llm.openrouter import OpenRouterProvider

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = HypothesisGraphStore(root / "live-eng")
        store.create_hypothesis(
            title="SQL injection on the login form", claim="the login form is injectable",
            phase_created="ANALYSIS", rationale="r", origin_type="tool_observation", impact=4,
            confidence_band="medium", confidence_reason="x", surface="/login",
        )
        noop = lambda hid, method, stop, deadline: WorkerResult(outcome="completed", verdict="refuted")
        driver = engine.build_wave_engine(
            "live-eng", provider=OpenRouterProvider(model="openai/gpt-4o-mini"),
            worker_runner=noop, emit=lambda *a, **k: None, engagements_root=root, max_waves=1,
        )
        summary = driver.run()
        self.assertEqual(summary["waves"], 1)
        self.assertEqual(summary["status"], "wave_cap")


if __name__ == "__main__":
    unittest.main()

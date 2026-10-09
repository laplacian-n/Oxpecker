"""Phase runners (§2.6). The seam that selects flat vs graph must use the same decider the
orchestrator uses (no third decider of the mode), and the graph runner's deterministic wiring —
seeding recon roots, dispatching their enumeration, closing them, and the done predicate — is
pinned with injected fakes (no live model). The flat runner's own behaviour is covered end to end
by test_autonomous_driver; here we assert the seam and the graph path.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .. import tiers
from ..hypothesis_graph.store import HypothesisGraphStore
from . import graph_coldstart as cs
from .phase_runner import FlatPhaseRunner, GraphPhaseRunner, runner_for


class _FakeDriver:
    """The minimum the runners read off the driver: its id and tier. The graph runner never takes
    the lock/bootstraps/advances — those are the real driver's — so a stub is enough."""
    def __init__(self, tier):
        self.engagement_id = "eng-runner"
        self.tier = tier


class RunnerSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runner-sel-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph = HypothesisGraphStore(self.tmp / "g")

    def test_runner_for_uses_the_same_decider_as_uses_graph(self):
        # The one mandate: no third decider of the mode. For every (phase, tier), the runner kind
        # must match tiers.uses_graph exactly.
        all_phases = ("INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT")
        for tier in ("low", "medium", "high"):
            drv = _FakeDriver(tier)
            for phase in all_phases:
                runner = runner_for(drv, phase, graph_store=self.graph, scope_entries=["a.com"])
                is_graph = isinstance(runner, GraphPhaseRunner)
                self.assertEqual(is_graph, tiers.uses_graph(phase, tier),
                                 f"{tier}/{phase}: runner kind disagrees with uses_graph")

    def test_a_flat_phase_needs_no_graph_store(self):
        runner = runner_for(_FakeDriver("medium"), "RECON")  # medium RECON is flat
        self.assertIsInstance(runner, FlatPhaseRunner)

    def test_a_graph_phase_without_a_store_is_a_wiring_error(self):
        with self.assertRaises(ValueError):
            runner_for(_FakeDriver("high"), "RECON")  # high RECON is graph; no store supplied

    def test_runner_for_forwards_the_engagement_model(self):
        runner = runner_for(_FakeDriver("high"), "ANALYSIS", graph_store=self.graph,
                            scope_entries=["a.com"], model="openai/gpt-4o-mini")
        self.assertEqual(runner.model, "openai/gpt-4o-mini")  # reaches the graph runner's wave


class GraphReconTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runner-recon-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph = HypothesisGraphStore(self.tmp / "g")
        self.drv = _FakeDriver("high")

    def test_recon_seeds_a_root_per_scope_entry_and_dispatches_their_enumeration(self):
        dispatched = []
        runner = GraphPhaseRunner(
            self.drv, graph_store=self.graph, scope_entries=["a.com", "b.com"],
            recon_runner=lambda dispatch: dispatched.extend(dispatch),
        )
        result = runner.produce("RECON", {})
        self.assertTrue(result.made_progress)
        # one dispatch per seeded root
        roots = {r["hypothesis_id"] for r in cs.scope_roots(self.graph)}
        self.assertEqual({hid for hid, _ in dispatched}, roots)
        self.assertEqual(len(dispatched), 2)

    def test_recon_closes_each_root_so_the_phase_can_conclude(self):
        runner = GraphPhaseRunner(
            self.drv, graph_store=self.graph, scope_entries=["a.com"],
            recon_runner=lambda dispatch: None,  # enumeration ran (no-op worker)
        )
        # An open root (seeded, not yet enumerated) must hold RECON open — the driver always
        # produces before it checks, so this is the mid-produce state the predicate must reflect.
        cs.seed_scope_roots(self.graph, ["a.com"])
        self.assertFalse(runner.wave_concluded("RECON"))  # open root -> not concluded
        runner.produce("RECON", {})
        self.assertTrue(runner.wave_concluded("RECON"))    # after: root enumerated -> concluded
        root = cs.scope_roots(self.graph)[0]
        self.assertEqual(root["lifecycle_status"], "completed")
        self.assertEqual(root["verdict"], "unassessed")   # recon is never a verdict

    def test_recon_is_idempotent_and_does_nothing_once_enumerated(self):
        calls = []
        runner = GraphPhaseRunner(
            self.drv, graph_store=self.graph, scope_entries=["a.com"],
            recon_runner=lambda dispatch: calls.append(dispatch),
        )
        runner.produce("RECON", {})          # enumerates and closes the root
        second = runner.produce("RECON", {})  # no open roots left -> nothing to do
        self.assertEqual(len(calls), 1)
        self.assertFalse(second.made_progress)

    def test_recon_refuses_to_run_if_roots_drift_from_scope(self):
        # Pre-seed a rogue root the scope does not authorize; produce must fail loudly (invariant 1).
        self.graph.create_hypothesis(
            title="rogue", claim="enumerate evil.com", phase_created="RECON", rationale="x",
            origin_type="user_message", origin_ref="scope:evil.com", impact=3,
            confidence_band="medium", confidence_reason="x",
        )
        runner = GraphPhaseRunner(self.drv, graph_store=self.graph, scope_entries=["a.com"],
                                  recon_runner=lambda d: None)
        with self.assertRaises(cs.ColdStartInvariantError):
            runner.produce("RECON", {})


class GraphStrategistPhaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runner-anal-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.graph = HypothesisGraphStore(self.tmp / "g")
        self.drv = _FakeDriver("high")

    def _child(self, title="SQLi"):
        return self.graph.create_hypothesis(
            title=title, claim="injectable", phase_created="ANALYSIS", rationale="r",
            origin_type="tool_observation", impact=4, confidence_band="medium", confidence_reason="x",
        )

    def test_analysis_runs_the_injected_wave_engine(self):
        class _FakeEngine:
            def run(self_inner):
                return {"status": "complete", "waves": 2}
        runner = GraphPhaseRunner(self.drv, graph_store=self.graph, scope_entries=["a.com"],
                                  wave_engine_factory=lambda: _FakeEngine())
        result = runner.produce("ANALYSIS", {})
        self.assertTrue(result.made_progress)  # 2 waves ran

    def test_concluded_when_no_open_hypotheses_remain_excluding_roots(self):
        runner = GraphPhaseRunner(self.drv, graph_store=self.graph, scope_entries=["a.com"],
                                  wave_engine_factory=lambda: None)
        cs.seed_scope_roots(self.graph, ["a.com"])  # a root exists but must not hold the phase open
        self.assertTrue(runner.wave_concluded("VALIDATION"))  # only a root -> concluded
        child = self._child()
        self.assertFalse(runner.wave_concluded("VALIDATION"))  # an open child -> not concluded
        h = self.graph.get_hypothesis(child)
        self.graph.set_lifecycle_status(child, h["version"], "completed", reason="tested")
        self.assertTrue(runner.wave_concluded("VALIDATION"))  # child closed -> concluded


if __name__ == "__main__":
    unittest.main()

"""Graph-mode seam (§2.6, §14.1 G): in the high tier, RECON..VALIDATION run on the hypothesis
graph (the wave engine), so the flat task machine stands aside — plan_tasks creates nothing and
check_transition does not advance the phase off an empty task list. The decision is made by a
single function (`tiers.uses_graph`) that both methods consult, so they can never drift. These
tests pin that both methods honour it in lockstep, that the wall-clock ceiling still applies, and
that nothing changes for the flat tiers.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .. import tiers
from ..engagement.store import EngagementStore
from .orchestrator import Budget, PipelineOrchestrator


def _goto(store: EngagementStore, phase: str) -> None:
    row = store.get_phase()
    store.transition_phase(phase, f"test -> {phase}", expected_version=row["version"])


class UsesGraphTruthTableTest(unittest.TestCase):
    def test_only_high_tier_graph_phases_use_the_graph(self):
        for phase in ("RECON", "ANALYSIS", "VALIDATION"):
            self.assertTrue(tiers.uses_graph(phase, "high"))
        for phase in ("INTAKE", "REPORT", "CLOSEOUT"):
            self.assertFalse(tiers.uses_graph(phase, "high"))

    def test_medium_and_low_never_use_the_graph(self):
        all_phases = ("INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT")
        for tier in ("medium", "low"):
            for phase in all_phases:
                self.assertFalse(tiers.uses_graph(phase, tier))

    def test_an_unknown_tier_is_flat_not_the_default_tier(self):
        # The safety property: None must NOT be read as DEFAULT_TIER (which is 'high'); a caller
        # that did not deliberately opt in keeps flat behaviour even though the default tier is high.
        self.assertEqual(tiers.DEFAULT_TIER, "high")
        self.assertFalse(tiers.uses_graph("RECON", None))


class GraphModeOrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="graph-mode-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = EngagementStore(self.tmp / "eng")
        self.store.upsert_asset("host", "target.local")  # so flat RECON would have entities

    def test_high_tier_recon_plans_no_flat_tasks(self):
        _goto(self.store, "RECON")
        orch = PipelineOrchestrator(self.store, "web_api", tier="high")
        self.assertEqual(orch.plan_tasks(), [])
        self.assertEqual(self.store.list_tasks(phase="RECON"), [])

    def test_high_tier_graph_phase_does_not_advance_off_an_empty_task_list(self):
        # The trap this seam exists to close: a graph phase has no flat templates, so the
        # "no templates -> ready" shortcut would jump it before the first wave ran. It must not.
        _goto(self.store, "VALIDATION")
        orch = PipelineOrchestrator(self.store, "web_api", tier="high")
        orch.plan_tasks()
        ready, reason = orch.check_transition()
        self.assertFalse(ready, f"a graph phase advanced off the task list: {reason}")
        self.assertIn("wave", reason)
        self.assertFalse(orch.advance_if_ready())
        self.assertEqual(self.store.get_phase()["current_phase"], "VALIDATION")  # stayed put

    def test_the_wall_clock_ceiling_still_advances_a_graph_phase(self):
        # "or budget" — the one automatic exit that still applies in graph mode.
        _goto(self.store, "ANALYSIS")
        orch = PipelineOrchestrator(self.store, "web_api", budget=Budget(max_phase_wall_clock_s=0),
                                    tier="high")
        ready, reason = orch.check_transition()
        self.assertTrue(ready)
        self.assertIn("wall-clock", reason)

    def test_plan_and_transition_agree_on_the_mode_across_all_graph_phases(self):
        # The anti-drift guard: for every graph phase, plan_tasks creating nothing and
        # check_transition deferring must come as a pair — never one in graph mode and the other
        # in flat mode (the hang-or-jump the single function prevents).
        for phase in ("RECON", "ANALYSIS", "VALIDATION"):
            store = EngagementStore(self.tmp / f"eng-{phase}")
            store.upsert_asset("host", "target.local")
            _goto(store, phase)
            orch = PipelineOrchestrator(store, "web_api", tier="high")
            planned = orch.plan_tasks()
            ready, reason = orch.check_transition()
            self.assertEqual(planned, [], f"{phase}: graph mode planned flat tasks")
            self.assertFalse(ready, f"{phase}: graph mode advanced off the task list")
            self.assertIn("wave", reason)


class FlatTiersUnchangedTest(unittest.TestCase):
    """The same orchestrator with no tier (or a flat tier) behaves exactly as before — graph mode
    is strictly additive and opt-in."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="graph-mode-flat-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = EngagementStore(self.tmp / "eng")
        self.store.upsert_asset("host", "target.local")

    def test_no_tier_recon_plans_flat_tasks_as_before(self):
        _goto(self.store, "RECON")
        orch = PipelineOrchestrator(self.store, "web_api")  # tier=None
        self.assertTrue(orch.plan_tasks(), "flat RECON should still create tasks")

    def test_medium_tier_recon_plans_flat_tasks(self):
        _goto(self.store, "RECON")
        orch = PipelineOrchestrator(self.store, "web_api", tier="medium")
        self.assertTrue(orch.plan_tasks(), "medium is the flat engine — RECON should create tasks")


if __name__ == "__main__":
    unittest.main()

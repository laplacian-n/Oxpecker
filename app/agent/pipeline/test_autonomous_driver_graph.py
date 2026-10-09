"""High-tier driver integration (§2.6, option A): the one AutonomousDriver, run at tier=high,
drives the graph phases (RECON/ANALYSIS/VALIDATION) through GraphPhaseRunners and advances each on
the wave's conclusion — while every invariant (lock, bootstrap, wall-clock, advancement, events)
stays in the driver. Fakes stand in for the wave so the loop mechanics are tested without a model.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import tiers
from ..engagement.store import EngagementStore
from .autonomous_driver import AutonomousDriver
from .phase_runner import ProduceResult


class _FakeGraphRunner:
    """A graph runner that concludes immediately — produce makes progress, the wave is concluded —
    so the driver can be observed advancing through the graph phases. For VALIDATION it also trips
    the stop_event, so the run halts cleanly at the REPORT boundary rather than running flat REPORT
    tasks (which would need a model)."""
    def __init__(self, driver, phase):
        self.driver = driver
        self.phase = phase

    def produce(self, phase, attempts):
        if phase == "VALIDATION":
            self.driver.stop_event.set()
        return ProduceResult(made_progress=True)

    def wave_concluded(self, phase):
        return True


class HighTierDriverGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="driver-graph-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engagement_id = "eng-graph"
        self._p = patch("agent.pipeline.autonomous_driver.config.ENGAGEMENTS_ROOT", self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.store = EngagementStore(self.tmp / self.engagement_id)
        self.store.upsert_asset("host", "target.local")
        self.events = []

    def test_the_driver_advances_through_the_graph_phases_and_stops_at_report(self):
        driver = AutonomousDriver(
            engagement_id=self.engagement_id, profile_name="web_api", mode="autonomous",
            session_id="s", on_event=self.events.append, tier="high",
        )
        self.assertEqual(driver.tier, "high")

        real_runner_for = driver._runner_for_phase

        def fake_runner_for(phase):
            # graph phases get the fake wave runner; flat phases (INTAKE) keep the real flat runner,
            # so INTAKE->RECON still advances through the orchestrator exactly as in production.
            if tiers.uses_graph(phase, driver.tier):
                return _FakeGraphRunner(driver, phase)
            return real_runner_for(phase)

        with patch.object(driver, "_runner_for_phase", side_effect=fake_runner_for):
            result = driver.run()

        # VALIDATION tripped the stop, so the run stops as the driver enters REPORT.
        self.assertEqual(result.status, "stopped_by_operator")
        # the three graph phases were each completed and advanced through
        for phase in ("RECON", "ANALYSIS", "VALIDATION"):
            self.assertIn(phase, result.phases_completed, f"{phase} was not advanced through")
        advanced = [e for e in self.events if e["type"] == "phase_advanced"]
        hops = {(e["from_phase"], e["to_phase"]) for e in advanced}
        self.assertIn(("RECON", "ANALYSIS"), hops)
        self.assertIn(("ANALYSIS", "VALIDATION"), hops)
        self.assertIn(("VALIDATION", "REPORT"), hops)

    def test_the_finding_gate_runs_at_validation_to_report_only(self):
        driver = AutonomousDriver(
            engagement_id=self.engagement_id, profile_name="web_api", mode="autonomous",
            session_id="s", on_event=self.events.append, tier="high",
        )
        real_runner_for = driver._runner_for_phase

        def fake_runner_for(phase):
            if tiers.uses_graph(phase, driver.tier):
                return _FakeGraphRunner(driver, phase)
            return real_runner_for(phase)

        gate_calls = []
        with patch.object(driver, "_runner_for_phase", side_effect=fake_runner_for), \
             patch.object(driver, "_run_finding_gate", side_effect=lambda: gate_calls.append(True)):
            driver.run()
        # the gate fires exactly once — on the VALIDATION->REPORT hand-off, not on RECON/ANALYSIS
        self.assertEqual(len(gate_calls), 1)

    def test_run_finding_gate_skips_cleanly_when_no_verifier_is_configured(self):
        (self.tmp / self.engagement_id).mkdir(parents=True, exist_ok=True)
        (self.tmp / self.engagement_id / "roe.json").write_text('{"tier": "high"}')  # no verifier
        driver = AutonomousDriver(
            engagement_id=self.engagement_id, profile_name="web_api", mode="autonomous",
            session_id="s", on_event=self.events.append, tier="high",
        )
        driver._run_finding_gate()
        self.assertTrue(any(e["type"] == "finding_gate_skipped" for e in self.events))

    def test_run_finding_gate_emits_counts_from_the_gate(self):
        driver = AutonomousDriver(
            engagement_id=self.engagement_id, profile_name="web_api", mode="autonomous",
            session_id="s", on_event=self.events.append, tier="high",
        )

        class _FakeGate:
            def run(self_inner):
                return [{"verdict": "refuted"}, {"verdict": "could_not_refute"}]

        with patch("agent.pipeline.finding_gate.build_finding_gate", return_value=_FakeGate()):
            driver._run_finding_gate()
        done = next(e for e in self.events if e["type"] == "finding_gate_done")
        self.assertEqual((done["verified"], done["refuted"]), (2, 1))

    def test_proposer_model_reads_roe_or_defaults(self):
        from ..llm import registry

        driver = AutonomousDriver(
            engagement_id=self.engagement_id, profile_name="web_api", mode="autonomous",
            session_id="s", tier="high",
        )
        self.assertEqual(driver._proposer_model(), registry.DEFAULT_PROVIDER)  # no roe model
        (self.tmp / self.engagement_id).mkdir(parents=True, exist_ok=True)
        (self.tmp / self.engagement_id / "roe.json").write_text('{"model": "openai/gpt-4o-mini"}')
        self.assertEqual(driver._proposer_model(), "openai/gpt-4o-mini")

    def test_a_medium_engagement_never_builds_a_graph_runner(self):
        # The same driver at a flat tier must select flat runners for every phase — the tier is the
        # only thing that changes, and it changes only the runner.
        driver = AutonomousDriver(
            engagement_id=self.engagement_id, profile_name="web_api", mode="autonomous",
            session_id="s", on_event=self.events.append, tier="medium",
        )
        from .phase_runner import FlatPhaseRunner
        for phase in ("INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT"):
            self.assertIsInstance(driver._runner_for_phase(phase), FlatPhaseRunner)


if __name__ == "__main__":
    unittest.main()

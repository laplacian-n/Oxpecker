"""Waste report over the trajectory store — totals, the §7.1 waste split, provenance keying, the
§2.7 idle-strategist smell, counts, the None-denominator cases, and the pure-read guarantee.
Every store gets an explicit tempdir and the engagement event root is patched to a tempdir too,
so nothing lands in the real state or engagements dirs."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .. import config
from .store import ExperimentRecord, StrategistRecord, TrajectoryStore
from .waste_report import waste_report


class _WasteReportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="waste-report-test-"))
        patcher = mock.patch.object(config, "ENGAGEMENTS_ROOT", self.tmp / "engagements")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = TrajectoryStore("eng-waste", trajectory_dir=self.tmp / "trajectory")


def _exp(experiment_id: str, **overrides) -> ExperimentRecord:
    defaults = dict(
        experiment_id=experiment_id,
        hypothesis_id="hyp-1",
        method_summary="probe",
        cost=0.0,
        wall_clock_s=0.0,
        made_progress=True,
        query_count=0,
        action_count=0,
        provenance="device-7",
    )
    defaults.update(overrides)
    return ExperimentRecord(**defaults)


class WasteReportTests(_WasteReportTestCase):
    def _record_mix(self):
        # Two provenances plus one unattributed (empty string). Progress and no-progress mixed.
        self.store.record_experiment(_exp("e1", cost=1.0, wall_clock_s=10.0, made_progress=True,
                                          query_count=4, action_count=2, provenance="device-7"))
        self.store.record_experiment(_exp("e2", cost=2.0, wall_clock_s=20.0, made_progress=False,
                                          query_count=6, action_count=1, provenance="device-7"))
        self.store.record_experiment(_exp("e3", cost=0.5, wall_clock_s=5.0, made_progress=False,
                                          query_count=1, action_count=1, provenance="worker-b"))
        self.store.record_experiment(_exp("e4", cost=0.25, wall_clock_s=2.5, made_progress=True,
                                          query_count=0, action_count=1, provenance=""))
        self.store.record_experiment(_exp("e5", cost=0.75, wall_clock_s=7.5, made_progress=False,
                                          query_count=2, action_count=0, provenance=""))
        # t1 dispatches and costs; t2 idles and costs (counts); t3 idles at zero cost (does not).
        self.store.record_strategist_turn(StrategistRecord(
            turn_id="t1", chosen=[["hyp-1", "dir-bust"]], cost=0.5, wall_clock_s=3.0))
        self.store.record_strategist_turn(StrategistRecord(
            turn_id="t2", chosen=[], declined=[{"hypothesis_id": "hyp-2"}], cost=0.3, wall_clock_s=1.5))
        self.store.record_strategist_turn(StrategistRecord(
            turn_id="t3", chosen=[], cost=0.0, wall_clock_s=0.5))

    def test_totals_include_experiments_and_strategist_turns(self):
        self._record_mix()
        report = waste_report(self.store)
        # experiments 1+2+0.5+0.25+0.75 = 4.5, strategist 0.5+0.3+0 = 0.8
        self.assertAlmostEqual(report["total_cost"], 5.3)
        # experiments 10+20+5+2.5+7.5 = 45, strategist 3+1.5+0.5 = 5
        self.assertAlmostEqual(report["total_wall_clock_s"], 50.0)

    def test_wasted_sums_only_no_progress_experiments(self):
        self._record_mix()
        report = waste_report(self.store)
        # e2 2.0 + e3 0.5 + e5 0.75; strategist cost must not leak in
        self.assertAlmostEqual(report["wasted_cost"], 3.25)
        # e2 20 + e3 5 + e5 7.5
        self.assertAlmostEqual(report["wasted_wall_clock_s"], 32.5)

    def test_waste_fraction_is_wasted_over_total(self):
        self._record_mix()
        report = waste_report(self.store)
        self.assertAlmostEqual(report["waste_fraction"], 3.25 / 5.3)

    def test_waste_fraction_is_zero_not_none_when_nothing_wasted(self):
        self.store.record_experiment(_exp("e1", cost=1.0, made_progress=True))
        report = waste_report(self.store)
        self.assertEqual(report["wasted_cost"], 0.0)
        self.assertEqual(report["waste_fraction"], 0.0)

    def test_by_provenance_splits_cost_and_waste(self):
        self._record_mix()
        by = waste_report(self.store)["by_provenance"]
        self.assertEqual(set(by), {"device-7", "worker-b", "unattributed"})
        # device-7: e1 1.0 + e2 2.0 cost; e2 wasted 2.0
        self.assertAlmostEqual(by["device-7"]["cost"], 3.0)
        self.assertAlmostEqual(by["device-7"]["wasted_cost"], 2.0)
        # worker-b: e3 only, all wasted
        self.assertAlmostEqual(by["worker-b"]["cost"], 0.5)
        self.assertAlmostEqual(by["worker-b"]["wasted_cost"], 0.5)
        # unattributed (empty provenance): e4 0.25 + e5 0.75; e5 wasted
        self.assertAlmostEqual(by["unattributed"]["cost"], 1.0)
        self.assertAlmostEqual(by["unattributed"]["wasted_cost"], 0.75)

    def test_by_provenance_excludes_strategist_cost(self):
        self._record_mix()
        by = waste_report(self.store)["by_provenance"]
        per_provenance_total = sum(v["cost"] for v in by.values())
        self.assertAlmostEqual(per_provenance_total, 4.5)

    def test_strategist_spend_no_dispatch_counts_only_idle_and_costly(self):
        self._record_mix()
        spend = waste_report(self.store)["strategist_spend_no_dispatch"]
        # t2 only: t1 dispatched, t3 idled but cost nothing
        self.assertEqual(spend["count"], 1)
        self.assertAlmostEqual(spend["cost"], 0.3)

    def test_query_action_ratio_passes_through_store(self):
        self._record_mix()
        report = waste_report(self.store)
        # queries 4+6+1+0+2 = 13 over actions 2+1+1+1+0 = 5
        self.assertAlmostEqual(report["query_action_ratio"], 13 / 5)
        self.assertAlmostEqual(report["query_action_ratio"], self.store.query_action_ratio())

    def test_counts(self):
        self._record_mix()
        report = waste_report(self.store)
        self.assertEqual(report["experiment_count"], 5)
        self.assertEqual(report["wasted_experiment_count"], 3)


class EmptyAndEdgeTests(_WasteReportTestCase):
    def test_empty_store_reports_zeros_and_none_denominators(self):
        report = waste_report(self.store)
        self.assertEqual(report["total_cost"], 0)
        self.assertEqual(report["total_wall_clock_s"], 0)
        self.assertEqual(report["wasted_cost"], 0)
        self.assertEqual(report["wasted_wall_clock_s"], 0)
        self.assertIsNone(report["waste_fraction"])
        self.assertEqual(report["by_provenance"], {})
        self.assertEqual(report["strategist_spend_no_dispatch"], {"count": 0, "cost": 0})
        self.assertIsNone(report["query_action_ratio"])
        self.assertEqual(report["experiment_count"], 0)
        self.assertEqual(report["wasted_experiment_count"], 0)

    def test_waste_fraction_is_none_when_total_cost_is_zero(self):
        # Experiments exist and are wasted, but nothing was spent: no denominator, no fraction.
        self.store.record_experiment(_exp("e1", cost=0.0, made_progress=False))
        report = waste_report(self.store)
        self.assertEqual(report["wasted_experiment_count"], 1)
        self.assertEqual(report["wasted_cost"], 0.0)
        self.assertIsNone(report["waste_fraction"])

    def test_strategist_only_store_has_cost_but_no_experiment_waste(self):
        self.store.record_strategist_turn(StrategistRecord(turn_id="t1", cost=2.0, wall_clock_s=4.0))
        report = waste_report(self.store)
        self.assertAlmostEqual(report["total_cost"], 2.0)
        self.assertAlmostEqual(report["total_wall_clock_s"], 4.0)
        self.assertEqual(report["wasted_cost"], 0)
        self.assertEqual(report["waste_fraction"], 0.0)
        self.assertEqual(report["strategist_spend_no_dispatch"]["count"], 1)
        self.assertIsNone(report["query_action_ratio"])

    def test_no_progress_experiment_with_zero_cost_adds_count_not_cost(self):
        self.store.record_experiment(_exp("e1", cost=1.0, made_progress=True))
        self.store.record_experiment(_exp("e2", cost=0.0, made_progress=False))
        report = waste_report(self.store)
        self.assertEqual(report["wasted_experiment_count"], 1)
        self.assertEqual(report["wasted_cost"], 0.0)
        self.assertEqual(report["waste_fraction"], 0.0)


class PureReadTests(_WasteReportTestCase):
    def test_report_does_not_write_to_the_store(self):
        self.store.record_experiment(_exp("e1", cost=1.0, made_progress=False))
        self.store.record_strategist_turn(StrategistRecord(turn_id="t1", cost=0.2))
        before = self.store.path.read_bytes()
        waste_report(self.store)
        waste_report(self.store)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_report_does_not_create_a_file_for_an_empty_store(self):
        self.assertFalse(self.store.path.exists())
        waste_report(self.store)
        self.assertFalse(self.store.path.exists())


if __name__ == "__main__":
    unittest.main()

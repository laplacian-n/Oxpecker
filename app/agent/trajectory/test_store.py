"""Trajectory record v1 store — round-trips, ordering, ratios, waste, validation and the
link-not-copy rule. No live model needed. Every store gets an explicit tempdir, and the engagement
event root is patched to a tempdir too, so nothing lands in the real state or engagements dirs."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .. import config
from .store import ChainLink, ExperimentRecord, StrategistRecord, TrajectoryStore


class _TrajectoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trajectory-test-"))
        self.traj_dir = self.tmp / "trajectory"
        # emit() resolves ENGAGEMENTS_ROOT at call time; point it at the tempdir for the test.
        patcher = mock.patch.object(config, "ENGAGEMENTS_ROOT", self.tmp / "engagements")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = TrajectoryStore("eng-1", trajectory_dir=self.traj_dir)


def _exp(experiment_id: str, **overrides) -> ExperimentRecord:
    defaults = dict(
        experiment_id=experiment_id,
        hypothesis_id="hyp-1",
        method_summary="probe /admin with default creds",
        expected_observation="302 to login",
        observed_result="302 to login",
        cost=0.5,
        wall_clock_s=12.0,
        made_progress=True,
        query_count=4,
        action_count=2,
        provenance="device-7",
    )
    defaults.update(overrides)
    return ExperimentRecord(**defaults)


class RoundTripTests(_TrajectoryTestCase):
    def test_experiment_round_trips_through_typed_reader(self):
        original = _exp("e1")
        self.store.record_experiment(original)
        got = self.store.experiments()
        self.assertEqual(got, [original])
        self.assertEqual(got[0].kind, "experiment")

    def test_strategist_turn_round_trips_through_typed_reader(self):
        original = StrategistRecord(
            turn_id="t1",
            chosen=[["hyp-1", "dir-bust"]],
            declined=[{"hypothesis_id": "hyp-2", "reason": "out of scope"}],
            cost=0.1,
            wall_clock_s=3.5,
        )
        self.store.record_strategist_turn(original)
        self.assertEqual(self.store.strategist_turns(), [original])

    def test_chain_round_trips_through_typed_reader(self):
        original = ChainLink(chain_id="c1", experiment_ids=["e1", "e2"], outcome="confirmed")
        self.store.link_chain(original)
        self.assertEqual(self.store.chains(), [original])

    def test_all_kinds_share_one_file_and_readers_filter_by_kind(self):
        self.store.record_experiment(_exp("e1"))
        self.store.record_strategist_turn(StrategistRecord(turn_id="t1"))
        self.store.link_chain(ChainLink(chain_id="c1", experiment_ids=["e1"]))
        self.assertEqual(len(self.store.list_all()), 3)
        self.assertEqual(len(self.store.experiments()), 1)
        self.assertEqual(len(self.store.strategist_turns()), 1)
        self.assertEqual(len(self.store.chains()), 1)
        self.assertEqual(self.store.path, self.traj_dir / "eng-1.jsonl")

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(self.store.list_all(), [])
        self.assertEqual(self.store.experiments(), [])
        self.assertIsNone(self.store.query_action_ratio())
        self.assertEqual(self.store.wasted_experiments(), [])


class OrderingTests(_TrajectoryTestCase):
    def test_list_all_preserves_file_order_and_carries_kind(self):
        self.store.record_strategist_turn(StrategistRecord(turn_id="t1"))
        self.store.record_experiment(_exp("e1"))
        self.store.link_chain(ChainLink(chain_id="c1", experiment_ids=["e1"]))
        self.store.record_experiment(_exp("e2"))

        rows = self.store.list_all()
        self.assertEqual([r["kind"] for r in rows], ["strategist", "experiment", "chain", "experiment"])
        self.assertEqual(rows[1]["experiment_id"], "e1")
        self.assertEqual(rows[3]["experiment_id"], "e2")

    def test_appends_do_not_rewrite_earlier_lines(self):
        self.store.record_experiment(_exp("e1"))
        first_line = self.store.path.read_text().splitlines()[0]
        self.store.record_experiment(_exp("e2"))
        self.assertEqual(self.store.path.read_text().splitlines()[0], first_line)


class RatioAndWasteTests(_TrajectoryTestCase):
    def test_query_action_ratio_is_total_queries_over_total_actions(self):
        self.store.record_experiment(_exp("e1", query_count=6, action_count=2))
        self.store.record_experiment(_exp("e2", query_count=1, action_count=1))
        self.assertAlmostEqual(self.store.query_action_ratio(), 7 / 3)

    def test_query_action_ratio_is_none_when_no_actions(self):
        self.store.record_experiment(_exp("e1", query_count=5, action_count=0))
        self.assertIsNone(self.store.query_action_ratio())

    def test_wasted_experiments_returns_only_no_progress(self):
        self.store.record_experiment(_exp("e1", made_progress=True))
        self.store.record_experiment(_exp("e2", made_progress=False))
        self.store.record_experiment(_exp("e3", made_progress=False))
        self.store.record_experiment(_exp("e4", made_progress=True))
        wasted = self.store.wasted_experiments()
        self.assertEqual([e.experiment_id for e in wasted], ["e2", "e3"])

    def test_strategist_and_chain_records_do_not_count_as_experiments(self):
        self.store.record_strategist_turn(StrategistRecord(turn_id="t1", cost=9.0))
        self.store.link_chain(ChainLink(chain_id="c1", experiment_ids=[]))
        self.assertEqual(self.store.experiments(), [])
        self.assertEqual(self.store.wasted_experiments(), [])
        self.assertIsNone(self.store.query_action_ratio())


class ValidationTests(_TrajectoryTestCase):
    def test_negative_cost_raises_on_experiment(self):
        with self.assertRaises(ValueError):
            _exp("e1", cost=-0.01)

    def test_negative_wall_clock_raises_on_experiment(self):
        with self.assertRaises(ValueError):
            _exp("e1", wall_clock_s=-1.0)

    def test_negative_cost_raises_on_strategist_turn(self):
        with self.assertRaises(ValueError):
            StrategistRecord(turn_id="t1", cost=-2.0)

    def test_negative_wall_clock_raises_on_strategist_turn(self):
        with self.assertRaises(ValueError):
            StrategistRecord(turn_id="t1", wall_clock_s=-0.5)

    def test_invalid_record_is_never_written(self):
        with self.assertRaises(ValueError):
            _exp("e1", cost=-1.0)
        self.assertEqual(self.store.list_all(), [])

    def test_zero_cost_and_wall_clock_are_allowed(self):
        _exp("e1", cost=0.0, wall_clock_s=0.0)
        StrategistRecord(turn_id="t1", cost=0.0, wall_clock_s=0.0)


class StrategistAndChainTests(_TrajectoryTestCase):
    def test_strategist_turn_with_empty_chosen_is_allowed(self):
        turn = StrategistRecord(turn_id="t-idle", chosen=[], declined=[{"hypothesis_id": "h9"}])
        self.store.record_strategist_turn(turn)
        got = self.store.strategist_turns()
        self.assertEqual(got, [turn])
        self.assertEqual(got[0].chosen, [])
        self.assertEqual(got[0].declined, [{"hypothesis_id": "h9"}])

    def test_link_chain_stores_references_not_copies(self):
        self.store.record_experiment(_exp("e1", observed_result="secret-body-text"))
        self.store.link_chain(ChainLink(chain_id="c1", experiment_ids=["e1"], outcome="dead end"))

        chain_row = [r for r in self.store.list_all() if r["kind"] == "chain"][0]
        self.assertEqual(
            set(chain_row),
            {"chain_id", "experiment_ids", "outcome", "kind", "recorded_at"},
        )
        self.assertEqual(chain_row["experiment_ids"], ["e1"])
        # The experiment's contents live only on the experiment record, never in the chain row.
        self.assertNotIn("secret-body-text", json.dumps(chain_row))

    def test_chain_outcome_is_appended_not_rewritten(self):
        self.store.link_chain(ChainLink(chain_id="c1", experiment_ids=["e1"], outcome=""))
        self.store.link_chain(ChainLink(chain_id="c1", experiment_ids=["e1"], outcome="confirmed"))
        chains = self.store.chains()
        self.assertEqual([c.outcome for c in chains], ["", "confirmed"])
        self.assertEqual({c.chain_id for c in chains}, {"c1"})


if __name__ == "__main__":
    unittest.main()

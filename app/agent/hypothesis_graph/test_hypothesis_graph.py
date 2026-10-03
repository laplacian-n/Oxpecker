"""Tests for the Hypothesis Graph MVP 0 (store + engine + service). No live model needed — this
is the deterministic semantics layer both review docs insist must be built and proven correct
BEFORE any UI (colors/lines/scores on top of shaky semantics is the failure mode they warn
against). Runs directly: `python3 -m agent.hypothesis_graph.test_hypothesis_graph`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from . import engine
from .service import HypothesisGraphService
from .store import ConflictError, CycleError, GraphValidationError, HypothesisGraphStore, NotFoundError


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hypgraph-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = HypothesisGraphStore(self.tmp)

    def _h(self, title="t", parent=None, impact=3, band="medium", phase="ANALYSIS", planned=1):
        return self.store.create_hypothesis(
            title=title, claim=f"{title} claim", phase_created=phase, rationale="because",
            origin_type="ai_inference", impact=impact, confidence_band=band,
            confidence_reason="r", primary_parent_id=parent, planned_tests=planned,
        )


class TestHypothesisCreation(StoreTestBase):
    def test_ordinals_are_sequential_and_unique(self):
        a, b, c = self._h("a"), self._h("b"), self._h("c")
        self.assertEqual([self.store.get_hypothesis(x)["ordinal"] for x in (a, b, c)], [1, 2, 3])

    def test_ordinal_not_reused_after_abandon(self):
        a = self._h("a")
        h = self.store.get_hypothesis(a)
        self.store.set_lifecycle_status(a, h["version"], "abandoned", reason="out of scope")
        b = self._h("b")
        self.assertEqual(self.store.get_hypothesis(b)["ordinal"], 2)  # not 1 reused

    def test_missing_rationale_rejected(self):
        with self.assertRaises(GraphValidationError):
            self.store.create_hypothesis(
                title="t", claim="c", phase_created="RECON", rationale="  ",
                origin_type="ai_inference", impact=3, confidence_band="low", confidence_reason="r",
            )

    def test_bad_impact_rejected(self):
        with self.assertRaises(GraphValidationError):
            self._h("t", impact=9)

    def test_primary_parent_creates_spawned_by_edge(self):
        a = self._h("a")
        b = self._h("b", parent=a)
        edges = self.store.list_edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["edge_type"], "spawned_by")
        self.assertEqual((edges[0]["from_hypothesis_id"], edges[0]["to_hypothesis_id"]), (a, b))

    def test_unknown_parent_rejected(self):
        with self.assertRaises(NotFoundError):
            self._h("b", parent="h_doesnotexist")


class TestCycleInvariant(StoreTestBase):
    def test_causal_cycle_rejected(self):
        a = self._h("a")
        b = self._h("b", parent=a)  # a -> b causal
        with self.assertRaises(CycleError):
            self.store.add_edge(b, a, "synthesized_from")  # b -> a causal would cycle

    def test_associative_reference_edge_does_not_trip_cycle_check(self):
        a = self._h("a")
        b = self._h("b", parent=a)
        # references is NOT causal, so b -> a references is allowed (cross-link)
        eid = self.store.add_edge(b, a, "references", reason="related surface")
        self.assertTrue(eid)
        self.assertEqual(self.store.check_invariants(), [])

    def test_self_edge_rejected(self):
        a = self._h("a")
        with self.assertRaises(GraphValidationError):
            self.store.add_edge(a, a, "supports")


class TestLifecycleAndVerdict(StoreTestBase):
    def test_park_requires_reason(self):
        a = self._h("a")
        h = self.store.get_hypothesis(a)
        with self.assertRaises(GraphValidationError):
            self.store.set_lifecycle_status(a, h["version"], "parked", reason=None)

    def test_abandon_requires_reason_and_records_it(self):
        a = self._h("a")
        h = self.store.get_hypothesis(a)
        self.store.set_lifecycle_status(a, h["version"], "abandoned", reason="RoE excludes this host")
        self.assertEqual(self.store.get_hypothesis(a)["abandon_reason"], "RoE excludes this host")

    def test_verdict_moves_to_completed(self):
        a = self._h("a")
        h = self.store.get_hypothesis(a)
        self.store.set_verdict(a, h["version"], "refuted")
        got = self.store.get_hypothesis(a)
        self.assertEqual(got["verdict"], "refuted")
        self.assertEqual(got["lifecycle_status"], "completed")

    def test_optimistic_version_conflict(self):
        a = self._h("a")
        h = self.store.get_hypothesis(a)
        self.store.set_confidence(a, h["version"], "high", "evidence")
        with self.assertRaises(ConflictError):
            self.store.set_confidence(a, h["version"], "low", "stale write")  # stale version


class TestExperimentsAndObservations(StoreTestBase):
    def test_attempt_numbers_increment(self):
        a = self._h("a")
        x1 = self.store.start_experiment(a, method_summary="try 1")
        self.store.complete_experiment(x1, status="completed", observed_result="r1")
        x2 = self.store.start_experiment(a, method_summary="try 2")
        nums = [x["attempt_no"] for x in self.store.list_experiments(a)]
        self.assertEqual(nums, [1, 2])

    def test_completing_experiment_accrues_direct_tokens(self):
        a = self._h("a")
        x = self.store.start_experiment(a, method_summary="m")
        self.store.complete_experiment(x, status="completed", observed_result="r", input_tokens=100, output_tokens=30)
        self.assertEqual(self.store.get_hypothesis(a)["direct_tokens"], 130)

    def test_running_experiment_moves_hypothesis_to_running(self):
        a = self._h("a")
        self.store.start_experiment(a, method_summary="m")
        self.assertEqual(self.store.get_hypothesis(a)["lifecycle_status"], "running")


class TestEngine(StoreTestBase):
    def test_coverage_is_completed_over_planned(self):
        a = self._h("a", planned=2)
        x = self.store.start_experiment(a, method_summary="m")
        self.store.complete_experiment(x, status="completed", observed_result="r")
        self.assertEqual(engine.coverage(self.store.get_hypothesis(a), self.store.list_experiments(a)), 0.5)

    def test_high_impact_medium_conf_outranks_weak_lead(self):
        parent = self._h("root", impact=3, band="low")
        strong = self._h("idor", parent=parent, impact=5, band="medium")
        weak = self._h("weak", parent=parent, impact=1, band="high")
        exps = {h["hypothesis_id"]: self.store.list_experiments(h["hypothesis_id"]) for h in self.store.list_hypotheses()}
        ranked = engine.rank_open(self.store.list_hypotheses(), exps)
        # the strong lead must rank above the weak one
        strong_idx = next(i for i, r in enumerate(ranked) if r["title"] == "idor")
        weak_idx = next(i for i, r in enumerate(ranked) if r["title"] == "weak")
        self.assertLess(strong_idx, weak_idx)

    def test_priority_does_not_use_spent_tokens(self):
        """Sunk cost must not change ranking — two identical hypotheses, one with a huge spent
        token count, must get the same priority score (doc 2 §8.3)."""
        a = self._h("a", impact=4, band="medium", planned=1)
        b = self._h("b", impact=4, band="medium", planned=1)
        self.store.add_direct_tokens(a, 50_000)  # a has spent a fortune already
        exps = {a: self.store.list_experiments(a), b: self.store.list_experiments(b)}
        pa = engine.priority_components(self.store.get_hypothesis(a), exps[a])
        pb = engine.priority_components(self.store.get_hypothesis(b), exps[b])
        self.assertEqual(pa["score"], pb["score"])

    def test_suggest_confidence_leans_with_evidence(self):
        a = self._h("a")
        x = self.store.start_experiment(a, method_summary="m")
        self.store.complete_experiment(x, status="completed", observed_result="r")
        self.store.add_observation(x, a, summary="s1", polarity="refutes", strength="strong")
        self.store.add_observation(x, a, summary="s2", polarity="refutes", strength="moderate")
        band, _ = engine.suggest_confidence_band(self.store.list_observations(a))
        self.assertEqual(band, "low")

    def test_digest_line_contains_ordinal_and_coverage(self):
        a = self._h("a", planned=2)
        line = engine.digest_line(self.store.get_hypothesis(a), [])
        self.assertIn("#1", line)
        self.assertIn("cov:0%", line)

    def test_stale_reason_flags_a_refuted_dependency(self):
        root = self._h("root")
        child = self._h("child", parent=root)
        self.store.set_verdict(root, self.store.get_hypothesis(root)["version"], "refuted")
        reason = engine.stale_reason(
            self.store.get_hypothesis(child), now=1e12,
            parent=self.store.get_hypothesis(root),
        )
        self.assertIn("refuted", reason)

    def test_stale_reason_flags_parent_evidence_that_moved_after_creation(self):
        root = self._h("root")
        child = self._h("child", parent=root)
        x = self.store.start_experiment(root, method_summary="m")
        self.store.complete_experiment(x, status="completed", observed_result="r")
        self.store.add_observation(x, root, summary="new", polarity="supports", strength="weak")
        reason = engine.stale_reason(
            self.store.get_hypothesis(child), now=1e12,
            parent=self.store.get_hypothesis(root),
            parent_observations=self.store.list_observations(root),
        )
        self.assertIn("evidence changed", reason)

    def test_stale_reason_none_for_a_fresh_open_hypothesis(self):
        a = self._h("a")
        self.assertIsNone(engine.stale_reason(self.store.get_hypothesis(a), now=self.store.get_hypothesis(a)["updated_at"]))


class TestServiceRetrievalAndContext(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hypgraph-svc-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.svc = HypothesisGraphService(self.tmp)
        self.root = self.svc.add_hypothesis(
            title="root", claim="c", phase_created="RECON", rationale="r",
            origin_type="ai_inference", impact=3, confidence_band="low", confidence_reason="x",
        )
        self.idor = self.svc.add_hypothesis(
            title="idor orders", claim="/api/orders IDOR", phase_created="ANALYSIS", rationale="ids",
            origin_type="tool_observation", impact=5, confidence_band="medium", confidence_reason="clue",
            primary_parent_id=self.root["hypothesis_id"], surface="/api/orders/{id}", planned_tests=2,
        )

    def test_resolve_by_ordinal_alias_and_id(self):
        att = self.svc.start_attempt("H-2", method_summary="m")
        self.assertTrue(att["experiment_id"])
        att2 = self.svc.start_attempt(str(self.idor["ordinal"]), method_summary="m2")
        self.assertTrue(att2["experiment_id"])

    def test_complete_attempt_recomputes_coverage_and_suggests_confidence(self):
        att = self.svc.start_attempt("H-2", method_summary="m")
        res = self.svc.complete_attempt(
            att["experiment_id"], status="completed", observed_result="200 cross read",
            observation_summary="worked", polarity="supports", strength="strong",
            input_tokens=100, output_tokens=20,
        )
        self.assertEqual(res["coverage"], 0.5)  # 1 of 2 planned
        self.assertIn(res["suggested_confidence_band"], ("low", "medium", "high"))

    def test_graph_search_matches_surface_and_title(self):
        self.assertEqual(len(self.svc.graph_search("idor")), 1)
        self.assertEqual(len(self.svc.graph_search("/api/orders")), 1)
        self.assertEqual(len(self.svc.graph_search("nonexistent-xyz")), 0)

    def test_graph_read_branch_capped_depth(self):
        res = self.svc.graph_read_branch("H-1", depth=99, mode="summary")
        self.assertLessEqual(res["depth"], 6)
        ords = [n["ordinal"] for n in res["nodes"]]
        self.assertIn(1, ords)
        self.assertIn(2, ords)

    def test_context_block_names_retrieval_tools(self):
        block = self.svc.build_context_block(current_phase="ANALYSIS")
        self.assertIn("HYPOTHESIS GRAPH", block)
        self.assertIn("graph_search", block)
        self.assertIn("do not guess", block)

    def test_active_path_is_set_with_reason(self):
        self.svc.set_active_path(["H-1", "H-2"], "pursuing IDOR")
        gs = self.svc.store.get_graph_state()
        self.assertEqual(gs["active_path_reason"], "pursuing IDOR")
        self.assertEqual(gs["active_hypothesis_id"], self.idor["hypothesis_id"])

    def test_invariants_hold_after_a_realistic_sequence(self):
        att = self.svc.start_attempt("H-2", method_summary="m")
        self.svc.complete_attempt(
            att["experiment_id"], status="completed", observed_result="r",
            observation_summary="s", polarity="supports", strength="moderate",
        )
        self.svc.set_verdict("H-2", "supported")
        self.svc.park("H-1", "lower priority than the IDOR lead")
        self.assertEqual(self.svc.store.check_invariants(), [])

    def test_operator_park_tags_the_reason_and_is_reversible(self):
        self.svc.park("H-2", "not worth it right now", actor="operator")
        h = self.svc.store.get_by_ordinal(2)
        self.assertEqual(h["lifecycle_status"], "parked")
        self.assertEqual(h["park_reason"], "[operator] not worth it right now")
        self.svc.reopen("H-2", actor="operator")
        h = self.svc.store.get_by_ordinal(2)
        self.assertEqual(h["lifecycle_status"], "open")
        self.assertIsNone(h["park_reason"])
        self.assertEqual(self.svc.store.check_invariants(), [])

    def test_lifecycle_event_records_the_actor(self):
        self.svc.park("H-2", "shelving", actor="operator")
        evs = [e for e in self.svc.store.list_events() if e["kind"] == "hypothesis.parked"]
        self.assertEqual(json.loads(evs[-1]["payload_json"])["actor"], "operator")

    def test_operator_note_is_appended_and_listed(self):
        self.svc.add_note("H-2", "confirm this is in scope before spending more time")
        self.svc.add_note("H-2", "customer asked us to prioritise auth issues")
        notes = self.svc.store.list_operator_notes(self.idor["hypothesis_id"])
        self.assertEqual([n["text"] for n in notes],
                         ["confirm this is in scope before spending more time",
                          "customer asked us to prioritise auth issues"])
        self.assertEqual(notes[0]["actor"], "operator")
        # a note surfaces in the drawer payload and never touches history
        detail = self.svc.node_detail("H-2")
        self.assertEqual(len(detail["operator_notes"]), 2)
        self.assertEqual(detail["claim"], "/api/orders IDOR")

    def test_empty_note_rejected(self):
        with self.assertRaises(GraphValidationError):
            self.svc.add_note("H-2", "   ")


if __name__ == "__main__":
    unittest.main()

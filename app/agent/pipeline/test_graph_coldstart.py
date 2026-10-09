"""Graph cold-start (§14.1 G). The three invariants from the owner's review, each with a test:
roots come only from RoE scope entries (and the set of roots equals the authorized set); the root
carries verifiable provenance (user_message + origin_ref); and — the hang guard, written before the
predicate it protects — a seeded root with no experiment must NOT keep RECON open forever.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from agent.hypothesis_graph.store import HypothesisGraphStore
from agent.pipeline import graph_coldstart as CS


class ColdStartBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="coldstart-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = HypothesisGraphStore(self.tmp / "eng")

    def _child(self, parent_id, title="SQLi on /login"):
        return self.store.create_hypothesis(
            title=title, claim="the form is injectable", phase_created="ANALYSIS",
            rationale="spawned by recon", origin_type="tool_observation", impact=4,
            confidence_band="medium", confidence_reason="x", primary_parent_id=parent_id,
            surface="/login",
        )


class SeedingTest(ColdStartBase):
    def test_one_root_per_scope_entry(self):
        ids = CS.seed_scope_roots(self.store, ["a.example.com", "b.example.com"])
        self.assertEqual(len(ids), 2)
        roots = CS.scope_roots(self.store)
        self.assertEqual({CS.root_scope_entry(r) for r in roots}, {"a.example.com", "b.example.com"})

    def test_seeding_is_idempotent(self):
        first = CS.seed_scope_roots(self.store, ["a.example.com"])
        again = CS.seed_scope_roots(self.store, ["a.example.com"])
        self.assertEqual(first, again)                       # same id, not a second root
        self.assertEqual(len(CS.scope_roots(self.store)), 1)

    def test_provenance_is_a_verifiable_reference_not_just_a_label(self):
        [hid] = CS.seed_scope_roots(self.store, ["a.example.com"])
        root = self.store.get_hypothesis(hid)
        self.assertEqual(root["origin_type"], "user_message")  # a human set the scope
        self.assertEqual(root["origin_ref"], "scope:a.example.com")  # checkable against the RoE
        self.assertIsNone(root["primary_parent_id"])          # a root has no parent

    def test_a_wildcard_entry_roots_host_enumeration_not_a_single_host(self):
        [hid] = CS.seed_scope_roots(self.store, ["*.example.com"])
        root = self.store.get_hypothesis(hid)
        self.assertEqual(CS.root_scope_entry(root), "*.example.com")
        self.assertIn("under", root["claim"])                 # "enumerate hosts under *.example.com"

    def test_blank_entries_are_skipped(self):
        ids = CS.seed_scope_roots(self.store, ["a.example.com", "", "  "])
        self.assertEqual(len(ids), 1)


class RootIdentityInvariantTest(ColdStartBase):
    """Invariant 1: the set of roots equals the set of authorized scope entries, and a
    recon-discovered host is a CHILD, never a root."""

    def test_a_discovered_host_child_is_not_a_root(self):
        [root_id] = CS.seed_scope_roots(self.store, ["a.example.com"])
        self._child(root_id, title="discovered host 10.0.0.5")  # a child under the asset's root
        roots = CS.scope_roots(self.store)
        self.assertEqual([r["hypothesis_id"] for r in roots], [root_id])  # still exactly one root
        # and the child, though in the graph, is not counted among the roots
        self.assertFalse(any(CS.is_scope_root(h) for h in self.store.list_hypotheses()
                             if h["hypothesis_id"] != root_id))

    def test_assert_roots_match_scope_passes_when_aligned(self):
        CS.seed_scope_roots(self.store, ["a.example.com", "b.example.com"])
        CS.assert_roots_match_scope(self.store, ["a.example.com", "b.example.com"])  # no raise

    def test_assert_roots_match_scope_catches_an_un_rooted_entry(self):
        CS.seed_scope_roots(self.store, ["a.example.com"])
        with self.assertRaises(CS.ColdStartInvariantError):
            CS.assert_roots_match_scope(self.store, ["a.example.com", "b.example.com"])

    def test_assert_roots_match_scope_catches_a_root_outside_scope(self):
        CS.seed_scope_roots(self.store, ["a.example.com", "rogue.example.com"])
        with self.assertRaises(CS.ColdStartInvariantError):
            CS.assert_roots_match_scope(self.store, ["a.example.com"])


class RootNotCountedAsOpenWorkTest(ColdStartBase):
    """Invariant 3, the hang guard — written before the predicate: a root that can never reach a
    verdict must not keep a graph phase open forever."""

    def test_a_seeded_root_with_no_experiment_leaves_no_open_work(self):
        CS.seed_scope_roots(self.store, ["a.example.com"])
        # Only a root exists, no experiment run — RECON must be able to conclude, so the open-work
        # predicate (what graph-mode advancement reads) must be empty despite the open root.
        self.assertEqual(CS.open_hypotheses_excluding_roots(self.store), [])

    def test_a_child_hypothesis_is_real_open_work(self):
        [root_id] = CS.seed_scope_roots(self.store, ["a.example.com"])
        child = self._child(root_id)
        open_work = CS.open_hypotheses_excluding_roots(self.store)
        self.assertEqual([h["hypothesis_id"] for h in open_work], [child])  # the child counts

    def test_mark_root_enumerated_closes_the_root_without_a_verdict(self):
        [root_id] = CS.seed_scope_roots(self.store, ["a.example.com"])
        CS.mark_root_enumerated(self.store, root_id)
        root = self.store.get_hypothesis(root_id)
        self.assertEqual(root["lifecycle_status"], "completed")  # a lifecycle close
        self.assertEqual(root["verdict"], "unassessed")          # never confirmed/refuted
        # and a completed root is still not open work (it never was)
        self.assertEqual(CS.open_hypotheses_excluding_roots(self.store), [])


if __name__ == "__main__":
    unittest.main()

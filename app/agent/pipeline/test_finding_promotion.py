"""Tests for the deterministic finding promoter (finding_promotion.py).

A graph-tier run records verdicts on the hypothesis graph; the report and the finding gate read
the findings store. The promoter is the bridge. These pin what it promotes, the conservative floor
it lands findings at (never self-promoting past the gate), and that it is idempotent.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ..findings.model import FindingsStore
from ..hypothesis_graph.store import HypothesisGraphStore
from .finding_promotion import promote_hypotheses_to_findings


def _hyp(store, title, *, claim="c", impact=3, surface=None):
    return store.create_hypothesis(
        title=title, claim=claim, phase_created="ANALYSIS", rationale="r",
        origin_type="tool_observation", impact=impact, confidence_band="medium",
        confidence_reason="x", surface=surface,
    )


def _verdict(store, hid, verdict):
    h = store.get_hypothesis(hid)
    store.set_verdict(hid, h["version"], verdict)


class FindingPromotionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.graph = HypothesisGraphStore(root / "eng")
        self.findings = FindingsStore("eng", findings_dir=root / "findings")

    def _promote(self):
        return promote_hypotheses_to_findings(
            self.graph, self.findings, engagement_id="eng", session_id="s1",
            default_target="t.example",
        )

    def test_promotes_confirmed_and_supported_only(self):
        c = _hyp(self.graph, "confirmed one", surface="/login", impact=5)
        s = _hyp(self.graph, "supported one", impact=2)
        r = _hyp(self.graph, "refuted one")
        i = _hyp(self.graph, "inconclusive one")
        _hyp(self.graph, "unassessed one")  # left at the default unassessed verdict
        _verdict(self.graph, c, "confirmed")
        _verdict(self.graph, s, "supported")
        _verdict(self.graph, r, "refuted")
        _verdict(self.graph, i, "inconclusive")

        created = self._promote()
        promoted_titles = {f.title for f in self.findings.list_all()}
        self.assertEqual(promoted_titles, {"confirmed one", "supported one"})
        self.assertEqual(len(created), 2)

    def test_severity_comes_from_impact(self):
        hid = _hyp(self.graph, "critical", impact=5, surface="/x")
        _verdict(self.graph, hid, "confirmed")
        self._promote()
        self.assertEqual(self.findings.list_all()[0].severity, "critical")

    def test_lands_at_the_conservative_floor(self):
        # The promoter must not self-promote past the gate (§14.1 A): model floor, needs_validation,
        # unreviewed — exactly where record_finding would put it.
        hid = _hyp(self.graph, "confirmed", surface="/x")
        _verdict(self.graph, hid, "confirmed")
        self._promote()
        f = self.findings.list_all()[0]
        self.assertEqual(f.confirmed_by, "model")
        self.assertEqual(f.status, "needs_validation")
        self.assertIsNone(f.reviewed_by)
        self.assertEqual(f.source_hypothesis_id, hid)

    def test_carries_supporting_observations_as_evidence(self):
        hid = _hyp(self.graph, "confirmed", surface="/x")
        exp = self.graph.start_experiment(hid, method_summary="poke")
        self.graph.add_observation(exp, hid, summary="server returned the injected marker",
                                   polarity="supports", strength="strong")
        _verdict(self.graph, hid, "confirmed")
        self._promote()
        f = self.findings.list_all()[0]
        self.assertIn("server returned the injected marker", f.description)
        self.assertEqual(f.demonstrated_impact, "server returned the injected marker")
        self.assertEqual(len(f.observation_refs), 1)

    def test_target_falls_back_to_default_when_no_surface(self):
        hid = _hyp(self.graph, "no surface")  # surface None
        _verdict(self.graph, hid, "supported")
        self._promote()
        self.assertEqual(self.findings.list_all()[0].target, "t.example")

    def test_is_idempotent(self):
        hid = _hyp(self.graph, "confirmed", surface="/x")
        _verdict(self.graph, hid, "confirmed")
        first = self._promote()
        second = self._promote()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])  # nothing new to promote
        self.assertEqual(len(self.findings.list_all()), 1)  # no duplicate

    def test_nothing_to_promote_is_empty(self):
        _hyp(self.graph, "unassessed")  # no verdict
        self.assertEqual(self._promote(), [])
        self.assertEqual(self.findings.list_all(), [])


if __name__ == "__main__":
    unittest.main()

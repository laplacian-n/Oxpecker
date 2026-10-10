"""Tests for applying a confirmation rule to a finding (§8.6.4 / §8.6.4.1).

evaluate() is pure and checked directly. apply_rule_to_finding() is checked end to end against a
real FindingsStore in a temp dir: a matching rule raises the finding to `rule`, a non-matching one
leaves the model's call in place and records the disagreement. No live model needed.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ..findings.model import Finding, FindingNotFoundError, FindingsStore
from .apply import RuleOutcome, apply_rule_to_finding, evaluate
from .rules import OPEN_PORT


def _finding(**overrides) -> Finding:
    defaults = dict(
        title="Exposed admin port",
        severity="medium",
        target="127.0.0.1",
        description="an admin service listens on a non-standard port",
        remediation="firewall the port",
        tool="port_scan",
        session_id="s1",
        engagement_id="e1",
    )
    defaults.update(overrides)
    return Finding(**defaults)


OPEN_OBS = [{"type": "port", "port": 8080, "state": "open"}]
CLOSED_OBS = [{"type": "port", "port": 8080, "state": "closed"}]


class TestEvaluate(unittest.TestCase):
    def test_matching_observations_give_a_matched_outcome(self):
        outcome = evaluate(OPEN_PORT, OPEN_OBS)
        self.assertEqual(outcome, RuleOutcome(rule_ref="open_port@1", matched=True))

    def test_non_matching_observations_give_an_unmatched_outcome(self):
        outcome = evaluate(OPEN_PORT, CLOSED_OBS)
        self.assertEqual(outcome, RuleOutcome(rule_ref="open_port@1", matched=False))

    def test_evaluate_does_not_mutate_its_observations(self):
        obs = [{"type": "port", "port": 8080, "state": "open"}]
        before = [dict(o) for o in obs]
        evaluate(OPEN_PORT, obs)
        self.assertEqual(obs, before)


class TestApplyRuleToFinding(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apply-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FindingsStore("e1", findings_dir=self.tmp)

    def test_matching_observations_raise_the_finding_to_rule(self):
        f = self.store.add(_finding())
        updated = apply_rule_to_finding(self.store, f.finding_id, OPEN_PORT, OPEN_OBS)
        self.assertEqual(updated.confirmed_by, "rule")
        self.assertIsNone(updated.rule_disagreed)
        self.assertEqual(self.store.get(f.finding_id).confirmed_by, "rule")  # persisted

    def test_non_matching_observations_leave_the_model_call_and_record_disagreement(self):
        f = self.store.add(_finding())
        updated = apply_rule_to_finding(self.store, f.finding_id, OPEN_PORT, CLOSED_OBS)
        self.assertEqual(updated.confirmed_by, "model")
        self.assertEqual(updated.rule_disagreed, "open_port@1")
        stored = self.store.get(f.finding_id)
        self.assertEqual(stored.confirmed_by, "model")
        self.assertEqual(stored.rule_disagreed, "open_port@1")

    def test_a_later_match_clears_an_earlier_disagreement(self):
        f = self.store.add(_finding())
        apply_rule_to_finding(self.store, f.finding_id, OPEN_PORT, CLOSED_OBS)
        updated = apply_rule_to_finding(self.store, f.finding_id, OPEN_PORT, OPEN_OBS)
        self.assertEqual(updated.confirmed_by, "rule")
        self.assertIsNone(updated.rule_disagreed)

    def test_a_rule_does_not_lower_an_earned_downstream_assurance(self):
        f = self.store.add(_finding(confirmed_by="downstream"))
        updated = apply_rule_to_finding(self.store, f.finding_id, OPEN_PORT, OPEN_OBS)
        self.assertEqual(updated.confirmed_by, "downstream")

    def test_unknown_finding_id_raises(self):
        with self.assertRaises(FindingNotFoundError):
            apply_rule_to_finding(self.store, "not-a-real-id", OPEN_PORT, OPEN_OBS)


if __name__ == "__main__":
    unittest.main()

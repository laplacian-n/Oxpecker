"""Tests for the rule-disagreement counter (§8.6.4.1 / §12 row 6d).

Stubs are SimpleNamespace objects carrying only the attributes rule_quality() reads. Assurance
levels are real ladder values, so assurance_rank() accepts them exactly as it would a Finding.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from .disagreement import rule_quality


def _f(rule=None, status="needs_validation", confirmed_by="model", reviewed_by=None):
    return SimpleNamespace(
        rule_disagreed=rule,
        status=status,
        confirmed_by=confirmed_by,
        reviewed_by=reviewed_by,
    )


def _rule_right(rule="r@1"):
    """Model was wrong: the finding was later shown to be a false positive."""
    return _f(rule, status="false_positive")


def _rule_broken_assurance(rule="r@1"):
    """Model was right: something independent raised its assurance."""
    return _f(rule, confirmed_by="differential")


def _pending(rule="r@1"):
    return _f(rule)


class TestIgnoresUnrelatedFindings(unittest.TestCase):
    def test_findings_without_rule_disagreed_are_ignored(self):
        findings = [
            _f(None, status="false_positive"),
            _f(None, status="confirmed", confirmed_by="human", reviewed_by="op"),
            _f(None),
        ]
        self.assertEqual(rule_quality(findings), {})

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(rule_quality([]), {})


class TestCounts(unittest.TestCase):
    def test_counts_and_quality_score_for_mixed_resolutions(self):
        findings = [
            _rule_right(), _rule_right(), _rule_right(),       # rule right x3
            _rule_broken_assurance(), _rule_broken_assurance(),  # rule broken x2
            _pending(),                                         # pending x1
        ]
        stats = rule_quality(findings)["r@1"]
        self.assertEqual(stats["disagreements"], 6)
        self.assertEqual(stats["rule_right"], 3)
        self.assertEqual(stats["rule_broken"], 2)
        self.assertEqual(stats["pending"], 1)
        self.assertAlmostEqual(stats["quality_score"], 3 / 5)

    def test_reviewed_by_counts_as_rule_broken(self):
        stats = rule_quality([_f("r@1", reviewed_by="operator")])["r@1"]
        self.assertEqual(stats["rule_broken"], 1)
        self.assertEqual(stats["rule_right"], 0)
        self.assertEqual(stats["pending"], 0)

    def test_confirmed_by_differential_counts_as_rule_broken(self):
        stats = rule_quality([_f("r@1", confirmed_by="differential")])["r@1"]
        self.assertEqual(stats["rule_broken"], 1)

    def test_status_confirmed_counts_as_rule_broken(self):
        stats = rule_quality([_f("r@1", status="confirmed")])["r@1"]
        self.assertEqual(stats["rule_broken"], 1)
        self.assertEqual(stats["pending"], 0)

    def test_false_positive_is_decisive_over_raised_assurance(self):
        finding = _f("r@1", status="false_positive", confirmed_by="human", reviewed_by="op")
        stats = rule_quality([finding])["r@1"]
        self.assertEqual(stats["rule_right"], 1)
        self.assertEqual(stats["rule_broken"], 0)

    def test_pending_is_excluded_from_quality_score(self):
        findings = [_rule_right(), _pending(), _pending(), _pending()]
        stats = rule_quality(findings)["r@1"]
        self.assertEqual(stats["pending"], 3)
        self.assertEqual(stats["quality_score"], 1.0)  # pending does not drag it down

    def test_all_pending_gives_none_quality_and_no_retire(self):
        stats = rule_quality([_pending(), _pending()], min_resolved=0)["r@1"]
        self.assertIsNone(stats["quality_score"])
        self.assertFalse(stats["retire"])


class TestRetire(unittest.TestCase):
    def _resolved(self, right, broken, rule="r@1"):
        return [_rule_right(rule) for _ in range(right)] + [
            _rule_broken_assurance(rule) for _ in range(broken)
        ]

    def test_retires_when_resolved_enough_and_quality_below_threshold(self):
        # 1 right / 5 resolved = 0.2 < 0.5, and 5 >= min_resolved=5
        stats = rule_quality(self._resolved(1, 4))["r@1"]
        self.assertTrue(stats["retire"])

    def test_exactly_at_threshold_does_not_retire(self):
        # 2 right / 4 resolved = 0.5, not strictly below 0.5
        stats = rule_quality(self._resolved(2, 2), min_resolved=4)["r@1"]
        self.assertEqual(stats["quality_score"], 0.5)
        self.assertFalse(stats["retire"])

    def test_just_below_threshold_retires(self):
        # 2 right / 5 resolved = 0.4 < 0.5
        stats = rule_quality(self._resolved(2, 3), min_resolved=5)["r@1"]
        self.assertTrue(stats["retire"])

    def test_min_resolved_gate_blocks_retirement_on_small_sample(self):
        # quality 0.0 but only 4 resolved, below min_resolved=5
        stats = rule_quality(self._resolved(0, 4))["r@1"]
        self.assertEqual(stats["quality_score"], 0.0)
        self.assertFalse(stats["retire"])

    def test_min_resolved_counts_only_resolved_not_pending(self):
        findings = self._resolved(0, 3) + [_pending() for _ in range(10)]
        stats = rule_quality(findings, min_resolved=5)["r@1"]
        self.assertEqual(stats["disagreements"], 13)
        self.assertFalse(stats["retire"])

    def test_threshold_overrides_are_respected(self):
        findings = self._resolved(2, 3)  # 0.4
        self.assertTrue(rule_quality(findings, min_resolved=5, retire_below=0.5)["r@1"]["retire"])
        self.assertFalse(rule_quality(findings, min_resolved=5, retire_below=0.4)["r@1"]["retire"])
        self.assertFalse(rule_quality(findings, min_resolved=6, retire_below=0.5)["r@1"]["retire"])

    def test_rule_that_is_always_right_never_retires(self):
        stats = rule_quality(self._resolved(6, 0))["r@1"]
        self.assertEqual(stats["quality_score"], 1.0)
        self.assertFalse(stats["retire"])


class TestGrouping(unittest.TestCase):
    def test_distinct_rule_refs_are_grouped_separately(self):
        findings = [
            _rule_right("alpha@1"),
            _rule_broken_assurance("alpha@1"),
            _rule_right("beta@2"),
            _rule_right("beta@2"),
            _pending("beta@2"),
        ]
        result = rule_quality(findings)
        self.assertEqual(set(result), {"alpha@1", "beta@2"})
        self.assertEqual(result["alpha@1"]["disagreements"], 2)
        self.assertEqual(result["alpha@1"]["quality_score"], 0.5)
        self.assertEqual(result["beta@2"]["disagreements"], 3)
        self.assertEqual(result["beta@2"]["rule_right"], 2)
        self.assertEqual(result["beta@2"]["pending"], 1)
        self.assertEqual(result["beta@2"]["quality_score"], 1.0)

    def test_version_is_part_of_the_ref(self):
        findings = [_rule_right("alpha@1"), _rule_right("alpha@2")]
        self.assertEqual(set(rule_quality(findings)), {"alpha@1", "alpha@2"})


if __name__ == "__main__":
    unittest.main()

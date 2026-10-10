"""Tests for the section 8.5.3 near-miss view. Pure function: no stores, no I/O."""
from __future__ import annotations

import types
import unittest

from .near_miss import near_miss_report


def _h(lifecycle_status=None, verdict=None, coverage=None) -> dict:
    row = {}
    if lifecycle_status is not None:
        row["lifecycle_status"] = lifecycle_status
    if verdict is not None:
        row["verdict"] = verdict
    if coverage is not None:
        row["coverage"] = coverage
    return row


def _f(confirmed_by="rule", rule_disagreed=None):
    return types.SimpleNamespace(confirmed_by=confirmed_by, rule_disagreed=rule_disagreed)


ZERO_CATEGORIES = {
    "out_of_scope": 0,
    "parked": 0,
    "blocked": 0,
    "refuted": 0,
    "exhausted_low_coverage": 0,
    "model_only_findings": 0,
    "rule_disagreed_findings": 0,
}


class EmptyInputTests(unittest.TestCase):
    def test_empty_inputs_return_all_zero(self):
        report = near_miss_report([], [])
        self.assertEqual(report["by_category"], ZERO_CATEGORIES)
        self.assertEqual(report["total_lost_hypotheses"], 0)
        self.assertEqual(report["weakly_confirmed_findings"], 0)

    def test_all_seven_keys_are_present_and_ints(self):
        report = near_miss_report([], [])
        self.assertEqual(set(report["by_category"]), set(ZERO_CATEGORIES))
        for value in report["by_category"].values():
            self.assertIs(type(value), int)

    def test_rows_without_keys_are_tolerated(self):
        report = near_miss_report([{}, {"verdict": "supported"}, {"coverage": 0.1}], [])
        self.assertEqual(report["by_category"], ZERO_CATEGORIES)


class HypothesisCategoryTests(unittest.TestCase):
    def test_abandoned_counts_as_out_of_scope(self):
        report = near_miss_report([_h("abandoned", "unassessed")], [])
        self.assertEqual(report["by_category"]["out_of_scope"], 1)
        self.assertEqual(report["by_category"]["blocked"], 0)

    def test_parked_and_awaiting_approval_count_as_parked(self):
        rows = [_h("parked"), _h("awaiting_approval"), _h("parked", "supported")]
        report = near_miss_report(rows, [])
        self.assertEqual(report["by_category"]["parked"], 3)

    def test_blocked_counts_as_blocked(self):
        report = near_miss_report([_h("blocked")], [])
        self.assertEqual(report["by_category"]["blocked"], 1)
        self.assertEqual(report["by_category"]["parked"], 0)

    def test_non_lost_statuses_are_not_counted(self):
        rows = [_h(s, "supported", 0.0) for s in ("draft", "open", "queued", "running")]
        report = near_miss_report(rows, [])
        self.assertEqual(report["by_category"], ZERO_CATEGORIES)
        self.assertEqual(report["total_lost_hypotheses"], 0)

    def test_refuted_counts_by_verdict_regardless_of_status(self):
        rows = [_h("completed", "refuted", 1.0), _h("open", "refuted"), _h(None, "refuted")]
        report = near_miss_report(rows, [])
        self.assertEqual(report["by_category"]["refuted"], 3)

    def test_completed_inconclusive_low_coverage_counts(self):
        report = near_miss_report([_h("completed", "inconclusive", 0.2)], [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 1)

    def test_completed_unassessed_low_coverage_counts(self):
        report = near_miss_report([_h("completed", "unassessed", 0.0)], [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 1)

    def test_completed_high_coverage_does_not_count(self):
        report = near_miss_report([_h("completed", "inconclusive", 0.9)], [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 0)

    def test_completed_supported_low_coverage_does_not_count(self):
        report = near_miss_report([_h("completed", "supported", 0.1)], [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 0)

    def test_non_completed_inconclusive_low_coverage_does_not_count(self):
        rows = [_h("running", "inconclusive", 0.1), _h("parked", "unassessed", 0.1)]
        report = near_miss_report(rows, [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 0)

    def test_missing_coverage_on_completed_inconclusive_is_treated_as_low(self):
        report = near_miss_report([_h("completed", "inconclusive")], [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 1)

    def test_each_hypothesis_category_counted_in_one_mixed_list(self):
        rows = [
            _h("abandoned", "unassessed"),
            _h("parked"),
            _h("awaiting_approval"),
            _h("blocked"),
            _h("completed", "refuted", 1.0),
            _h("completed", "inconclusive", 0.1),
            _h("completed", "inconclusive", 0.9),
            _h("running", "supported", 0.3),
        ]
        cats = near_miss_report(rows, [])["by_category"]
        self.assertEqual(cats["out_of_scope"], 1)
        self.assertEqual(cats["parked"], 2)
        self.assertEqual(cats["blocked"], 1)
        self.assertEqual(cats["refuted"], 1)
        self.assertEqual(cats["exhausted_low_coverage"], 1)


class ThresholdTests(unittest.TestCase):
    def test_coverage_exactly_at_default_threshold_is_not_low(self):
        report = near_miss_report([_h("completed", "inconclusive", 0.5)], [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 0)

    def test_coverage_just_below_default_threshold_is_low(self):
        report = near_miss_report([_h("completed", "inconclusive", 0.4999)], [])
        self.assertEqual(report["by_category"]["exhausted_low_coverage"], 1)

    def test_custom_threshold_is_honored(self):
        rows = [_h("completed", "inconclusive", 0.6)]
        self.assertEqual(
            near_miss_report(rows, [], low_coverage_threshold=0.7)["by_category"][
                "exhausted_low_coverage"
            ],
            1,
        )
        self.assertEqual(
            near_miss_report(rows, [], low_coverage_threshold=0.6)["by_category"][
                "exhausted_low_coverage"
            ],
            0,
        )

    def test_threshold_is_keyword_only(self):
        with self.assertRaises(TypeError):
            near_miss_report([], [], 0.5)


class FindingAssuranceTests(unittest.TestCase):
    def test_model_only_findings_counted(self):
        findings = [_f("model"), _f("model"), _f("rule"), _f("verifier")]
        report = near_miss_report([], findings)
        self.assertEqual(report["by_category"]["model_only_findings"], 2)

    def test_rule_disagreed_counted_when_not_none(self):
        findings = [
            _f("rule", rule_disagreed="cwe-79"),
            _f("model", rule_disagreed=""),
            _f("rule", rule_disagreed=None),
        ]
        report = near_miss_report([], findings)
        # An empty string is still a recorded disagreement, not None.
        self.assertEqual(report["by_category"]["rule_disagreed_findings"], 2)

    def test_model_and_rule_disagreed_on_same_finding_counts_in_both(self):
        report = near_miss_report([], [_f("model", rule_disagreed="x")])
        self.assertEqual(report["by_category"]["model_only_findings"], 1)
        self.assertEqual(report["by_category"]["rule_disagreed_findings"], 1)
        self.assertEqual(report["weakly_confirmed_findings"], 2)

    def test_plain_stubs_and_missing_attributes(self):
        findings = [types.SimpleNamespace(), object()]
        report = near_miss_report([], findings)
        self.assertEqual(report["by_category"]["model_only_findings"], 0)
        self.assertEqual(report["by_category"]["rule_disagreed_findings"], 0)


class TotalsTests(unittest.TestCase):
    def test_total_lost_is_sum_of_five_hypothesis_categories_only(self):
        rows = [
            _h("abandoned"),
            _h("parked"),
            _h("blocked"),
            _h("completed", "refuted", 1.0),
            _h("completed", "unassessed", 0.0),
        ]
        findings = [_f("model"), _f("rule", rule_disagreed="y")]
        report = near_miss_report(rows, findings)
        self.assertEqual(report["total_lost_hypotheses"], 5)
        self.assertEqual(report["weakly_confirmed_findings"], 2)

    def test_weakly_confirmed_is_sum_of_finding_counters(self):
        findings = [_f("model"), _f("model"), _f("model", rule_disagreed="z"), _f("rule")]
        report = near_miss_report([], findings)
        self.assertEqual(report["weakly_confirmed_findings"], 4)
        self.assertEqual(report["total_lost_hypotheses"], 0)

    def test_finding_counters_do_not_leak_into_total_lost(self):
        findings = [_f("model", rule_disagreed="q")] * 3
        report = near_miss_report([], findings)
        self.assertEqual(report["total_lost_hypotheses"], 0)


if __name__ == "__main__":
    unittest.main()

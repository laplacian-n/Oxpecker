"""Findings model/store/report/sarif — including the human review gate
(Finding.reviewed_by/reviewed_at, FindingsStore.mark_reviewed, and how report.py/sarif.py
surface review status). No live model needed."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .model import (
    CONFIRMED_BY_FLOOR,
    Finding,
    FindingNotFoundError,
    FindingsStore,
    assurance_rank,
    can_submit,
    submission_blocker,
    unsubmittable,
)
from .report import render_markdown
from .sarif import render_sarif


def _finding(**overrides) -> Finding:
    defaults = dict(
        title="Reflected XSS in search",
        severity="high",
        target="http://127.0.0.1:3000/search",
        description="the search param is reflected unescaped",
        remediation="escape output",
        tool="http_recon",
        session_id="s1",
        engagement_id="e1",
    )
    defaults.update(overrides)
    return Finding(**defaults)


class TestFindingSchema(unittest.TestCase):
    def test_reviewed_fields_default_to_unreviewed(self):
        f = _finding()
        self.assertIsNone(f.reviewed_by)
        self.assertIsNone(f.reviewed_at)

    def test_old_record_without_review_fields_still_loads(self):
        # Simulates a Finding written before reviewed_by/reviewed_at existed — to_dict() from an
        # old schema wouldn't have had these keys at all; Finding(**old_dict) must still work.
        old = _finding().to_dict()
        del old["reviewed_by"]
        del old["reviewed_at"]
        f = Finding(**old)
        self.assertIsNone(f.reviewed_by)


class TestFindingsStoreReview(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="findings-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FindingsStore("e1", findings_dir=self.tmp)

    def test_mark_reviewed_sets_identity_and_timestamp(self):
        f = self.store.add(_finding())
        reviewed = self.store.mark_reviewed(f.finding_id, "alice")
        self.assertEqual(reviewed.reviewed_by, "alice")
        self.assertIsNotNone(reviewed.reviewed_at)
        # persisted, not just returned in memory
        self.assertEqual(self.store.get(f.finding_id).reviewed_by, "alice")

    def test_mark_reviewed_only_touches_the_matching_finding(self):
        a = self.store.add(_finding(title="A"))
        b = self.store.add(_finding(title="B"))
        self.store.mark_reviewed(a.finding_id, "alice")
        self.assertIsNone(self.store.get(b.finding_id).reviewed_by)

    def test_mark_reviewed_unknown_id_raises(self):
        with self.assertRaises(FindingNotFoundError):
            self.store.mark_reviewed("not-a-real-id", "alice")

    def test_mark_reviewed_rejects_empty_identity(self):
        f = self.store.add(_finding())
        with self.assertRaises(ValueError):
            self.store.mark_reviewed(f.finding_id, "   ")

    def test_get_unknown_id_raises(self):
        with self.assertRaises(FindingNotFoundError):
            self.store.get("nope")

    def test_reopening_the_store_sees_the_review(self):
        f = self.store.add(_finding())
        self.store.mark_reviewed(f.finding_id, "alice")
        reopened = FindingsStore("e1", findings_dir=self.tmp)
        self.assertEqual(reopened.get(f.finding_id).reviewed_by, "alice")


class TestReportReviewStatus(unittest.TestCase):
    def test_unreviewed_finding_gets_the_warning_banner_and_tag(self):
        md = render_markdown([_finding()], "e1")
        self.assertIn("have NOT been reviewed by a human", md)
        self.assertIn("NOT REVIEWED BY A HUMAN", md)
        self.assertNotIn("All findings below have been human-reviewed", md)

    def test_fully_reviewed_report_gets_the_all_clear_banner(self):
        f = _finding()
        f.reviewed_by = "alice"
        f.reviewed_at = 1234567890.0
        md = render_markdown([f], "e1")
        self.assertIn("All findings below have been human-reviewed", md)
        self.assertIn("Reviewed by alice", md)
        self.assertNotIn("NOT REVIEWED", md)

    def test_mixed_report_counts_correctly(self):
        reviewed = _finding(title="reviewed one")
        reviewed.reviewed_by = "alice"
        reviewed.reviewed_at = 1234567890.0
        unreviewed = _finding(title="unreviewed one")
        md = render_markdown([reviewed, unreviewed], "e1")
        self.assertIn("1 of 2 finding(s) have NOT been reviewed", md)

    def test_empty_findings_list_has_no_review_banner_at_all(self):
        md = render_markdown([], "e1")
        self.assertNotIn("reviewed", md.lower())


class TestSarifReviewStatus(unittest.TestCase):
    def test_review_fields_are_in_properties(self):
        f = _finding()
        f.reviewed_by = "alice"
        f.reviewed_at = 42.0
        sarif = render_sarif([f])
        props = sarif["runs"][0]["results"][0]["properties"]
        self.assertEqual(props["reviewed_by"], "alice")
        self.assertEqual(props["reviewed_at"], 42.0)

    def test_unreviewed_finding_has_null_review_fields(self):
        sarif = render_sarif([_finding()])
        props = sarif["runs"][0]["results"][0]["properties"]
        self.assertIsNone(props["reviewed_by"])


class TestConfirmedByLadder(unittest.TestCase):
    def test_a_finding_defaults_to_the_model_floor(self):
        self.assertEqual(_finding().confirmed_by, CONFIRMED_BY_FLOOR)
        self.assertIsNone(_finding().rule_disagreed)

    def test_an_unknown_confirmed_by_is_rejected(self):
        with self.assertRaises(ValueError):
            _finding(confirmed_by="vibes")

    def test_the_ladder_order_is_low_to_high(self):
        self.assertLess(assurance_rank("model"), assurance_rank("rule"))
        self.assertLess(assurance_rank("rule"), assurance_rank("differential"))
        self.assertLess(assurance_rank("differential"), assurance_rank("downstream"))
        self.assertLess(assurance_rank("downstream"), assurance_rank("human"))

    def test_rank_rejects_an_unknown_level(self):
        with self.assertRaises(ValueError):
            assurance_rank("nonsense")

    def test_confirmed_by_round_trips_through_the_store(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = FindingsStore("e1", findings_dir=tmp)
        store.add(_finding(confirmed_by="differential", rule_disagreed="xss-reflected@3"))
        back = store.list_all()[0]
        self.assertEqual(back.confirmed_by, "differential")
        self.assertEqual(back.rule_disagreed, "xss-reflected@3")


class TestSubmissionBoundary(unittest.TestCase):
    """§8.6.5 #1 — model-only findings cannot cross unseen; above-model crosses on its own."""

    def test_model_only_unreviewed_is_blocked(self):
        f = _finding(confirmed_by="model")
        self.assertFalse(can_submit(f))
        self.assertIn("§8.6.5 #1", submission_blocker(f))

    def test_model_confirmed_crosses_once_a_person_reviews_it(self):
        f = _finding(confirmed_by="model")
        f.reviewed_by = "alice"
        self.assertTrue(can_submit(f))

    def test_above_the_model_floor_crosses_without_review(self):
        for level in ("rule", "differential", "downstream", "human"):
            f = _finding(confirmed_by=level)
            self.assertTrue(can_submit(f), f"{level} should cross on its own")

    def test_a_shelved_finding_is_never_submittable(self):
        self.assertFalse(can_submit(_finding(confirmed_by="human", status="false_positive")))
        self.assertFalse(can_submit(_finding(confirmed_by="differential", status="wont_fix")))

    def test_unsubmittable_lists_each_blocked_finding_with_a_reason(self):
        ok = _finding(confirmed_by="differential")
        blocked = _finding(confirmed_by="model")  # model-only, unreviewed
        reasons = unsubmittable([ok, blocked])
        self.assertEqual([f.finding_id for f, _ in reasons], [blocked.finding_id])
        self.assertTrue(reasons[0][1])  # a non-empty reason


class TestVerificationRaisesAssurance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FindingsStore("e1", findings_dir=self.tmp)

    def test_record_verification_raises_model_to_differential(self):
        f = self.store.add(_finding(confirmed_by="model"))
        self.store.record_verification(
            f.finding_id, verdict="could_not_refute", reason=None, rationale="holds",
            by="verifier", confirmed_by="differential",
        )
        self.assertEqual(self.store.get(f.finding_id).confirmed_by, "differential")

    def test_record_verification_never_lowers_an_earned_assurance(self):
        f = self.store.add(_finding(confirmed_by="downstream"))
        self.store.record_verification(
            f.finding_id, verdict="could_not_refute", reason=None, rationale="holds",
            by="verifier", confirmed_by="differential",  # weaker than downstream
        )
        self.assertEqual(self.store.get(f.finding_id).confirmed_by, "downstream")  # unchanged


class TestRuleOutcome(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rule-outcome-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FindingsStore("e1", findings_dir=self.tmp)

    def test_a_match_raises_model_to_rule_and_leaves_no_disagreement(self):
        f = self.store.add(_finding(confirmed_by="model"))
        updated = self.store.record_rule_outcome(f.finding_id, rule_ref="open_port@1", matched=True)
        self.assertEqual(updated.confirmed_by, "rule")
        self.assertIsNone(updated.rule_disagreed)
        self.assertEqual(self.store.get(f.finding_id).confirmed_by, "rule")

    def test_a_match_does_not_lower_a_downstream_finding(self):
        f = self.store.add(_finding(confirmed_by="downstream"))
        updated = self.store.record_rule_outcome(f.finding_id, rule_ref="open_port@1", matched=True)
        self.assertEqual(updated.confirmed_by, "downstream")
        self.assertEqual(self.store.get(f.finding_id).confirmed_by, "downstream")

    def test_a_match_clears_a_previously_set_disagreement(self):
        f = self.store.add(_finding())
        self.store.record_rule_outcome(f.finding_id, rule_ref="open_port@1", matched=False)
        updated = self.store.record_rule_outcome(f.finding_id, rule_ref="open_port@1", matched=True)
        self.assertIsNone(updated.rule_disagreed)
        self.assertEqual(updated.confirmed_by, "rule")

    def test_a_non_match_leaves_confirmed_by_and_sets_the_disagreement(self):
        f = self.store.add(_finding(confirmed_by="model"))
        updated = self.store.record_rule_outcome(f.finding_id, rule_ref="open_port@1", matched=False)
        self.assertEqual(updated.confirmed_by, "model")
        self.assertEqual(updated.rule_disagreed, "open_port@1")
        stored = self.store.get(f.finding_id)
        self.assertEqual(stored.confirmed_by, "model")
        self.assertEqual(stored.rule_disagreed, "open_port@1")

    def test_a_non_match_does_not_lower_an_earned_assurance(self):
        f = self.store.add(_finding(confirmed_by="differential"))
        updated = self.store.record_rule_outcome(f.finding_id, rule_ref="open_port@1", matched=False)
        self.assertEqual(updated.confirmed_by, "differential")

    def test_rule_outcome_does_not_touch_review_verifier_or_status(self):
        f = self.store.add(_finding(status="hypothesis"))
        self.store.mark_reviewed(f.finding_id, "alice")
        self.store.record_rule_outcome(f.finding_id, rule_ref="open_port@1", matched=True)
        stored = self.store.get(f.finding_id)
        self.assertEqual(stored.reviewed_by, "alice")
        self.assertEqual(stored.verifier, "unspecified")
        self.assertIsNone(stored.last_verified)
        self.assertEqual(stored.status, "hypothesis")

    def test_rule_outcome_only_touches_the_matching_finding(self):
        a = self.store.add(_finding(title="A"))
        b = self.store.add(_finding(title="B"))
        self.store.record_rule_outcome(a.finding_id, rule_ref="open_port@1", matched=True)
        self.assertEqual(self.store.get(b.finding_id).confirmed_by, "model")

    def test_rule_outcome_on_unknown_id_raises(self):
        with self.assertRaises(FindingNotFoundError):
            self.store.record_rule_outcome("not-a-real-id", rule_ref="open_port@1", matched=True)


def _section(md: str, heading: str) -> str:
    """The text of one `## ` section: from its heading up to the next `## ` heading (or EOF)."""
    start = md.index(heading)
    rest = md[start + len(heading):]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


BLOCKED_HEADING = "## ⚠ Not ready for submission (§8.6.5 #1)"
SUBMITTABLE_HEADING = "## Submittable findings"


class TestReportAssuranceAndSubmissionBoundary(unittest.TestCase):
    def test_model_only_unreviewed_finding_is_under_not_ready_with_the_reason(self):
        f = _finding(title="model-only one", confirmed_by="model")
        md = render_markdown([f], "e1")
        blocked = _section(md, BLOCKED_HEADING)
        self.assertIn("model-only one", blocked)
        self.assertIn("§8.6.5 #1", blocked)
        self.assertNotIn("model-only one", _section(md, SUBMITTABLE_HEADING))

    def test_differential_finding_is_under_submittable(self):
        f = _finding(title="differential one", confirmed_by="differential")
        md = render_markdown([f], "e1")
        self.assertIn("differential one", _section(md, SUBMITTABLE_HEADING))
        self.assertNotIn("differential one", _section(md, BLOCKED_HEADING))

    def test_reviewed_model_finding_is_submittable(self):
        f = _finding(title="reviewed model one", confirmed_by="model")
        f.reviewed_by = "alice"
        f.reviewed_at = 1234567890.0
        md = render_markdown([f], "e1")
        self.assertIn("reviewed model one", _section(md, SUBMITTABLE_HEADING))

    def test_split_puts_each_finding_in_exactly_one_group(self):
        md = render_markdown([
            _finding(title="blocked one", confirmed_by="model"),
            _finding(title="ok one", confirmed_by="human"),
        ], "e1")
        self.assertIn("blocked one", _section(md, BLOCKED_HEADING))
        self.assertNotIn("ok one", _section(md, BLOCKED_HEADING))
        self.assertIn("ok one", _section(md, SUBMITTABLE_HEADING))
        self.assertNotIn("blocked one", _section(md, SUBMITTABLE_HEADING))

    def test_confirmed_by_level_is_shown(self):
        md = render_markdown([_finding(confirmed_by="downstream")], "e1")
        self.assertIn("`downstream`", md)

    def test_rule_disagreed_is_shown_when_set(self):
        md = render_markdown([_finding(confirmed_by="model", rule_disagreed="xss-reflected@3")], "e1")
        self.assertIn("xss-reflected@3", md)

    def test_rule_disagreed_is_absent_when_unset(self):
        md = render_markdown([_finding()], "e1")
        self.assertNotIn("Rule disagreed", md)

    def test_existing_fields_are_still_rendered(self):
        md = render_markdown([_finding(confirmed_by="model")], "e1")
        self.assertIn("**Remediation**", md)
        self.assertIn("**Status:**", md)
        self.assertIn("NOT REVIEWED BY A HUMAN", md)

    def test_empty_report_has_both_group_headings(self):
        md = render_markdown([], "e1")
        self.assertIn(BLOCKED_HEADING, md)
        self.assertIn(SUBMITTABLE_HEADING, md)


class TestSarifAssurance(unittest.TestCase):
    def test_model_only_unreviewed_is_not_submittable_with_a_reason(self):
        props = render_sarif([_finding(confirmed_by="model")])["runs"][0]["results"][0]["properties"]
        self.assertEqual(props["confirmed_by"], "model")
        self.assertIs(props["submittable"], False)
        self.assertIn("§8.6.5 #1", props["submission_blocker"])

    def test_differential_is_submittable_with_null_blocker(self):
        props = render_sarif([_finding(confirmed_by="differential")])["runs"][0]["results"][0]["properties"]
        self.assertEqual(props["confirmed_by"], "differential")
        self.assertIs(props["submittable"], True)
        self.assertIsNone(props["submission_blocker"])

    def test_rule_disagreed_is_in_properties(self):
        props = render_sarif([_finding(rule_disagreed="xss-reflected@3")])["runs"][0]["results"][0]["properties"]
        self.assertEqual(props["rule_disagreed"], "xss-reflected@3")
        self.assertIsNone(render_sarif([_finding()])["runs"][0]["results"][0]["properties"]["rule_disagreed"])


if __name__ == "__main__":
    unittest.main()

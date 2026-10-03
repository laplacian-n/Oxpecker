"""Findings model/store/report/sarif — including the human review gate
(Finding.reviewed_by/reviewed_at, FindingsStore.mark_reviewed, and how report.py/sarif.py
surface review status). No live model needed."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .model import Finding, FindingNotFoundError, FindingsStore
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


if __name__ == "__main__":
    unittest.main()

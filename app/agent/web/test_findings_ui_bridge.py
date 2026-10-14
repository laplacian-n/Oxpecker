"""The findings UI bridge (dev_server): a graph-tier autonomous run writes its findings to the
on-disk FindingsStore (JSONL) via the driver's promotion step, not to dev_server's in-memory
_findings (which only ever holds assistant-mode findings). Before this bridge the Findings tab read
0 for every autonomous run even when report.md was full. These pin that the tab surfaces the
on-disk findings and that the review/delete buttons act on them.

Run directly: `python3 -m unittest agent.web.test_findings_ui_bridge`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from ..findings.model import Finding, FindingsStore
from . import dev_server


class FindingsUiBridgeTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="findings-ui-bridge-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions", "_engagements", "_findings")}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self.addCleanup(self._restore)
        self._patchers = [
            patch.object(config, "FINDINGS_DIR", self.tmp / "findings"),
            patch.object(config, "ENGAGEMENTS_ROOT", self.tmp / "eng"),
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-such-key.txt"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def _restore(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _write_ondisk(self, engagement_id="eng-x", **over):
        fields = dict(
            title="SQLi on /login", severity="high", target="/login",
            description="the login form is injectable", remediation="parameterize",
            tool="hypothesis-graph", session_id="s1", engagement_id=engagement_id,
            source_hypothesis_id="h-1",
        )
        fields.update(over)
        return FindingsStore(engagement_id).add(Finding(**fields))

    def test_ondisk_findings_appear_in_the_list(self):
        f = self._write_ondisk()
        data = self.client.get("/api/engagements/eng-x/findings").json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["findings"][0]["finding_id"], f.finding_id)
        self.assertEqual(data["findings"][0]["source_hypothesis_id"], "h-1")

    def test_review_falls_through_to_the_ondisk_store(self):
        f = self._write_ondisk()
        resp = self.client.post(
            f"/api/engagements/eng-x/findings/{f.finding_id}/review",
            json={"reviewed_by": "operator"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["reviewed_by"], "operator")
        self.assertEqual(
            self.client.get("/api/engagements/eng-x/findings").json()["reviewed_count"], 1)
        # persisted to disk, not just returned
        self.assertEqual(FindingsStore("eng-x").get(f.finding_id).reviewed_by, "operator")

    def test_review_unknown_finding_404s(self):
        resp = self.client.post(
            "/api/engagements/eng-x/findings/no-such-id/review",
            json={"reviewed_by": "operator"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_delete_falls_through_to_the_ondisk_store(self):
        f = self._write_ondisk()
        resp = self.client.delete(f"/api/engagements/eng-x/findings/{f.finding_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])
        self.assertEqual(self.client.get("/api/engagements/eng-x/findings").json()["count"], 0)

    def test_no_ondisk_store_is_an_empty_tab_not_an_error(self):
        data = self.client.get("/api/engagements/never-ran/findings").json()
        self.assertEqual(data, {"findings": [], "count": 0, "reviewed_count": 0})


if __name__ == "__main__":
    unittest.main()
